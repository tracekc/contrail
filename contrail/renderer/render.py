"""Renderer: drives scene.html in headless Chromium and saves screenshots.

The renderer is intentionally dumb. All the broadcast logic (what aircraft are
on screen, what the flight desk is saying, who's being tracked) lives in
``state.json``, which this module writes next to ``scene.html`` before each
capture. The HTML page itself polls that file and redraws.

Two entry points:

- ``capture_once`` — single screenshot, useful for smoke-testing the scene.
- ``capture_loop`` — repeated screenshots into a frames directory, for the
  orchestrator to hand off to ffmpeg.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import logging
import random
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional

from playwright.sync_api import sync_playwright

log = logging.getLogger(__name__)

VIEWPORT = {"width": 1280, "height": 720}
SCENE_DIR = Path(__file__).parent
SCENE_HTML = SCENE_DIR / "scene.html"
STATE_JSON = SCENE_DIR / "state.json"

# How long to let the page settle after load: base map fetch (countries-110m)
# plus the first state.json poll (scene.html polls every 1000ms).
_RENDER_SETTLE_S = 1.5


@contextlib.contextmanager
def _local_server(directory: Path) -> Iterator[str]:
    """Serve `directory` over plain HTTP on an ephemeral local port.

    scene.html fetches ./state.json with the page's `fetch()` API, which
    Chromium refuses to do for file:// pages ("URL scheme 'file' is not
    supported"). Serving the directory over http://127.0.0.1 sidesteps that
    restriction without requiring any change to the self-contained HTML.
    """
    handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(
        *args, directory=str(directory), **kwargs
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}/scene.html"
    finally:
        server.shutdown()
        server.server_close()


def _write_state(state: dict[str, Any], state_path: Path = STATE_JSON) -> None:
    """Write state.json next to scene.html so the page's relative fetch finds it."""
    state_path.write_text(json.dumps(state), encoding="utf-8")


def capture_once(state: dict[str, Any], out_path: str) -> None:
    """Render a single frame of the broadcast scene to a PNG.

    Writes ``state`` to state.json beside scene.html, loads scene.html in
    headless Chromium at 1280x720, waits for the map and first state poll to
    render, then screenshots to ``out_path``.
    """
    _write_state(state)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport=VIEWPORT)
            with _local_server(SCENE_DIR) as url:
                page.goto(url)
                page.wait_for_timeout(int(_RENDER_SETTLE_S * 1000))
                page.screenshot(path=out_path)
        finally:
            browser.close()

    log.info("Saved frame to %s", out_path)


def capture_loop(
    state_path: str,
    frames_dir: str,
    fps: int = 2,
    duration_s: Optional[float] = None,
) -> None:
    """Repeatedly screenshot the scene into frames_dir for ffmpeg to consume.

    Reads the freshest state from ``state_path`` on every tick (the
    orchestrator is expected to keep overwriting that file) and copies it into
    scene.html's own state.json so the loaded page picks it up. Frames are
    written as zero-padded sequential PNGs (frame_000001.png, ...) so ffmpeg's
    image2 demuxer can glob them directly.

    Runs until ``duration_s`` elapses, or forever if ``duration_s`` is None.
    """
    state_path = Path(state_path)
    out_dir = Path(frames_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    interval_s = 1.0 / fps
    start = time.monotonic()
    frame_idx = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport=VIEWPORT)

            # Seed state.json before first load so the scene isn't empty.
            if state_path.exists():
                _write_state(json.loads(state_path.read_text(encoding="utf-8")))

            with _local_server(SCENE_DIR) as url:
                page.goto(url)
                page.wait_for_timeout(int(_RENDER_SETTLE_S * 1000))

                while duration_s is None or (time.monotonic() - start) < duration_s:
                    tick_start = time.monotonic()

                    # Pick up whatever the orchestrator last wrote, if anything.
                    if state_path.exists():
                        try:
                            latest = json.loads(state_path.read_text(encoding="utf-8"))
                            _write_state(latest)
                        except (OSError, json.JSONDecodeError) as exc:
                            log.warning("Could not read %s: %s", state_path, exc)

                    frame_idx += 1
                    frame_path = out_dir / f"frame_{frame_idx:06d}.png"
                    page.screenshot(path=str(frame_path))

                    elapsed = time.monotonic() - tick_start
                    sleep_for = interval_s - elapsed
                    if sleep_for > 0:
                        time.sleep(sleep_for)
        finally:
            browser.close()

    log.info("Captured %d frames to %s", frame_idx, out_dir)


def _build_sample_state() -> dict[str, Any]:
    """Build a realistic sample state: ~120 aircraft within a London-centered
    regional bounding box, plus one tracked emergency (BAW286), for the
    smoke-test frame. Mirrors the orchestrator's real feed, which now scopes
    to a single ~250nm region rather than the whole world.
    """
    random.seed(42)

    bounds = [-8.0, 47.0, 6.0, 57.0]  # [west, south, east, north]
    camera = [-4.5, 49.5, 1.5, 55.5]  # tight box around BAW286, for the event view

    aircraft = []
    # Scatter aircraft within the regional bounding box so they read as a
    # spread-out, zoomed-in scene rather than a single piled-up cluster.
    for _ in range(120):
        lat = random.uniform(bounds[1], bounds[3])
        lon = random.uniform(bounds[0], bounds[2])
        aircraft.append(
            {
                "lat": round(lat, 3),
                "lon": round(lon, 3),
                "track": round(random.uniform(0, 360), 1),
                "emergency": False,
                "focus": False,
            }
        )

    # The tracked emergency flight, also present in the aircraft list, placed
    # inside the same region.
    tracked_aircraft = {
        "lat": 52.5,
        "lon": -1.5,
        "track": 95.0,
        "emergency": True,
        "focus": True,
        "callsign": "BAW286",
    }
    aircraft.append(tracked_aircraft)

    return {
        "generated": time.time(),
        "viewers": 1963,
        "airborne": 11482,
        "busiest": "LHR · 142/hr",
        "segment": "event",
        "caption": (
            "A British Airways 777 has squawked 7700 over the Midlands "
            "and is turning back toward London."
        ),
        "bounds": bounds,
        "camera": camera,
        "tracking": {
            "callsign": "BAW286",
            "type": "Boeing 777-300ER",
            "route": "San Francisco → London",
            "alt": 37000,
            "speed": 488,
            "squawk": "7700",
            "emergency": True,
            "lat": 52.5,
            "lon": -1.5,
        },
        "alerts": [
            "QFA7 ultra-long-haul over the Pacific",
            "loss-of-signal near Reykjavik",
        ],
        "aircraft": aircraft,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    sample_state = _build_sample_state()
    out_file = Path(__file__).parent / "test_frame.png"
    capture_once(sample_state, str(out_file))
    print(f"Wrote {out_file}")
