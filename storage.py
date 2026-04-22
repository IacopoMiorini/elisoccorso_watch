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
    callsign        TEXT,
    inferred        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS geofences (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   INTEGER NOT NULL,
    name      TEXT    NOT NULL,
    lat       REAL    NOT NULL,
    lon       REAL    NOT NULL,
    radius_km REAL    NOT NULL,
    UNIQUE (chat_id, name),
    FOREIGN KEY (chat_id) REFERENCES subscribers(chat_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS current_states (
    helicopter_key TEXT PRIMARY KEY,
    lat            REAL,
    lon            REAL,
    altitude_m     INTEGER,
    velocity_ms    REAL,
    heading_deg    REAL,
    on_ground      INTEGER,
    in_flight      INTEGER NOT NULL DEFAULT 0,
    callsign       TEXT,
    source         TEXT,
    updated_ts     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS flight_track_points (
    flight_id INTEGER NOT NULL,
    idx       INTEGER NOT NULL,
    lat       REAL    NOT NULL,
    lon       REAL    NOT NULL,
    PRIMARY KEY (flight_id, idx),
    FOREIGN KEY (flight_id) REFERENCES flights(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_heli
    ON subscriptions(helicopter_key);
CREATE INDEX IF NOT EXISTS idx_flights_heli_ts
    ON flights(helicopter_key, takeoff_ts DESC);
CREATE INDEX IF NOT EXISTS idx_geofences_chat
    ON geofences(chat_id);
CREATE INDEX IF NOT EXISTS idx_flight_track_points_flight
    ON flight_track_points(flight_id);
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
        self._migrate()
        self._write_lock = threading.Lock()

    def _migrate(self) -> None:
        """Migrazioni idempotenti su DB già esistenti.

        SQLite non ha `ALTER TABLE ADD COLUMN IF NOT EXISTS`, quindi controlliamo
        `PRAGMA table_info` e aggiungiamo le colonne mancanti.
        """
        cols_flights = {
            r[1] for r in self._conn.execute("PRAGMA table_info(flights)").fetchall()
        }
        if "inferred" not in cols_flights:
            self._conn.execute(
                "ALTER TABLE flights ADD COLUMN inferred INTEGER NOT NULL DEFAULT 0"
            )

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
        callsign: str | None = None,
        inferred: bool = False,
    ) -> int:
        """Registra un volo completato. Ritorna l'id generato.

        Le colonne `landing_lat`, `landing_lon`, `landing_site` vengono lasciate
        NULL: il tracker notifica solo i decolli e la chiusura del record è
        silenziosa (niente lookup del sito di atterraggio).

        `inferred=True` se la chiusura è dedotta dalla perdita del segnale."""
        duration = max(0, int(landing_ts - takeoff_ts))
        t_lat, t_lon = takeoff_position if takeoff_position else (None, None)
        with self._tx() as c:
            cur = c.execute(
                """
                INSERT INTO flights (
                    helicopter_key, takeoff_ts, landing_ts, duration_s,
                    distance_km, max_altitude_m, max_velocity_ms,
                    takeoff_lat, takeoff_lon, callsign, inferred
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    callsign,
                    1 if inferred else 0,
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

    # --- geofences -----------------------------------------------------------

    def add_geofence(
        self,
        chat_id: int,
        name: str,
        lat: float,
        lon: float,
        radius_km: float,
    ) -> bool:
        """Aggiunge una zona per l'utente. Ritorna False se esiste già una zona
        con lo stesso nome per lo stesso utente (IntegrityError sul UNIQUE)."""
        with self._tx() as c:
            try:
                c.execute(
                    "INSERT INTO geofences (chat_id, name, lat, lon, radius_km) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (chat_id, name, float(lat), float(lon), float(radius_km)),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def remove_geofence(self, chat_id: int, name: str) -> bool:
        with self._tx() as c:
            cur = c.execute(
                "DELETE FROM geofences WHERE chat_id = ? AND name = ?",
                (chat_id, name),
            )
            return cur.rowcount > 0

    def geofences_of(self, chat_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT name, lat, lon, radius_km FROM geofences "
            "WHERE chat_id = ? ORDER BY name",
            (chat_id,),
        ).fetchall()
        return [
            {
                "name": r[0],
                "lat": float(r[1]),
                "lon": float(r[2]),
                "radius_km": float(r[3]),
            }
            for r in rows
        ]

    # --- current_states (stato live, per dashboard) --------------------------

    def upsert_current_state(
        self,
        helicopter_key: str,
        lat: float | None,
        lon: float | None,
        altitude_m: float | None,
        velocity_ms: float | None,
        heading_deg: float | None,
        on_ground: bool | None,
        in_flight: bool,
        callsign: str | None,
        source: str | None,
    ) -> None:
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO current_states (
                    helicopter_key, lat, lon, altitude_m, velocity_ms, heading_deg,
                    on_ground, in_flight, callsign, source, updated_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(helicopter_key) DO UPDATE SET
                    lat=excluded.lat, lon=excluded.lon,
                    altitude_m=excluded.altitude_m, velocity_ms=excluded.velocity_ms,
                    heading_deg=excluded.heading_deg, on_ground=excluded.on_ground,
                    in_flight=excluded.in_flight, callsign=excluded.callsign,
                    source=excluded.source, updated_ts=excluded.updated_ts
                """,
                (
                    helicopter_key,
                    lat,
                    lon,
                    int(altitude_m) if altitude_m is not None else None,
                    float(velocity_ms) if velocity_ms is not None else None,
                    float(heading_deg) if heading_deg is not None else None,
                    (1 if on_ground else 0) if on_ground is not None else None,
                    1 if in_flight else 0,
                    callsign,
                    source,
                    int(time.time()),
                ),
            )

    def set_in_flight(self, helicopter_key: str, in_flight: bool) -> None:
        """Aggiorna solo il flag in_flight (usato al landing inferito senza state)."""
        with self._tx() as c:
            c.execute(
                "UPDATE current_states SET in_flight = ?, updated_ts = ? "
                "WHERE helicopter_key = ?",
                (1 if in_flight else 0, int(time.time()), helicopter_key),
            )

    def all_current_states(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT helicopter_key, lat, lon, altitude_m, velocity_ms, heading_deg, "
            "       on_ground, in_flight, callsign, source, updated_ts "
            "FROM current_states"
        ).fetchall()
        return [
            {
                "helicopter_key": r[0],
                "lat": r[1],
                "lon": r[2],
                "altitude_m": r[3],
                "velocity_ms": r[4],
                "heading_deg": r[5],
                "on_ground": bool(r[6]) if r[6] is not None else None,
                "in_flight": bool(r[7]),
                "callsign": r[8],
                "source": r[9],
                "updated_ts": int(r[10]),
            }
            for r in rows
        ]

    # --- flight_track_points (per render mappa) ------------------------------

    def save_flight_track(
        self,
        flight_id: int,
        waypoints: list[tuple[float, float]],
    ) -> None:
        if not waypoints or flight_id <= 0:
            return
        with self._tx() as c:
            c.executemany(
                "INSERT INTO flight_track_points (flight_id, idx, lat, lon) "
                "VALUES (?, ?, ?, ?)",
                [(flight_id, i, lat, lon) for i, (lat, lon) in enumerate(waypoints)],
            )

    def recent_flights_with_tracks(self, limit: int = 20) -> list[dict]:
        """Ultimi N voli + lista waypoint. Usato dalla dashboard per mappare tracce."""
        flight_rows = self._conn.execute(
            """
            SELECT id, helicopter_key, takeoff_ts, landing_ts, duration_s,
                   distance_km, landing_site, callsign, inferred,
                   takeoff_lat, takeoff_lon, landing_lat, landing_lon
            FROM flights
            ORDER BY takeoff_ts DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        out: list[dict] = []
        for f in flight_rows:
            fid = int(f[0])
            pts = self._conn.execute(
                "SELECT lat, lon FROM flight_track_points "
                "WHERE flight_id = ? ORDER BY idx",
                (fid,),
            ).fetchall()
            out.append(
                {
                    "id": fid,
                    "helicopter_key": f[1],
                    "takeoff_ts": int(f[2]),
                    "landing_ts": int(f[3]) if f[3] else None,
                    "duration_s": int(f[4]) if f[4] else None,
                    "distance_km": float(f[5]) if f[5] else None,
                    "landing_site": f[6],
                    "callsign": f[7],
                    "inferred": bool(f[8]),
                    "takeoff_lat": f[9],
                    "takeoff_lon": f[10],
                    "landing_lat": f[11],
                    "landing_lon": f[12],
                    "track": [{"lat": p[0], "lon": p[1]} for p in pts],
                }
            )
        return out

    def recent_flights(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT id, helicopter_key, takeoff_ts, landing_ts, duration_s,
                   distance_km, max_altitude_m, max_velocity_ms,
                   landing_site, callsign, inferred
            FROM flights
            ORDER BY takeoff_ts DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [
            {
                "id": int(r[0]),
                "helicopter_key": r[1],
                "takeoff_ts": int(r[2]),
                "landing_ts": int(r[3]) if r[3] else None,
                "duration_s": int(r[4]) if r[4] else None,
                "distance_km": float(r[5]) if r[5] else None,
                "max_altitude_m": int(r[6]) if r[6] else None,
                "max_velocity_ms": float(r[7]) if r[7] else None,
                "landing_site": r[8],
                "callsign": r[9],
                "inferred": bool(r[10]),
            }
            for r in rows
        ]

    def all_geofences(self) -> list[tuple[int, dict]]:
        """Tutte le geofences con chat_id. Serve al broadcast Model C per
        trovare utenti che "matchano per zona" anche senza iscrizione al mezzo."""
        rows = self._conn.execute(
            "SELECT chat_id, name, lat, lon, radius_km FROM geofences"
        ).fetchall()
        return [
            (
                int(r[0]),
                {
                    "name": r[1],
                    "lat": float(r[2]),
                    "lon": float(r[3]),
                    "radius_km": float(r[4]),
                },
            )
            for r in rows
        ]

    # --- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass
