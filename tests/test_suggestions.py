import asyncio
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from telegram.error import TimedOut

from bot import suggestions
from bot.suggestion_store import SubmissionError, SuggestionStore
from web import suggestions as web


class FormTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SuggestionStore(Path(directory.name) / "queue.db")
        patcher = patch.object(web, "store", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(web.router)
        self.client = TestClient(app, client=("198.51.100.5", 1234))
        self.addCleanup(self.client.close)

    def form(self):
        response = self.client.get("/suggest")
        self.assertEqual(response.status_code, 200)
        return {name: unescape(value) for name, value in re.findall(r'name="(csrf|request_id)" value="([^"]+)"', response.text)} | {"text": "Привет <b>мир</b>", "consent": "yes"}

    def test_submit_native_and_fetch_preserve_text_and_capture_real_client(self):
        data = self.form()
        response = self.client.post("/suggest", data=data, headers={"Accept": "application/json", "X-Forwarded-For": "1.2.3.4"})
        self.assertEqual(response.status_code, 201)
        receipt = response.json()["receipt"]
        row = self.store.get(receipt)
        self.assertEqual(row["source_ip"], "198.51.100.5")
        self.assertEqual(row["body"], data["text"])
        self.assertNotEqual(row["browser_hash"], self.client.cookies[web.COOKIE])
        replay = self.client.post("/suggest", data=data)
        self.assertEqual(replay.status_code, 201)
        self.assertIn(receipt, replay.text)
        self.assertEqual(len(self.store.outstanding()), 1)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_security_consent_and_html_escaping(self):
        data = self.form()
        for change, headers, status in (
            ({"csrf": "forged"}, {}, 403),
            ({}, {"Origin": "https://foreign.example"}, 403),
            ({"consent": "no"}, {}, 400),
            ({"text": ""}, {}, 400),
            ({"text": "😀" * 1501}, {}, 400),
        ):
            with self.subTest(change=change):
                response = self.client.post("/suggest", data=data | change, headers=headers)
                self.assertEqual(response.status_code, status)
        response = self.client.post("/suggest", data=data | {"consent": "no", "text": "</textarea><script>alert(1)</script>"})
        self.assertNotIn("<script>alert(1)</script>", response.text)
        self.assertIn("&lt;script&gt;", response.text)
        self.assertEqual(self.store.outstanding(), [])
        self.assertEqual(self.client.post("/suggest", content="x" * 40001, headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code, 413)

    def test_repeated_submissions_and_ip_and_cookie_bans(self):
        receipts = []
        for _ in range(4):
            response = self.client.post("/suggest", data=self.form(), headers={"Accept": "application/json"})
            self.assertEqual(response.status_code, 201)
            receipts.append(response.json()["receipt"])
        self.store.block(receipts[0])
        self.client.cookies.clear()  # A new cookie cannot evade the IP ban.
        self.assertEqual(self.client.post("/suggest", data=self.form()).status_code, 403)
        row = self.store.get(receipts[0])
        with self.assertRaises(SubmissionError) as error:
            self.store.submit("same browser, different IP", "203.0.113.5", row["browser_hash"], "new-nonce")
        self.assertEqual(error.exception.status, 403)
        self.store.unblock(receipts[0])
        receipt = self.store.submit("allowed again", "203.0.113.5", row["browser_hash"], "new-nonce")
        self.assertEqual(self.store.get(receipt)["state"], "pending")

    def test_concurrent_api_requests_share_one_receipt(self):
        def send(_):
            return SuggestionStore(self.store.path).submit("text", "198.51.100.5", "browser", "same-request")
        with ThreadPoolExecutor(max_workers=4) as pool:
            receipts = list(pool.map(send, range(8)))
        self.assertEqual(len(set(receipts)), 1)


class ModerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SuggestionStore(Path(directory.name) / "queue.db")
        patcher = patch.object(suggestions, "store", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = {"mode": "normal", "ban_messages": "off"}
        config_patch = patch.object(suggestions, "moderation_config", lambda: (self.config, {1, 2}, -100))
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.receipt = self.store.submit("<b>Текст</b>", "198.51.100.5", "browser", "nonce")
        self.store.notified(self.receipt, 1, 10, "pending")
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=50)), edit_message_text=AsyncMock())
        self.context = SimpleNamespace(bot=self.bot)

    async def click(self, action, user=1):
        query = SimpleNamespace(data=f"suggestion^{action}^{self.receipt}", from_user=SimpleNamespace(id=user),
                                message=SimpleNamespace(chat_id=1, message_id=10), answer=AsyncMock())
        await suggestions.suggestion_callback(SimpleNamespace(callback_query=query), self.context)
        return query

    async def test_moderators_receive_ip_and_text_once(self):
        await suggestions.notify_once(self.bot)
        await suggestions.notify_once(self.bot)
        self.bot.send_message.assert_awaited_once()
        args = self.bot.send_message.call_args.args
        self.assertEqual(args[0], 2)
        self.assertIn("IP: 198.51.100.5", args[1])
        self.assertIn("<b>Текст</b>", args[1])
        self.assertNotIn("browser", args[1])
        self.assertIsNone(self.bot.send_message.call_args.kwargs["parse_mode"])

    async def test_publish_once_clear_body_and_never_publish_ip(self):
        await asyncio.gather(self.click("approve"), self.click("approve"))
        self.bot.send_message.assert_awaited_once_with(-100, "<b>Текст</b>", parse_mode=None, disable_web_page_preview=True)
        row = self.store.get(self.receipt)
        self.assertEqual((row["state"], row["body"], row["message_id"]), ("published", None, 50))
        self.assertNotIn("198.51.100.5", self.bot.edit_message_text.call_args.args[0])

    async def test_unauthorized_and_lockdown_cannot_publish(self):
        await self.click("approve", user=999)
        for config in ({"mode": "Judgment Day"}, {"ban_messages": "all"}):
            self.config.clear()
            self.config.update(config)
            await self.click("approve")
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(self.store.get(self.receipt)["state"], "pending")

    async def test_ambiguous_publication_requires_manual_check_even_after_restart(self):
        self.bot.send_message.side_effect = TimedOut()
        await self.click("approve")
        self.assertEqual(self.store.get(self.receipt)["state"], "uncertain")
        await self.click("approve")
        self.bot.send_message.assert_awaited_once()
        await self.click("retry")
        self.assertEqual(self.store.get(self.receipt)["state"], "pending")
        self.store.transition(self.receipt, "pending", "publishing")
        self.store.recover()
        self.assertEqual(self.store.get(self.receipt)["state"], "uncertain")
        await self.click("confirm")
        self.assertIsNone(self.store.get(self.receipt)["body"])

    async def test_block_and_unban_from_notification(self):
        await self.click("block")
        self.assertTrue(self.store.blocked(self.receipt))
        self.assertEqual(self.store.get(self.receipt)["state"], "rejected")
        self.assertIn("198.51.100.5", self.bot.edit_message_text.call_args.args[0])
        await self.click("unban")
        self.assertFalse(self.store.blocked(self.receipt))
        self.assertNotIn("198.51.100.5", self.bot.edit_message_text.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
