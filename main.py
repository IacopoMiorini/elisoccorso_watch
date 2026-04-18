"""
heli-tracker — monitor di elicotteri di elisoccorso con notifiche Telegram.

Ciclo di funzionamento:
  1. Carica la lista degli elicotteri da `helicopters.yaml`.
  2. Per ogni mezzo senza `icao24`, prova a risolverlo da `registration` via OpenSky.
  3. Poll OpenSky ogni POLL_INTERVAL secondi su /states/all (filtrato per icao24).
  4. Rileva transizioni on_ground: true → false (DECOLLO) e false → true (ATTERRAGGIO).
  5. Invia notifiche Telegram con link a ADS-B Exchange e Google Maps.

Configurazione via variabili d'ambiente (vedi .env.example):
  TELEGRAM_BOT_TOKEN   token del bot (da @BotFather)
  TELEGRAM_CHAT_ID     chat o canale destinatario
  OPENSKY_USERNAME     (opzionale) user OpenSky per rate limit maggiore
  OPENSKY_PASSWORD     (opzionale) password OpenSky
  POLL_INTERVAL        (opzionale, default 45) secondi fra un poll e l'altro
  HELICOPTERS_FILE     (opzionale, default ./helicopters.yaml)
  LOG_LEVEL            (opzionale, default INFO)
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yaml
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Costanti e setup
# ---------------------------------------------------------------------------

load_dotenv()

OPENSKY_BASE = "https://opensky-network.org/api"
TELEGRAM_API = "https://api.telegram.org"

# Soglia di velocità al suolo (m/s) oltre la quale consideriamo il movimento "vero"
# e non solo rumore dei dati ADS-B sul piazzale. 20 nodi ≈ 10 m/s.
TAKEOFF_MIN_VELOCITY_MS = 10.0

# Numero di cicli consecutivi che un mezzo deve risultare "off-network" prima
# di considerarlo effettivamente atterrato/spento (evita falsi atterraggi per
# buchi di copertura ADS-B in valle).
OFFLINE_CYCLES_FOR_LANDED = 4

log = logging.getLogger("heli-tracker")


# ---------------------------------------------------------------------------
# Modello dati
# ---------------------------------------------------------------------------


@dataclass
class Helicopter:
    icao24: str = ""
    registration: str = ""
    nickname: str = ""
    base: str = ""
    operator: str = ""

    # Stato dinamico
    last_on_ground: bool | None = None
    last_seen_ts: float = 0.0
    missing_cycles: int = 0
    in_flight: bool = False
    last_position: tuple[float, float] | None = None  # (lat, lon)
    last_heading: float | None = None
    last_altitude: float | None = None
    last_velocity: float | None = None

    @property
    def display_name(self) -> str:
        return self.nickname or self.registration or self.icao24.upper()


@dataclass
class Config:
    telegram_token: str
    telegram_chat_id: str
    opensky_user: str | None
    opensky_pass: str | None
    poll_interval: int
    helicopters_file: Path
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat:
            raise SystemExit(
                "ERRORE: TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID sono obbligatori."
            )
        return cls(
            telegram_token=token,
            telegram_chat_id=chat,
            opensky_user=os.environ.get("OPENSKY_USERNAME") or None,
            opensky_pass=os.environ.get("OPENSKY_PASSWORD") or None,
            poll_interval=int(os.environ.get("POLL_INTERVAL", "45")),
            helicopters_file=Path(
                os.environ.get("HELICOPTERS_FILE", "helicopters.yaml")
            ),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        )


# ---------------------------------------------------------------------------
# OpenSky client
# ---------------------------------------------------------------------------


class OpenSkyClient:
    def __init__(self, username: str | None, password: str | None):
        self.auth = (username, password) if username and password else None
        self.session = requests.Session()

    def resolve_icao24(self, registration: str) -> str | None:
        """Cerca l'ICAO24 hex dato il registro (es. I-GOOO).

        OpenSky espone /api/metadata/aircraft/registration/{reg}. Se fallisce,
        torna None. L'endpoint è best-effort e il DB non è sempre aggiornato
        per gli elicotteri nuovi."""
        url = f"{OPENSKY_BASE}/metadata/aircraft/registration/{registration}"
        try:
            r = self.session.get(url, timeout=15)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json()
            icao = (data.get("icao24") or "").strip().lower()
            return icao or None
        except requests.RequestException as e:
            log.warning("Lookup registration %s fallito: %s", registration, e)
            return None

    def fetch_states(self, icao24_list: list[str]) -> dict[str, dict[str, Any]]:
        """Interroga /states/all passando la lista di icao24. Ritorna un dict
        {icao24: state_vector_dict} contenente solo i mezzi visti in questo poll."""
        if not icao24_list:
            return {}
        params = [("icao24", i) for i in icao24_list]
        try:
            r = self.session.get(
                f"{OPENSKY_BASE}/states/all",
                params=params,
                auth=self.auth,
                timeout=20,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning("OpenSky /states/all fallito: %s", e)
            return {}

        data = r.json()
        states = data.get("states") or []
        result: dict[str, dict[str, Any]] = {}
        # Schema degli state vector: https://openskynetwork.github.io/opensky-api/rest.html#response
        for s in states:
            icao = (s[0] or "").strip().lower()
            if not icao:
                continue
            result[icao] = {
                "callsign": (s[1] or "").strip(),
                "origin_country": s[2],
                "time_position": s[3],
                "last_contact": s[4],
                "longitude": s[5],
                "latitude": s[6],
                "baro_altitude_m": s[7],
                "on_ground": bool(s[8]),
                "velocity_ms": s[9],
                "heading_deg": s[10],
                "vertical_rate_ms": s[11],
                "geo_altitude_m": s[13],
            }
        return result


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.base = f"{TELEGRAM_API}/bot{token}"
        self.chat_id = chat_id
        self.session = requests.Session()

    def send(self, text: str, disable_preview: bool = False) -> None:
        try:
            r = self.session.post(
                f"{self.base}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": disable_preview,
                },
                timeout=15,
            )
            if r.status_code != 200:
                log.warning("Telegram sendMessage %s: %s", r.status_code, r.text[:200])
        except requests.RequestException as e:
            log.warning("Telegram send errore: %s", e)


# ---------------------------------------------------------------------------
# Formattazione messaggi
# ---------------------------------------------------------------------------


def adsbx_link(icao24: str) -> str:
    return f"https://globe.adsbexchange.com/?icao={icao24}"


def fr24_link(reg: str) -> str:
    return f"https://www.flightradar24.com/data/aircraft/{reg.lower()}"


def maps_link(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={lat:.5f},{lon:.5f}"


def format_takeoff(h: Helicopter, state: dict[str, Any]) -> str:
    lat = state.get("latitude")
    lon = state.get("longitude")
    alt = state.get("geo_altitude_m") or state.get("baro_altitude_m")
    vel = state.get("velocity_ms")
    cs = state.get("callsign") or "—"
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    parts = [
        f"🚁 <b>DECOLLO</b> — {h.display_name}",
        f"Ora: {now}",
        f"Callsign: <code>{cs}</code>",
    ]
    if h.base:
        parts.append(f"Base: {h.base}")
    if lat is not None and lon is not None:
        parts.append(f"Posizione: {lat:.4f}, {lon:.4f}")
        parts.append(f'<a href="{maps_link(lat, lon)}">📍 Mappa</a>')
    if alt is not None:
        parts.append(f"Quota: {int(alt)} m")
    if vel is not None:
        parts.append(f"Velocità: {vel * 1.944:.0f} kt")
    parts.append("")
    parts.append(f'<a href="{adsbx_link(h.icao24)}">🛰 ADS-B Exchange (live)</a>')
    if h.registration:
        parts.append(f'<a href="{fr24_link(h.registration)}">✈️ Flightradar24</a>')
    return "\n".join(parts)


def format_landing(h: Helicopter, state: dict[str, Any] | None) -> str:
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    parts = [f"🛬 <b>ATTERRAGGIO</b> — {h.display_name}", f"Ora: {now}"]
    if state:
        lat = state.get("latitude")
        lon = state.get("longitude")
        if lat is not None and lon is not None:
            parts.append(f"Posizione: {lat:.4f}, {lon:.4f}")
            parts.append(f'<a href="{maps_link(lat, lon)}">📍 Mappa</a>')
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------


def load_helicopters(path: Path) -> list[Helicopter]:
    if not path.exists():
        raise SystemExit(f"File config non trovato: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = data.get("helicopters") or []
    helis: list[Helicopter] = []
    for entry in raw:
        h = Helicopter(
            icao24=(entry.get("icao24") or "").strip().lower(),
            registration=(entry.get("registration") or "").strip().upper(),
            nickname=(entry.get("nickname") or "").strip(),
            base=(entry.get("base") or "").strip(),
            operator=(entry.get("operator") or "").strip(),
        )
        helis.append(h)
    return helis


def resolve_missing_icao24(helis: list[Helicopter], client: OpenSkyClient) -> None:
    """Risolve icao24 mancanti da registration. Non blocca se fallisce: stampa warning."""
    for h in helis:
        if h.icao24 or not h.registration:
            continue
        log.info("Risolvo ICAO24 per %s...", h.registration)
        icao = client.resolve_icao24(h.registration)
        if icao:
            h.icao24 = icao
            log.info("  %s → %s", h.registration, icao)
        else:
            log.warning(
                "  %s: ICAO24 NON risolto. Aggiungilo manualmente in helicopters.yaml.",
                h.registration,
            )


def process_update(
    h: Helicopter, state: dict[str, Any] | None, notifier: TelegramNotifier
) -> None:
    """Aggiorna lo stato di un elicottero e invia notifiche quando serve."""
    if state is None:
        # Mezzo non visto in questo poll
        if h.in_flight:
            h.missing_cycles += 1
            if h.missing_cycles >= OFFLINE_CYCLES_FOR_LANDED:
                log.info(
                    "%s: considerato atterrato (perso %d cicli)",
                    h.display_name,
                    h.missing_cycles,
                )
                notifier.send(format_landing(h, None))
                h.in_flight = False
                h.last_on_ground = True
                h.missing_cycles = 0
        return

    h.missing_cycles = 0
    h.last_seen_ts = time.time()

    on_ground = state["on_ground"]
    vel = state.get("velocity_ms") or 0.0
    lat = state.get("latitude")
    lon = state.get("longitude")
    if lat is not None and lon is not None:
        h.last_position = (lat, lon)
    h.last_heading = state.get("heading_deg")
    h.last_altitude = state.get("geo_altitude_m") or state.get("baro_altitude_m")
    h.last_velocity = vel

    prev_on_ground = h.last_on_ground

    # DECOLLO: transizione on_ground true→false con velocità significativa,
    # oppure primo contatto già in aria.
    is_takeoff = False
    if not on_ground and vel >= TAKEOFF_MIN_VELOCITY_MS:
        if prev_on_ground is True:
            is_takeoff = True
        elif prev_on_ground is None and not h.in_flight:
            # Prima osservazione e già in volo: notifico lo stesso, è un decollo che
            # ci siamo persi o siamo appena partiti.
            is_takeoff = True

    if is_takeoff and not h.in_flight:
        log.info("%s: DECOLLO rilevato", h.display_name)
        notifier.send(format_takeoff(h, state))
        h.in_flight = True

    # ATTERRAGGIO: transizione false→true
    if on_ground and h.in_flight:
        log.info("%s: ATTERRAGGIO rilevato", h.display_name)
        notifier.send(format_landing(h, state))
        h.in_flight = False

    h.last_on_ground = on_ground


def main() -> int:
    cfg = Config.from_env()
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    log.info("heli-tracker avvio. Config: %s", cfg.helicopters_file)

    helis = load_helicopters(cfg.helicopters_file)
    if not helis:
        raise SystemExit("Nessun elicottero configurato in helicopters.yaml")

    client = OpenSkyClient(cfg.opensky_user, cfg.opensky_pass)
    notifier = TelegramNotifier(cfg.telegram_token, cfg.telegram_chat_id)

    resolve_missing_icao24(helis, client)

    tracked = [h for h in helis if h.icao24]
    if not tracked:
        raise SystemExit(
            "Nessun elicottero ha un ICAO24 valido. Aggiungili a helicopters.yaml."
        )

    log.info(
        "Traccio %d mezzi: %s",
        len(tracked),
        ", ".join(h.display_name for h in tracked),
    )

    notifier.send(
        f"✅ heli-tracker avviato — monitoro {len(tracked)} mezzi.\n"
        + "\n".join(f"• {h.display_name} ({h.icao24})" for h in tracked)
    )

    # Signal handling per uscire pulitamente
    stop = {"flag": False}

    def handle_sig(signum: int, _frame: Any) -> None:
        log.info("Ricevuto segnale %d, esco...", signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    icao_list = [h.icao24 for h in tracked]
    by_icao = {h.icao24: h for h in tracked}

    while not stop["flag"]:
        start = time.time()
        states = client.fetch_states(icao_list)
        for icao, h in by_icao.items():
            process_update(h, states.get(icao), notifier)

        elapsed = time.time() - start
        sleep_for = max(1.0, cfg.poll_interval - elapsed)
        # Sleep a passi brevi per reagire ai segnali
        while sleep_for > 0 and not stop["flag"]:
            step = min(2.0, sleep_for)
            time.sleep(step)
            sleep_for -= step

    log.info("Bye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
