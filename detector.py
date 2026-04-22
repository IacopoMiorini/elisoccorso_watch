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
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import requests
import yaml

log = logging.getLogger("heli-tracker.detector")

OPENSKY_BASE = "https://opensky-network.org/api"
ADSBLOL_BASE = "https://api.adsb.lol/v2"
AIRPLANESLIVE_BASE = "https://api.airplanes.live/v2"
ADSBONE_BASE = "https://api.adsb.one/v2"

# Conversioni unità adsb.lol → internal (OpenSky-compatible)
# adsb.lol riporta altitudini in feet e velocità al suolo (gs) in knots.
FT_TO_M = 0.3048
KT_TO_MS = 0.514444

# Numero di osservazioni consecutive con on_ground=False richieste per dichiarare
# decollo quando non abbiamo mai visto il mezzo a terra (o il primo sample post-
# liftoff cattura hover/climb a bassa ground-speed). Filtra flicker del bit
# Mode-S sul piazzale senza perdere i decolli di elicotteri in hover prolungato.
# Con POLL_INTERVAL=45s sono ~90s: ritardo accettabile, zero falsi negativi.
AIRBORNE_CYCLES_FOR_TAKEOFF = 2

# Numero di poll consecutivi senza contatto prima di dichiarare un "landing inferito"
# (segnale perso). Con POLL_INTERVAL=45s sono ~6 minuti — abbastanza per tollerare
# buchi di copertura in valle senza dichiarare falsi atterraggi in hover/cruise.
OFFLINE_CYCLES_FOR_LANDED = 8

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
    airborne_pending_cycles: int = 0
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
    flight_track: list[tuple[float, float]] = field(default_factory=list)
    flight_callsign: str | None = None

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
    def broadcast_event(
        self,
        text: str,
        helicopter_key: str,
        position: tuple[float, float] | None = None,
        photo_path: Path | None = None,
    ) -> None: ...

    def send_direct(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any: ...


# ---------------------------------------------------------------------------
# OpenSky client
# ---------------------------------------------------------------------------


class OpenSkyClient:
    """Client OpenSky. fetch_states solleva HTTPError sui fallimenti HTTP
    (incluso 429), non su risposte vuote (che sono valide: semplicemente nessun
    mezzo visto). Rispetta l'header `Retry-After` sul 429 mettendosi in
    back-off fino allo scadere del tempo indicato, così non prolunga il
    rate-limit su OpenSky battendogli addosso."""

    def __init__(self, username: str | None, password: str | None):
        self.auth = (username, password) if username and password else None
        self.session = requests.Session()
        self.back_off_until = 0.0

    @property
    def is_backing_off(self) -> bool:
        return time.time() < self.back_off_until

    @staticmethod
    def _parse_retry_after(response: requests.Response) -> int:
        """Ritorna secondi di back-off da rispettare. Clamp fra 5s e 24h.

        OpenSky usa un header custom `X-Rate-Limit-Retry-After-Seconds` che
        contiene il tempo residuo fino al reset del quota giornaliero (può
        essere anche ~11h). Proviamo quello per primo, poi il `Retry-After`
        standard; fallback 60s se entrambi assenti o malformati.
        """
        candidates = [
            response.headers.get("X-Rate-Limit-Retry-After-Seconds"),
            response.headers.get("Retry-After"),
        ]
        for h in candidates:
            if not h:
                continue
            try:
                return max(5, min(86400, int(h)))
            except ValueError:
                continue
        return 60

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
        r = self.session.get(
            f"{OPENSKY_BASE}/states/all",
            params=params,
            auth=self.auth,
            timeout=20,
        )
        if r.status_code == 429:
            retry = self._parse_retry_after(r)
            self.back_off_until = time.time() + retry
            log.warning(
                "OpenSky 429: back-off per %ds (fino a %s)",
                retry,
                datetime.fromtimestamp(self.back_off_until, tz=timezone.utc)
                .strftime("%H:%M:%S UTC"),
            )
        r.raise_for_status()
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
                "source": "opensky",
            }
        return result


