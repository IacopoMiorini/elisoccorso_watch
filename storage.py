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

CREATE TABLE IF NOT EXISTS flights (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    helicopter_key  TEXT    NOT NULL,
    takeoff_ts      INTEGER NOT NULL,
    landing_ts      INTEGER,
    duration_s      INTEGER,
    distance_km     REAL,
    max_altitude_m  INTEGER,
    max_velocity_ms REAL,
    takeoff_lat     REAL,
    takeoff_lon     REAL,
    landing_lat     REAL,
    landing_lon     REAL,
    landing_site    TEXT,
    callsign        TEXT
);

-- Migrazione: la feature geofence è stata rimossa. Puliamo eventuali residui.
DROP INDEX IF EXISTS idx_geofences_chat;
DROP TABLE IF EXISTS geofences;

CREATE INDEX IF NOT EXISTS idx_subscriptions_heli
    ON subscriptions(helicopter_key);
CREATE INDEX IF NOT EXISTS idx_flights_heli_ts
    ON flights(helicopter_key, takeoff_ts DESC);
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

    # --- flights (storico voli) ----------------------------------------------

    def record_flight(
        self,
        helicopter_key: str,
        takeoff_ts: int,
        landing_ts: int,
        distance_km: float | None = None,
        max_altitude_m: float | None = None,
        max_velocity_ms: float | None = None,
        takeoff_position: tuple[float, float] | None = None,
        landing_position: tuple[float, float] | None = None,
        landing_site: str | None = None,
        callsign: str | None = None,
    ) -> int:
        """Registra un volo completato. Ritorna l'id generato."""
        duration = max(0, int(landing_ts - takeoff_ts))
        t_lat, t_lon = takeoff_position if takeoff_position else (None, None)
        l_lat, l_lon = landing_position if landing_position else (None, None)
        with self._tx() as c:
            cur = c.execute(
                """
                INSERT INTO flights (
                    helicopter_key, takeoff_ts, landing_ts, duration_s,
                    distance_km, max_altitude_m, max_velocity_ms,
                    takeoff_lat, takeoff_lon, landing_lat, landing_lon,
                    landing_site, callsign
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    helicopter_key,
                    int(takeoff_ts),
                    int(landing_ts),
                    duration,
                    float(distance_km) if distance_km is not None else None,
                    int(max_altitude_m) if max_altitude_m is not None else None,
                    float(max_velocity_ms) if max_velocity_ms is not None else None,
                    t_lat,
                    t_lon,
                    l_lat,
                    l_lon,
                    landing_site,
                    callsign,
                ),
            )
            return int(cur.lastrowid or 0)

    def stats_since(self, since_ts: int) -> list[dict]:
        """Aggregato per mezzo degli ultimi voli: count, durata, distanza."""
        rows = self._conn.execute(
            """
            SELECT helicopter_key,
                   COUNT(*)               AS n_flights,
                   SUM(duration_s)        AS total_duration_s,
                   SUM(distance_km)       AS total_distance_km,
                   MAX(takeoff_ts)        AS last_takeoff_ts
            FROM flights
            WHERE takeoff_ts >= ?
            GROUP BY helicopter_key
            ORDER BY n_flights DESC
            """,
            (int(since_ts),),
        ).fetchall()
        return [
            {
                "helicopter_key": r[0],
                "n_flights": int(r[1] or 0),
                "total_duration_s": int(r[2] or 0),
                "total_distance_km": float(r[3] or 0.0),
                "last_takeoff_ts": int(r[4] or 0),
            }
            for r in rows
        ]

    def last_flights_per_heli(self) -> list[dict]:
        """Ultimo volo registrato per ciascun mezzo."""
        rows = self._conn.execute(
            """
            SELECT f.helicopter_key, f.takeoff_ts, f.landing_ts, f.duration_s,
                   f.distance_km, f.max_altitude_m, f.landing_site, f.callsign
            FROM flights f
            INNER JOIN (
                SELECT helicopter_key, MAX(takeoff_ts) AS max_ts
                FROM flights
                GROUP BY helicopter_key
            ) m ON f.helicopter_key = m.helicopter_key
               AND f.takeoff_ts = m.max_ts
            ORDER BY f.takeoff_ts DESC
            """
        ).fetchall()
        return [
            {
                "helicopter_key": r[0],
                "takeoff_ts": int(r[1]),
                "landing_ts": int(r[2]) if r[2] else None,
                "duration_s": int(r[3]) if r[3] else None,
                "distance_km": float(r[4]) if r[4] else None,
                "max_altitude_m": int(r[5]) if r[5] else None,
                "landing_site": r[6],
                "callsign": r[7],
            }
            for r in rows
        ]

    # --- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass
