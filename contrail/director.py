"""The director: turns scored story candidates into the next on-air line.

This is the "soul" of the channel. It picks a segment (lock onto a developing
event, or rotate through ambient segments), assembles a tightly-constrained
prompt, and asks an LLM to write one spoken broadcast line — grounded in the
structured aircraft data so facts stay correct while the phrasing stays varied.
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from .data.reference import SQUAWK_EMERGENCY, SQUAWK_RADIO_FAIL
from .models import Aircraft, StoryCandidate

log = logging.getLogger(__name__)

EVENT_PRIORITY_FLOOR = 70  # candidates at/above this trigger a locked "event" segment

# How long after airing a story before we'll air "the same" one again.
COOLDOWN_EVENT = 90        # ongoing events get periodic updates, not every cycle
COOLDOWN_AIRCRAFT = 600    # an ordinary/notable aircraft: once per 10 min
COOLDOWN_KIND = 240        # kind-level (e.g. the airborne-count milestone)

SYSTEM_PROMPT = """You are Miles, the live on-air voice of Skywatch — a 24/7 \
channel that watches the world's air traffic over a live map. Stay in character \
as Miles at all times.

WHO YOU ARE:
- A warm, dry-witted aviation obsessive who has watched the skies for decades. \
Calm and unflappable, but quietly delighted by the genuinely cool stuff: a rare \
type, a marathon long-haul, the old 747 you still call the Queen of the Skies.
- You have gentle opinions and the odd wry aside, but you never overdo it — at \
most one light touch per line.
- You talk TO the viewer, like a knowledgeable friend pointing things out at the \
window, not a PA system reading a list.

HOW YOU SOUND:
- Output ONE spoken-aloud line, 1-3 sentences. Plain text only: no markdown, no \
stage directions, no quotation marks, no line breaks.
- Tell a tiny story; don't read data. Pick the 1-2 most interesting things and \
build a natural sentence with a little color. A short, breezy line is sometimes \
better than a packed one — vary the length and the energy.
- Match pacing to the moment: unhurried and easy for routine traffic; a touch \
more alert for something unusual; calm, steady and reassuring for an emergency — \
informative, never sensational or breathless.
- Build light anticipation when it fits ("worth keeping an eye on...").
- Vary your phrasing. Never reuse sentence openings or wording from the recent \
lines listed, and don't lean on the same catchphrase every line.
- Write numbers for the ear ("thirty-eight thousand feet", "flight level \
three-seven-zero").