class ReadsbCompatClient:
    """Client generico per API readsb-compatible (adsb.lol, airplanes.live, ...).

    Entrambe espongono `/v2/hex/<hex>[,<hex>...]` con lo stesso JSON schema.
    Passa `base_url` e un tag `source` per distinguere nei log e nei record.
    """

    def __init__(self, base_url: str, source: str):
        self.base_url = base_url
        self.source = source
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "heli-tracker-bot/1.0"})

    def fetch_states(self, icao24_list: list[str]) -> dict[str, dict[str, Any]]:
        if not icao24_list:
            return {}
        joined = ",".join(icao24_list)
        r = self.session.get(f"{self.base_url}/hex/{joined}", timeout=15)
        r.raise_for_status()
        data = r.json()
        aircraft = data.get("ac") or []
        result: dict[str, dict[str, Any]] = {}
        for a in aircraft:
            icao = (a.get("hex") or "").strip().lower()
            if not icao:
                continue
            alt_baro = a.get("alt_baro")
            alt_geom = a.get("alt_geom")
            gs_kt = a.get("gs")
            # readsb riporta `alt_baro` come "ground" (string) quando a terra.
            on_ground = isinstance(alt_baro, str) and alt_baro.lower() == "ground"
            baro_m = (
                alt_baro * FT_TO_M if isinstance(alt_baro, (int, float)) else None
            )
            geo_m = alt_geom * FT_TO_M if isinstance(alt_geom, (int, float)) else None
            vel_ms = gs_kt * KT_TO_MS if isinstance(gs_kt, (int, float)) else None
            result[icao] = {
                "callsign": (a.get("flight") or "").strip(),
                "origin_country": None,
                "time_position": a.get("seen_pos"),
                "last_contact": a.get("seen"),
                "longitude": a.get("lon"),
                "latitude": a.get("lat"),
                "baro_altitude_m": baro_m,
                "on_ground": on_ground,
                "velocity_ms": vel_ms,
                "heading_deg": a.get("track"),
                "vertical_rate_ms": (
                    a.get("baro_rate") * FT_TO_M / 60.0
                    if isinstance(a.get("baro_rate"), (int, float))
                    else None
                ),
                "geo_altitude_m": geo_m,
                "source": self.source,
            }
        return result


class AdsbLolClient(ReadsbCompatClient):
    def __init__(self):
        super().__init__(ADSBLOL_BASE, "adsblol")


class AirplanesLiveClient(ReadsbCompatClient):
    def __init__(self):
        super().__init__(AIRPLANESLIVE_BASE, "airplaneslive")


class AdsbOneClient(ReadsbCompatClient):
    def __init__(self):
        super().__init__(ADSBONE_BASE, "adsbone")


class AdsbClient:
    """Wrapper con cascading fallback: OpenSky primary, adsb.lol + airplanes.live
    come fallback paralleli per gli ICAO non visti dalla primary.

    Traccia i fallimenti consecutivi della primary (OpenSky) per permettere al
    chiamante di allertare dopo N cicli falliti.
    """

    def __init__(
        self,
        opensky_user: str | None,
        opensky_pass: str | None,
        enable_fallback: bool = True,
    ):
        self.primary = OpenSkyClient(opensky_user, opensky_pass)
        self.fallbacks: list[ReadsbCompatClient] = (
            [AdsbLolClient(), AirplanesLiveClient(), AdsbOneClient()]
            if enable_fallback
            else []
        )
        self.consecutive_opensky_failures = 0
        self.last_failure_alerted = 0  # quante failure avevamo all'ultima notifica

    def resolve_icao24(self, registration: str) -> str | None:
        return self.primary.resolve_icao24(registration)

    def fetch_states(self, icao24_list: list[str]) -> dict[str, dict[str, Any]]:
        if self.primary.is_backing_off:
            # Non chiamiamo OpenSky finché siamo nel back-off richiesto dal
            # server. I fallback coprono intanto.
            result: dict[str, dict[str, Any]] = {}
        else:
            try:
                result = self.primary.fetch_states(icao24_list)
                self.consecutive_opensky_failures = 0
                self.last_failure_alerted = 0
            except requests.RequestException as e:
                self.consecutive_opensky_failures += 1
                log.warning(
                    "OpenSky fetch_states fallito (#%d): %s",
                    self.consecutive_opensky_failures,
                    e,
                )
                result = {}

        # Proviamo tutti i fallback in ordine per gli ICAO ancora mancanti.
        # Ogni provider ha rete feeder diversa → unione massimizza copertura.
        for fb in self.fallbacks:
            missing = [i for i in icao24_list if i not in result]
            if not missing:
                break
            try:
                got = fb.fetch_states(missing)
                if got:
                    log.info(
                        "%s ha recuperato %d mezzi: %s",
                        fb.source,
                        len(got),
                        list(got.keys()),
                    )
                result.update(got)
            except requests.RequestException as e:
                log.warning("%s fallback fallito: %s", fb.source, e)
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
    """Link alla mappa live di FR24 centrata sull'aircraft.
    Formato: `/<REG_NO_DASH>`: FR24 accetta la registrazione senza trattino e
    reindirizza alla live view del volo in corso se attivo."""
    clean = reg.replace("-", "").upper()
    return f"https://www.flightradar24.com/{clean}"


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


