"""
database.py - Stores price alerts in a small SQLite database.

SQLite is a database that lives in a single file (alerts.db) and is built
into Python, so there is nothing extra to install or run.

Each alert has two numbers:
- id        : internal database ID, unique across everyone (never shown to users)
- user_num  : the number a user sees (#1, #2, ...), counted separately per user
"""

import sqlite3
from contextlib import contextmanager

DB_FILE = "alerts.db"


@contextmanager
def _connect():
    """Open the database, commit changes if all went well, and always close it."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row  # rows behave like dicts: row["symbol"]
    try:
        with conn:  # commits on success, rolls back on error
            yield conn
    finally:
        conn.close()


def init_db():
    """Create the alerts table if it does not exist yet."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,  -- which Telegram chat to notify
                user_num   INTEGER NOT NULL,  -- per-user alert number shown as #1, #2...
                symbol     TEXT    NOT NULL,  -- e.g. BTC or XAUUSD
                target     REAL    NOT NULL,  -- the price level to watch
                direction  TEXT    NOT NULL,  -- 'above' or 'below'
                created_at TEXT    DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (chat_id, user_num)    -- no two alerts share a number per user
            )
            """
        )


def add_alert(chat_id, symbol, target, direction):
    """
    Save a new alert and return its per-user number.
    The number is one higher than this user's highest current alert, so a
    user with no alerts starts again at #1.
    """
    with _connect() as conn:
        # Working out the number and inserting happen in ONE statement,
        # so two alerts can never accidentally get the same number.
        conn.execute(
            """
            INSERT INTO alerts (chat_id, user_num, symbol, target, direction)
            SELECT ?, COALESCE(MAX(user_num), 0) + 1, ?, ?, ?
            FROM alerts WHERE chat_id = ?
            """,
            (chat_id, symbol, target, direction, chat_id),
        )
        return conn.execute(
            "SELECT MAX(user_num) FROM alerts WHERE chat_id = ?", (chat_id,)
        ).fetchone()[0]


def get_alerts_for_chat(chat_id):
    """Return all alerts belonging to one user/chat, in number order."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM alerts WHERE chat_id = ? ORDER BY user_num", (chat_id,)
        ).fetchall()


def get_all_alerts():
    """Return every alert (used by the background price checker)."""
    with _connect() as conn:
        return conn.execute("SELECT * FROM alerts").fetchall()


def delete_alert(chat_id, user_num):
    """
    Delete one of a user's alerts by its per-user number.
    Because chat_id is part of the lookup, users can only delete their own
    alerts. Returns True if something was deleted.
    """
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM alerts WHERE chat_id = ? AND user_num = ?", (chat_id, user_num)
        )
        return cursor.rowcount > 0
