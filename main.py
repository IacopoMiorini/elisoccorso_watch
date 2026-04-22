"""heli-tracker — orchestratore: poll OpenSky, dispatch, spawn bot Telegram.

Il worker gira due thread:

1. main thread      — poll loop OpenSky, chiama process_update per ogni mezzo,
                      invia notifiche via TelegramNotifier.broadcast (canale +
                      subscriber per-mezzo dal DB SQLite).
2. telegram-poller  — long-polling su getUpdates, dispaccia i comandi del bot
                      (/subscribe, /list, /all, /none, /stop, /mock) alla
                      CommandHandler.

Configurazione via env (vedi README). I segreti obbligatori sono
TELEGRAM_BOT_TOKEN; TELEGRAM_CHAT_ID è opzionale (se presente, il canale riceve
tutti gli eventi in aggiunta ai subscriber del bot).
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from detector import (
    AdsbClient,
    load_helicopters,
    process_update,
    resolve_missing_icao24,
)
from storage import Storage
from telegram_bot import (
    CommandHandler,
    TelegramClient,
    TelegramNotifier,
    TelegramPoller,
)

load_dotenv()

log = logging.getLogger("heli-tracker")


@dataclass
class Config:
    telegram_token: str
    telegram_chat_id: str | None  # canale (opzionale)
    admin_chat_id: int | None     # user id dell'admin (per /mock)
    opensky_user: str | None
    opensky_pass: str | None
    poll_interval: int
    helicopters_file: Path
    db_path: Path
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit("ERRORE: TELEGRAM_BOT_TOKEN è obbligatorio.")
        channel = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or None
        admin_raw = os.environ.get("TELEGRAM_ADMIN_USER_ID", "").strip()
        admin_id = int(admin_raw) if admin_raw else None
        return cls(
            telegram_token=token,
            telegram_chat_id=channel,
            admin_chat_id=admin_id,
            opensky_user=os.environ.get("OPENSKY_USERNAME") or None,
            opensky_pass=os.environ.get("OPENSKY_PASSWORD") or None,
            poll_interval=int(os.environ.get("POLL_INTERVAL", "45")),
            helicopters_file=Path(
                os.environ.get("HELICOPTERS_FILE", "helicopters.yaml")
            ),
            db_path=Path(os.environ.get("DB_PATH", "heli_tracker.db")),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        )


def main() -> int:
    cfg = Config.from_env()
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("heli-tracker avvio. DB=%s config=%s", cfg.db_path, cfg.helicopters_file)

    helis = load_helicopters(cfg.helicopters_file)
    if not helis:
        raise SystemExit("Nessun elicottero configurato in helicopters.yaml")

    storage = Storage(cfg.db_path)

    adsb = AdsbClient(cfg.opensky_user, cfg.opensky_pass, enable_fallback=True)
    resolve_missing_icao24(helis, adsb)

    tracked = [h for h in helis if h.icao24]
    if not tracked:
        raise SystemExit(
            "Nessun elicottero ha un ICAO24 valido. Aggiungili a helicopters.yaml."
        )

    # Pulisce subscriptions orfane verso mezzi non più in config
    valid_keys = {h.icao24 for h in tracked}
    n_orphans = storage.cleanup_orphans(valid_keys)
    if n_orphans:
        log.info("Rimosse %d subscription orfane (mezzi non più in config).", n_orphans)

    log.info(
        "Traccio %d mezzi (%d subscriber nel DB): %s",
        len(tracked),
        storage.count_subscribers(),
        ", ".join(h.display_name for h in tracked),
    )

    tg_client = TelegramClient(cfg.telegram_token)
    tg_client.set_my_commands(
        [
            {"command": "start", "description": "Attiva il bot"},
            {"command": "subscribe", "description": "Scegli quali mezzi seguire"},
            {"command": "list", "description": "Mostra le tue iscrizioni"},
            {"command": "all", "description": "Iscriviti a tutti i mezzi"},
            {"command": "none", "description": "Rimuovi tutte le iscrizioni"},
            {"command": "stats", "description": "Statistiche voli (default 7 giorni)"},
            {"command": "last", "description": "Ultimo volo per ogni mezzo"},
            {"command": "zone_add", "description": "Aggiungi zona (nome lat lon raggio_km)"},
            {"command": "zone_list", "description": "Elenca le tue zone"},
            {"command": "zone_del", "description": "Rimuovi una zona per nome"},
            {"command": "stop", "description": "Cancellati dal bot"},
            {"command": "help", "description": "Guida ai comandi"},
        ]
    )
    notifier = TelegramNotifier(tg_client, cfg.telegram_chat_id, storage)
    handler = CommandHandler(notifier, storage, tracked, cfg.admin_chat_id)

    stop: dict[str, bool] = {"flag": False}

    def handle_sig(signum: int, _frame: Any) -> None:
        log.info("Ricevuto segnale %d, esco...", signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    poller = TelegramPoller(tg_client, storage, handler, stop)
    poller.start()

    # Welcome al canale (solo canale: i subscriber del bot lo ricevono al /start).
    if cfg.telegram_chat_id:
        tg_client.send_message(
            cfg.telegram_chat_id,
            f"✅ heli-tracker avviato — monitoro {len(tracked)} mezzi.\n"
            + "\n".join(f"• {h.display_name} ({h.icao24})" for h in tracked),
        )

    icao_list = [h.icao24 for h in tracked]
    by_icao = {h.icao24: h for h in tracked}

    # Alert sui fallimenti consecutivi di OpenSky: prima notifica dopo 10 cicli,
    # poi ogni 50 per evitare flood. Il fallback adsb.lol copre intanto i dati.
    ALERT_FIRST = 10
    ALERT_EVERY = 50

    while not stop["flag"]:
        start = time.time()
        states = adsb.fetch_states(icao_list)

        fails = adsb.consecutive_opensky_failures
        if cfg.admin_chat_id is not None and fails >= ALERT_FIRST:
            should_alert = (
                fails == ALERT_FIRST
                or (fails - ALERT_FIRST) % ALERT_EVERY == 0
            )
            if should_alert and fails != adsb.last_failure_alerted:
                adsb.last_failure_alerted = fails
                mins = int(fails * cfg.poll_interval / 60)
                notifier.send_direct(
                    cfg.admin_chat_id,
                    f"⚠️ OpenSky unresponsive: {fails} poll falliti "
                    f"(~{mins} min). Fallback adsb.lol in uso.",
                )

        for icao, h in by_icao.items():
            process_update(h, states.get(icao), notifier, storage)

        elapsed = time.time() - start
        sleep_for = max(1.0, cfg.poll_interval - elapsed)
        while sleep_for > 0 and not stop["flag"]:
            step = min(2.0, sleep_for)
            time.sleep(step)
            sleep_for -= step

    poller.join(timeout=5)
    storage.close()
    log.info("Bye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
