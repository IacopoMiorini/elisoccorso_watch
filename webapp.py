"""Dashboard web read-only per heli-tracker.

Espone:
- `/`         lista voli (ultimi N dalla tabella `flights`)
- `/map`      mappa Leaflet con posizioni live + tracce ultimi voli
- `/api/flights` JSON dei voli recenti
- `/api/live`    JSON delle posizioni correnti (current_states)
- `/api/tracks`  JSON tracce ultimi voli (flight_track_points)

Gira in un processo separato dal worker (systemd unit dedicata) e condivide
il DB SQLite in WAL mode, quindi può leggere concorrentemente senza bloccare
le scritture del detector.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template

from detector import load_helicopters
from storage import Storage

load_dotenv()
log = logging.getLogger("heli-tracker.web")

DB_PATH = Path(os.environ.get("DB_PATH", "heli_tracker.db"))
HELICOPTERS_FILE = Path(os.environ.get("HELICOPTERS_FILE", "helicopters.yaml"))
LANDING_SITES_FILE = Path(os.environ.get("LANDING_SITES_FILE", "landing_sites.yaml"))
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))

app = Flask(__name__)

# Storage condiviso (WAL consente multi-reader)
storage = Storage(DB_PATH)
helicopters = load_helicopters(HELICOPTERS_FILE)
_name_by_icao = {h.icao24: h.display_name for h in helicopters}


def _load_sites() -> list[dict]:
    if not LANDING_SITES_FILE.exists():
        return []
    data = yaml.safe_load(LANDING_SITES_FILE.read_text(encoding="utf-8")) or {}
    return [
        {
            "name": s.get("name", ""),
            "city": s.get("city", ""),
            "lat": float(s["lat"]),
            "lon": float(s["lon"]),
            "radius_m": float(s.get("radius_m", 500)),
        }
        for s in (data.get("sites") or [])
        if s.get("name") and s.get("lat") is not None and s.get("lon") is not None
    ]


def _iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _enrich_flight(f: dict) -> dict:
    f = dict(f)
    f["display_name"] = _name_by_icao.get(f["helicopter_key"], f["helicopter_key"])
    f["takeoff_iso"] = _iso(f.get("takeoff_ts"))
    f["landing_iso"] = _iso(f.get("landing_ts"))
    return f


# --- routes: HTML --------------------------------------------------------


@app.route("/")
def index() -> Any:
    flights = [_enrich_flight(f) for f in storage.recent_flights(limit=50)]
    return render_template("index.html", flights=flights)


@app.route("/map")
def map_view() -> Any:
    return render_template("map.html")


# --- routes: JSON --------------------------------------------------------


@app.route("/api/flights")
def api_flights() -> Any:
    limit = min(max(1, int(os.environ.get("FLIGHTS_LIMIT", "50"))), 200)
    return jsonify([_enrich_flight(f) for f in storage.recent_flights(limit=limit)])


@app.route("/api/live")
def api_live() -> Any:
    states = storage.all_current_states()
    for s in states:
        s["display_name"] = _name_by_icao.get(s["helicopter_key"], s["helicopter_key"])
    return jsonify(states)


@app.route("/api/tracks")
def api_tracks() -> Any:
    tracks = storage.recent_flights_with_tracks(limit=10)
    for t in tracks:
        t["display_name"] = _name_by_icao.get(t["helicopter_key"], t["helicopter_key"])
        t["takeoff_iso"] = _iso(t.get("takeoff_ts"))
        t["landing_iso"] = _iso(t.get("landing_ts"))
    return jsonify(tracks)


@app.route("/api/sites")
def api_sites() -> Any:
    return jsonify(_load_sites())


@app.route("/api/helicopters")
def api_helicopters() -> Any:
    return jsonify(
        [
            {
                "icao24": h.icao24,
                "registration": h.registration,
                "nickname": h.nickname,
                "base": h.base,
                "operator": h.operator,
                "display_name": h.display_name,
            }
            for h in helicopters
        ]
    )


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info(
        "heli-tracker web avvio. DB=%s host=%s port=%d helicopters=%d",
        DB_PATH,
        WEB_HOST,
        WEB_PORT,
        len(helicopters),
    )
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