WHAT YOU MUST NOT DO (accuracy is sacred — you would rather say less than be wrong):
- Use ONLY the facts provided. Never invent an operator, route, destination, \
registration, or backstory that is not given. If no operator is provided, refer \
to the flight by its callsign or simply by aircraft type — never guess an airline.
- Any added context (what a type is known for, what an emergency code means) must \
be general aviation knowledge that is broadly true; keep speculation soft \
("likely", "often") and never attach an invented specific to this flight.
- Do NOT spell out registrations or callsigns letter by letter. You MAY read an \
emergency squawk digit by digit (e.g. "seven-seven-zero-zero").
- Mention the transponder/squawk code ONLY when it signals an emergency. Ignore \
routine codes entirely."""


@dataclass
class ScriptLine:
    text: str
    segment: str
    priority: int
    aircraft: Optional[Aircraft] = None
    detail: dict = field(default_factory=dict)


# Ambient segments rotated through when nothing urgent is happening.
AMBIENT_ROTATION = ("rare_type", "ultra_long_haul", "milestone_airborne")

# ── incident tracking ─────────────────────────────────────────
INCIDENT_PRIORITY = 95            # always interrupts the ambient rotation
EMERGENCY_KINDS = {"emergency_7700", "radio_fail_7600"}
INCIDENT_ALT_DELTA = 3000         # ft change worth a new update
INCIDENT_HDG_DELTA = 30           # degrees
INCIDENT_SPD_DELTA = 50           # knots
INCIDENT_STATUS_INTERVAL = 150    # secs: a "still developing" check-in even if steady
INCIDENT_LOST_REPORT = 2          # cycles missing before noting lost signal
INCIDENT_LOST_CLOSE = 6           # cycles missing before giving up
INCIDENT_CLEAR_CONFIRM = 2        # cycles of normal squawk before calling it resolved


def _is_emergency(ac: Aircraft) -> bool:
    emerg = (ac.emergency or "none").lower()
    return (ac.squawk in {SQUAWK_EMERGENCY, SQUAWK_RADIO_FAIL}
            or emerg in {"general", "downed", "lifeguard", "minfuel", "nordo"})


def _describe_changes(prev, ac: Aircraft) -> Optional[str]:
    if not prev:
        return None
    palt, ptrk, pgs = prev
    bits: list[str] = []
    if palt is not None and ac.altitude is not None and abs(ac.altitude - palt) >= INCIDENT_ALT_DELTA:
        bits.append(f"{'descended' if ac.altitude < palt else 'climbed'} "
                    f"from {palt} to {ac.altitude} ft")
    if ptrk is not None and ac.track is not None:
        d = abs((ac.track - ptrk + 180) % 360 - 180)
        if d >= INCIDENT_HDG_DELTA:
            bits.append(f"turned about {round(d)} degrees")
    if pgs is not None and ac.ground_speed is not None and abs(ac.ground_speed - pgs) >= INCIDENT_SPD_DELTA:
        bits.append(f"{'slowed' if ac.ground_speed < pgs else 'sped up'} "
                    f"to {round(ac.ground_speed)} knots")
    return "; ".join(bits) if bits else None


class IncidentTracker:
    """Follows one emergency aircraft across cycles, returning a story candidate
    only when there's something to report (start, a real change, a periodic
    check-in, or resolution). Otherwise stays silent so normal rotation runs.
    """

    def __init__(self) -> None:
        self.active = False
        self.hex: Optional[str] = None
        self.kind: Optional[str] = None
        self.last_ac: Optional[Aircraft] = None
        self._reported_state = None          # (alt, track, gs) at last AIRED update
        self._last_reported_at = 0.0
        self._misses = 0
        self._lost_reported = False
        self._cleared = 0

    def step(self, candidates, aircraft, now) -> Optional[StoryCandidate]:
        by_hex = {a.hex: a for a in (aircraft or [])}
        emerg = [c for c in candidates if c.kind in EMERGENCY_KINDS and c.aircraft]

        if not self.active:
            return self._open(emerg[0]) if emerg else None

        ac = by_hex.get(self.hex)
        if ac is None:                       # dropped off the feed
            self._misses += 1
            if self._misses >= INCIDENT_LOST_CLOSE:
                self._close()
                return self._mk("lost", note="contact lost; likely beyond receiver range")
            if self._misses == INCIDENT_LOST_REPORT and not self._lost_reported:
                self._lost_reported = True
                return self._mk("lost")
            return None

        self._misses = 0
        self._lost_reported = False
        self.last_ac = ac

        if ac.on_ground:                     # landed
            self._close()
            return self._mk("resolved", ac=ac, note="appears to have landed")

        if not _is_emergency(ac):            # squawk back to normal
            self._cleared += 1
            if self._cleared >= INCIDENT_CLEAR_CONFIRM:
                self._close()
                return self._mk("resolved", ac=ac, note="emergency code cleared")
            return None
        self._cleared = 0

        changes = _describe_changes(self._reported_state, ac)
        if changes or (now - self._last_reported_at) >= INCIDENT_STATUS_INTERVAL:
            self._reported_state = (ac.altitude, ac.track, ac.ground_speed)
            self._last_reported_at = now
            return self._mk("update", ac=ac, changes=changes)
        return None

    def _open(self, cand: StoryCandidate) -> StoryCandidate:
        ac = cand.aircraft
        self.active, self.hex, self.kind, self.last_ac = True, ac.hex, cand.kind, ac
        self._reported_state = (ac.altitude, ac.track, ac.ground_speed)
        self._last_reported_at = time.monotonic()
        self._misses, self._lost_reported, self._cleared = 0, False, 0
        return self._mk("open", ac=ac)

    def _close(self) -> None:
        self.active, self.hex = False, None

    def _mk(self, phase, ac=None, changes=None, note=None) -> StoryCandidate:
        ac = ac or self.last_ac
        incident = {"phase": phase}
        if changes:
            incident["changes"] = changes
        if note:
            incident["note"] = note
        return StoryCandidate(
            kind=self.kind or "emergency_7700", priority=INCIDENT_PRIORITY,
            headline=f"incident {phase}", aircraft=ac, detail={"incident": incident})


class Director:
    def __init__(self, client=None, memory=None) -> None:
        self.model = os.getenv("LLM_MODEL_LIVE", "claude-haiku-4-5-20251001")
        self._client = client  # injected anthropic.Anthropic (lazy if None)
        self._memory = memory  # optional SessionMemory; None = no narrative memory
        self._recent: deque[str] = deque(maxlen=6)
        self._last_focus_hex: Optional[str] = None
        self._last_ambient_kind: Optional[str] = None
        self._aired: dict[str, float] = {}  # story key -> last-aired monotonic time
        self._incident = IncidentTracker()

    # ── public ────────────────────────────────────────────────
    def next_line(
        self, candidates: list[StoryCandidate], context: dict,
        aircraft: Optional[list[Aircraft]] = None,
    ) -> Optional[ScriptLine]:
        now = time.monotonic()
        # The incident tracker gets first refusal: it interrupts only when it has
        # something to report, otherwise it stays silent and normal rotation runs.
        focus = self._incident.step(candidates, aircraft or [], now)
        if focus is None:
            pool = candidates
            if self._incident.active and self._incident.hex:
                # don't let normal selection re-cover the plane we're tracking
                pool = [c for c in candidates
                        if not (c.aircraft and c.aircraft.hex == self._incident.hex)]
            focus = self._choose(pool)
        if focus is None:
            return None
        segment = "event" if focus.priority >= EVENT_PRIORITY_FLOOR else focus.kind

        user_prompt = self._build_user_prompt(focus, segment, context)
        text = self._call_llm(user_prompt)
        if not text:
            return None

        self._recent.append(text)
        self._aired[self._key(focus)] = time.monotonic()
        self._prune()
        if focus.aircraft:
            self._last_focus_hex = focus.aircraft.hex
        if focus.priority < EVENT_PRIORITY_FLOOR:
            self._last_ambient_kind = focus.kind

        line = ScriptLine(
            text=text,
            segment=segment,
            priority=focus.priority,
            aircraft=focus.aircraft,
            detail=focus.detail,
        )

        if self._memory is not None:
            self._memory.note_aired(line)

        return line

    # ── segment / focus selection ─────────────────────────────
    def _choose(self, candidates: list[StoryCandidate]) -> Optional[StoryCandidate]:
        now = time.monotonic()
        fresh = [c for c in candidates if self._fresh(c, now)]
        if not fresh:
            return None

        # Events always win (highest-scored fresh one; candidates are pre-sorted).
        events = [c for c in fresh if c.priority >= EVENT_PRIORITY_FLOOR]
        if events:
            return events[0]

        ambient = [c for c in fresh if c.priority < EVENT_PRIORITY_FLOOR]
        if not ambient:
            return None
        # Prefer a different kind than last, and not the aircraft we just featured.
        for c in ambient:
            if c.kind == self._last_ambient_kind:
                continue
            if c.aircraft and c.aircraft.hex == self._last_focus_hex:
                continue
            return c
        return ambient[0]

    # ── airing cooldowns ──────────────────────────────────────
    @staticmethod
    def _key(c: StoryCandidate) -> str:
        return f"{c.kind}:{c.aircraft.hex}" if c.aircraft else c.kind

    @staticmethod
    def _cooldown_for(c: StoryCandidate) -> float:
        if c.priority >= EVENT_PRIORITY_FLOOR:
            return COOLDOWN_EVENT
        if c.aircraft is None:
            return COOLDOWN_KIND
        return COOLDOWN_AIRCRAFT

    def _fresh(self, c: StoryCandidate, now: float) -> bool:
        return now - self._aired.get(self._key(c), 0.0) >= self._cooldown_for(c)

    def _prune(self) -> None:
        if len(self._aired) <= 4000:
            return
        cutoff = time.monotonic() - max(COOLDOWN_AIRCRAFT, COOLDOWN_KIND)
        self._aired = {k: t for k, t in self._aired.items() if t >= cutoff}

    # ── prompt assembly ───────────────────────────────────────
    def _build_user_prompt(
        self, focus: StoryCandidate, segment: str, context: dict
    ) -> str:
        lines: list[str] = []
        if segment == "event":
            inc = focus.detail.get("incident", {})
            phase = inc.get("phase")
            if phase == "open":
                lines.append("SEGMENT: BREAKING — a developing situation has just begun. "
                             "Introduce it to viewers for the first time.")
            elif phase == "update":
                lines.append("SEGMENT: developing situation — a FOLLOW-UP on an incident "
                             "you are already covering. Phrase it as returning to / "
                             "continuing the story, NOT as introducing it fresh.")
                if inc.get("changes"):
                    lines.append(f"WHAT CHANGED SINCE THE LAST UPDATE: {inc['changes']}")
                else:
                    lines.append("No major change — give a brief 'still developing' check-in.")
            elif phase == "resolved":
                lines.append("SEGMENT: the incident appears to be RESOLVING. Give a calm, "
                             "brief closing update.")
                if inc.get("note"):
                    lines.append(f"RESOLUTION: {inc['note']}")
            elif phase == "lost":
                lines.append("SEGMENT: we've lost the aircraft's signal — possibly just a "
                             "coverage gap, NOT necessarily a bad outcome. Note it carefully, "
                             "without alarm or speculation about its fate.")
            else:
                lines.append("SEGMENT: live event — focus on this developing situation.")
        elif segment == "rare_type":
            lines.append("SEGMENT: ambient — a brief spotlight on a notable aircraft aloft.")
        elif segment == "ultra_long_haul":
            lines.append("SEGMENT: ambient — a brief note on a long-haul flight in progress.")
        elif segment == "traffic_spotlight":
            lines.append("SEGMENT: ambient — a brief, varied, human-interest note on "
                         "this flight passing through. Keep it light and conversational.")
        else:
            lines.append("SEGMENT: ambient — a calm overview of current traffic.")

        ac = focus.aircraft
        if ac:
            lines.append("\nFOCUS AIRCRAFT:")
            lines.append(f"- callsign: {ac.callsign or 'unknown'}")
            if ac.airline:
                lines.append(f"- operator: {ac.airline}")
            if ac.type_desc:
                lines.append(f"- type: {ac.type_desc}")
            if ac.registration:
                lines.append(f"- registration: {ac.registration}")
            if ac.altitude is not None:
                lines.append(f"- altitude: {ac.altitude} ft")
            if ac.ground_speed is not None:
                lines.append(f"- ground speed: {ac.ground_speed:.0f} kt")
            if ac.vertical_rate is not None and abs(ac.vertical_rate) >= 500:
                verb = "climbing" if ac.vertical_rate > 0 else "descending"
                lines.append(f"- {verb} at {abs(ac.vertical_rate)} ft/min")
            # Only surface the squawk when it signals an emergency.
            sq = focus.detail.get("squawk") or ac.squawk
            meaning = {
                SQUAWK_EMERGENCY: " (general emergency)",
                SQUAWK_RADIO_FAIL: " (radio failure)",
            }.get(sq)
            if meaning:
                lines.append(f"- squawk: {sq}{meaning}")
            note = focus.detail.get("note")
            if note:
                lines.append(f"- note: {note}")

        n = context.get("region_count")
        if n:
            lines.append(f"\nCONTEXT: {n} aircraft currently in the coverage region.")

        if self._memory is not None:
            snippets = self._memory.recall(focus, context)
            if snippets:
                lines.append(
                    "\nMEMORY (only reference if it fits naturally — never force a callback):"
                )
                lines.extend(f"- {s}" for s in snippets)

        if self._recent:
            lines.append("\nRECENT LINES (do not repeat these phrasings):")
            lines.extend(f"- {r}" for r in self._recent)

        lines.append("\nWrite the next on-air line.")
        return "\n".join(lines)

    # ── LLM call ──────────────────────────────────────────────
    def _ensure_client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        return self._client

    def _call_llm(self, user_prompt: str) -> Optional[str]:
        try:
            client = self._ensure_client()
            resp = client.messages.create(
                model=self.model,
                max_tokens=220,
                temperature=0.85,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            text = "".join(
                block.text for block in resp.content if block.type == "text"
            ).strip()
            return _clean(text) or None
        except Exception as exc:
            log.warning("director LLM call failed: %s", exc)
            return None


def _clean(text: str) -> str:
    text = text.strip().strip('"').strip()
    # collapse any accidental line breaks into one spoken line
    return " ".join(text.split())
