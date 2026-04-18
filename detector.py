"""Detector di decolli/atterraggi e formattazione messaggi.

Contiene la logica pura della pipeline: modelli dati, client OpenSky, helper
geografici, formattazione dei messaggi Telegram e funzione `process_update` che
consuma uno state vector e decide cosa notificare.

Il detector dipende da un `Notifier` con il metodo `broadcast(text, heli_key)`
(definito in telegram_bot.py) ma non importa direttamente quel modulo.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import requests
import yaml

log = logging.getLogger("heli-tracker.detector")

OPENSKY_BASE = "https://opensky-network.org/api"

# Soglia di velocità al suolo (m/s) oltre la quale consideriamo il movimento "vero"
# e non rumore dei dati ADS-B sul piazzale. ~10 m/s ≈ 20 nodi.
TAKEOFF_MIN_VELOCITY_MS = 10.0

# Numero di poll consecutivi senza contatto prima di dichiarare l'atterraggio.
OFFLINE_CYCLES_FOR_LANDED = 4

# Raggio di default in metri per il match di un sito d'atterraggio se il YAML
# non specifica `radius_m` sulla voce.
DEFAULT_SITE_RADIUS_M = 500.0


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

    # Statistiche cumulative del volo corrente — reset ad ogni decollo
    flight_start_ts: float | None = None
    flight_start_position: tuple[float, float] | None = None
    flight_max_altitude_m: float | None = None
    flight_max_velocity_ms: float | None = None
    flight_distance_km: float = 0.0

    @property
    def display_name(self) -> str:
        return self.nickname or self.registration or self.icao24.upper()


@dataclass
class LandingSite:
    name: str
    city: str = ""
    lat: float = 0.0
    lon: float = 0.0
    radius_m: float = DEFAULT_SITE_RADIUS_M

    @property
    def label(self) -> str:
        return f"{self.name} ({self.city})" if self.city else self.name


class Notifier(Protocol):
    def broadcast(self, text: str, helicopter_key: str) -> None: ...


# ---------------------------------------------------------------------------
# OpenSky client
# ---------------------------------------------------------------------------


class OpenSkyClient:
    def __init__(self, username: str | None, password: str | None):
        self.auth = (username, password) if username and password else None
        self.session = requests.Session()

    def resolve_icao24(self, registration: str) -> str | None:
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
# Geo helpers
# ---------------------------------------------------------------------------


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distanza ortodromica in km fra due punti lat/lon."""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def find_landing_site(
    lat: float, lon: float, sites: list[LandingSite]
) -> LandingSite | None:
    best: tuple[float, LandingSite] | None = None
    for s in sites:
        dist_m = haversine_km(lat, lon, s.lat, s.lon) * 1000.0
        if dist_m <= s.radius_m and (best is None or dist_m < best[0]):
            best = (dist_m, s)
    return best[1] if best else None


def format_duration(seconds: float) -> str:
    secs = max(0, int(seconds))
    if secs < 60:
        return "< 1 min"
    minutes = secs // 60
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m"


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


def format_landing(
    h: Helicopter,
    state: dict[str, Any] | None,
    sites: list[LandingSite],
) -> str:
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    parts = [f"🛬 <b>ATTERRAGGIO</b> — {h.display_name}", f"Ora: {now}"]

    if h.flight_start_ts is not None:
        duration = time.time() - h.flight_start_ts
        parts.append(f"Durata: {format_duration(duration)}")
    if h.flight_distance_km > 0:
        parts.append(f"Distanza: {h.flight_distance_km:.0f} km")
    if h.flight_max_altitude_m is not None:
        parts.append(f"Quota max: {int(h.flight_max_altitude_m)} m")
    if h.flight_max_velocity_ms is not None:
        parts.append(f"Velocità max: {h.flight_max_velocity_ms * 1.944:.0f} kt")

    lat: float | None = None
    lon: float | None = None
    if state is not None:
        lat = state.get("latitude")
        lon = state.get("longitude")
    if (lat is None or lon is None) and h.last_position is not None:
        lat, lon = h.last_position

    if lat is not None and lon is not None:
        site = find_landing_site(lat, lon, sites)
        if site is not None:
            parts.append(f"Sito: <b>{site.label}</b>")
            parts.append(f'<a href="{maps_link(site.lat, site.lon)}">📍 Mappa</a>')
        else:
            parts.append(f"Posizione: {lat:.4f}, {lon:.4f}")
            parts.append(f'<a href="{maps_link(lat, lon)}">📍 Mappa</a>')

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Caricamento config YAML
# ---------------------------------------------------------------------------


