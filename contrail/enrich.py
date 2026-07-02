"""Enrich raw aircraft with airline and notability flags."""

from __future__ import annotations

import re

from .data.aircraft_types import TYPE_NAMES
from .data.reference import AIRLINES, RARE_TYPES, ULTRA_LONG_HAUL
from .models import Aircraft

_PREFIX = re.compile(r"^([A-Z]{3})")


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
