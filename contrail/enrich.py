"""Enrich raw aircraft with airline, notability flags, and external API data."""

from __future__ import annotations

import logging
import re
import threading
from typing import TYPE_CHECKING, Optional

from .data.aircraft_types import TYPE_NAMES
from .data.reference import AIRLINES, RARE_TYPES, ULTRA_LONG_HAUL
from .models import Aircraft

if TYPE_CHECKING:
    from .enrich_cache import EnrichmentCache

log = logging.getLogger(__name__)

_PREFIX = re.compile(r"^([A-Z]{3})")

_pending: set[str] = set()
_pending_lock = threading.Lock()


def airline_for(callsign: str | None) -> str | None:
    if not callsign:
        return None
    m = _PREFIX.match(callsign.upper())
    if not m:
        return None
    return AIRLINES.get(m.group(1))


def enrich(ac: Aircraft) -> Aircraft:
    ac.airline = airline_for(ac.callsign)

    # Backfill a readable type name when the feed omits `desc` (e.g. adsb.lol).
    if not ac.type_desc and ac.type_code:
        ac.type_desc = TYPE_NAMES.get(ac.type_code)

    flags: list[str] = []
    if ac.type_code in RARE_TYPES:
        flags.append("rare_type")
    if ac.callsign and ac.callsign.upper() in ULTRA_LONG_HAUL:
        flags.append("ull")
    ac.flags = flags
    return ac


def enrich_all(aircraft: list[Aircraft]) -> list[Aircraft]:
    return [enrich(a) for a in aircraft]


# ── External API enrichment (non-blocking, background threads) ────────────────


def _fetch_aircraft_bg(hex_: str, cache: "EnrichmentCache") -> None:
    """Background: fetch aircraft details from adsbdb + photo from planespotters."""
    import requests

    data: dict = {}
    try:
        r = requests.get(
            f"https://api.adsbdb.com/v0/aircraft/{hex_}",
            timeout=8, headers={"User-Agent": "contrail-skywatch/1.0"},
        )
        if r.status_code == 200:
            ac_info = (r.json().get("response") or {}).get("aircraft") or {}
            data["registration"] = ac_info.get("registration")
            built = ac_info.get("built") or ""
            data["built_year"] = int(built[:4]) if len(built) >= 4 else None
            data["operator"] = ac_info.get("registered_owner")
    except Exception as exc:
        log.debug("adsbdb aircraft lookup failed %s: %s", hex_, exc)

    try:
        r2 = requests.get(
            f"https://api.planespotters.net/pub/photos/hex/{hex_}",
            timeout=8, headers={"User-Agent": "contrail-skywatch/1.0"},
        )
        if r2.status_code == 200:
            photos = r2.json().get("photos") or []
            if photos:
                ph = photos[0]
                data["photo_url"] = (ph.get("thumbnail_large") or {}).get("src")
                data["photo_credit"] = ph.get("photographer")
    except Exception as exc:
        log.debug("planespotters lookup failed %s: %s", hex_, exc)

    if data:
        cache.set_aircraft(hex_, data)

    with _pending_lock:
        _pending.discard(f"ac:{hex_.lower()}")


def lookup_aircraft(hex_: str, cache: "EnrichmentCache") -> Optional[dict]:
    """Return cached aircraft data immediately; fire background fetch if stale."""
    cached = cache.get_aircraft(hex_)
    if cached:
        return cached
    key = f"ac:{hex_.lower()}"
    with _pending_lock:
        if key not in _pending:
            _pending.add(key)
            threading.Thread(
                target=_fetch_aircraft_bg, args=(hex_, cache), daemon=True
            ).start()
    return None


def _fetch_route_bg(callsign: str, cache: "EnrichmentCache") -> None:
    """Background: fetch route from adsbdb."""
    import requests

    try:
        r = requests.get(
            f"https://api.adsbdb.com/v0/callsign/{callsign}",
            timeout=8, headers={"User-Agent": "contrail-skywatch/1.0"},
        )
        if r.status_code != 200:
            return
        fr = (r.json().get("response") or {}).get("flightroute") or {}
        origin = fr.get("origin") or {}
        dest = fr.get("destination") or {}
        if origin and dest:
            cache.set_route(callsign, {
                "origin_icao": origin.get("icao_code"),
                "origin_iata": origin.get("iata_code"),
                "origin_name": origin.get("name"),
                "dest_icao": dest.get("icao_code"),
                "dest_iata": dest.get("iata_code"),
                "dest_name": dest.get("name"),
            })
    except Exception as exc:
        log.debug("adsbdb route lookup failed %s: %s", callsign, exc)
    finally:
        with _pending_lock:
            _pending.discard(f"rt:{callsign.upper()}")


def lookup_route(callsign: Optional[str], cache: "EnrichmentCache") -> Optional[dict]:
    """Return cached route immediately; fire background fetch if stale."""
    if not callsign:
        return None
    cached = cache.get_route(callsign)
    if cached:
        return cached
    key = f"rt:{callsign.upper()}"
    with _pending_lock:
        if key not in _pending:
            _pending.add(key)
            threading.Thread(
                target=_fetch_route_bg, args=(callsign, cache), daemon=True
            ).start()
    return None