def load_helicopters(path: Path) -> list[Helicopter]:
    if not path.exists():
        raise SystemExit(f"File config non trovato: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = data.get("helicopters") or []
    helis: list[Helicopter] = []
    for entry in raw:
        helis.append(
            Helicopter(
                icao24=(entry.get("icao24") or "").strip().lower(),
                registration=(entry.get("registration") or "").strip().upper(),
                nickname=(entry.get("nickname") or "").strip(),
                base=(entry.get("base") or "").strip(),
                operator=(entry.get("operator") or "").strip(),
            )
        )
    return helis


def load_landing_sites(path: Path) -> list[LandingSite]:
    if not path.exists():
        log.warning("File landing sites non trovato (%s): lookup disabilitato.", path)
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = data.get("sites") or []
    sites: list[LandingSite] = []
    for entry in raw:
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        try:
            sites.append(
                LandingSite(
                    name=name,
                    city=(entry.get("city") or "").strip(),
                    lat=float(entry["lat"]),
                    lon=float(entry["lon"]),
                    radius_m=float(entry.get("radius_m", DEFAULT_SITE_RADIUS_M)),
                )
            )
        except (KeyError, TypeError, ValueError) as e:
            log.warning("Voce landing site scartata (%s): %s", name, e)
    return sites


def resolve_missing_icao24(
    helis: list[Helicopter], client: OpenSkyClient
) -> None:
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


# ---------------------------------------------------------------------------
# Core: process_update
# ---------------------------------------------------------------------------


def process_update(
    h: Helicopter,
    state: dict[str, Any] | None,
    notifier: Notifier,
    sites: list[LandingSite],
) -> None:
    """Aggiorna lo stato di un elicottero e invia notifiche quando serve."""
    if state is None:
        if h.in_flight:
            h.missing_cycles += 1
            if h.missing_cycles >= OFFLINE_CYCLES_FOR_LANDED:
                log.info(
                    "%s: considerato atterrato (perso %d cicli)",
                    h.display_name,
                    h.missing_cycles,
                )
                notifier.broadcast(format_landing(h, None, sites), h.icao24)
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
    alt = state.get("geo_altitude_m") or state.get("baro_altitude_m")

    # Cattura la posizione precedente PRIMA della sovrascrittura per misurare i
    # segmenti di distanza.
    prev_position = h.last_position

    if lat is not None and lon is not None:
        h.last_position = (lat, lon)
    h.last_heading = state.get("heading_deg")
    h.last_altitude = alt
    h.last_velocity = vel

    prev_on_ground = h.last_on_ground

    is_takeoff = False
    if not on_ground and vel >= TAKEOFF_MIN_VELOCITY_MS:
        if prev_on_ground is True:
            is_takeoff = True
        elif prev_on_ground is None and not h.in_flight:
            # Prima osservazione e già in volo: notifico come decollo "tardivo".
            is_takeoff = True

    if is_takeoff and not h.in_flight:
        log.info("%s: DECOLLO rilevato", h.display_name)
        h.flight_start_ts = time.time()
        h.flight_start_position = (
            (lat, lon) if lat is not None and lon is not None else None
        )
        h.flight_max_altitude_m = alt
        h.flight_max_velocity_ms = vel
        h.flight_distance_km = 0.0
        notifier.broadcast(format_takeoff(h, state), h.icao24)
        h.in_flight = True
    elif h.in_flight and not on_ground:
        if alt is not None:
            h.flight_max_altitude_m = max(h.flight_max_altitude_m or alt, alt)
        if vel is not None:
            h.flight_max_velocity_ms = max(h.flight_max_velocity_ms or vel, vel)
        if prev_position is not None and lat is not None and lon is not None:
            segment = haversine_km(prev_position[0], prev_position[1], lat, lon)
            # Filtra outlier: >500 km in un singolo poll è un errore di geoloc.
            if segment < 500:
                h.flight_distance_km += segment

    if on_ground and h.in_flight:
        log.info("%s: ATTERRAGGIO rilevato", h.display_name)
        notifier.broadcast(format_landing(h, state, sites), h.icao24)
        h.in_flight = False

    h.last_on_ground = on_ground


# ---------------------------------------------------------------------------
# Simulazione (usata da scripts/mock_test.py e dal comando admin /mock)
# ---------------------------------------------------------------------------


def simulate_flight(
    helicopters: list[Helicopter],
    sites: list[LandingSite],
    send: Callable[[str], None],
) -> None:
    """Simula un decollo+atterraggio del primo elicottero della lista.

    `send` è una funzione (text) -> None che invia il messaggio formattato:
    passarla consente al chiamante di decidere la destinazione (canale,
    admin, chat singola) senza attraversare il broadcast.
    """
    from copy import deepcopy

    if not helicopters:
        raise RuntimeError("Nessun elicottero configurato")

    target = deepcopy(helicopters[0])
    target.nickname = (
        f"[TEST] {target.nickname or target.registration or target.icao24}"
    )

    now = time.time()
    state_flying = {
        "callsign": "MOCKFLY",
        "origin_country": "Italy",
        "time_position": now,
        "last_contact": now,
        "longitude": 13.4721,
        "latitude": 45.8275,
        "baro_altitude_m": 350.0,
        "on_ground": False,
        "velocity_ms": 45.0,
        "heading_deg": 270.0,
        "vertical_rate_ms": 5.0,
        "geo_altitude_m": 360.0,
    }

    # Popola manualmente le statistiche come se il volo durasse 18 min.
    target.last_position = (state_flying["latitude"], state_flying["longitude"])
    target.flight_start_ts = now - 18 * 60
    target.flight_start_position = target.last_position
    target.flight_max_altitude_m = 1820.0
    target.flight_max_velocity_ms = 68.0
    target.flight_distance_km = 37.0

    send(format_takeoff(target, state_flying))

    # Atterraggio esattamente su Cattinara (Trieste), deve matchare il sito.
    state_grounded = {
        **state_flying,
        "on_ground": True,
        "velocity_ms": 0.0,
        "baro_altitude_m": 80.0,
        "geo_altitude_m": 85.0,
        "latitude": 45.6285,
        "longitude": 13.7960,
    }
    target.last_position = (state_grounded["latitude"], state_grounded["longitude"])

    send(format_landing(target, state_grounded, sites))
