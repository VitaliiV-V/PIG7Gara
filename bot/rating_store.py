"""Persistent reaction totals; Telegram count updates are absolute snapshots."""

from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3


class RatingStore:
    def __init__(self, path="rating.db"):
        self.path = Path(path)

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS rating_authors (
                    user_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rating_posts (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    author_id INTEGER,
                    album_id TEXT,
                    signature TEXT,
                    reactions INTEGER NOT NULL DEFAULT 0 CHECK (reactions >= 0),
                    reaction_date INTEGER NOT NULL DEFAULT -1,
                    update_id INTEGER NOT NULL DEFAULT -1,
                    PRIMARY KEY (chat_id, message_id)
                );
            """)
            with db:
                yield db

    def record_post(self, chat_id, message_id, author, album_id=None, signature=None):
        with self.connection() as db:
            db.execute(
                "INSERT INTO rating_authors VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name",
                (author["id"], author["name"]),
            )
            # Preserve the original author on redelivery and edits. A reaction
            # update can arrive before the publication has been recorded.
            db.execute(
                "INSERT INTO rating_posts (chat_id, message_id, author_id, album_id, signature) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id, message_id) DO UPDATE SET "
                "author_id = COALESCE(rating_posts.author_id, excluded.author_id), "
                "album_id = COALESCE(rating_posts.album_id, excluded.album_id), "
                "signature = COALESCE(rating_posts.signature, excluded.signature)",
                (chat_id, message_id, author["id"], album_id, signature),
            )

    def album_author(self, chat_id, album_id, signature):
        if not album_id or not self.path.exists():
            return None
        with self.connection() as db:
            row = db.execute(
                "SELECT a.user_id, a.name FROM rating_posts p "
                "JOIN rating_authors a ON a.user_id = p.author_id "
                "WHERE p.chat_id = ? AND p.album_id = ? AND p.signature = ? LIMIT 1",
                (chat_id, album_id, signature),
            ).fetchone()
            return {"id": row[0], "name": row[1]} if row else None

    def set_reactions(self, chat_id, message_id, total, date, update_id):
        if type(total) is not int or total < 0:
            raise ValueError("Reaction count must be a non-negative integer")
        with self.connection() as db:
            cursor = db.execute(
                "INSERT INTO rating_posts "
                "(chat_id, message_id, reactions, reaction_date, update_id) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id, message_id) DO UPDATE SET "
                "reactions = excluded.reactions, reaction_date = excluded.reaction_date, "
                "update_id = excluded.update_id "
                "WHERE (excluded.reaction_date, excluded.update_id) > "
                "(rating_posts.reaction_date, rating_posts.update_id)",
                (chat_id, message_id, total, date, update_id),
            )
            return cursor.rowcount == 1

    def remove_post(self, chat_id, message_id):
        if not self.path.exists():
            return
        with self.connection() as db:
            db.execute(
                "DELETE FROM rating_posts WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            )

    def leaderboard(self, chat_id, authors, limit=20):
        with self.connection() as db:
            db.executemany(
                "INSERT INTO rating_authors VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name",
                [(author["id"], author["name"]) for author in authors],
            )
            return db.execute(
                "SELECT a.user_id, a.name, COALESCE(SUM(p.reactions), 0) AS score "
                "FROM rating_authors a LEFT JOIN rating_posts p "
                "ON p.author_id = a.user_id AND p.chat_id = ? "
                "GROUP BY a.user_id ORDER BY score DESC, a.name COLLATE NOCASE, a.user_id LIMIT ?",
                (chat_id, limit),
            ).fetchall()
