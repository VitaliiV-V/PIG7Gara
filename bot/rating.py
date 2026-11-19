"""Public administrator ratings, independent of reaction sentiment."""

import html
import logging
import sqlite3

from bot.rating_store import RatingStore

logger = logging.getLogger(__name__)
store = RatingStore()


def _author(user_id, name):
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return None
    if user_id <= 0:
        return None
    name = "".join(char for char in (name or "") if not 0xE0100 <= ord(char) < 0xE01F0)
    return {"id": user_id, "name": name.strip()[:64] or "Администратор"}


def configured_authors(config, owner_id, owner_username):
    authors = {}
    owner = _author(owner_id, config.get("owner_name") or owner_username or "Владелец")
    if owner:
        authors[owner["id"]] = owner
    for category in ("alpha_users", "admins"):
        for entry in config.get(category, []):
            author = _author(entry.get("id"), entry.get("name") or entry.get("owner"))
            if author and author["id"] not in authors:
                authors[author["id"]] = author
    return list(authors.values())


def author_for_post(msg, config, owner_id, owner_username):
    """Capture a known identity before protection rotates its UUID.

    This does not authorize a post: callers must record it only after the
    existing protection/moderation pipeline has accepted it.
    """
    signature = msg.author_signature or ""
    owner_name = config.get("owner_name", "")
    if owner_name and signature == owner_name + config.get("uuid", ""):
        return _author(owner_id, owner_name or owner_username)
    for category in ("alpha_users", "admins"):
        for entry in config.get(category, []):
            name = entry.get("name", "")
            if name and signature == name + entry.get("uuid", ""):
                return _author(entry.get("id"), name)
    album_id = getattr(msg, "media_group_id", None)
    if album_id:
        try:
            author = store.album_author(msg.chat_id, album_id, signature)
            if author:
                return author
        except (OSError, sqlite3.Error):
            logger.exception("Could not resolve rating album author")
    user = getattr(msg, "from_user", None)
    allowed_ids = {author["id"] for author in configured_authors(config, owner_id, owner_username)}
    allowed_ids.update(config.get("root_users", []))
    if user and not user.is_bot and user.id in allowed_ids:
        return _author(user.id, user.full_name)
    return None


def remember_post(msg, author):
    if author is None:
        return
    try:
        store.record_post(
            msg.chat_id, msg.message_id, author,
            getattr(msg, "media_group_id", None), getattr(msg, "author_signature", None),
        )
    except (OSError, sqlite3.Error):
        logger.exception("Could not record rating author for message %s", msg.message_id)


def forget_post(chat_id, message_id):
    try:
        store.remove_post(chat_id, message_id)
    except (OSError, sqlite3.Error):
        logger.exception("Could not remove rating post %s", message_id)


def record_reactions(event, update_id):
    total = sum(item.total_count for item in event.reactions)
    try:
        store.set_reactions(
            event.chat.id, event.message_id, total, int(event.date.timestamp()), update_id
        )
    except (OSError, sqlite3.Error):
        logger.exception("Could not update reaction rating for message %s", event.message_id)


async def rating_handler(update, context):
    from bot.settings import MAIN_CHANNEL_ID, OWNER_ID, OWNER_USERNAME
    from config.config import load_config

    msg = update.message
    if msg is None:
        return
    authors = configured_authors(load_config(), OWNER_ID, OWNER_USERNAME)
    try:
        leaders = store.leaderboard(MAIN_CHANNEL_ID, authors)
    except (OSError, sqlite3.Error):
        logger.exception("Could not load administrator rating")
        await msg.reply_text("Рейтинг временно недоступен. Попробуйте позже.")
        return
    lines = ["<b>Рейтинг администраторов · топ-20</b>", "Все реакции равнозначны: 👍 = 👎.", ""]
    for rank, (_, name, score) in enumerate(leaders, 1):
        lines.append(f"{rank}. {html.escape(name)} — <b>{score}</b>")
    if not leaders:
        lines.append("Пока нет данных.")
    lines += ["", "Учёт новых постов. Shadow-публикации в рейтинг не входят."]
    await msg.reply_text("\n".join(lines), parse_mode="HTML")