def format_landing_inferred(
    h: Helicopter,
    sites: list[LandingSite],
) -> str:
    """Messaggio di 'landing inferito' quando il mezzo sparisce dall'ADS-B.

    Usiamo un wording diverso da un landing confermato: in montagna un elicottero
    può sparire dal network per parecchi minuti pur continuando a volare. Il
    messaggio è onesto su questa incertezza e mostra l'ultimo punto noto, non
    una posizione di atterraggio certa."""
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    minutes_since_contact = (
        int((time.time() - h.last_seen_ts) / 60) if h.last_seen_ts else None
    )

    parts = [f"🛬 <b>ATTERRAGGIO (segnale perso)</b> — {h.display_name}"]
    parts.append(f"Ora: {now}")
    if minutes_since_contact is not None:
        parts.append(f"Ultimo contatto ADS-B: {minutes_since_contact} min fa")
    parts.append(
        "<i>Probabilmente atterrato, ma il mezzo è uscito dalla copertura ADS-B; "
        "potrebbe anche essere solo un buco di segnale in valle.</i>"
    )

    if h.flight_start_ts is not None:
        # La durata va calcolata fino all'ultimo contatto utile, non al "now"
        end_ts = h.last_seen_ts if h.last_seen_ts else time.time()
        duration = end_ts - h.flight_start_ts
        parts.append(f"Durata (fino all'ultimo contatto): {format_duration(duration)}")
    if h.flight_distance_km > 0:
        parts.append(f"Distanza tracciata: {h.flight_distance_km:.0f} km")
    if h.flight_max_altitude_m is not None:
        parts.append(f"Quota max: {int(h.flight_max_altitude_m)} m")

    if h.last_position is not None:
        lat, lon = h.last_position
        site = find_landing_site(lat, lon, sites)
        if site is not None:
            parts.append(f"Ultimo punto noto: <b>{site.label}</b>")
            parts.append(f'<a href="{maps_link(site.lat, site.lon)}">📍 Mappa</a>')
        else:
            parts.append(f"Ultimo punto noto: {lat:.4f}, {lon:.4f}")
            parts.append(f'<a href="{maps_link(lat, lon)}">📍 Mappa</a>')

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
    helis: list[Helicopter], client: "AdsbClient | OpenSkyClient"
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
# Rendering traccia di volo (PNG)
# ---------------------------------------------------------------------------


def render_track_png(
    waypoints: list[tuple[float, float]],
    width: int = 800,
    height: int = 500,
) -> Path | None:
    """Genera un PNG temporaneo con la rotta del volo sovrapposta a OSM.

    Ritorna il Path del file o None se il rendering non è possibile (pochi
    waypoint o libreria non installata o errore di download tiles).
    Il file è temporaneo: il chiamante deve cancellarlo dopo l'invio.
    """
    if len(waypoints) < 2:
        return None
    try:
        from staticmap import CircleMarker, Line, StaticMap
    except ImportError:
        log.info("staticmap non installato: skip rendering traccia.")
        return None

    try:
        m = StaticMap(width, height, padding_x=40, padding_y=40)
        # staticmap vuole (lon, lat)
        coords = [(lon, lat) for lat, lon in waypoints]
        m.add_line(Line(coords, "#d62828", 3))
        m.add_marker(CircleMarker(coords[0], "#2a9d8f", 12))   # verde = decollo
        m.add_marker(CircleMarker(coords[-1], "#d62828", 12))  # rosso = atterraggio
        img = m.render()
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.close()
        out = Path(tmp.name)
        img.save(out)
        return out
    except Exception as e:
        log.warning("Rendering traccia fallito: %s", e)
        return None


