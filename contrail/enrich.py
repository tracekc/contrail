"""Enrich raw aircraft with airline, notability flags, and external API data."""

from __future__ import annotations

import logging
import re
import threading
import time
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

# Identify the app per Planespotters/adsbdb API etiquette — a descriptive
# User-Agent with a contact URL reduces 403 throttling.
_UA = "Contrail-Skywatch/1.0 (+https://github.com/tracekc/contrail)"
# After an unresolved (failed/throttled) photo lookup, don't re-hit
# Planespotters for the same airframe more often than this.
PHOTO_RETRY_COOLDOWN = 600  # seconds


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


def _fetch_photo(hex_: str) -> tuple[dict, bool]:
    """Fetch a Planespotters photo for `hex_`.

    Returns (photo_fields, resolved). `resolved` is True only on a definitive
    HTTP 200 — whether or not it contained a photo, since a genuine "no photo
    on file" is a real answer worth caching. On 403/timeout/error it is False,
    so the lookup is retried later instead of cached permanently as "no photo"
    (the bug that pinned the hit rate near 7%)."""
    import requests

    try:
        r = requests.get(
            f"https://api.planespotters.net/pub/photos/hex/{hex_}",
            timeout=8, headers={"User-Agent": _UA},
        )
    except Exception as exc:
        log.debug("planespotters lookup failed %s: %s", hex_, exc)
        return {}, False
    if r.status_code != 200:
        log.debug("planespotters %s -> HTTP %s (will retry)", hex_, r.status_code)
        return {}, False
    try:
        photos = r.json().get("photos") or []
    except Exception:
        return {}, False
    if photos:
        ph = photos[0]
        return {
            "photo_url": (ph.get("thumbnail_large") or {}).get("src"),
            "photo_credit": ph.get("photographer"),
        }, True
    return {}, True  # genuine "no photo on file" — resolved, stop retrying


def _fetch_aircraft_bg(hex_: str, cache: "EnrichmentCache", on_complete=None) -> None:
    """Background: fetch aircraft details from adsbdb + photo from planespotters."""
    import requests

    data: dict = {}
    try:
        r = requests.get(
            f"https://api.adsbdb.com/v0/aircraft/{hex_}",
            timeout=8, headers={"User-Agent": _UA},
        )
        if r.status_code == 200:
            ac_info = (r.json().get("response") or {}).get("aircraft") or {}
            data["registration"] = ac_info.get("registration")
            built = ac_info.get("built") or ""
            data["built_year"] = int(built[:4]) if len(built) >= 4 else None
            data["operator"] = ac_info.get("registered_owner")
    except Exception as exc:
        log.debug("adsbdb aircraft lookup failed %s: %s", hex_, exc)

    photo_fields, resolved = _fetch_photo(hex_)
    data.update(photo_fields)
    data["photo_resolved"] = resolved
    data["photo_tried_at"] = time.time()

    if data:
        cache.set_aircraft(hex_, data)
        if on_complete:
            try:
                on_complete(data)
            except Exception as exc:
                log.debug("aircraft on_complete callback failed: %s", exc)

    with _pending_lock:
        _pending.discard(f"ac:{hex_.lower()}")


def _fetch_photo_bg(hex_: str, cache: "EnrichmentCache", on_complete=None) -> None:
    """Background: retry ONLY the Planespotters photo for an already-cached
    airframe whose photo never resolved (e.g. an earlier 403). Merges into the
    existing entry so the adsbdb metadata is preserved."""
    photo_fields, resolved = _fetch_photo(hex_)
    patch = {"photo_resolved": resolved, "photo_tried_at": time.time(), **photo_fields}
    cache.update_aircraft(hex_, patch)
    if photo_fields and on_complete:
        try:
            on_complete(cache.get_aircraft(hex_))
        except Exception as exc:
            log.debug("photo on_complete callback failed: %s", exc)

    with _pending_lock:
        _pending.discard(f"ph:{hex_.lower()}")


def lookup_aircraft(hex_: str, cache: "EnrichmentCache", on_complete=None) -> Optional[dict]:
    """Return cached aircraft data immediately; fetch in the background on a miss.

    If the entry exists but its photo never resolved (unknown airframe, or a
    throttled/failed Planespotters call), fire a photo-only retry — rate-limited
    per airframe by PHOTO_RETRY_COOLDOWN — so transient failures don't leave the
    photo permanently blank. on_complete(data) is called from the background
    thread when fresh data lands, to push it to the UI.
    """
    cached = cache.get_aircraft(hex_)
    if cached is not None:
        unresolved = not cached.get("photo_url") and not cached.get("photo_resolved")
        if unresolved and (time.time() - cached.get("photo_tried_at", 0)) >= PHOTO_RETRY_COOLDOWN:
            key = f"ph:{hex_.lower()}"
            with _pending_lock:
                if key not in _pending:
                    _pending.add(key)
                    threading.Thread(
                        target=_fetch_photo_bg, args=(hex_, cache, on_complete), daemon=True
                    ).start()
        return cached
    key = f"ac:{hex_.lower()}"
    with _pending_lock:
        if key not in _pending:
            _pending.add(key)
            threading.Thread(
                target=_fetch_aircraft_bg, args=(hex_, cache, on_complete), daemon=True
            ).start()
    return None


def _fetch_route_bg(callsign: str, cache: "EnrichmentCache", on_complete=None) -> None:
    """Background: fetch route from adsbdb."""
    import requests

    try:
        r = requests.get(
            f"https://api.adsbdb.com/v0/callsign/{callsign}",
            timeout=8, headers={"User-Agent": _UA},
        )
        if r.status_code != 200:
            return
        fr = (r.json().get("response") or {}).get("flightroute") or {}
        origin = fr.get("origin") or {}
        dest = fr.get("destination") or {}
        if origin and dest:
            data = {
                "origin_icao": origin.get("icao_code"),
                "origin_iata": origin.get("iata_code"),
                "origin_name": origin.get("name"),
                "dest_icao": dest.get("icao_code"),
                "dest_iata": dest.get("iata_code"),
                "dest_name": dest.get("name"),
            }
            cache.set_route(callsign, data)
            if on_complete:
                try:
                    on_complete(data)
                except Exception as exc:
                    log.debug("route on_complete callback failed: %s", exc)
    except Exception as exc:
        log.debug("adsbdb route lookup failed %s: %s", callsign, exc)
    finally:
        with _pending_lock:
            _pending.discard(f"rt:{callsign.upper()}")


def lookup_route(callsign: Optional[str], cache: "EnrichmentCache", on_complete=None) -> Optional[dict]:
    """Return cached route immediately; fire background fetch if stale.

    on_complete(data) is called from the background thread when a fresh fetch
    lands — use it to push data into the UI without waiting for the next cycle.
    """
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
                target=_fetch_route_bg, args=(callsign, cache, on_complete), daemon=True
            ).start()
    return None
