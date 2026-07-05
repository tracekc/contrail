"""Web-Mercator raster tile provider for the basemap (Phase A, flag-gated).

Design constraint learned the hard way: the render thread must NEVER block on
the network. So `get()` only ever returns an already-decoded tile (from memory
or disk) instantly, or None. Misses are handed to a background worker that
fetches + caches them for a later frame; the renderer draws dark in the
meantime. Nothing here can stall a frame.

Tiles are standard slippy-map (z/x/y). Source is configurable via env; the
default is CARTO dark for local testing only — a production source is chosen
in Phase B (see FUTURE_PLANS §2). Attribution is required when productionised.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Optional

import skia

log = logging.getLogger(__name__)

TILE = 256
_CACHE_DIR = Path(__file__).parent / "tile_cache"
_UA = "contrail-skywatch/1.0 (+github.com/tracekc/contrail)"

# Dark raster source. Prefer Stadia (Alidade Smooth Dark) when a key is set;
# otherwise fall back to CARTO dark for keyless local testing. Override the
# whole URL with TILE_URL_TEMPLATE if needed. `.format` ignores unused fields,
# so one call fills {s}/{z}/{x}/{y}/{key} regardless of which the template uses.
_TILE_KEY = os.getenv("STADIA_API_KEY", "").strip()
_STADIA = "https://tiles.stadiamaps.com/tiles/alidade_smooth_dark/{z}/{x}/{y}.png?api_key={key}"
_CARTO = "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"
_URL_TEMPLATE = os.getenv("TILE_URL_TEMPLATE") or (_STADIA if _TILE_KEY else _CARTO)
_SUBDOMAINS = "abc"

# Attribution string for the active source — must be shown on-screen.
ATTRIBUTION = ("© Stadia Maps © OpenMapTiles © OpenStreetMap"
               if (_TILE_KEY or "stadiamaps" in _URL_TEMPLATE)
               else "© CARTO © OpenStreetMap")

_MEM_MAX = 320       # decoded tiles held in RAM (a viewport is ~24, + margin/history)
_DISK_MAX = 8000     # PNGs kept on disk before LRU eviction (~ a few hundred MB)
_N_WORKERS = 3       # parallel fetch threads
_MAX_QUEUE = 160     # cap pending fetches; a camera zoom-tween can request tiles
                     # across many zooms in seconds — keep only the newest.
_NEG_COOLDOWN = 45   # secs before retrying a failed tile (never cache a failure
                     # permanently — that turned a transient throttle into a
                     # permanent dark square).


# ── mercator math ─────────────────────────────────────────────────────────────
def lon_to_norm(lon: float) -> float:
    """Longitude -> [0,1) across the world in Web Mercator."""
    return (lon + 180.0) / 360.0


def lat_to_norm(lat: float) -> float:
    """Latitude -> [0,1) (0 = north edge ~85.05, 1 = south edge)."""
    lat = max(min(lat, 85.05112878), -85.05112878)
    s = math.asinh(math.tan(math.radians(lat)))
    return (1.0 - s / math.pi) / 2.0


def world_px_for_zoom(z: int) -> int:
    return (2 ** z) * TILE


class TileProvider:
    def __init__(self, url_template: str = _URL_TEMPLATE,
                 cache_dir: Path = _CACHE_DIR) -> None:
        self._url = url_template
        self._dir = cache_dir
        self._mem: "OrderedDict[tuple[int,int,int], Optional[skia.Image]]" = OrderedDict()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._pending: set[tuple[int, int, int]] = set()
        self._stack: "deque[tuple[int,int,int]]" = deque()  # newest-last; popped newest-first
        self._workers_started = False

    # ── public: called from the render thread; never blocks on network ────────
    def get(self, z: int, x: int, y: int) -> Optional[skia.Image]:
        n = 2 ** z
        if not (0 <= x < n and 0 <= y < n):
            return None
        key = (z, x, y)
        now = time.time()
        with self._lock:
            entry = self._mem.get(key)
            if entry is not None:
                if isinstance(entry, skia.Image):
                    self._mem.move_to_end(key)
                    return entry
                # negative sentinel = a retry-after timestamp
                if now < entry:
                    return None            # still cooling down; draw dark
                del self._mem[key]         # cooldown elapsed — allow a refetch

        # Not in memory — try disk (fast, safe to do inline).
        p = self._tile_path(z, x, y)
        if p.exists():
            try:
                img = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(p.read_bytes()))
            except Exception:
                img = None
            if img is not None:
                self._remember(key, img)
                return img
            # corrupt file on disk — drop it so a refetch can replace it
            try:
                p.unlink()
            except OSError:
                pass

        # Miss (or expired negative) — enqueue a background fetch, draw dark now.
        self._enqueue(key)
        return None

    def prewarm(self, tiles: list[tuple[int, int, int]]) -> None:
        """Queue a batch of tiles for background fetch (e.g. region rotation)."""
        for key in tiles:
            self._enqueue(key)

    # ── internals ─────────────────────────────────────────────────────────────
    def _remember(self, key, img) -> None:
        with self._lock:
            self._mem[key] = img
            self._mem.move_to_end(key)
            while len(self._mem) > _MEM_MAX:
                self._mem.popitem(last=False)

    def _remember_negative(self, key) -> None:
        """Cache a short-lived 'failed, retry after' marker (a timestamp), so a
        transient fetch failure draws dark briefly but recovers on its own."""
        with self._lock:
            self._mem[key] = time.time() + _NEG_COOLDOWN
            self._mem.move_to_end(key)
            while len(self._mem) > _MEM_MAX:
                self._mem.popitem(last=False)

    def _enqueue(self, key) -> None:
        with self._cv:
            if key in self._pending:
                return
            self._pending.add(key)
            self._stack.append(key)  # newest-last
            # Cap the backlog: during a zoom tween tiles are requested across
            # many zooms; drop the oldest so the current view's tiles win.
            while len(self._stack) > _MAX_QUEUE:
                self._pending.discard(self._stack.popleft())
            if not self._workers_started:
                self._workers_started = True
                for _ in range(_N_WORKERS):
                    threading.Thread(target=self._fetch_loop, daemon=True).start()
            self._cv.notify()

    def _tile_path(self, z, x, y) -> Path:
        return self._dir / f"{z}_{x}_{y}.png"

    def _fetch_loop(self) -> None:
        import requests
        while True:
            with self._cv:
                while not self._stack:
                    self._cv.wait()
                z, x, y = self._stack.pop()  # newest first (LIFO)
            key = (z, x, y)
            ok = False
            try:
                url = self._url.format(s=_SUBDOMAINS[(x + y) % len(_SUBDOMAINS)],
                                       z=z, x=x, y=y, key=_TILE_KEY)
                r = requests.get(url, timeout=10, headers={"User-Agent": _UA})
                if r.status_code == 200 and r.content:
                    try:
                        img = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(r.content))
                    except Exception:
                        img = None
                    if img is not None:
                        self._dir.mkdir(parents=True, exist_ok=True)
                        tmp = self._tile_path(z, x, y).with_suffix(".png.tmp")
                        tmp.write_bytes(r.content)
                        os.replace(tmp, self._tile_path(z, x, y))
                        self._remember(key, img)
                        self._evict_disk_if_needed()
                        ok = True
                else:
                    log.debug("tile %s/%s/%s -> HTTP %s", z, x, y, r.status_code)
                time.sleep(0.03)  # be polite to the tile server
            except Exception as exc:
                log.debug("tile fetch %s/%s/%s failed: %s", z, x, y, exc)
            finally:
                if not ok:
                    self._remember_negative(key)  # retry after cooldown, not forever
                with self._lock:
                    self._pending.discard(key)

    _evict_counter = 0

    def _evict_disk_if_needed(self) -> None:
        # Cheap: only scan occasionally, not every fetch.
        self._evict_counter += 1
        if self._evict_counter % 200 != 0:
            return
        try:
            files = sorted(self._dir.glob("*.png"), key=lambda f: f.stat().st_mtime)
            for f in files[:max(0, len(files) - _DISK_MAX)]:
                f.unlink(missing_ok=True)
        except Exception:
            pass
