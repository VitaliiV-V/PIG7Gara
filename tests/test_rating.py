import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bot import rating
from bot.rating_store import RatingStore

MAIN = -1001234567890
ADA = {"id": 10, "name": "Ada <admin>"}
BOB = {"id": 20, "name": "Bob"}


def event(message_id=1, counts=(), date=100, chat_id=MAIN):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id), message_id=message_id,
        date=datetime.fromtimestamp(date, timezone.utc),
        reactions=[SimpleNamespace(total_count=count, type=SimpleNamespace(type="emoji", emoji=emoji))
                   for emoji, count in counts],
    )


def channel_handlers(environment):
    nodes = [node for node in ast.parse(Path("bot/channel.py").read_text()).body
             if isinstance(node, ast.AsyncFunctionDef)]
    environment.update(Update=object, ContextTypes=SimpleNamespace(DEFAULT_TYPE=object))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "bot/channel.py", "exec"), environment)
    return environment["reply_in_channel"]


class RatingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = RatingStore(Path(self.directory.name) / "rating.db")
        self.patch = patch.object(rating, "store", self.store)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def scores(self):
        return {user_id: score for user_id, name, score in self.store.leaderboard(MAIN, [])}

    def test_lazy_storage_and_equal_weights_for_all_reactions(self):
        self.assertFalse(self.store.path.exists())
        self.store.record_post(MAIN, 1, ADA)
        rating.record_reactions(event(counts=(("👍", 2), ("👎", 3))), 1)
        self.assertEqual(self.scores()[10], 5)
        rating.record_reactions(event(counts=(("👍", 4), ("👎", 1)), date=101), 2)
        self.assertEqual(self.scores()[10], 5)

    def test_duplicate_out_of_order_and_removed_reactions(self):
        self.store.record_post(MAIN, 1, ADA)
        rating.record_reactions(event(counts=(("👎", 8),)), 10)
        rating.record_reactions(event(counts=(("👎", 8),)), 10)
        self.assertEqual(self.scores()[10], 8)
        rating.record_reactions(event(counts=(("👍", 2),), date=101), 11)
        rating.record_reactions(event(counts=(("👍", 99),), date=100), 100)
        self.assertEqual(self.scores()[10], 2)
        rating.record_reactions(event(counts=(), date=102), 12)
        self.assertEqual(self.scores()[10], 0)

    def test_same_second_orders_by_update_id(self):
        self.store.record_post(MAIN, 1, ADA)
        self.store.set_reactions(MAIN, 1, 3, 100, 12)
        self.store.set_reactions(MAIN, 1, 7, 100, 11)
        self.assertEqual(self.scores()[10], 3)
        self.store.set_reactions(MAIN, 1, 4, 100, 13)
        self.assertEqual(self.scores()[10], 4)

    def test_multiple_posts_reload_and_separate_chats(self):
        self.store.record_post(MAIN, 1, ADA)
        self.store.record_post(MAIN, 2, ADA)
        self.store.record_post(MAIN, 3, BOB)
        self.store.record_post(-99, 1, BOB)
        for chat, message, count in ((MAIN, 1, 2), (MAIN, 2, 4), (MAIN, 3, 5), (-99, 1, 100)):
            self.store.set_reactions(chat, message, count, 100, message)
        reloaded = RatingStore(self.store.path).leaderboard(MAIN, [])
        self.assertEqual([(row[0], row[2]) for row in reloaded], [(10, 6), (20, 5)])

    def test_early_reaction_update_and_immutable_post_author(self):
        self.store.set_reactions(MAIN, 1, 7, 100, 1)
        self.assertEqual(self.scores(), {})
        self.store.record_post(MAIN, 1, ADA)
        self.store.record_post(MAIN, 1, BOB)
        self.assertEqual(self.scores(), {10: 7, 20: 0})

    def test_moderation_removes_only_the_deleted_post(self):
        for message_id in (1, 2):
            self.store.record_post(MAIN, message_id, ADA)
            self.store.set_reactions(MAIN, message_id, 3, 100, message_id)
        rating.forget_post(MAIN, 1)
        rating.record_reactions(event(message_id=1, counts=(("👍", 9),), date=101), 3)
        self.assertEqual(self.scores()[10], 3)

    def test_concurrent_writers_preserve_post_totals(self):
        def write(message_id):
            self.store.record_post(MAIN, message_id, ADA)
            self.store.set_reactions(MAIN, message_id, message_id, 100, message_id)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(write, range(1, 13)))
        self.assertEqual(self.scores()[10], sum(range(1, 13)))

    def test_negative_counts_are_rejected(self):
        with self.assertRaises(ValueError):
            self.store.set_reactions(MAIN, 1, -1, 100, 1)

    def test_album_keeps_verified_author_after_nonce_rotation(self):
        self.store.record_post(MAIN, 1, ADA, "album", "Adaold")
        config = {"owner_name": "Owner", "uuid": "new", "admins": [
            {"id": "10", "name": "Ada", "uuid": "new"},
        ]}
        msg = SimpleNamespace(chat_id=MAIN, author_signature="Adaold", from_user=None, media_group_id="album")
        self.assertEqual(rating.author_for_post(msg, config, 1, "@owner"), ADA)
        msg.media_group_id = "another-album"
        self.assertIsNone(rating.author_for_post(msg, config, 1, "@owner"))
        msg.media_group_id = "album"
        msg.chat_id = -99
        self.assertIsNone(rating.author_for_post(msg, config, 1, "@owner"))

    async def test_post_command_records_the_actual_requester(self):
        source = ast.parse(Path("bot/handlers.py").read_text())
        handler = next(node for node in source.body if isinstance(node, ast.AsyncFunctionDef)
                       and node.name == "post_handler")
        environment = {
            "Update": object, "ContextTypes": SimpleNamespace(DEFAULT_TYPE=object),
            "MAIN_CHANNEL_ID": MAIN, "check_id": AsyncMock(return_value=True),
            "remember_post": rating.remember_post,
        }
        exec(compile(ast.Module(body=[handler], type_ignores=[]), "bot/handlers.py", "exec"), environment)
        posted = SimpleNamespace(chat_id=MAIN, message_id=1)
        msg = SimpleNamespace(from_user=SimpleNamespace(id=10, full_name="Ada"),
                              text="/post text", reply_text=AsyncMock())
        bot = SimpleNamespace(send_message=AsyncMock(return_value=posted))
        await environment["post_handler"](SimpleNamespace(message=msg), SimpleNamespace(bot=bot))
        self.store.set_reactions(MAIN, 1, 3, 100, 1)
        self.assertEqual(self.scores(), {10: 3})

    def test_known_signature_uses_stable_owner_id(self):
        config = {"owner_name": "Owner", "uuid": "nonce", "admins": [
            {"id": "10", "name": "Ada", "uuid": "old"},
            {"id": "10", "name": "Another channel", "uuid": "new"},
        ]}
        msg = SimpleNamespace(author_signature="Adaold", from_user=None)
        self.assertEqual(rating.author_for_post(msg, config, 1, "@owner"), {"id": 10, "name": "Ada"})
        msg.author_signature = "Another channelnew"
        self.assertEqual(rating.author_for_post(msg, config, 1, "@owner")["id"], 10)
        msg.author_signature = "Adaforged"
        self.assertIsNone(rating.author_for_post(msg, config, 1, "@owner"))

    async def test_channel_captures_author_before_nonce_rotation(self):
        config = {"owner_name": "Owner", "uuid": "old"}
        msg = SimpleNamespace(chat_id=MAIN, message_id=1, author_signature="Ownerold", from_user=None)
        async def accept_owner(**kwargs):
            config["uuid"] = "new"
            return True
        remember = Mock(side_effect=rating.remember_post)
        environment = {
            "MAIN_CHANNEL_ID": MAIN, "OWNER_ID": 1, "OWNER_USERNAME": "@owner",
            "load_config": lambda: config, "check_shadow_post": AsyncMock(return_value=False),
            "author_for_post": rating.author_for_post, "remember_post": remember,
            "check_owner": accept_owner,
        }
        await channel_handlers(environment)(SimpleNamespace(edited_channel_post=None, channel_post=msg), object())
        remember.assert_called_once_with(msg, {"id": 1, "name": "Owner"})

    async def test_shadow_and_rejected_posts_do_not_enter_rating(self):
        msg = SimpleNamespace(chat_id=MAIN, message_id=1)
        update = SimpleNamespace(edited_channel_post=None, channel_post=msg)
        for shadow_post, accepted in ((True, True), (False, False)):
            with self.subTest(shadow=shadow_post, accepted=accepted):
                environment = {
                    "MAIN_CHANNEL_ID": MAIN, "OWNER_ID": 1, "OWNER_USERNAME": "@owner",
                    "load_config": lambda: {}, "check_shadow_post": AsyncMock(return_value=shadow_post),
                    "author_for_post": Mock(return_value=ADA), "remember_post": Mock(),
                    "check_owner": AsyncMock(return_value=False), "check_super_user": AsyncMock(return_value=False),
                    "check_message": AsyncMock(return_value=accepted),
                }
                await channel_handlers(environment)(update, object())
                environment["remember_post"].assert_not_called()
                if shadow_post:
                    environment["author_for_post"].assert_not_called()

    async def test_public_rating_escapes_author_names(self):
        self.store.record_post(MAIN, 1, ADA)
        self.store.set_reactions(MAIN, 1, 5, 100, 1)
        msg = SimpleNamespace(from_user=SimpleNamespace(id=999), reply_text=AsyncMock())
        settings = SimpleNamespace(MAIN_CHANNEL_ID=MAIN, OWNER_ID=1, OWNER_USERNAME="@owner")
        with patch.dict("sys.modules", {"bot.settings": settings, "config.config": SimpleNamespace(load_config=lambda: {})}):
            await rating.rating_handler(SimpleNamespace(message=msg), object())
        text = msg.reply_text.call_args.args[0]
        self.assertIn("Ada &lt;admin&gt; — <b>5</b>", text)
        self.assertIn("Shadow-публикации в рейтинг не входят", text)

    async def test_existing_reaction_handler_keeps_owner_notifications(self):
        source = ast.parse(Path("bot/handlers.py").read_text())
        handler = next(node for node in source.body if isinstance(node, ast.AsyncFunctionDef)
                       and node.name == "on_reaction_counts_handler")
        environment = {"MAIN_CHANNEL_ID": MAIN, "OWNER_ID": 1, "record_reactions": rating.record_reactions}
        exec(compile(ast.Module(body=[handler], type_ignores=[]), "bot/handlers.py", "exec"), environment)
        self.store.record_post(MAIN, 1, ADA)
        bot = SimpleNamespace(send_message=AsyncMock())
        await environment["on_reaction_counts_handler"](
            SimpleNamespace(message_reaction_count=event(counts=(("👎", 4),)), update_id=1),
            SimpleNamespace(bot=bot),
        )
        self.assertEqual(self.scores()[10], 4)
        self.assertEqual(bot.send_message.call_args.kwargs["chat_id"], 1)
        await environment["on_reaction_counts_handler"](
            SimpleNamespace(message_reaction_count=event(chat_id=-99), update_id=2), SimpleNamespace(bot=bot)
        )
        self.assertEqual(bot.send_message.await_count, 1)


if __name__ == "__main__":
    unittest.main()
