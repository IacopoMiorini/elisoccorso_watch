"""SQLite storage per le iscrizioni del bot.

Schema:
    subscribers(chat_id, created_at, is_admin)
    subscriptions(chat_id, helicopter_key)
    bot_state(key, value)  -- es. last_update_id del getUpdates

Thread-safety: il modulo sqlite3 stdlib con `check_same_thread=False` permette
di condividere la connessione fra thread se si serializzano le scritture con
un Lock. Il `journal_mode=WAL` permette letture concorrenti senza blocchi.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

log = logging.getLogger("heli-tracker.storage")

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id    INTEGER PRIMARY KEY,
    created_at INTEGER NOT NULL,
    is_admin   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS subscriptions (
    chat_id        INTEGER NOT NULL,
    helicopter_key TEXT    NOT NULL,
    PRIMARY KEY (chat_id, helicopter_key),
    FOREIGN KEY (chat_id) REFERENCES subscribers(chat_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_heli
    ON subscriptions(helicopter_key);
"""


class Storage:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            isolation_level=None,  # autocommit: gestiamo BEGIN/COMMIT esplicito
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._write_lock = threading.Lock()

    @contextmanager
    def _tx(self):
        with self._write_lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # --- subscribers ---------------------------------------------------------

    def add_subscriber(self, chat_id: int, is_admin: bool = False) -> bool:
        """Inserisce il subscriber. Ritorna True se era nuovo, False se esisteva già."""
        with self._tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO subscribers(chat_id, created_at, is_admin) "
                "VALUES (?, ?, ?)",
                (chat_id, int(time.time()), 1 if is_admin else 0),
            )
            return cur.rowcount > 0

    def mark_admin(self, chat_id: int) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE subscribers SET is_admin = 1 WHERE chat_id = ?", (chat_id,)
            )

    def remove_subscriber(self, chat_id: int) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))

    def count_subscribers(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()
        return int(row[0]) if row else 0

    # --- subscriptions -------------------------------------------------------

    def set_subscription(self, chat_id: int, heli_key: str, subscribed: bool) -> None:
        with self._tx() as c:
            if subscribed:
                c.execute(
                    "INSERT OR IGNORE INTO subscriptions(chat_id, helicopter_key) "
                    "VALUES (?, ?)",
                    (chat_id, heli_key),
                )
            else:
                c.execute(
                    "DELETE FROM subscriptions WHERE chat_id = ? AND helicopter_key = ?",
                    (chat_id, heli_key),
                )

    def set_all_subscriptions(self, chat_id: int, heli_keys: Iterable[str]) -> None:
        keys = list(heli_keys)
        with self._tx() as c:
            c.execute("DELETE FROM subscriptions WHERE chat_id = ?", (chat_id,))
            if keys:
                c.executemany(
                    "INSERT INTO subscriptions(chat_id, helicopter_key) VALUES (?, ?)",
                    [(chat_id, k) for k in keys],
                )

    def subscriptions_of(self, chat_id: int) -> set[str]:
        rows = self._conn.execute(
            "SELECT helicopter_key FROM subscriptions WHERE chat_id = ?",
            (chat_id,),
        ).fetchall()
        return {r[0] for r in rows}

    def subscribers_for(self, heli_key: str) -> list[int]:
        rows = self._conn.execute(
            "SELECT chat_id FROM subscriptions WHERE helicopter_key = ?",
            (heli_key,),
        ).fetchall()
        return [int(r[0]) for r in rows]

    def cleanup_orphans(self, valid_keys: set[str]) -> int:
        """Rimuove iscrizioni a elicotteri non più presenti in helicopters.yaml."""
        if not valid_keys:
            return 0
        placeholders = ",".join("?" for _ in valid_keys)
        with self._tx() as c:
            cur = c.execute(
                f"DELETE FROM subscriptions WHERE helicopter_key NOT IN ({placeholders})",
                list(valid_keys),
            )
            return cur.rowcount

    # --- bot_state -----------------------------------------------------------

    def get_state(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM bot_state WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO bot_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # --- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass
