"""Mock test: simula un decollo+atterraggio senza toccare OpenSky.

Uso (dalla root del repo):
    TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... .venv/bin/python scripts/mock_test.py

Invia le due notifiche al `TELEGRAM_CHAT_ID` configurato (tipicamente il
canale). Non tocca lo storage né i subscriber del bot — è solo uno smoke test
del formato dei messaggi. Per testare la broadcast completa (canale +
subscriber) serve un decollo vero o il comando /mock del bot (che invia solo
all'admin).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from detector import load_helicopters, load_landing_sites, simulate_flight  # noqa: E402
from main import Config  # noqa: E402
from telegram_bot import TelegramClient  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = Config.from_env()
    if not cfg.telegram_chat_id:
        raise SystemExit("TELEGRAM_CHAT_ID non settato: non saprei a chi mandare.")

    helis = [h for h in load_helicopters(cfg.helicopters_file) if h.icao24]
    sites = load_landing_sites(cfg.landing_sites_file)

    client = TelegramClient(cfg.telegram_token)

    def send(text: str) -> None:
        client.send_message(cfg.telegram_chat_id, text)

    print(f"[MOCK] target chat_id = {cfg.telegram_chat_id}")
    print(f"[MOCK] landing sites caricati: {len(sites)}")
    simulate_flight(helis, sites, send)
    print("[MOCK] fatto. Controlla Telegram.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
