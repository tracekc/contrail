"""Event detection: turn a snapshot of aircraft into scored story candidates.

The detector is a PURE generator — given a snapshot it returns every candidate
worth considering this instant, scored by priority. It holds no cooldown state:
"don't repeat what we just aired" is the Director's job, because only the
Director knows which single candidate actually went to air each cycle.

Snapshot-based, so diversions/go-arounds that need a track history are omitted.
"""

from __future__ import annotations

import random

from .data.reference import (
    RARE_TYPES,
    SQUAWK_EMERGENCY,
    SQUAWK_HIJACK,
    SQUAWK_RADIO_FAIL,
    ULTRA_LONG_HAUL,
)
from .models import Aircraft, StoryCandidate

# base priorities
PRI_EMERGENCY = 95
PRI_RADIO_FAIL = 80
PRI_HIJACK = 90          # sensitive; suppressed from output by default
PRI_RAPID_DESCENT = 70
PRI_RARE_TYPE = 45
PRI_ULL = 40
PRI_MILESTONE = 25
PRI_FILLER = 15          # ordinary-traffic chatter, always available

# Routine approach descents reach ~-3000 ft/min, so that's not notable. A
# genuinely steep descent from altitude is more like -6000+ ft/min; requiring
# the aircraft to still be high keeps normal "descending to land" out.
RAPID_DESCENT_FPM = -6000   # ft/min
RAPID_DESCENT_MIN_ALT = 12000

# Filler ("traffic spotlight") only considers airborne, cruising-ish aircraft
# with enough data to say something concrete.
FILLER_MIN_ALT = 10000
FILLER_SAMPLE = 12


class EventDetector:
    def __init__(self, emit_sensitive: bool = False) -> None:
        self.emit_sensitive = emit_sensitive

    def detect(
        self, aircraft: list[Aircraft], context: dict | None = None
    ) -> list[StoryCandidate]:
        context = context or {}
        out: list[StoryCandidate] = []
        for ac in aircraft:
            out.extend(self._aircraft_candidates(ac))
        out.extend(self._filler_candidates(aircraft))
        out.extend(self._milestone_candidates(context))

        if not self.emit_sensitive:
            out = [c for c in out if not c.sensitive]
        for c in out:
            c.score = float(c.priority)
        out.sort(key=lambda c: c.score, reverse=True)
        return out

    # ── per-aircraft rules ────────────────────────────────────
    def _aircraft_candidates(self, ac: Aircraft) -> list[StoryCandidate]:
        cands: list[StoryCandidate] = []
        emerg = (ac.emergency or "none").lower()

        if ac.squawk == SQUAWK_HIJACK or emerg == "unlawful":
            cands.append(StoryCandidate(
                kind="hijack_7500", priority=PRI_HIJACK,
                headline=f"Unlawful-interference code from {ac.label}",
                aircraft=ac, sensitive=True, detail={"squawk": ac.squawk}))
            return cands  # don't pile other candidates on a sensitive one

        if ac.squawk == SQUAWK_EMERGENCY or emerg in {
            "general", "downed", "lifeguard", "minfuel"
        }:
            cands.append(StoryCandidate(
                kind="emergency_7700", priority=PRI_EMERGENCY,
                headline=f"Emergency squawk from {ac.label}"
                + (f", {ac.airline}" if ac.airline else ""),
                aircraft=ac,
                detail={"squawk": ac.squawk, "emergency": emerg, "alt": ac.altitude}))

        if ac.squawk == SQUAWK_RADIO_FAIL or emerg == "nordo":
            cands.append(StoryCandidate(
                kind="radio_fail_7600", priority=PRI_RADIO_FAIL,
                headline=f"Radio-failure code from {ac.label}",
                aircraft=ac, detail={"squawk": ac.squawk}))

        if (ac.vertical_rate is not None and ac.vertical_rate <= RAPID_DESCENT_FPM
                and (ac.altitude or 0) >= RAPID_DESCENT_MIN_ALT):
            cands.append(StoryCandidate(
                kind="rapid_descent", priority=PRI_RAPID_DESCENT,
                headline=f"Rapid descent: {ac.label} at {ac.vertical_rate} ft/min",
                aircraft=ac,
                detail={"vertical_rate": ac.vertical_rate, "alt": ac.altitude,
                        "corroborate": True}))

        airborne = not ac.on_ground and ac.altitude is not None

        if "rare_type" in ac.flags and airborne:
            cands.append(StoryCandidate(
                kind="rare_type", priority=PRI_RARE_TYPE,
                headline=f"{RARE_TYPES.get(ac.type_code, ac.type_code)} aloft: {ac.label}",
                aircraft=ac, detail={"type": ac.type_code}))

        if "ull" in ac.flags and ac.callsign and airborne:
            cands.append(StoryCandidate(
                kind="ultra_long_haul", priority=PRI_ULL,
                headline=f"{ac.callsign}: {ULTRA_LONG_HAUL.get(ac.callsign.upper(), '')}",
                aircraft=ac, detail={"note": ULTRA_LONG_HAUL.get(ac.callsign.upper())}))
        return cands

    # ── ordinary-traffic filler ───────────────────────────────
    def _filler_candidates(self, aircraft: list[Aircraft]) -> list[StoryCandidate]:
        pool = [
            a for a in aircraft
            if a.callsign and a.altitude and a.altitude >= FILLER_MIN_ALT
            and not a.on_ground and a.lat is not None
        ]
        random.shuffle(pool)
        return [
            StoryCandidate(
                kind="traffic_spotlight", priority=PRI_FILLER,
                headline=f"{ac.callsign}"
                + (f" ({ac.airline})" if ac.airline else "")
                + f" at {ac.altitude} ft",
                aircraft=ac)
            for ac in pool[:FILLER_SAMPLE]
        ]

    # ── ambient milestones ────────────────────────────────────
    def _milestone_candidates(self, context: dict) -> list[StoryCandidate]:
        n = context.get("region_count")
        if not n:
            return []
        return [StoryCandidate(
            kind="milestone_airborne", priority=PRI_MILESTONE,
            headline=f"{n} aircraft in the coverage region right now",
            aircraft=None, detail={"count": n})]
