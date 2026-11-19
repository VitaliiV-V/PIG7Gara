"""Moderation of website submissions. No Telegram identity is assigned to a post."""

import asyncio
from contextlib import suppress
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, TelegramError

from bot.suggestion_store import SuggestionStore

logger = logging.getLogger(__name__)
store = SuggestionStore()


def moderation_config():
    from bot.settings import OWNER_ID, MAIN_CHANNEL_ID
    from config.config import load_config
    config = load_config()
    recipients = {OWNER_ID, *(int(user) for user in config.get("root_users", []))}
    return config, recipients, MAIN_CHANNEL_ID


def notification(row):
    receipt, state = row["receipt"], row["state"]

    def button(text, action):
        return InlineKeyboardButton(text, callback_data=f"suggestion^{action}^{receipt}")

    title = f"Веб-предложка · {receipt}"
    sender = f"{title}\nIP: {row['source_ip']}"
    if state == "pending":
        return f"{sender}\n\n{row['body']}", InlineKeyboardMarkup([
            [button("Опубликовать", "approve"), button("Отклонить", "reject")],
            [button("Отклонить и заблокировать IP + cookie", "block")],
        ])
    if state == "uncertain":
        return (f"{sender}\n\nСтатус отправки неизвестен. Сначала проверьте канал: пост мог быть опубликован.\n\n{row['body']}",
                InlineKeyboardMarkup([[button("Уже опубликовано", "confirm")],
                                      [button("Проверил канал — повторить", "retry")]]))
    if state == "rejected" and store.blocked(receipt):
        return f"{sender}\n\nОтклонено. IP-адрес и cookie заблокированы. Общий IP затрагивает и других посетителей.", InlineKeyboardMarkup([[button("Снять блокировку", "unban")]])
    status = {"publishing": "Публикация…", "published": "Опубликовано.", "rejected": "Отклонено."}[state]
    return f"{title}\n\n{status}", InlineKeyboardMarkup([])


async def sync_notifications(bot, row, recipients=()):
    text, markup = notification(row)
    existing = {notice["chat_id"]: notice for notice in store.notifications(row["receipt"])}
    for chat_id in set(existing) | set(recipients):
        notice = existing.get(chat_id)
        if notice and notice["state"] == row["state"]:
            continue
        try:
            if notice:
                try:
                    await bot.edit_message_text(text, chat_id=chat_id, message_id=notice["message_id"],
                                                reply_markup=markup, parse_mode=None)
                except BadRequest as exc:
                    if "message is not modified" not in str(exc).lower():
                        raise
                message_id = notice["message_id"]
            else:
                sent = await bot.send_message(chat_id, text, reply_markup=markup, parse_mode=None,
                                              disable_web_page_preview=True)
                message_id = sent.message_id
            store.notified(row["receipt"], chat_id, message_id, row["state"])
        except TelegramError:
            # Never include proposal text, cookie, IP or raw Telegram exceptions in these logs.
            logger.warning("Could not update a suggestion moderation notification")


async def suggestion_callback(update, context):
    query = update.callback_query
    config, recipients, channel_id = moderation_config()
    if query.from_user.id not in recipients:
        await query.answer("Недостаточно прав.", show_alert=True)
        return
    _, action, receipt = query.data.split("^")
    row = store.get(receipt)
    message = query.message
    if not row or not message or not any(
        notice["chat_id"] == message.chat_id and notice["message_id"] == message.message_id
        for notice in store.notifications(receipt)
    ):
        await query.answer("Заявка недоступна.", show_alert=True)
        return
    if action == "approve":
        if config.get("mode") == "Judgment Day" or config.get("ban_messages") == "all":
            await query.answer("Публикации приостановлены настройками модерации.", show_alert=True)
            return
        await query.answer("Обрабатываю заявку…")
        if not store.transition(receipt, "pending", "publishing"):
            await sync_notifications(context.bot, store.get(receipt))
            return
        try:
            sent = await context.bot.send_message(channel_id, row["body"], parse_mode=None,
                                                   disable_web_page_preview=True)
        except (BadRequest, Forbidden):
            store.transition(receipt, "publishing", "pending")
            await context.bot.send_message(message.chat_id, "Не удалось опубликовать заявку. Проверьте права бота и настройки канала, затем повторите.")
        except TelegramError:
            store.transition(receipt, "publishing", "uncertain")
        else:
            store.transition(receipt, "publishing", "published", sent.message_id)
    else:
        if action == "block":
            changed = store.block(receipt)
        elif action == "unban":
            changed = store.unblock(receipt)
        else:
            expected, state = {"reject": ("pending", "rejected"),
                               "retry": ("uncertain", "pending"),
                               "confirm": ("uncertain", "published")}[action]
            changed = store.transition(receipt, expected, state)
        await query.answer("Готово." if changed else "Заявка уже обработана.")
    await sync_notifications(context.bot, store.get(receipt))


async def notify_once(bot):
    _, recipients, _ = moderation_config()
    for row in store.outstanding():
        await sync_notifications(bot, row, recipients if row["state"] in ("pending", "uncertain") else ())


async def notification_worker(application):
    ticks = 0
    recovered = False
    while True:
        try:
            if not recovered:
                store.recover()
                recovered = True
            await notify_once(application.bot)
            if ticks % 720 == 0:
                store.prune()
            ticks += 1
        except Exception:
            logger.warning("Could not process the suggestion queue")
        await asyncio.sleep(5)


async def start_suggestions(application):
    application.bot_data["suggestion_worker"] = asyncio.create_task(notification_worker(application))


async def stop_suggestions(application):
    task = application.bot_data.pop("suggestion_worker", None)
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
