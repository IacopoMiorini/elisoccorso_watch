"""Mock test: simula un decollo e un atterraggio senza toccare OpenSky.

Uso (dalla root del repo):
    TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... .venv/bin/python scripts/mock_test.py

Prende il primo elicottero con ICAO24 valido da helicopters.yaml, gli
mette un prefisso [TEST] nel nickname per distinguerlo dai decolli veri,
e chiama process_update con due state vector fittizi: uno "in volo" e
uno "a terra". Il worker in prod non viene toccato — questo script gira
solo in locale contro l'API Telegram.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import (  # noqa: E402
    Config,
    TelegramNotifier,
    load_helicopters,
    process_update,
)


def main() -> int:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = Config.from_env()
    helis = load_helicopters(cfg.helicopters_file)
    target = next((h for h in helis if h.icao24), None)
    if target is None:
        raise SystemExit("Nessun elicottero con ICAO24 in helicopters.yaml")

    target.nickname = f"[TEST] {target.nickname or target.registration or target.icao24}"

    notifier = TelegramNotifier(cfg.telegram_token, cfg.telegram_chat_id)

    print(f"[MOCK] target: {target.display_name} ({target.icao24})")

    now = time.time()
    state_flying = {
        "callsign": "MOCKFLY",
        "origin_country": "Italy",
        "time_position": now,
        "last_contact": now,
        "longitude": 13.4821,
        "latitude": 45.8275,
        "baro_altitude_m": 350.0,
        "on_ground": False,
        "velocity_ms": 45.0,
        "heading_deg": 270.0,
        "vertical_rate_ms": 5.0,
        "geo_altitude_m": 360.0,
    }

    print("[MOCK] invio stato 'in volo' → attesa notifica DECOLLO")
    process_update(target, state_flying, notifier)

    time.sleep(2)

    state_grounded = {
        **state_flying,
        "on_ground": True,
        "velocity_ms": 0.0,
        "vertical_rate_ms": 0.0,
        "baro_altitude_m": 80.0,
        "geo_altitude_m": 85.0,
        "latitude": 46.0320,
        "longitude": 13.1870,
    }

    print("[MOCK] invio stato 'a terra' → attesa notifica ATTERRAGGIO")
    process_update(target, state_grounded, notifier)

    print("[MOCK] fatto. Controlla Telegram.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
