#!/usr/bin/env python3
"""Contrail — live orchestrator (Phases 2-5).

Ties the pipeline together:
    ingest -> enrich -> detect -> director -> narrate -> state.json -> renderer -> stream

Modes:
    python run_live.py                 full local: speaks each line, writes state.json
    python run_live.py --silent        director only (no TTS); needs only ANTHROPIC_API_KEY
    python run_live.py --once          a single cycle then exit
    python run_live.py --stream-test   render+narrate to a local MP4 (validate before live)
    python run_live.py --stream        go live to YouTube (needs YOUTUBE_STREAM_KEY)
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from contrail.config import Config
from contrail.data.regions import REGIONS
from contrail.detect import EventDetector
from contrail.director import Director, ScriptLine
from contrail.enrich import enrich_all, lookup_aircraft, lookup_route
from contrail.enrich_cache import EnrichmentCache
from contrail import photo_cache
from contrail.ingest import AdsbClient
from contrail.memory import SessionMemory
from contrail.models import Aircraft
from contrail.narrate import Narrator, audio_duration_seconds, estimate_speech_seconds, play

EMERGENCY_SQUAWKS = ("7700", "7600")
STATE_PATH = Path(__file__).parent / "contrail" / "renderer" / "state.json"
MAX_PLOTTED = 200
_STATE_LOCK = threading.Lock()

# Globe "travel beat": when rotating regions we pull all the way out to the world
# before zooming into the next region. Equirectangular-friendly world extent.
WORLD_BOUNDS = [-170.0, -58.0, 178.0, 74.0]

# How long to dwell on a region before rotating. Jittered so it's not clockwork;
# REGION_ROTATE_SECONDS env forces a fixed value (handy for testing).
ROTATE_MIN_S = 600
ROTATE_MAX_S = 900

_TRAVEL_LINES = [
    "That's the picture over {old} for now. Let's pull back and cross to {new}.",
    "We'll leave {old} there, and travel across to {new}.",
    "Time to move on from {old} — taking the long way around to {new}.",
    "Pulling out to the wider world now, and setting our sights on {new}.",
    "We've spent a while over {old}. Let's swing the camera around to {new}.",
]


def _next_rotation_delay() -> float:
    override = os.getenv("REGION_ROTATE_SECONDS")
    if override:
        try:
            return float(override)
        except ValueError:
            pass
    return random.uniform(ROTATE_MIN_S, ROTATE_MAX_S)

log = logging.getLogger("contrail.live")

_enrich_cache = EnrichmentCache()


def _patch_tracking(ac_hex: str, fields: dict) -> None:
    """Called from background enrichment threads to push data into state.json immediately.

    Only applies if the currently tracked aircraft still matches ac_hex, so
    a stale fetch from a previous aircraft never corrupts the active panel.
    """
    with _STATE_LOCK:
        if not STATE_PATH.exists():
            return
        try:
            state = json.loads(STATE_PATH.read_text())
        except Exception:
            return
        t = state.get("tracking")
        if not t or t.get("hex") != ac_hex:
            return
        t.update(fields)
        # If a photo_url just arrived and we don't have a local path yet, download now.
        photo_url = fields.get("photo_url")
        if photo_url and not t.get("photo_path"):
            ph = photo_cache.get_photo_path(ac_hex, photo_url)
            t["photo_path"] = str(ph) if ph else None
        STATE_PATH.write_text(json.dumps(state))
        log.info("enrichment patch applied: hex=%s fields=%s", ac_hex, list(fields))


def _dedupe(aircraft: list[Aircraft]) -> list[Aircraft]:
    seen: dict[str, Aircraft] = {}
    for ac in aircraft:
        if ac.hex and ac.hex not in seen:
            seen[ac.hex] = ac
    return list(seen.values())


def _fetch(client: AdsbClient, region: tuple) -> tuple[list[Aircraft], int]:
    """Region traffic plus GLOBAL emergencies (7700/7600 are not region-limited,
    so a developing incident anywhere on earth can still break into the show)."""
    emergencies: list[Aircraft] = []
    for code in EMERGENCY_SQUAWKS:
        emergencies += client.get_squawk(code)
    region_ac = client.get_region(*region)
    aircraft = enrich_all(_dedupe(emergencies + region_ac))
    return aircraft, len(region_ac)


def _speak(line, narrator, streamer) -> None:
    """Play/queue a line's audio (or just pace the loop when silent)."""
    if line and narrator:
        try:
            path = narrator.synth(line.text)
            if streamer:
                streamer.enqueue_audio(path)
                time.sleep(estimate_speech_seconds(line.text))
            else:
                play(path)  # blocking; paces the loop
        except Exception as exc:
            log.warning("TTS failed: %s", exc)
            time.sleep(estimate_speech_seconds(line.text))
    else:
        time.sleep(estimate_speech_seconds(line.text) if line else 5)