# ---------------------------------------------------------------------------
# Core: process_update
# ---------------------------------------------------------------------------


def _landed_position(
    h: Helicopter, state: dict[str, Any] | None
) -> tuple[float, float] | None:
    if state is not None:
        lat = state.get("latitude")
        lon = state.get("longitude")
        if lat is not None and lon is not None:
            return (lat, lon)
    return h.last_position


def _emit_landing(
    h: Helicopter,
    state: dict[str, Any] | None,
    sites: list[LandingSite],
    notifier: Notifier,
    storage: Any | None,
    inferred: bool = False,
) -> None:
    """Formatta landing message, renderizza traccia, broadcasta, registra nel DB.

    `inferred=True` significa che il mezzo è sparito dalla rete ADS-B per N
    cicli: non abbiamo certezza dell'atterraggio, quindi usiamo un messaggio
    diverso e registriamo il volo col flag corrispondente.
    """
    if inferred:
        text = format_landing_inferred(h, sites)
        pos = h.last_position
        # Al landing inferito l'ultimo punto noto può essere a bassa quota su un
        # punto qualsiasi del percorso — la traccia PNG ha comunque valore ma
        # va chiaro che NON include il touchdown.
        landing_ts = int(h.last_seen_ts) if h.last_seen_ts else int(time.time())
    else:
        text = format_landing(h, state, sites)
        pos = _landed_position(h, state)
        landing_ts = int(time.time())

    png_path = render_track_png(h.flight_track) if h.flight_track else None
    notifier.broadcast_event(
        text=text, helicopter_key=h.icao24, position=pos, photo_path=png_path
    )
    if png_path is not None:
        try:
            png_path.unlink(missing_ok=True)
        except OSError:
            pass

    if storage is not None and h.flight_start_ts is not None:
        site = find_landing_site(*pos, sites) if pos else None
        try:
            flight_id = storage.record_flight(
                helicopter_key=h.icao24,
                takeoff_ts=int(h.flight_start_ts),
                landing_ts=landing_ts,
                distance_km=h.flight_distance_km or None,
                max_altitude_m=h.flight_max_altitude_m,
                max_velocity_ms=h.flight_max_velocity_ms,
                takeoff_position=h.flight_start_position,
                landing_position=pos,
                landing_site=site.name if site else None,
                callsign=h.flight_callsign,
                inferred=inferred,
            )
            # Persistiamo la traccia di volo per la dashboard (Leaflet polyline)
            if flight_id and h.flight_track:
                storage.save_flight_track(flight_id, list(h.flight_track))
        except Exception:
            log.exception("Errore salvando il volo di %s", h.display_name)


