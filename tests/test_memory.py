"""Tests for contrail.memory — pure Python, no network, no LLM, no skia."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest

from contrail.memory import (
    CALLBACK_EVERY_N,
    ARC_DORMANT_AFTER,
    SALIENCE_FEATURED_MIN,
    SALIENCE_ARC_MIN,
    SessionMemory,
    _ago,
    _angle_label,
    _best_kind_for,
)
from contrail.models import Aircraft, StoryCandidate


# ── factories ─────────────────────────────────────────────────────────────────

def _ac(
    hex_="abc123",
    callsign="TST001",
    type_code="A388",
    type_desc="Airbus A380",
    altitude=35000,
    on_ground=False,
    ground_speed=480.0,
    track=90.0,
    vertical_rate=None,
    squawk="2000",
    emergency="none",
    flags=None,
    lat=51.5,
    lon=-0.5,
    registration="G-TEST",
    airline="Test Airways",
) -> Aircraft:
    ac = Aircraft(
        hex=hex_,
        callsign=callsign,
        registration=registration,
        type_code=type_code,
        type_desc=type_desc,
        altitude=altitude,
        on_ground=on_ground,
        ground_speed=ground_speed,
        track=track,
        vertical_rate=vertical_rate,
        squawk=squawk,
        emergency=emergency,
        category=None,
        lat=lat,
        lon=lon,
        distance_nm=50.0,
    )
    ac.airline = airline
    ac.flags = flags or []
    return ac


def _candidate(
    kind="rare_type",
    priority=45,
    headline="test candidate",
    ac=None,
    detail=None,
) -> StoryCandidate:
    return StoryCandidate(
        kind=kind,
        priority=priority,
        headline=headline,
        aircraft=ac,
        detail=detail or {},
    )


def _make_memory(tmp_path: Path) -> SessionMemory:
    return SessionMemory(store_path=tmp_path / "memory_store.json")


# ── salience / observe ────────────────────────────────────────────────────────

class TestSalience:
    def test_high_salience_aircraft_featured(self, tmp_path):
        mem = _make_memory(tmp_path)
        ac = _ac(flags=["rare_type"])
        cand = _candidate(kind="rare_type", priority=45, ac=ac)
        mem.observe([ac], [cand])
        assert ac.hex in mem.featured_aircraft

    def test_low_salience_aircraft_not_featured(self, tmp_path):
        """Ordinary cruising airliner (filler, pri=15) must not enter featured."""
        mem = _make_memory(tmp_path)
        ac = _ac(hex_="ordinary", type_code="B737", type_desc="Boeing 737",
                 flags=[])
        cand = _candidate(kind="traffic_spotlight", priority=15, ac=ac)
        mem.observe([ac], [cand])
        assert "ordinary" not in mem.featured_aircraft

    def test_emergency_opens_arc(self, tmp_path):
        mem = _make_memory(tmp_path)
        ac = _ac(squawk="7700", emergency="general")
        cand = _candidate(kind="emergency_7700", priority=95, ac=ac)
        mem.observe([ac], [cand])
        assert ac.hex in mem.arcs
        assert mem.arcs[ac.hex].state in ("open", "updating")

    def test_rare_type_opens_arc(self, tmp_path):
        mem = _make_memory(tmp_path)
        ac = _ac(flags=["rare_type"])
        cand = _candidate(kind="rare_type", priority=45, ac=ac)
        mem.observe([ac], [cand])
        assert ac.hex in mem.arcs

    def test_highest_altitude_record_updated(self, tmp_path):
        mem = _make_memory(tmp_path)
        ac_low = _ac(hex_="low", altitude=20000)
        ac_high = _ac(hex_="high", callsign="HIGH001", altitude=42000, flags=["rare_type"])
        cand_low = _candidate(priority=45, ac=ac_low)
        cand_high = _candidate(priority=45, ac=ac_high)
        mem.observe([ac_low, ac_high], [cand_low, cand_high])
        assert mem.records.highest_alt == 42000
        assert mem.records.highest_alt_callsign == "HIGH001"

    def test_notable_type_counted_once_per_hex(self, tmp_path):
        """Each hex should only be counted once even across multiple observe calls."""
        mem = _make_memory(tmp_path)
        ac = _ac(type_code="A388", flags=["rare_type"])
        cand = _candidate(kind="rare_type", priority=45, ac=ac)
        mem.observe([ac], [cand])
        mem.observe([ac], [cand])
        assert mem.records.notable_type_counts.get("A388", 0) == 1

    def test_incident_counted(self, tmp_path):
        mem = _make_memory(tmp_path)
        ac = _ac(squawk="7700", emergency="general")
        cand = _candidate(kind="emergency_7700", priority=95, ac=ac)
        mem.observe([ac], [cand])
        assert mem.records.incident_count == 1


# ── arc dormant / lost signal ─────────────────────────────────────────────────

class TestArcDormancy:
    def test_arc_goes_dormant_when_aircraft_disappears(self, tmp_path):
        mem = _make_memory(tmp_path)
        ac = _ac(flags=["rare_type"])
        cand = _candidate(kind="rare_type", priority=45, ac=ac)
        mem.observe([ac], [cand])
        assert ac.hex in mem.arcs

        # Backdate last_seen so the dormancy threshold is exceeded
        mem.arcs[ac.hex].last_seen -= (ARC_DORMANT_AFTER + 10)

        # observe with the aircraft absent (empty list)
        mem.observe([], [])
        assert mem.arcs[ac.hex].state == "dormant"

    def test_dormant_arc_note_contains_lost_signal(self, tmp_path):
        mem = _make_memory(tmp_path)
        ac = _ac(flags=["rare_type"])
        cand = _candidate(kind="rare_type", priority=45, ac=ac)
        mem.observe([ac], [cand])
        mem.arcs[ac.hex].last_seen -= (ARC_DORMANT_AFTER + 10)
        mem.observe([], [])
        events = mem.arcs[ac.hex].events
        assert any("signal" in e or "coverage" in e for e in events)


# ── prune / decay ─────────────────────────────────────────────────────────────

class TestPrune:
    def test_stale_featured_pruned(self, tmp_path):
        mem = _make_memory(tmp_path)
        ac = _ac(flags=["rare_type"])
        cand = _candidate(kind="rare_type", priority=45, ac=ac)
        mem.observe([ac], [cand])
        assert ac.hex in mem.featured_aircraft

        # Age it out
        from contrail.memory import FEATURED_MAX_AGE
        mem.featured_aircraft[ac.hex].last_seen -= (FEATURED_MAX_AGE + 10)
        mem.prune(time.time())

        assert ac.hex not in mem.featured_aircraft

    def test_dormant_arc_closed_after_close_threshold(self, tmp_path):
        from contrail.memory import ARC_CLOSE_AFTER
        mem = _make_memory(tmp_path)
        ac = _ac(flags=["rare_type"])
        cand = _candidate(kind="rare_type", priority=45, ac=ac)
        mem.observe([ac], [cand])
        mem.arcs[ac.hex].state = "dormant"
        mem.arcs[ac.hex].last_seen -= (ARC_CLOSE_AFTER + 10)
        mem.prune(time.time())
        assert mem.arcs[ac.hex].state == "closed"

    def test_excess_closed_arcs_evicted(self, tmp_path):
        """More than 20 closed arcs: oldest ones should be evicted."""
        mem = _make_memory(tmp_path)
        from contrail.memory import Arc
        now = time.time()
        for i in range(25):
            hex_ = f"dead{i:03d}"
            mem.arcs[hex_] = Arc(
                hex=hex_, callsign=f"EV{i}", type_desc="B737",
                state="closed", events=["landed"],
                opened_at=now - 3600 - i,
                last_seen=now - 3600 - i,
                last_mentioned=0.0,
            )
        mem.prune(now)
        assert len(mem.arcs) <= 20


# ── save → load round-trip ────────────────────────────────────────────────────

class TestPersistence:
    def test_save_load_round_trip(self, tmp_path):
        mem1 = _make_memory(tmp_path)
        ac = _ac(flags=["rare_type"])
        cand = _candidate(kind="rare_type", priority=45, ac=ac)
        mem1.observe([ac], [cand])
        mem1.records.incident_count = 3
        mem1.save()

        mem2 = _make_memory(tmp_path)
        mem2.load()

        assert ac.hex in mem2.featured_aircraft
        assert ac.hex in mem2.arcs
        assert mem2.records.incident_count == 3
        assert mem2.featured_aircraft[ac.hex].callsign == ac.callsign

    def test_load_missing_file_starts_fresh(self, tmp_path):
        mem = _make_memory(tmp_path)
        mem.load()  # no file present
        assert mem.featured_aircraft == {}
        assert mem.arcs == {}

    def test_load_corrupt_file_starts_fresh(self, tmp_path):
        store = tmp_path / "memory_store.json"
        store.write_text("this is not json {{{")
        mem = _make_memory(tmp_path)
        mem.load()
        assert mem.featured_aircraft == {}

    def test_load_wrong_version_starts_fresh(self, tmp_path):
        store = tmp_path / "memory_store.json"
        store.write_text(json.dumps({"version": 99, "featured_aircraft": {}, "arcs": {}, "records": {}}))
        mem = _make_memory(tmp_path)
        mem.load()
        assert mem.featured_aircraft == {}

    def test_atomic_write_uses_tmp_then_replace(self, tmp_path):
        """save() should not leave a .json.tmp file behind."""
        mem = _make_memory(tmp_path)
        mem.save()
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []
        assert (tmp_path / "memory_store.json").exists()


# ── recall — bounded and relevant ────────────────────────────────────────────

class TestRecall:
    def _setup_featured(self, mem: SessionMemory, hex_: str, callsign: str,
                        last_featured_offset: float = -300.0) -> None:
        from contrail.memory import FeaturedAircraft
        now = time.time()
        mem.featured_aircraft[hex_] = FeaturedAircraft(
            hex=hex_, callsign=callsign, type_code="A388",
            type_desc="Airbus A380", angle_used="rare_type",
            alt=35000, track=90.0, gs=480.0,
            first_seen=now - 3600,
            last_featured=now + last_featured_offset,  # negative = in the past
            last_seen=now - 60,
        )

    def test_recall_bounded_max_three(self, tmp_path):
        mem = _make_memory(tmp_path)
        # Force enough aired lines so cadence guard passes
        mem._aired_count = CALLBACK_EVERY_N
        mem._last_callback_at_count = 0

        # Add lots of featured aircraft and records to ensure >3 potential snippets
        for i in range(5):
            self._setup_featured(mem, f"hex{i}", f"FL{i}00")
        mem.records.highest_alt = 43000
        mem.records.highest_alt_callsign = "FL100"
        mem.records.incident_count = 5

        # recall with a focus that matches one of the featured
        ac = _ac(hex_="hex0", callsign="FL000", flags=["rare_type"])
        cand = _candidate(kind="rare_type", priority=45, ac=ac)
        snippets = mem.recall(cand, {})
        assert len(snippets) <= 3

    def test_recall_cadence_guard_suppresses(self, tmp_path):
        """Within CALLBACK_EVERY_N aired lines, recall must return []."""
        mem = _make_memory(tmp_path)
        # aired_count and last_callback same — no lines since last callback
        mem._aired_count = 5
        mem._last_callback_at_count = 5

        self._setup_featured(mem, "abc123", "TST001")
        ac = _ac(hex_="abc123", callsign="TST001", flags=["rare_type"])
        cand = _candidate(kind="rare_type", priority=45, ac=ac)
        snippets = mem.recall(cand, {})
        assert snippets == []

    def test_recall_callback_eligible_after_n_lines(self, tmp_path):
        """After CALLBACK_EVERY_N new lines, recall should return something."""
        mem = _make_memory(tmp_path)
        mem._aired_count = CALLBACK_EVERY_N
        mem._last_callback_at_count = 0  # so lines_since = CALLBACK_EVERY_N

        self._setup_featured(mem, "abc123", "TST001")
        ac = _ac(hex_="abc123", callsign="TST001", flags=["rare_type"])
        cand = _candidate(kind="rare_type", priority=45, ac=ac)
        snippets = mem.recall(cand, {})
        # Should have at least one snippet (the callback on the featured aircraft)
        assert len(snippets) >= 1

    def test_recall_returns_empty_with_no_memory(self, tmp_path):
        mem = _make_memory(tmp_path)
        mem._aired_count = 100
        mem._last_callback_at_count = 0
        snippets = mem.recall(None, {})
        assert snippets == []

    def test_recall_updates_last_callback_count(self, tmp_path):
        mem = _make_memory(tmp_path)
        mem._aired_count = CALLBACK_EVERY_N
        mem._last_callback_at_count = 0
        self._setup_featured(mem, "abc123", "TST001")
        ac = _ac(hex_="abc123", callsign="TST001", flags=["rare_type"])
        cand = _candidate(kind="rare_type", priority=45, ac=ac)
        mem.recall(cand, {})
        # After a successful recall, last_callback_at_count should advance
        assert mem._last_callback_at_count == mem._aired_count

    def test_recall_snippets_are_strings(self, tmp_path):
        mem = _make_memory(tmp_path)
        mem._aired_count = CALLBACK_EVERY_N
        mem._last_callback_at_count = 0
        self._setup_featured(mem, "abc123", "TST001")
        ac = _ac(hex_="abc123", callsign="TST001", flags=["rare_type"])
        cand = _candidate(kind="rare_type", priority=45, ac=ac)
        snippets = mem.recall(cand, {})
        for s in snippets:
            assert isinstance(s, str)
            assert len(s) > 0


# ── helper functions ──────────────────────────────────────────────────────────

class TestHelpers:
    def test_ago_seconds(self):
        assert "s ago" in _ago(30)

    def test_ago_minutes(self):
        assert "m ago" in _ago(300)

    def test_ago_hours(self):
        assert "h ago" in _ago(7200)

    def test_angle_label_known(self):
        assert _angle_label("emergency_7700") == "emergency"

    def test_angle_label_unknown_passthrough(self):
        assert _angle_label("made_up_kind") == "made_up_kind"

    def test_best_kind_for_picks_highest_priority(self):
        ac = _ac()
        low = _candidate(kind="traffic_spotlight", priority=15, ac=ac)
        high = _candidate(kind="emergency_7700", priority=95, ac=ac)
        result = _best_kind_for(ac, [low, high])
        assert result == "emergency_7700"

    def test_best_kind_for_no_candidates(self):
        ac = _ac()
        result = _best_kind_for(ac, [])
        assert result == "traffic_spotlight"  # default fallback