def _region_bounds(region) -> list[float]:
    """[west, south, east, north] for the coverage circle, so the renderer can
    zoom the map to where the traffic actually is instead of showing the world."""
    lat, lon, r_nm = region
    dlat = r_nm / 60.0
    dlon = r_nm / (60.0 * max(math.cos(math.radians(lat)), 0.1))
    return [lon - dlon, lat - dlat, lon + dlon, lat + dlat]


# Radius (nm) of the zoomed-in box around the narrated flight. Our basemap
# (coarse country outlines) has no ground detail at city zoom, so we stay at a
# regional zoom where coastlines are visible — the chase-cam then slides those
# outlines past the centered subject for a gentle motion cue. (Tighter, adsb.lol
# -style motion would need a detailed tile basemap; deferred.)
FOCUS_RADIUS_NM_EVENT = 40.0    # emergencies: closer
FOCUS_RADIUS_NM_AMBIENT = 70.0  # ordinary subjects: more context


def _write_state(aircraft, line, candidates, region_count, bounds=None) -> None:
    focus_hex = line.aircraft.hex if (line and line.aircraft) else None

    # Keep the focus aircraft from being dropped by the MAX_PLOTTED cap.
    positioned = [a for a in aircraft if a.lat is not None and a.lon is not None]
    positioned.sort(key=lambda a: 0 if a.hex == focus_hex else 1)
    plotted = [
        {
            "id": a.hex, "lat": a.lat, "lon": a.lon, "track": a.track or 0,
            "gs": round(a.ground_speed) if a.ground_speed else 0,
            "emergency": (a.emergency or "none").lower() != "none"
            or a.squawk in EMERGENCY_SQUAWKS,
            "focus": a.hex == focus_hex,
        }
        for a in positioned
    ][:MAX_PLOTTED]

    tracking = None
    if line and line.aircraft and line.aircraft.lat is not None:
        ac = line.aircraft
        hex_ = ac.hex or ""

        # Callbacks: when a background fetch completes it calls _patch_tracking
        # immediately, so enrichment appears on the panel without waiting for the
        # next narration cycle.
        def _on_aircraft(data: dict, _hex=hex_) -> None:
            _patch_tracking(_hex, {
                "registration": data.get("registration"),
                "built_year": data.get("built_year"),
                "operator": data.get("operator"),
                "photo_url": data.get("photo_url"),
                "photo_credit": data.get("photo_credit"),
            })

        def _on_route(data: dict, _hex=hex_) -> None:
            o_name = data.get("origin_name") or data.get("origin_iata") or data.get("origin_icao")
            d_name = data.get("dest_name") or data.get("dest_iata") or data.get("dest_icao")
            _patch_tracking(_hex, {
                "route": f"{o_name} → {d_name}" if o_name and d_name else None,
                "origin_iata": data.get("origin_iata"),
                "dest_iata": data.get("dest_iata"),
            })

        ac_data = lookup_aircraft(hex_, _enrich_cache, on_complete=_on_aircraft) if hex_ else None
        rt_data = lookup_route(ac.callsign, _enrich_cache, on_complete=_on_route)

        # Build route display string from enriched names or fall back to IATAs.
        route_str = None
        if rt_data:
            o_name = rt_data.get("origin_name") or rt_data.get("origin_iata") or rt_data.get("origin_icao")
            d_name = rt_data.get("dest_name") or rt_data.get("dest_iata") or rt_data.get("dest_icao")
            if o_name and d_name:
                route_str = f"{o_name} → {d_name}"

        photo_url = (ac_data or {}).get("photo_url")
        photo_path = None
        if hex_ and photo_url:
            p = photo_cache.get_photo_path(hex_, photo_url)
            photo_path = str(p) if p else None

        tracking = {
            "hex": hex_,
            "callsign": ac.callsign or hex_,
            "type": ac.type_desc or ac.type_code or "",
            "route": route_str,
            "alt": ac.altitude,
            "speed": round(ac.ground_speed) if ac.ground_speed else None,
            "squawk": ac.squawk,
            "emergency": line.segment == "event",
            "lat": ac.lat, "lon": ac.lon,
            # enrichment fields (None until background fetch completes)
            "registration": (ac_data or {}).get("registration"),
            "built_year": (ac_data or {}).get("built_year"),
            "operator": (ac_data or {}).get("operator"),
            "origin_iata": (rt_data or {}).get("origin_iata"),
            "dest_iata": (rt_data or {}).get("dest_iata"),
            "photo_path": photo_path,
            "photo_credit": (ac_data or {}).get("photo_credit"),
        }

    alerts = [c.headline for c in candidates
              if not (c.aircraft and c.aircraft.hex == focus_hex)][:6]

    # Camera: follow whatever aircraft we're narrating so its green/red highlight
    # is actually visible. Emergencies zoom in tighter; ordinary subjects get a
    # slightly wider box. With no aircraft subject we keep the calm region view.
    camera = bounds
    if line and line.aircraft and line.aircraft.lat is not None:
        flat, flon = line.aircraft.lat, line.aircraft.lon
        radius_nm = (FOCUS_RADIUS_NM_EVENT if line.segment == "event"
                     else FOCUS_RADIUS_NM_AMBIENT)
        span = radius_nm / 60.0
        dlon = span / max(math.cos(math.radians(flat)), 0.1)
        camera = [flon - dlon, flat - span, flon + dlon, flat + span]

    state = {
        "generated": time.time(),
        "viewers": 0,
        "airborne": region_count,
        "busiest": "",
        "segment": line.segment if line else "ambient",
        "caption": line.text if line else "",
        "tracking": tracking,
        "alerts": alerts,
        "bounds": bounds,            # [west, south, east, north] full region
        "camera": camera,            # current view box (tightens on notable flights)
        "aircraft": plotted,
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _STATE_LOCK:
        STATE_PATH.write_text(json.dumps(state))

    # Per-cycle focus diagnostics (appended, so every cycle is captured).
    n_focus = sum(1 for a in plotted if a["focus"])
    log.info(
        "cycle: seg=%s focus_hex=%r in_plotted=%s n_focus=%d caption=%.60s",
        state["segment"], focus_hex,
        any(a["id"] == focus_hex for a in plotted) if focus_hex else False,
        n_focus, state["caption"],
    )


def _pipeline_loop(stop: threading.Event, cfg: Config, *, silent: bool,
                   streamer=None, once: bool = False) -> None:
    """The brains: poll -> detect -> direct -> narrate -> state.json.

    Local mode plays audio inline (paces the loop). Stream mode hands audio to
    the streamer and lets speech duration pace the loop.

    Streaming path (streamer is not None AND narrator is not None) uses a
    one-step lookahead: while clip N plays, we generate (LLM + TTS) clip N+1
    so it is ready to enqueue immediately when N finishes. Sleep is based on
    the clip's ACTUAL audio duration (via ffprobe) rather than a text estimate,
    eliminating the two sources of silent padding: duration mismatch and serial
    LLM/synth latency.
    """
    client = AdsbClient(cfg.adsb_source)
    detector = EventDetector()
    memory = SessionMemory()
    memory.load()
    director = Director(memory=memory)
    narrator = None if silent else Narrator()
    _last_memory_save = time.time()
    _MEMORY_SAVE_INTERVAL = 300  # save memory every 5 minutes

    # Region rotation state.
    region_idx = 0
    region = REGIONS[region_idx]
    cur_region = region.tuple
    region_started = time.time()
    rotate_after = _next_rotation_delay()

    bounds = _region_bounds(cur_region)
    aircraft, region_count = _fetch(client, cur_region)
    last_poll = time.time()
    emergency_redirect = False  # True while we've shifted the map to a global emergency

    # ── streaming pipeline state ──────────────────────────────────────────────
    # pending holds the (line, aircraft_snapshot, candidates_snapshot,
    # region_count_snapshot, bounds_snapshot, audio_path) for the line that has
    # been LLM+TTS-generated but not yet enqueued. None on the first cycle.
    # Only used when streamer is not None and narrator is not None.
    _PendingClip = tuple  # (line, aircraft, candidates, region_count, bounds, audio_path)
    pending: _PendingClip | None = None
    # ─────────────────────────────────────────────────────────────────────────

    _streaming = (streamer is not None and narrator is not None)

    while not stop.is_set():
        now = time.time()

        # Rotate to the next region on a timer — but never while an emergency is
        # being tracked, so we don't abandon a developing incident mid-story.
        if (not once and not director._incident.active
                and now - region_started >= rotate_after):
            old_name = region.name
            region_idx = (region_idx + 1) % len(REGIONS)
            region = REGIONS[region_idx]
            cur_region = region.tuple

            # Globe beat: one line on the world map while we "travel" there. The
            # renderer eases the camera out to WORLD_BOUNDS, then into the next
            # region on the following cycle.
            travel = ScriptLine(
                text=random.choice(_TRAVEL_LINES).format(old=old_name, new=region.name),
                segment="travel", priority=0)
            _write_state(aircraft, travel, [], region_count, WORLD_BOUNDS)
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"[{ts}Z] (travel) {travel.text}")

            # Travel beat: flush any pending lookahead (it was for a different
            # region/context) and use synchronous _speak for this one-off clip.
            # After _speak returns we resume the pipeline from a clean state.
            if _streaming:
                pending = None
            _speak(travel, narrator, streamer)

            # Arrive: refetch the new region and resume.
            bounds = _region_bounds(cur_region)
            new_ac, new_rc = _fetch(client, cur_region)
            if new_ac:
                aircraft, region_count = new_ac, new_rc
            last_poll = time.time()
            region_started = time.time()
            rotate_after = _next_rotation_delay()
            continue

        if now - last_poll >= cfg.poll_interval:
            new_ac, new_rc = _fetch(client, cur_region)
            last_poll = now
            if new_ac:  # keep the last good snapshot if a fetch fails (e.g. 429)
                aircraft, region_count = new_ac, new_rc

        ctx = {"region_count": region_count, "region_name": region.name}
        candidates = detector.detect(aircraft, ctx)
        memory.observe(aircraft, candidates)
        line = director.next_line(candidates, ctx, aircraft)

        # Periodic memory save + prune (every _MEMORY_SAVE_INTERVAL seconds).
        if now - _last_memory_save >= _MEMORY_SAVE_INTERVAL:
            memory.prune(now)
            memory.save()
            _last_memory_save = now

        # ── global emergency redirect ─────────────────────────────────────────
        # When the incident tracker locks onto an emergency aircraft that lies
        # outside the current map view, shift the whole channel to that location:
        # update bounds (what the map renders), cur_region (what the next fetch
        # pulls), and do an immediate refetch so the area fills with real traffic.
        # When the incident clears, return to the scheduled named region.
        eac = director._incident.last_ac if director._incident.active else None
        if eac and eac.lat is not None and eac.lon is not None:
            w, s, e, n = bounds
            outside = not (s <= eac.lat <= n and w <= eac.lon <= e)
            if outside and not emergency_redirect:
                log.info(
                    "emergency redirect: incident at %.2f,%.2f (%s) outside current region — shifting",
                    eac.lat, eac.lon, eac.hex,
                )
                cur_region = (eac.lat, eac.lon, 300.0)
                bounds = _region_bounds(cur_region)
                new_ac, new_rc = _fetch(client, cur_region)
                if new_ac:
                    aircraft, region_count = new_ac, new_rc
                last_poll = time.time()
                emergency_redirect = True

                # Flush the pending lookahead — it was computed with the old
                # bounds/region and must not be enqueued. This cycle's `line`
                # (already reflecting the incident aircraft) is re-synthesized
                # below with the corrected bounds; the incident becomes visible
                # on the next aired clip (one clip of lag, ~5s).
                if _streaming:
                    pending = None

        if not director._incident.active and emergency_redirect:
            log.info("emergency redirect ended; returning to %s", region.name)
            cur_region = region.tuple
            bounds = _region_bounds(cur_region)
            new_ac, new_rc = _fetch(client, cur_region)
            if new_ac:
                aircraft, region_count = new_ac, new_rc
            last_poll = time.time()
            region_started = time.time()
            emergency_redirect = False
            # Also flush pending so the next cycle starts fresh from the
            # restored region.
            if _streaming:
                pending = None
        # ─────────────────────────────────────────────────────────────────────

        if once:
            _write_state(aircraft, line, candidates, region_count, bounds)
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            if line:
                tag = "EVENT" if line.segment == "event" else line.segment
                print(f"[{ts}Z] ({tag}) {line.text}")
            else:
                print(f"[{ts}Z] (nothing to say this cycle)")
            return

        # ── streaming one-step lookahead path ─────────────────────────────────
        # When pending is None (first cycle, or just flushed by a travel beat /
        # emergency redirect), Step 1 airs nothing and Step 2 primes `line` for
        # the next cycle — costing one short priming wait, not a dropped line.
        if _streaming:
            # ── Step 1: air the pending clip (if any) ────────────────────────
            clip_started: float | None = None
            clip_dur: float | None = None

            if pending is not None:
                p_line, p_aircraft, p_candidates, p_rc, p_bounds, p_audio_path = pending
                _write_state(p_aircraft, p_line, p_candidates, p_rc, p_bounds)
                streamer.enqueue_audio(p_audio_path)
                clip_started = time.monotonic()
                clip_dur = audio_duration_seconds(
                    p_audio_path,
                    fallback=estimate_speech_seconds(p_line.text) if p_line else 5.0,
                )
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                if p_line:
                    tag = "EVENT" if p_line.segment == "event" else p_line.segment
                    print(f"[{ts}Z] ({tag}) {p_line.text}")
                else:
                    print(f"[{ts}Z] (nothing to say this cycle)")
                pending = None

            # ── Step 2: prepare NEXT clip (LLM + TTS) during playback ────────
            # `line` and the associated aircraft/candidates snapshot were already
            # computed at the top of this iteration — they represent the NEXT
            # line to air. Synth it now so it is ready when the current clip ends.
            next_audio_path: str | None = None
            if line and narrator:
                try:
                    next_audio_path = narrator.synth(line.text)
                except Exception as exc:
                    log.warning("TTS failed for next line: %s", exc)
            # Freeze a snapshot of the state that goes with this line.
            pending = (line, list(aircraft), list(candidates), region_count, bounds, next_audio_path)

            # ── Step 3: sleep for the remainder of the current clip ───────────
            if clip_started is not None and clip_dur is not None:
                elapsed = time.monotonic() - clip_started
                sleep_for = max(0.0, clip_dur - elapsed)
                if sleep_for > 0:
                    stop.wait(timeout=sleep_for)
            else:
                # Nothing was aired this cycle (first cycle or just-flushed):
                # pace with a short wait so the loop isn't spinning while we
                # also don't have a clip queued yet.
                stop.wait(timeout=5.0)
            continue
        # ─────────────────────────────────────────────────────────────────────

        # Non-streaming / silent path — unchanged.
        _write_state(aircraft, line, candidates, region_count, bounds)

        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        if line:
            tag = "EVENT" if line.segment == "event" else line.segment
            print(f"[{ts}Z] ({tag}) {line.text}")
        else:
            print(f"[{ts}Z] (nothing to say this cycle)")

        _speak(line, narrator, streamer)