def process_update(
    h: Helicopter,
    state: dict[str, Any] | None,
    notifier: Notifier,
    sites: list[LandingSite],
    storage: Any | None = None,
) -> None:
    """Aggiorna lo stato di un elicottero e invia notifiche quando serve.

    `storage`: se fornito, registra ogni volo completato in `flights`.
    """
    if state is None:
        if h.in_flight:
            h.missing_cycles += 1
            if h.missing_cycles >= OFFLINE_CYCLES_FOR_LANDED:
                log.info(
                    "%s: landing INFERITO (perso %d cicli, ~%d min)",
                    h.display_name,
                    h.missing_cycles,
                    int(h.missing_cycles * 45 / 60),
                )
                _emit_landing(h, None, sites, notifier, storage, inferred=True)
                h.in_flight = False
                h.last_on_ground = True
                h.missing_cycles = 0
                h.airborne_pending_cycles = 0
                h.flight_track = []
                h.flight_callsign = None
                if storage is not None:
                    try:
                        storage.set_in_flight(h.icao24, False)
                    except Exception:
                        log.exception("Errore aggiornando current_states per %s", h.icao24)
        return

    h.missing_cycles = 0
    h.last_seen_ts = time.time()

    on_ground = state["on_ground"]
    vel = state.get("velocity_ms") or 0.0
    lat = state.get("latitude")
    lon = state.get("longitude")
    alt = state.get("geo_altitude_m") or state.get("baro_altitude_m")

    prev_position = h.last_position

    if lat is not None and lon is not None:
        h.last_position = (lat, lon)
    h.last_heading = state.get("heading_deg")
    h.last_altitude = alt
    h.last_velocity = vel

    prev_on_ground = h.last_on_ground

    # Takeoff detection: si basa sul bit on_ground, NON sulla velocità. Gli
    # elicotteri HEMS spesso restano in hover/low-speed per decine di secondi
    # dopo il liftoff, e un gate sulla ground-speed perdeva i decolli ogni volta
    # che il primo sample post-liftoff cadeva in quella finestra.
    # Per filtrare il flicker del bit Mode-S sul piazzale richiediamo o una
    # transizione pulita True→False, oppure N osservazioni consecutive in aria.
    is_takeoff = False
    if not on_ground and not h.in_flight:
        h.airborne_pending_cycles += 1
        if prev_on_ground is True:
            is_takeoff = True
        elif prev_on_ground is None:
            # Prima osservazione e già in volo: notifico come decollo "tardivo".
            is_takeoff = True
        elif h.airborne_pending_cycles >= AIRBORNE_CYCLES_FOR_TAKEOFF:
            # prev_on_ground=False da 1+ ciclo: confermo dopo N cicli in aria,
            # nel caso abbiamo mancato la transizione pulita (hover a bassa vel).
            is_takeoff = True
    elif on_ground:
        h.airborne_pending_cycles = 0

    if is_takeoff and not h.in_flight:
        log.info("%s: DECOLLO rilevato", h.display_name)
        h.flight_start_ts = time.time()
        h.flight_start_position = (
            (lat, lon) if lat is not None and lon is not None else None
        )
        h.flight_max_altitude_m = alt
        h.flight_max_velocity_ms = vel
        h.flight_distance_km = 0.0
        h.flight_track = (
            [(lat, lon)] if lat is not None and lon is not None else []
        )
        h.flight_callsign = (state.get("callsign") or "").strip() or None
        takeoff_pos = (lat, lon) if lat is not None and lon is not None else None
        notifier.broadcast_event(
            text=format_takeoff(h, state),
            helicopter_key=h.icao24,
            position=takeoff_pos,
        )
        h.in_flight = True
    elif h.in_flight and not on_ground:
        if alt is not None:
            h.flight_max_altitude_m = max(h.flight_max_altitude_m or alt, alt)
        if vel is not None:
            h.flight_max_velocity_ms = max(h.flight_max_velocity_ms or vel, vel)
        if prev_position is not None and lat is not None and lon is not None:
            segment = haversine_km(prev_position[0], prev_position[1], lat, lon)
            if segment < 500:  # filtra outlier di geolocalizzazione
                h.flight_distance_km += segment
        if lat is not None and lon is not None:
            h.flight_track.append((lat, lon))
        cs = (state.get("callsign") or "").strip()
        if cs and not h.flight_callsign:
            h.flight_callsign = cs

    if on_ground and h.in_flight:
        log.info("%s: ATTERRAGGIO rilevato", h.display_name)
        _emit_landing(h, state, sites, notifier, storage)
        h.in_flight = False
        h.flight_track = []
        h.flight_callsign = None

    h.last_on_ground = on_ground

    # Persistenza dello stato live per la dashboard web. L'upsert avviene a
    # ogni poll con dati freschi — safe anche durante write del worker grazie
    # al WAL; la web app legge concorrente senza bloccare.
    if storage is not None:
        try:
            storage.upsert_current_state(
                helicopter_key=h.icao24,
                lat=lat,
                lon=lon,
                altitude_m=alt,
                velocity_ms=vel,
                heading_deg=state.get("heading_deg"),
                on_ground=on_ground,
                in_flight=h.in_flight,
                callsign=(state.get("callsign") or "").strip() or None,
                source=state.get("source"),
            )
        except Exception:
            log.exception("Errore aggiornando current_states per %s", h.icao24)


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
