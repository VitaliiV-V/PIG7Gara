"""Shared, process-safe queue for the website and the main Telegram bot."""

from contextlib import contextmanager
import os
from pathlib import Path
import secrets
import sqlite3
import time


class SubmissionError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class SuggestionStore:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv("SUGGESTIONS_DB", "suggestions.db"))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS suggestions (
                    receipt TEXT PRIMARY KEY,
                    request_key TEXT NOT NULL UNIQUE,
                    body TEXT,
                    source_ip TEXT NOT NULL,
                    browser_hash TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    created_at INTEGER NOT NULL,
                    closed_at INTEGER,
                    message_id INTEGER
                );
                CREATE INDEX IF NOT EXISTS suggestions_created ON suggestions(created_at);
                CREATE INDEX IF NOT EXISTS suggestions_ip ON suggestions(source_ip, created_at);
                CREATE TABLE IF NOT EXISTS bans (
                    kind TEXT NOT NULL,
                    value TEXT NOT NULL,
                    receipt TEXT NOT NULL,
                    PRIMARY KEY (kind, value)
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    receipt TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    PRIMARY KEY (receipt, chat_id)
                );
            """)
            with db:
                yield db
        finally:
            db.close()

    def submit(self, text, source_ip, browser_hash, request_key):
        now = int(time.time())
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM bans WHERE (kind='ip' AND value=?) OR (kind='browser' AND value=?)",
                (source_ip, browser_hash),
            ).fetchone():
                raise SubmissionError("Отправка с вашего IP-адреса или браузера заблокирована.", 403)
            previous = db.execute(
                "SELECT receipt, body, state FROM suggestions WHERE request_key=?", (request_key,)
            ).fetchone()
            if previous:
                if previous["state"] == "pending" and previous["body"] != text:
                    raise SubmissionError("Эта форма уже отправлена. Откройте новую заявку.", 409)
                return previous["receipt"]
            if db.execute("SELECT COUNT(*) FROM suggestions WHERE state IN ('pending', 'publishing', 'uncertain')").fetchone()[0] >= 200:
                raise SubmissionError("Очередь заполнена. Попробуйте позже.", 503)
            receipt = secrets.token_hex(12)
            db.execute(
                "INSERT INTO suggestions (receipt, request_key, body, source_ip, browser_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (receipt, request_key, text, source_ip, browser_hash, now),
            )
            return receipt

    def get(self, receipt):
        with self.connection() as db:
            row = db.execute("SELECT * FROM suggestions WHERE receipt=?", (receipt,)).fetchone()
            return dict(row) if row else None

    def outstanding(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM suggestions WHERE state IN ('pending', 'uncertain') OR EXISTS (SELECT 1 FROM notifications n WHERE n.receipt=suggestions.receipt AND n.state!=suggestions.state) ORDER BY created_at LIMIT 200"
            )]

    def notifications(self, receipt):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM notifications WHERE receipt=?", (receipt,))]

    def notified(self, receipt, chat_id, message_id, state):
        with self.connection() as db:
            db.execute(
                "INSERT INTO notifications VALUES (?, ?, ?, ?) ON CONFLICT(receipt, chat_id) DO UPDATE SET message_id=excluded.message_id, state=excluded.state",
                (receipt, chat_id, message_id, state),
            )

    def transition(self, receipt, expected, state, message_id=None):
        closed = state in ("published", "rejected")
        with self.connection() as db:
            return db.execute(
                "UPDATE suggestions SET state=?, body=CASE WHEN ? THEN NULL ELSE body END, closed_at=?, message_id=? WHERE receipt=? AND state=?",
                (state, closed, int(time.time()) if closed else None, message_id, receipt, expected),
            ).rowcount == 1

    def block(self, receipt):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM suggestions WHERE receipt=? AND state='pending'", (receipt,)).fetchone()
            if not row:
                return False
            for kind, value in (("ip", row["source_ip"]), ("browser", row["browser_hash"])):
                db.execute("INSERT OR IGNORE INTO bans VALUES (?, ?, ?)", (kind, value, receipt))
            db.execute("UPDATE suggestions SET state='rejected', body=NULL, closed_at=? WHERE receipt=?", (int(time.time()), receipt))
            return True

    def blocked(self, receipt):
        with self.connection() as db:
            return bool(db.execute("SELECT 1 FROM bans WHERE receipt=?", (receipt,)).fetchone())

    def unblock(self, receipt):
        with self.connection() as db:
            changed = db.execute("DELETE FROM bans WHERE receipt=?", (receipt,)).rowcount > 0
            if changed:
                db.execute("UPDATE notifications SET state='' WHERE receipt=?", (receipt,))
            return changed

    def recover(self):
        # A crash after Telegram accepted a post must never automatically send it again.
        with self.connection() as db:
            db.execute("UPDATE suggestions SET state='uncertain' WHERE state='publishing'")

    def prune(self):
        with self.connection() as db:
            db.execute(
                "DELETE FROM suggestions WHERE closed_at<? AND NOT EXISTS (SELECT 1 FROM bans WHERE bans.receipt=suggestions.receipt)",
                (int(time.time()) - 30 * 86400,),
            )
            db.execute("DELETE FROM notifications WHERE receipt NOT IN (SELECT receipt FROM suggestions)")