def _run_session(cfg: Config, *, target: str, max_session_s=None,
                 test_duration_s: float = 45.0) -> float:
    """One streamer session: pipeline thread + streamer on the main thread.
    Returns how many seconds it ran. Lets KeyboardInterrupt propagate."""
    from contrail.stream import LiveStreamer
    streamer = LiveStreamer(target=target, test_duration_s=test_duration_s,
                            max_session_s=max_session_s)
    stop = threading.Event()
    worker = threading.Thread(
        target=_pipeline_loop, args=(stop, cfg),
        kwargs={"silent": False, "streamer": streamer}, daemon=True,
    )
    worker.start()
    started = time.time()
    try:
        streamer.run()  # blocks until max_session_s/test duration, crash, or ^C
    finally:
        stop.set()
        streamer.stop()
        worker.join(timeout=3)
    return time.time() - started


def _run_live_supervised(cfg: Config) -> None:
    """24/7 live: restart the session on crash or on a scheduled interval (to
    recycle Chromium/ffmpeg before they leak), with backoff on rapid failures."""
    restart_s = float(os.getenv("STREAM_RESTART_SECONDS") or "21600")  # 6h default
    backoff = 5.0
    while True:
        try:
            ran = _run_session(cfg, target="rtmp", max_session_s=restart_s)
        except KeyboardInterrupt:
            print("\nstopped.")
            return
        except Exception as exc:  # crash: log and restart
            log.error("stream session crashed: %s", exc)
            ran = 0.0
        if ran < 30:  # crashed fast — back off so we don't hammer
            backoff = min(backoff * 2, 120)
            print(f"session ended after {ran:.0f}s; restarting in {backoff:.0f}s")
            time.sleep(backoff)
        else:        # healthy run or scheduled recycle — restart promptly
            backoff = 5.0
            print(f"session ran {ran:.0f}s; recycling and restarting")
            time.sleep(2)


