"""Narrative memory for the commentary.

Holds structured facts only — never LLM prose — so callbacks can't hallucinate.
The store persists across process restarts (stream recycles every ~30 min / 6 h).

Usage sketch:
    memory = SessionMemory()
    memory.load()
    # each cycle:
    memory.observe(aircraft, candidates)
    line = director.next_line(...)
    memory.note_aired(line)
    snippets = memory.recall(focus_candidate, context)  # injected into next prompt
    # periodically:
    memory.prune(time.time())
    memory.save()
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .detect import (
    PRI_EMERGENCY,
    PRI_RADIO_FAIL,
    PRI_RARE_TYPE,
    PRI_ULL,
    PRI_MILESTONE,
    PRI_FILLER,
)
from .models import Aircraft, StoryCandidate

log = logging.getLogger(__name__)

STORE_PATH = Path(__file__).parent / "memory_store.json"

# ── salience thresholds ──────────────────────────────────────────────────────
# Only aircraft/events at or above this salience are added to featured/arcs.
SALIENCE_FEATURED_MIN = 30      # roughly rare_type and above
SALIENCE_ARC_MIN = 40           # only open a full arc for genuinely interesting planes
SALIENCE_EMERGENCY = 95
SALIENCE_RADIO_FAIL = 80
SALIENCE_RARE_TYPE = 45
SALIENCE_ULL = 40
SALIENCE_FILLER = 5             # ordinary cruiser — don't feature, don't open arc

# ── arc timings ──────────────────────────────────────────────────────────────
ARC_DORMANT_AFTER = 300         # secs absent -> dormant
ARC_CLOSE_AFTER = 1200          # secs dormant -> closed
ARC_MAX_EVENTS = 20             # cap per arc

# ── cadence guard ────────────────────────────────────────────────────────────
# Callbacks are rate-limited: at most one every CALLBACK_EVERY_N aired lines.
CALLBACK_EVERY_N = 4
# Arc check-in: minimum seconds between mentioning the same arc.
ARC_CHECKIN_MIN_S = 180
# Only call back to an aircraft we featured at least this long ago — a callback
# to something mentioned seconds earlier is meaningless.
CALLBACK_MIN_AGE_S = 180
# Session-record snippets are surfaced at most this often (in aired lines).
RECORD_EVERY_N = 10
# Offer a "if you're just joining us" recap for new viewers at most this often.
RECAP_EVERY_N = 25
# Don't recap until the session has been on air at least this long (secs) —
# a recap five minutes in has nothing to summarise.
RECAP_MIN_SESSION_S = 1800

# ── pruning ──────────────────────────────────────────────────────────────────
FEATURED_MAX_AGE = 7200         # prune featured aircraft not seen for 2 h
RECORDS_KEEP = True             # session records are never pruned mid-session

# Notable type codes for counting (superset of RARE_TYPES, kept here to avoid
# coupling to the reference module's exact content at call time).
NOTABLE_TYPE_CODES = {"A388", "B744", "B748", "B742", "A124", "A342",
                      "A343", "A345", "A346", "MD11", "IL76", "B703"}


# ── data structures ──────────────────────────────────────────────────────────

@dataclass
class FeaturedAircraft:
    hex: str
    callsign: Optional[str]
    type_code: Optional[str]
    type_desc: Optional[str]
    angle_used: str              # kind of the candidate that sent it to air
    alt: Optional[int]
    track: Optional[float]
    gs: Optional[float]
    first_seen: float            # wall time
    last_featured: float         # wall time; updated on note_aired
    last_seen: float             # wall time; updated on observe


@dataclass
class Arc:
    hex: str
    callsign: Optional[str]
    type_desc: Optional[str]
    state: str                   # open | updating | dormant | closed
    events: list[str]            # short factual event strings
    opened_at: float             # wall time
    last_seen: float             # wall time
    last_mentioned: float        # wall time; updated on note_aired callback


@dataclass
class SessionRecords:
    highest_alt: Optional[int] = None
    highest_alt_hex: Optional[str] = None
    highest_alt_callsign: Optional[str] = None
    rarest_type_code: Optional[str] = None
    rarest_type_desc: Optional[str] = None
    rarest_type_hex: Optional[str] = None
    notable_type_counts: dict = field(default_factory=dict)  # type_code -> int
    incident_count: int = 0
    busiest_count: int = 0       # peak region_count
    busiest_at: Optional[float] = None
    session_started: float = field(default_factory=time.time)


def _salience(candidate: StoryCandidate) -> float:
    """Map a candidate's priority to a salience score (0-100).

    We use the detector's priority constants directly; higher = more memorable.
    """
    return float(candidate.priority)


def _aircraft_salience(ac: Aircraft, candidates: list[StoryCandidate]) -> float:
    """Best salience this aircraft has across all its candidates this cycle."""
    best = SALIENCE_FILLER
    for c in candidates:
        if c.aircraft and c.aircraft.hex == ac.hex:
            best = max(best, _salience(c))
    return best


# ── main class ───────────────────────────────────────────────────────────────

class SessionMemory:
    """Session-scoped structured memory for the commentary narrative.

    Thread-safety: NOT thread-safe. Must be called from the pipeline thread only.
    """

    def __init__(self, store_path: Path = STORE_PATH) -> None:
        self._path = store_path
        self.featured_aircraft: dict[str, FeaturedAircraft] = {}
        self.arcs: dict[str, Arc] = {}
        self.records = SessionRecords()
        self._aired_count = 0           # total lines aired this session
        # No callback until real history accrues (needs CALLBACK_EVERY_N lines first).
        self._last_callback_at_count = 0
        self._last_record_at_count = -RECORD_EVERY_N
        self._last_recap_at_count = 0

    # ── observe ──────────────────────────────────────────────────────────────

    def observe(self, aircraft: list[Aircraft], candidates: list[StoryCandidate]) -> None:
        """Ingest a snapshot. Update featured aircraft, arcs, and session records.

        Called once per pipeline cycle BEFORE director.next_line().
        """
        now = time.time()
        live_hexes = {ac.hex for ac in aircraft if not ac.on_ground}

        # Update records from context (region_count is on candidates via milestone)
        for c in candidates:
            if c.kind == "milestone_airborne" and c.detail.get("count"):
                cnt = c.detail["count"]
                if cnt > self.records.busiest_count:
                    self.records.busiest_count = cnt
                    self.records.busiest_at = now

        for ac in aircraft:
            if ac.on_ground or ac.altitude is None:
                continue

            sal = _aircraft_salience(ac, candidates)

            # Session records: highest altitude
            if ac.altitude is not None and (
                self.records.highest_alt is None
                or ac.altitude > self.records.highest_alt
            ):
                self.records.highest_alt = ac.altitude
                self.records.highest_alt_hex = ac.hex
                self.records.highest_alt_callsign = ac.callsign

            # Notable type counting
            if ac.type_code in NOTABLE_TYPE_CODES:
                if ac.type_code not in self.records.notable_type_counts:
                    self.records.notable_type_counts[ac.type_code] = 0
                # Only count each hex once per session (across observe calls)
                _seen_key = f"_seen_{ac.hex}"
                if not getattr(self.records, _seen_key, False):
                    self.records.notable_type_counts[ac.type_code] += 1
                    setattr(self.records, _seen_key, True)

            # Rarest type: prefer lower-priority types (more exotic)
            if "rare_type" in ac.flags and ac.type_code:
                if self.records.rarest_type_code is None:
                    self.records.rarest_type_code = ac.type_code
                    self.records.rarest_type_desc = ac.type_desc
                    self.records.rarest_type_hex = ac.hex

            # Update or create featured aircraft entry
            if sal >= SALIENCE_FEATURED_MIN:
                best_kind = _best_kind_for(ac, candidates)
                if ac.hex in self.featured_aircraft:
                    fa = self.featured_aircraft[ac.hex]
                    fa.alt = ac.altitude
                    fa.track = ac.track
                    fa.gs = ac.ground_speed
                    fa.last_seen = now
                    if sal > SALIENCE_FEATURED_MIN:
                        fa.angle_used = best_kind  # upgrade angle if we get a better candidate
                else:
                    self.featured_aircraft[ac.hex] = FeaturedAircraft(
                        hex=ac.hex,
                        callsign=ac.callsign,
                        type_code=ac.type_code,
                        type_desc=ac.type_desc,
                        angle_used=best_kind,
                        alt=ac.altitude,
                        track=ac.track,
                        gs=ac.ground_speed,
                        first_seen=now,
                        last_featured=0.0,   # 0 = never aired
                        last_seen=now,
                    )

            # Arc management
            if sal >= SALIENCE_ARC_MIN:
                if ac.hex not in self.arcs:
                    desc = ac.type_desc or ac.type_code or "unknown type"
                    cs = ac.callsign or ac.hex
                    first_event = f"first spotted at {ac.altitude} ft"
                    if ac.vertical_rate and ac.vertical_rate > 500:
                        first_event += ", climbing"
                    elif ac.vertical_rate and ac.vertical_rate < -500:
                        first_event += ", descending"
                    self.arcs[ac.hex] = Arc(
                        hex=ac.hex,
                        callsign=ac.callsign,
                        type_desc=desc,
                        state="open",
                        events=[first_event],
                        opened_at=now,
                        last_seen=now,
                        last_mentioned=0.0,
                    )
                else:
                    arc = self.arcs[ac.hex]
                    arc.last_seen = now
                    if arc.state in ("dormant", "closed"):
                        arc.state = "updating"
                        arc.events.append("re-acquired signal")
                    elif arc.state == "open":
                        # Record significant changes
                        fa = self.featured_aircraft.get(ac.hex)
                        if fa and fa.alt is not None and ac.altitude is not None:
                            delta = abs(ac.altitude - fa.alt)
                            if delta >= 3000:
                                verb = "climbed" if ac.altitude > fa.alt else "descended"
                                arc.events.append(
                                    f"{verb} to {ac.altitude} ft"
                                )
                        arc.state = "updating"
                    if len(arc.events) > ARC_MAX_EVENTS:
                        arc.events = arc.events[-ARC_MAX_EVENTS:]

        # Mark arcs dormant/closed when aircraft leave the feed
        for hex_, arc in self.arcs.items():
            if arc.state in ("open", "updating") and hex_ not in live_hexes:
                age_absent = now - arc.last_seen
                if age_absent >= ARC_DORMANT_AFTER:
                    arc.state = "dormant"
                    if "lost signal" not in (arc.events[-1] if arc.events else ""):
                        arc.events.append("lost signal — may be beyond coverage")

        # Mark incident emergencies
        emergency_hexes = {
            c.aircraft.hex for c in candidates
            if c.kind in ("emergency_7700", "radio_fail_7600") and c.aircraft
        }
        for hex_ in emergency_hexes:
            if hex_ in self.arcs:
                arc = self.arcs[hex_]
                if not any("emergency" in e for e in arc.events):
                    arc.events.append("declared emergency")
            # Count incident (once per hex)
            if hex_ not in getattr(self, "_counted_incidents", set()):
                if not hasattr(self, "_counted_incidents"):
                    self._counted_incidents: set = set()
                self._counted_incidents.add(hex_)
                self.records.incident_count += 1

    # ── note_aired ───────────────────────────────────────────────────────────

    def note_aired(self, line) -> None:
        """Record a line that was successfully aired.

        `line` is a ScriptLine (from director.py); we read .aircraft, .segment.
        """
        if line is None:
            return
        self._aired_count += 1
        now = time.time()
        ac = line.aircraft
        if ac is None:
            return
        if ac.hex in self.featured_aircraft:
            fa = self.featured_aircraft[ac.hex]
            fa.last_featured = now
            fa.angle_used = line.segment  # track the angle we actually used
        if ac.hex in self.arcs:
            self.arcs[ac.hex].last_mentioned = now

    # ── recall ───────────────────────────────────────────────────────────────

    def recall(
        self,
        focus: Optional[StoryCandidate],
        context: dict,
    ) -> list[str]:
        """Return a small list of relevant factual snippets for prompt injection.

        Bounded to 2-3 items max. Returns [] when there's nothing worth saying
        or the cadence guard suppresses callbacks.
        """
        now = time.time()
        results: list[str] = []

        # Session recap for new viewers ("if you're just joining us…"). Runs on
        # its own slower cadence, independent of the callback guard, since a
        # 24/7 stream always has people dropping in mid-session.
        if (self._aired_count - self._last_recap_at_count) >= RECAP_EVERY_N:
            recap = self._session_recap(now)
            if recap:
                self._last_recap_at_count = self._aired_count
                self._last_callback_at_count = self._aired_count
                return [recap]

        # Cadence guard: only allow a callback every CALLBACK_EVERY_N aired lines.
        lines_since_last = self._aired_count - self._last_callback_at_count
        if lines_since_last < CALLBACK_EVERY_N:
            return []

        focus_hex = focus.aircraft.hex if (focus and focus.aircraft) else None

        # 1. Continuity callback: the focus aircraft was featured a while ago and
        #    is still with us. Phrased as a natural fact, no internal labels.
        if focus_hex and focus_hex in self.featured_aircraft:
            fa = self.featured_aircraft[focus_hex]
            if fa.last_featured > 0 and (now - fa.last_featured) > CALLBACK_MIN_AGE_S:
                label = fa.callsign or fa.hex
                type_str = f", the {fa.type_desc}," if fa.type_desc else ""
                results.append(
                    f"You mentioned {label}{type_str} about {_ago(now - fa.last_featured)} — "
                    f"it's still in view, now around {fa.alt} ft."
                )

        # 2. Closure of a resolved incident on a DIFFERENT aircraft — a satisfying
        #    callback ("that emergency from earlier landed safely").
        if not results:
            for hex_, arc in self.arcs.items():
                if hex_ == focus_hex or arc.state != "closed" or not arc.events:
                    continue
                last_ev = arc.events[-1]
                if any(k in last_ev for k in ("landed", "resolved", "cleared")):
                    label = arc.callsign or hex_
                    results.append(
                        f"Earlier, {label}'s situation wrapped up {_ago(now - arc.last_seen)} — "
                        f"{last_ev}."
                    )
                    break

        # 3. A session record — sparingly (at most every RECORD_EVERY_N lines).
        if not results and (self._aired_count - self._last_record_at_count) >= RECORD_EVERY_N:
            record_snip = self._best_record_snippet(now)
            if record_snip:
                results.append(record_snip)
                self._last_record_at_count = self._aired_count

        if results:
            self._last_callback_at_count = self._aired_count

        return results[:2]  # hard cap — keep the prompt lean

    def _session_recap(self, now: float) -> Optional[str]:
        """Build a factual session-summary snippet for a 'just joining us' recap,
        or None if the session is too young / too quiet to be worth recapping."""
        r = self.records
        dur_s = now - r.session_started
        if dur_s < RECAP_MIN_SESSION_S:
            return None

        bits: list[str] = []
        hrs = dur_s / 3600.0
        if hrs >= 1:
            bits.append(f"on air about {round(hrs)} hour{'s' if round(hrs) != 1 else ''}")
        else:
            bits.append(f"on air about {int(dur_s / 60)} minutes")

        notable = sum(r.notable_type_counts.values())
        if notable:
            bits.append(f"{notable} notable aircraft featured")
        if r.incident_count:
            bits.append(f"{r.incident_count} incident{'s' if r.incident_count != 1 else ''} followed")
        if r.busiest_count:
            bits.append(f"peak traffic around {r.busiest_count} aircraft")

        # Need more than just the "on air" bit for a recap to be worthwhile.
        if len(bits) < 2:
            return None
        return (
            "SESSION SO FAR (you may open with a brief 'if you're just joining us' "
            "recap, only if it fits naturally): " + "; ".join(bits) + "."
        )

    def _best_record_snippet(self, now: float) -> Optional[str]:
        """Return one record snippet if there's something interesting to note."""
        r = self.records

        # Highest altitude
        if r.highest_alt and r.highest_alt_callsign:
            return (
                f"The highest we've tracked tonight is {r.highest_alt_callsign}, "
                f"up at {r.highest_alt} ft."
            )

        # Notable type counts
        for code, count in r.notable_type_counts.items():
            from .data.reference import RARE_TYPES
            name = RARE_TYPES.get(code, code)
            if count >= 2:
                return f"That's the {count}th {name} we've seen this session."
            elif count == 1:
                return f"First {name} of the session so far."

        # Incident count
        if r.incident_count >= 2:
            return f"We've followed {r.incident_count} incidents this session."

        return None

    # ── prune ────────────────────────────────────────────────────────────────

    def prune(self, now: float) -> None:
        """Remove stale low-value entries. Called on load and periodically."""
        # Prune featured aircraft not seen for a while
        stale_featured = [
            hex_ for hex_, fa in self.featured_aircraft.items()
            if now - fa.last_seen > FEATURED_MAX_AGE
        ]
        for hex_ in stale_featured:
            del self.featured_aircraft[hex_]

        # Close dormant arcs that have been silent too long
        for arc in self.arcs.values():
            if arc.state == "dormant" and now - arc.last_seen > ARC_CLOSE_AFTER:
                arc.state = "closed"

        # Evict very old closed arcs (keep at most 20)
        closed = [(hex_, a) for hex_, a in self.arcs.items() if a.state == "closed"]
        closed.sort(key=lambda x: x[1].last_seen)
        for hex_, _ in closed[:-20]:
            del self.arcs[hex_]

    # ── persistence ──────────────────────────────────────────────────────────

    def save(self) -> None:
        """Atomically persist memory to JSON."""
        data = {
            "version": 1,
            "saved_at": time.time(),
            "featured_aircraft": {
                hex_: {
                    "hex": fa.hex,
                    "callsign": fa.callsign,
                    "type_code": fa.type_code,
                    "type_desc": fa.type_desc,
                    "angle_used": fa.angle_used,
                    "alt": fa.alt,
                    "track": fa.track,
                    "gs": fa.gs,
                    "first_seen": fa.first_seen,
                    "last_featured": fa.last_featured,
                    "last_seen": fa.last_seen,
                }
                for hex_, fa in self.featured_aircraft.items()
            },
            "arcs": {
                hex_: {
                    "hex": arc.hex,
                    "callsign": arc.callsign,
                    "type_desc": arc.type_desc,
                    "state": arc.state,
                    "events": arc.events,
                    "opened_at": arc.opened_at,
                    "last_seen": arc.last_seen,
                    "last_mentioned": arc.last_mentioned,
                }
                for hex_, arc in self.arcs.items()
            },
            "records": {
                "highest_alt": self.records.highest_alt,
                "highest_alt_hex": self.records.highest_alt_hex,
                "highest_alt_callsign": self.records.highest_alt_callsign,
                "rarest_type_code": self.records.rarest_type_code,
                "rarest_type_desc": self.records.rarest_type_desc,
                "rarest_type_hex": self.records.rarest_type_hex,
                "notable_type_counts": self.records.notable_type_counts,
                "incident_count": self.records.incident_count,
                "busiest_count": self.records.busiest_count,
                "busiest_at": self.records.busiest_at,
                "session_started": self.records.session_started,
            },
            "aired_count": self._aired_count,
            "last_callback_at_count": self._last_callback_at_count,
            "last_record_at_count": self._last_record_at_count,
            "last_recap_at_count": self._last_recap_at_count,
        }
        tmp = self._path.with_suffix(".json.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, indent=2))
            os.replace(tmp, self._path)
            log.debug("memory saved: %d featured, %d arcs", len(self.featured_aircraft), len(self.arcs))
        except Exception as exc:
            log.warning("memory save failed: %s", exc)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def load(self) -> None:
        """Load memory from disk. Silently starts fresh if file is missing/corrupt."""
        if not self._path.exists():
            log.debug("memory_store.json not found — starting fresh")
            return
        try:
            data = json.loads(self._path.read_text())
            if data.get("version") != 1:
                log.warning("memory store version mismatch — starting fresh")
                return

            for hex_, fd in data.get("featured_aircraft", {}).items():
                self.featured_aircraft[hex_] = FeaturedAircraft(
                    hex=fd["hex"],
                    callsign=fd.get("callsign"),
                    type_code=fd.get("type_code"),
                    type_desc=fd.get("type_desc"),
                    angle_used=fd.get("angle_used", "unknown"),
                    alt=fd.get("alt"),
                    track=fd.get("track"),
                    gs=fd.get("gs"),
                    first_seen=fd.get("first_seen", time.time()),
                    last_featured=fd.get("last_featured", 0.0),
                    last_seen=fd.get("last_seen", time.time()),
                )

            for hex_, ad in data.get("arcs", {}).items():
                self.arcs[hex_] = Arc(
                    hex=ad["hex"],
                    callsign=ad.get("callsign"),
                    type_desc=ad.get("type_desc"),
                    state=ad.get("state", "closed"),
                    events=ad.get("events", []),
                    opened_at=ad.get("opened_at", time.time()),
                    last_seen=ad.get("last_seen", time.time()),
                    last_mentioned=ad.get("last_mentioned", 0.0),
                )

            rd = data.get("records", {})
            r = self.records
            r.highest_alt = rd.get("highest_alt")
            r.highest_alt_hex = rd.get("highest_alt_hex")
            r.highest_alt_callsign = rd.get("highest_alt_callsign")
            r.rarest_type_code = rd.get("rarest_type_code")
            r.rarest_type_desc = rd.get("rarest_type_desc")
            r.rarest_type_hex = rd.get("rarest_type_hex")
            r.notable_type_counts = rd.get("notable_type_counts", {})
            r.incident_count = rd.get("incident_count", 0)
            r.busiest_count = rd.get("busiest_count", 0)
            r.busiest_at = rd.get("busiest_at")
            # Keep session_started from the original (the show goes on)
            r.session_started = rd.get("session_started", r.session_started)

            self._aired_count = data.get("aired_count", 0)
            self._last_callback_at_count = data.get("last_callback_at_count", 0)
            self._last_record_at_count = data.get("last_record_at_count", -RECORD_EVERY_N)
            self._last_recap_at_count = data.get("last_recap_at_count", self._aired_count)

            # Prune stale entries immediately on load
            self.prune(time.time())

            log.info(
                "memory loaded: %d featured, %d arcs, %d aired",
                len(self.featured_aircraft), len(self.arcs), self._aired_count,
            )
        except Exception as exc:
            log.warning("memory load failed (%s) — starting fresh", exc)
            self.featured_aircraft = {}
            self.arcs = {}
            self.records = SessionRecords()


# ── helpers ───────────────────────────────────────────────────────────────────

def _angle_label(kind: str) -> str:
    """Human-readable label for the angle/segment kind."""
    _labels = {
        "emergency_7700": "emergency",
        "radio_fail_7600": "radio failure",
        "rapid_descent": "rapid descent",
        "rare_type": "rare type spotlight",
        "ultra_long_haul": "long-haul",
        "traffic_spotlight": "traffic spotlight",
        "milestone_airborne": "count milestone",
        "event": "live event",
    }
    return _labels.get(kind, kind)


def _ago(seconds: float) -> str:
    """Return a human-readable elapsed-time string."""
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds / 60)}m ago"
    return f"{seconds / 3600:.1f}h ago"


def _best_kind_for(ac: Aircraft, candidates: list[StoryCandidate]) -> str:
    """Pick the highest-priority candidate kind for this aircraft."""
    best_priority = -1
    best_kind = "traffic_spotlight"
    for c in candidates:
        if c.aircraft and c.aircraft.hex == ac.hex:
            if c.priority > best_priority:
                best_priority = c.priority
                best_kind = c.kind
    return best_kind
