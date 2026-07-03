"""Streamer: composite the live scene + narration audio and push to RTMP.

Architecture (one long-lived ffmpeg process, two real-time inputs):

    Playwright page (scene.html)  --PNG frames-->  ffmpeg stdin  (image2pipe)
    narration mp3 queue + silence --s16le PCM-->   ffmpeg FIFO   (rawaudio)
                                                       |
                                                       v
                                          libx264 + aac --> FLV --> RTMP (YouTube)

The orchestrator keeps overwriting state.json (which the page polls) and calls
``enqueue_audio(path)`` whenever a narration clip is ready. The streamer paces
both inputs to wall-clock so the muxer stays roughly in sync; frame-accurate
audio sync is unnecessary here since the on-screen caption is the source of
truth and narration is intermittent.

Use ``target="rtmp"`` for live, or ``target="test"`` to write a local MP4 for
validation before going live.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import subprocess
import tempfile
import threading
import time
from typing import Optional

from playwright.sync_api import sync_playwright

from .renderer.render import SCENE_DIR, _local_server, _write_state

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 1280, 720
SAMPLE_RATE = 44100
CHANNELS = 1
BYTES_PER_SEC = SAMPLE_RATE * CHANNELS * 2  # s16le
_SILENCE_CHUNK_S = 0.1
_SILENCE = b"\x00" * int(BYTES_PER_SEC * _SILENCE_CHUNK_S)

# If ffmpeg stops draining our frame writes for this long, something downstream
# has stalled (typically the RTMP socket to YouTube blocking on a dead/slow
# connection). A blocking stdin.write() has no timeout of its own, so without
# this watchdog the frame thread — and therefore the whole session — can hang
# forever even though the process looks "running" to systemd, silently
# defeating the periodic supervisor restart. 20s is generous headroom over any
# real fps interval (12fps = ~83ms/frame).
STALL_TIMEOUT_S = 20.0
_WATCHDOG_POLL_S = 3.0


def ffmpeg_ok() -> bool:
    """True if ffmpeg actually runs (catches the broken-dylib case)."""
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _decode_to_pcm(path: str) -> bytes:
    """Decode any audio file to raw s16le mono @ SAMPLE_RATE via ffmpeg."""
    r = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-i", path, "-f", "s16le",
         "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "-"],
        capture_output=True,
    )
    return r.stdout


def _stream_fps() -> int:
    """Target capture fps — env override lets you tune for the host machine."""
    try:
        return int(os.getenv("STREAM_FPS") or "4")
    except ValueError:
        return 4


class LiveStreamer:
    def __init__(self, target: str = "rtmp", fps: int | None = None,
                 test_out: str = "contrail_stream_test.mp4",
                 test_duration_s: float = 12.0,
                 max_session_s: float | None = None) -> None:
        self.target = target
        self.fps = fps if fps is not None else _stream_fps()
        self.test_out = test_out
        self.test_duration_s = test_duration_s
        # For live (rtmp): stop cleanly after this many seconds so the supervisor
        # can restart us, which recycles Chromium/ffmpeg before they leak over
        # days. None = run until stopped/crashed.
        self.max_session_s = max_session_s
        self._audio_q: "queue.Queue[str]" = queue.Queue()
        self._stop = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._fifo_path: Optional[str] = None
        self._last_frame_write = time.monotonic()

    # ── public ────────────────────────────────────────────────
    def enqueue_audio(self, path: str) -> None:
        self._audio_q.put(path)

    def run(self) -> None:
        """Blocking. Starts ffmpeg + audio thread + frame loop until stopped."""
        if not ffmpeg_ok():
            raise RuntimeError(
                "ffmpeg is not runnable (broken install?). On macOS try: "
                "brew reinstall ffmpeg"
            )
        self._fifo_path = _make_fifo()
        self._proc = subprocess.Popen(
            self._ffmpeg_cmd(self._fifo_path), stdin=subprocess.PIPE
        )
        self._last_frame_write = time.monotonic()
        audio_thread = threading.Thread(target=self._audio_loop, daemon=True)
        audio_thread.start()
        watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        watchdog_thread.start()
        try:
            self._frame_loop()
        finally:
            self.stop()
            audio_thread.join(timeout=2)
            watchdog_thread.join(timeout=2)
            self._cleanup()

    def stop(self) -> None:
        self._stop.set()

    def _watchdog_loop(self) -> None:
        """Force-kill ffmpeg if frame writes stall (e.g. RTMP socket hung on a
        dead connection). This unblocks the frame thread's stuck stdin.write()
        with a clean BrokenPipeError, letting the normal session-teardown and
        supervisor-restart path recover instead of hanging forever."""
        while not self._stop.wait(_WATCHDOG_POLL_S):
            stale_for = time.monotonic() - self._last_frame_write
            if stale_for > STALL_TIMEOUT_S:
                log.error(
                    "frame writes stalled for %.0fs (ffmpeg not draining stdin — "
                    "likely a stuck RTMP connection); killing ffmpeg to force a restart",
                    stale_for,
                )
                if self._proc:
                    with contextlib.suppress(Exception):
                        self._proc.kill()
                return

    # ── ffmpeg command ────────────────────────────────────────
    def _ffmpeg_cmd(self, fifo: str) -> list[str]:
        cmd = [
            "ffmpeg", "-y",
            # JPEG frames from native Skia renderer or Playwright screenshot
            "-f", "image2pipe", "-vcodec", "mjpeg", "-framerate", str(self.fps), "-i", "pipe:0",
            # audio: raw PCM from the FIFO
            "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "-i", fifo,
            "-c:v", "libx264", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", "-g", str(self.fps * 2), "-b:v", "4500k",
            "-c:a", "aac", "-b:a", "128k", "-ar", str(SAMPLE_RATE),
        ]
        if self.target == "rtmp":
            url = os.getenv("YOUTUBE_RTMP_URL", "rtmp://a.rtmp.youtube.com/live2")
            key = os.getenv("YOUTUBE_STREAM_KEY", "")
            if not key:
                raise RuntimeError("YOUTUBE_STREAM_KEY is empty; cannot go live")
            cmd += ["-f", "flv", f"{url.rstrip('/')}/{key}"]
        else:  # test: local mp4
            cmd += ["-t", str(self.test_duration_s), "-f", "mp4", self.test_out]
        return cmd

    # ── frame producer ────────────────────────────────────────
    def _session_end(self):
        """Wall-clock deadline for this session, or None to run until stopped."""
        if self.target == "test":
            return time.monotonic() + self.test_duration_s
        if self.max_session_s:
            return time.monotonic() + self.max_session_s
        return None

    def _frame_loop(self) -> None:
        """Dispatch to the browser or native frame source.

        RENDERER=native draws frames directly with Skia (no Chromium, no
        screenshot IPC); anything else (default) uses the Playwright path.
        """
        if os.getenv("RENDERER", "browser").strip().lower() == "native":
            self._frame_loop_native()
        else:
            self._frame_loop_browser()

    def _frame_loop_native(self) -> None:
        from .renderer.native import NativeRenderer
        from .renderer.render import STATE_JSON

        interval = 1.0 / self.fps
        deadline = time.monotonic()
        end = self._session_end()
        renderer = NativeRenderer()
        try:
            while not self._stop.is_set():
                if end and time.monotonic() >= end:
                    break
                renderer.poll_state(STATE_JSON)
                frame = renderer.render_frame(quality=92)
                try:
                    self._proc.stdin.write(frame)
                except (BrokenPipeError, ValueError):
                    break
                self._last_frame_write = time.monotonic()
                deadline += interval
                sleep = deadline - time.monotonic()
                if sleep > 0:
                    time.sleep(sleep)
        finally:
            if self._proc and self._proc.stdin:
                with contextlib.suppress(Exception):
                    self._proc.stdin.close()

    def _frame_loop_browser(self) -> None:
        interval = 1.0 / self.fps
        deadline = time.monotonic()
        end = self._session_end()
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
                with _local_server(SCENE_DIR) as url:
                    page.goto(url)
                    page.wait_for_timeout(1500)
                    while not self._stop.is_set():
                        if end and time.monotonic() >= end:
                            break
                        png = page.screenshot(type="jpeg", quality=75)
                        try:
                            self._proc.stdin.write(png)
                        except (BrokenPipeError, ValueError):
                            break
                        self._last_frame_write = time.monotonic()
                        deadline += interval
                        sleep = deadline - time.monotonic()
                        if sleep > 0:
                            time.sleep(sleep)
            finally:
                browser.close()
                if self._proc and self._proc.stdin:
                    with contextlib.suppress(Exception):
                        self._proc.stdin.close()

    # ── audio producer ────────────────────────────────────────
    def _audio_loop(self) -> None:
        # Opening for write blocks until ffmpeg opens the read end — that's the
        # handshake that lets ffmpeg start consuming.
        with open(self._fifo_path, "wb") as fifo:
            deadline = time.monotonic()
            while not self._stop.is_set():
                try:
                    clip = self._audio_q.get_nowait()
                except queue.Empty:
                    clip = None
                try:
                    if clip:
                        pcm = _decode_to_pcm(clip)
                        if pcm:
                            fifo.write(pcm)
                            deadline += len(pcm) / BYTES_PER_SEC
                    else:
                        fifo.write(_SILENCE)
                        deadline += _SILENCE_CHUNK_S
                    fifo.flush()
                except (BrokenPipeError, ValueError):
                    break
                sleep = deadline - time.monotonic()
                if sleep > 0:
                    time.sleep(sleep)

    # ── cleanup ───────────────────────────────────────────────
    def _cleanup(self) -> None:
        if self._proc:
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._fifo_path and os.path.exists(self._fifo_path):
            os.unlink(self._fifo_path)


def _make_fifo() -> str:
    d = tempfile.mkdtemp(prefix="contrail_")
    path = os.path.join(d, "audio.pcm")
    os.mkfifo(path)
    return path


if __name__ == "__main__":
    # Smoke test: render the current state.json (or a placeholder) to a local
    # MP4 with silence. Requires a working ffmpeg.
    logging.basicConfig(level=logging.INFO)
    state_file = SCENE_DIR / "state.json"
    if not state_file.exists():
        _write_state({"generated": time.time(), "viewers": 0, "airborne": 0,
                      "busiest": "", "segment": "ambient",
                      "caption": "Skywatch streamer smoke test.",
                      "tracking": None, "alerts": [], "aircraft": []})
    s = LiveStreamer(target="test", test_duration_s=8.0)
    s.run()
    print(f"Wrote {s.test_out}")