def main() -> None:
    p = argparse.ArgumentParser(description="Contrail live orchestrator")
    p.add_argument("--once", action="store_true", help="single cycle then exit")
    p.add_argument("--silent", action="store_true",
                   help="director only, no TTS (needs only ANTHROPIC_API_KEY)")
    p.add_argument("--stream", action="store_true", help="go live to YouTube RTMP")
    p.add_argument("--stream-test", action="store_true",
                   help="render+narrate to a local MP4 instead of RTMP")
    args = p.parse_args()

    cfg = Config.load()
    logging.basicConfig(level=getattr(logging, cfg.log_level.upper(), logging.INFO),
                        format="%(levelname)s %(name)s: %(message)s")

    streaming = args.stream or args.stream_test
    print(f"Contrail live — source={cfg.adsb_source} "
          f"mode={'stream' if streaming else ('silent' if args.silent else 'local')}")

    if not streaming:
        stop = threading.Event()
        try:
            _pipeline_loop(stop, cfg, silent=args.silent, once=args.once)
        except KeyboardInterrupt:
            print("\nstopped.")
        return

    if args.stream:
        # 24/7 live: supervised, auto-restarting.
        _run_live_supervised(cfg)
        return

    # --stream-test: one-shot local MP4 for validation.
    try:
        _run_session(cfg, target="test", test_duration_s=45.0)
    except KeyboardInterrupt:
        print("\nstopping…")


if __name__ == "__main__":
    main()
