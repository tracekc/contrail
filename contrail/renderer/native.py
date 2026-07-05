"""Native Skia renderer — browser-free frame generation for the live stream.

A faithful port of ``scene.html``: same world-atlas countries-110m map, same
d3-style equirectangular projection, same dead-reckoning marker model, camera
tween + chase-cam, plane glyphs / focus rings, and the same lower-third chrome
(LIVE badge, tracking panel, stat chips, caption strip, alerts ticker) — drawn
directly with Skia instead of screenshotting headless Chromium.

Selected at runtime via ``RENDERER=native`` (default ``browser`` keeps the
Playwright path). Skia is the same 2D engine Chromium's <canvas> uses, so output
is visually equivalent; the win is throughput (no per-frame screenshot IPC) and
reliability (no browser, no CDN fetch, no memory growth).

``NativeRenderer.render_frame()`` returns JPEG bytes ready for ffmpeg's stdin.
"""
from __future__ import annotations

import functools
import json
import math
import os
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

import skia

from . import tiles

WIDTH, HEIGHT = 1280, 720
SCENE_DIR = Path(__file__).parent
WORLD_CACHE = SCENE_DIR / "countries-110m.json"
WORLD_URL = "https://cdn.jsdelivr.net/npm/world-atlas@2/countries-110m.json"

# Web-Mercator basemap zoom bounds (Phase A, flag-gated via BASEMAP=tiles).
MIN_TILE_Z, MAX_TILE_Z = 2, 12

# ── constants mirrored from scene.html ───────────────────────────────
MAP_PADDING = 20
CAMERA_DURATION_MS = 1600.0
VIEW_EPSILON = 0.01
CORRECT_MS = 800.0
MAX_EXTRAP_S = 120.0
PLANE_LENGTH = 11
FOCUS_RADIUS = 5
NORMAL_MARKER_RADIUS = 3
# Extra map rendered around the viewport so that chase-cam pans (constant scale,
# translation only) can be served by blitting the cached base at an offset,
# instead of re-projecting the whole world every frame. Rebuild only when the
# pan runs past this margin or the zoom (scale) changes.
BASE_MARGIN = 512


def _c(hex_str: str, a: int = 255) -> int:
    h = hex_str.lstrip("#")
    return skia.Color(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a)


# colors from scene.html :root
BG = _c("#0a0e1a")
LAND = _c("#1b2536")
LAND_EDGE = _c("#334155", 90)
GRAT = _c("#2a3346")
CYAN = _c("#4ad0e0")
RED = _c("#e0584a")
LIVE_RED = _c("#c0392b")
TEXT = _c("#cfd6e4")
TEXT_DIM = _c("#7e8aa3")
WHITE = _c("#ffffff")
PANEL_BG = _c("#0a0e1a", int(0.82 * 255))
CAPTION_BG = _c("#0a0e1a", int(0.92 * 255))
TICKER_BG = _c("#050811")
PANEL_BORDER = _c("#ffffff", int(0.08 * 255))
FOCUS_COLOR_EMERGENCY = _c("#e0584a")
FOCUS_COLOR_NORMAL = _c("#3ddc84")
MUTED_COLOR = _c("#5a6678")


# Explicit font files, tried in order per weight. Skia's family-name lookup
# silently falls back to a system default on Linux (no Helvetica/Arial there),
# which is why the stream's text rendered with an unexpected font. Loading a
# real file by path makes rendering deterministic across macOS and the Linux
# host. Override with CONTRAIL_FONT_REGULAR / CONTRAIL_FONT_BOLD if desired.
_FONT_FILES = {
    False: [
        os.getenv("CONTRAIL_FONT_REGULAR"),
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ],
    True: [
        os.getenv("CONTRAIL_FONT_BOLD"),
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ],
}


@functools.lru_cache(maxsize=4)
def _typeface(bold: bool = False) -> skia.Typeface:
    # Prefer an explicit font file (deterministic). Any failure falls through
    # to the next candidate, and finally to the family-name lookup, so this can
    # never raise or hang — worst case it matches the previous behaviour.
    for path in _FONT_FILES[bold]:
        if path and os.path.exists(path):
            try:
                tf = skia.Typeface.MakeFromFile(path)
                if tf is not None:
                    return tf
            except Exception:
                pass
    style = skia.FontStyle.Bold() if bold else skia.FontStyle.Normal()
    for name in ("Helvetica Neue", "Helvetica", "Arial"):
        tf = skia.Typeface(name, style)
        if tf is not None:
            return tf
    return skia.Typeface(None, style)


def _font(size: float, bold: bool = False) -> skia.Font:
    f = skia.Font(_typeface(bold), size)
    f.setSubpixel(True)
    f.setEdging(skia.Font.Edging.kAntiAlias)
    return f


# Glyphs the base Helvetica typeface lacks (Skia does no Unicode fallback the
# way a browser does, so a bare → renders as tofu). Normalize the common ones
# that show up in routes/captions to visually-equivalent ASCII/Latin-1.
_GLYPH_SUBST = {
    "→": "->", "←": "<-", "↔": "<->",  # arrows
    "•": "·", "→︎": "->",           # bullet variants
    "–": "-", "—": "—",                  # dashes (em-dash is in Helvetica)
    "…": "...",                                     # ellipsis
    "“": '"', "”": '"', "‘": "'", "’": "'",  # smart quotes
}


def _safe(text: str) -> str:
    if not text:
        return text
    for bad, good in _GLYPH_SUBST.items():
        if bad in text:
            text = text.replace(bad, good)
    return text


# ── math helpers (ports from scene.html) ─────────────────────────────
def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _clamp01(t: float) -> float:
    return 0.0 if t < 0 else 1.0 if t > 1 else t


def _ease_in_out_quad(t: float) -> float:
    return 2 * t * t if t < 0.5 else -1 + (4 - 2 * t) * t


# ── TopoJSON decode ──────────────────────────────────────────────────
def _decode_topojson(topo: dict, obj_name: str) -> list[list[list[tuple[float, float]]]]:
    """TopoJSON -> [polygon][ring][(lon, lat), ...]."""
    sx, sy = topo["transform"]["scale"]
    tx, ty = topo["transform"]["translate"]

    def dequant(arc):
        pts, x, y = [], 0, 0
        for dx, dy in arc:
            x += dx
            y += dy
            pts.append((x * sx + tx, y * sy + ty))
        return pts

    arcs = [dequant(a) for a in topo["arcs"]]

    def resolve(i):
        return list(reversed(arcs[~i])) if i < 0 else arcs[i]

    def ring(idxs):
        out: list = []
        for i in idxs:
            pts = resolve(i)
            out.extend(pts[1:] if out else pts)
        return out

    polys: list = []
    for g in topo["objects"][obj_name]["geometries"]:
        if g["type"] == "Polygon":
            polys.append([ring(r) for r in g["arcs"]])
        elif g["type"] == "MultiPolygon":
            for poly in g["arcs"]:
                polys.append([ring(r) for r in poly])
    return polys


def _load_world() -> Optional[list]:
    try:
        if not WORLD_CACHE.exists():
            urllib.request.urlretrieve(WORLD_URL, WORLD_CACHE)
        topo = json.loads(WORLD_CACHE.read_text())
        return _decode_topojson(topo, "countries")
    except Exception:  # noqa: BLE001 — offline / bad cache: plotless background
        return None


# ── projection (d3.geoEquirectangular, scale + translate) ────────────
class _Projection:
    def __init__(self) -> None:
        self.scale = 150.0
        self.tx = WIDTH / 2
        self.ty = HEIGHT / 2

    def project(self, lon: float, lat: float) -> tuple[float, float]:
        return (self.tx + self.scale * math.radians(lon),
                self.ty - self.scale * math.radians(lat))

    def set_camera(self, lon: float, lat: float, scale: float) -> None:
        self.scale = scale
        self.tx = WIDTH / 2 - scale * math.radians(lon)
        self.ty = HEIGHT / 2 + scale * math.radians(lat)

    def invert_center(self) -> tuple[float, float]:
        lon = math.degrees((WIDTH / 2 - self.tx) / self.scale)
        lat = math.degrees((self.ty - HEIGHT / 2) / self.scale)
        return lon, lat


# ── projection (Web Mercator, for the raster-tile basemap) ───────────
class _MercatorProjection:
    """Same interface as _Projection but Web Mercator, so aircraft markers land
    on the same coordinate system as the map tiles. `scale` here means world_px
    (the pixel width of the whole world = 2**zoom * 256), so the camera-fit and
    tween math in NativeRenderer works unchanged."""

    def __init__(self) -> None:
        self.center_lon = 0.0
        self.center_lat = 0.0
        self.world_px = float(tiles.world_px_for_zoom(3))

    @property
    def scale(self) -> float:  # alias for interface compatibility
        return self.world_px

    def center_px(self) -> tuple[float, float]:
        return (tiles.lon_to_norm(self.center_lon) * self.world_px,
                tiles.lat_to_norm(self.center_lat) * self.world_px)

    def project(self, lon: float, lat: float) -> tuple[float, float]:
        gx = tiles.lon_to_norm(lon) * self.world_px
        gy = tiles.lat_to_norm(lat) * self.world_px
        cgx, cgy = self.center_px()
        return (gx - cgx + WIDTH / 2, gy - cgy + HEIGHT / 2)

    def set_camera(self, lon: float, lat: float, scale: float) -> None:
        self.center_lon, self.center_lat, self.world_px = lon, lat, float(scale)

    def invert_center(self) -> tuple[float, float]:
        return self.center_lon, self.center_lat


class NativeRenderer:
    def __init__(self) -> None:
        self.world = _load_world()
        # BASEMAP=tiles switches to a Web Mercator projection + raster tile
        # basemap. Default (unset) keeps the equirectangular country-outline map,
        # so the live stream is unaffected until the flag is set.
        self._basemap_tiles = os.getenv("BASEMAP", "").strip().lower() == "tiles"
        self.proj = _MercatorProjection() if self._basemap_tiles else _Projection()
        self.tiles = tiles.TileProvider() if self._basemap_tiles else None
        self.surface = skia.Surface(WIDTH, HEIGHT)

        # Cached base map (bg + graticule + land). Rendered into an oversized
        # surface (viewport + BASE_MARGIN on every side) so a chase-cam pan is a
        # cheap blit-at-offset rather than a full world re-projection each frame.
        self._base_surface = skia.Surface(WIDTH + 2 * BASE_MARGIN, HEIGHT + 2 * BASE_MARGIN)
        self._base_cache: Optional[skia.Image] = None
        self._cache_scale: Optional[float] = None
        self._cache_tx: Optional[float] = None
        self._cache_ty: Optional[float] = None

        # marker + camera state (mirrors scene.html closures)
        self.tracked: dict[str, dict] = {}
        self.latest_state: dict[str, Any] = {}
        self.current_bounds: Optional[list] = None
        self.current_view: Optional[dict] = None
        self.target_view: Optional[dict] = None
        self.transition_from: Optional[dict] = None
        self.transition_start: Optional[float] = None
        self.chase_key: Optional[str] = None
        self.chase_scale: Optional[float] = None
        self._state_mtime: float = -1.0

        # Cache for skia.Image objects loaded from local photo files. Key: file path str.
        self._photo_image_cache: dict[str, Optional[skia.Image]] = {}

        self._init_view(None)

    # ── camera / projection helpers ──────────────────────────────────
    def _view_for_bounds(self, bounds: Optional[list]) -> dict:
        """Fit projection to a [W,S,E,N] bbox with padding (d3 fitExtent),
        then read back {lon, lat, scale}. Mirrors viewForBounds()."""
        if bounds:
            west, south, east, north = bounds
        elif self.world:
            west, south, east, north = -180, -58, 180, 78  # ~land extent
        else:
            west, south, east, north = -180, -85, 180, 85
        if self._basemap_tiles:
            # Mercator fit: world_px that makes the bbox fill the padded viewport.
            nx = abs(tiles.lon_to_norm(east) - tiles.lon_to_norm(west)) or 1e-9
            ny = abs(tiles.lat_to_norm(south) - tiles.lat_to_norm(north)) or 1e-9
            world_px = min((WIDTH - 2 * MAP_PADDING) / nx, (HEIGHT - 2 * MAP_PADDING) / ny)
            world_px = max(float(tiles.world_px_for_zoom(MIN_TILE_Z)),
                           min(world_px, float(tiles.world_px_for_zoom(MAX_TILE_Z))))
            clon, clat = (west + east) / 2, (south + north) / 2
            self.proj.set_camera(clon, clat, world_px)
            return {"lon": clon, "lat": clat, "scale": world_px}
        span_lon = math.radians(east) - math.radians(west)
        span_lat = math.radians(north) - math.radians(south)
        avail_x = WIDTH - 2 * MAP_PADDING
        avail_y = HEIGHT - 2 * MAP_PADDING
        scale = min(avail_x / span_lon, avail_y / span_lat)
        self.proj.set_camera((west + east) / 2, (south + north) / 2, scale)
        lon, lat = self.proj.invert_center()
        return {"lon": lon, "lat": lat, "scale": scale}

    def _init_view(self, bounds: Optional[list]) -> None:
        view = self._view_for_bounds(bounds)
        self.current_view = view
        self.target_view = view
        self.transition_from = None
        self.transition_start = None
        self.proj.set_camera(view["lon"], view["lat"], view["scale"])

    @staticmethod
    def _views_differ(a: Optional[dict], b: Optional[dict]) -> bool:
        if not a or not b:
            return True
        if abs(a["lon"] - b["lon"]) > VIEW_EPSILON:
            return True
        if abs(a["lat"] - b["lat"]) > VIEW_EPSILON:
            return True
        return abs(a["scale"] / b["scale"] - 1) > VIEW_EPSILON

    def _apply_bounds(self, bounds: Optional[list], now: float) -> None:
        if bounds == self.current_bounds:
            return
        self.current_bounds = bounds
        nxt = self._view_for_bounds(bounds)
        if self.current_view:
            self.proj.set_camera(self.current_view["lon"], self.current_view["lat"],
                                 self.current_view["scale"])
        if not self.target_view or self._views_differ(nxt, self.target_view):
            self.transition_from = self.current_view or nxt
            self.target_view = nxt
            self.transition_start = now

    def _update_camera(self, now: float) -> None:
        if self.transition_start is None:
            return
        t = _clamp01((now - self.transition_start) / CAMERA_DURATION_MS)
        e = _ease_in_out_quad(t)
        start, target = self.transition_from, self.target_view
        lon = _lerp(start["lon"], target["lon"], e)
        lat = _lerp(start["lat"], target["lat"], e)
        center_dist = math.hypot(target["lon"] - start["lon"], target["lat"] - start["lat"])
        dip_amount = min(center_dist / 12, 1)
        dip = 1 - 0.6 * dip_amount * math.sin(math.pi * t)
        scale = _lerp(start["scale"], target["scale"], e) * dip
        self.proj.set_camera(lon, lat, scale)
        self.current_view = {"lon": lon, "lat": lat, "scale": scale}
        if t >= 1:
            self.current_view = self.target_view
            self.transition_start = None
            self.transition_from = None

    # ── dead-reckoning marker model ──────────────────────────────────
    @staticmethod
    def _dead_reckon(lat, lon, track, gs, dt_s) -> tuple[float, float]:
        if not gs or gs <= 0 or dt_s <= 0:
            return lat, lon
        t = min(dt_s, MAX_EXTRAP_S)
        dist_deg = (gs * (t / 3600)) / 60
        rad = math.radians(track or 0)
        d_lat = dist_deg * math.cos(rad)
        d_lon = (dist_deg * math.sin(rad)) / max(math.cos(math.radians(lat)), 0.01)
        return lat + d_lat, lon + d_lon

    def _marker_pos(self, m: dict, now: float) -> tuple[float, float]:
        ext_lat, ext_lon = self._dead_reckon(
            m["baseLat"], m["baseLon"], m["track"], m["gs"], (now - m["baseTime"]) / 1000)
        c = min(1, (now - m["correctStart"]) / CORRECT_MS)
        if c < 1:
            e = _ease_in_out_quad(c)
            return (m["correctFromLat"] + (ext_lat - m["correctFromLat"]) * e,
                    m["correctFromLon"] + (ext_lon - m["correctFromLon"]) * e)
        return ext_lat, ext_lon

    @staticmethod
    def _key_for(ac: dict, idx: int) -> str:
        return ac.get("id") or ac.get("callsign") or ac.get("hex") or f"idx{idx}"

    # ── state ingestion (mirrors applyState) ─────────────────────────
    def poll_state(self, state_path: Path) -> None:
        """Re-read + apply state.json only when it changes (browser polls 1Hz)."""
        try:
            mtime = state_path.stat().st_mtime
        except OSError:
            return
        if mtime == self._state_mtime:
            return
        self._state_mtime = mtime
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.apply_state(state)

    def apply_state(self, state: dict, now: Optional[float] = None) -> None:
        if now is None:
            now = time.monotonic() * 1000
        self.latest_state = state
        seen: dict[str, bool] = {}
        for idx, ac in enumerate(state.get("aircraft", [])):
            if not isinstance(ac.get("lat"), (int, float)) or not isinstance(ac.get("lon"), (int, float)):
                continue
            k = self._key_for(ac, idx)
            seen[k] = True
            ex = self.tracked.get(k)
            if ex:
                if ac["lat"] != ex["baseLat"] or ac["lon"] != ex["baseLon"]:
                    cur_lat, cur_lon = self._marker_pos(ex, now)
                    ex.update(correctFromLat=cur_lat, correctFromLon=cur_lon,
                              correctStart=now, baseLat=ac["lat"], baseLon=ac["lon"],
                              baseTime=now)
                ex["emergency"] = bool(ac.get("emergency"))
                ex["focus"] = bool(ac.get("focus"))
                ex["track"] = ac.get("track")
                ex["gs"] = ac.get("gs")
            else:
                self.tracked[k] = {
                    "baseLat": ac["lat"], "baseLon": ac["lon"], "baseTime": now,
                    "correctFromLat": ac["lat"], "correctFromLon": ac["lon"],
                    "correctStart": now, "emergency": bool(ac.get("emergency")),
                    "focus": bool(ac.get("focus")), "track": ac.get("track"),
                    "gs": ac.get("gs"),
                }
        for k in list(self.tracked):
            if k not in seen:
                del self.tracked[k]

        focus_key = next((k for k, m in self.tracked.items() if m["focus"]), None)
        cam = state.get("camera") or state.get("bounds")
        if focus_key:
            self.chase_scale = self._view_for_bounds(cam)["scale"]
            if self.current_view:
                self.proj.set_camera(self.current_view["lon"], self.current_view["lat"],
                                     self.current_view["scale"])
            if focus_key != self.chase_key:
                fp_lat, fp_lon = self._marker_pos(self.tracked[focus_key], now)
                self.transition_from = self.current_view or {"lon": fp_lon, "lat": fp_lat, "scale": self.chase_scale}
                self.target_view = {"lon": fp_lon, "lat": fp_lat, "scale": self.chase_scale}
                self.transition_start = now
                self.chase_key = focus_key
            self.current_bounds = cam
        else:
            self.chase_key = None
            self.chase_scale = None
            self._apply_bounds(cam, now)

    # ── drawing ──────────────────────────────────────────────────────
    def _draw_basemap_tiles(self, c: skia.Canvas) -> None:
        """Composite Web-Mercator raster tiles as the base layer. Only draws
        tiles already cached (TileProvider.get never blocks); any not-yet-fetched
        tile is left as dark background and fills in on a later frame."""
        c.clear(BG)
        if self.tiles is None:
            return
        wp = self.proj.world_px
        z = max(MIN_TILE_Z, min(MAX_TILE_Z, int(round(math.log2(wp / tiles.TILE)))))
        step = tiles.TILE * (wp / float(tiles.world_px_for_zoom(z)))  # on-screen tile size
        cgx, cgy = self.proj.center_px()
        ox, oy = cgx - WIDTH / 2, cgy - HEIGHT / 2
        n = 2 ** z
        sampling = skia.SamplingOptions(skia.FilterMode.kLinear)
        src = skia.Rect.MakeWH(tiles.TILE, tiles.TILE)
        tx0, tx1 = int(math.floor(ox / step)), int(math.floor((ox + WIDTH) / step))
        ty0, ty1 = int(math.floor(oy / step)), int(math.floor((oy + HEIGHT) / step))
        for tx in range(tx0, tx1 + 1):
            wx = (tx % n + n) % n  # wrap longitude
            for ty in range(ty0, ty1 + 1):
                if ty < 0 or ty >= n:
                    continue
                img = self.tiles.get(z, wx, ty)
                if img is None:
                    continue
                dst = skia.Rect.MakeXYWH(tx * step - ox, ty * step - oy, step, step)
                c.drawImageRect(img, src, dst, sampling, None)

    def _draw_base(self, c: skia.Canvas) -> None:
        """Paint the base map onto the frame canvas, using the cached oversized
        base when the zoom is stable (overview + chase-cam) and only re-rendering
        the world during a scale-changing camera transition."""
        if self._basemap_tiles:
            self._draw_basemap_tiles(c)
            return
        scale, tx, ty = self.proj.scale, self.proj.tx, self.proj.ty

        # Mid-transition the zoom changes every frame, so a pan-blit can't stand
        # in — draw the base directly at viewport size (same cost as before) and
        # drop the stale cache. Transitions are brief (~1.6s).
        if self.transition_start is not None:
            c.clear(BG)
            self._paint_base(c)
            self._base_cache = None
            return

        # Stable zoom: (re)build the oversized cache only when missing, the scale
        # changed, or a pan ran past the margin. Otherwise reuse it.
        if (self._base_cache is None or scale != self._cache_scale
                or abs(tx - self._cache_tx) > BASE_MARGIN
                or abs(ty - self._cache_ty) > BASE_MARGIN):
            self._render_base_cache(scale, tx, ty)

        dtx, dty = tx - self._cache_tx, ty - self._cache_ty
        c.clear(BG)
        c.drawImage(self._base_cache, -BASE_MARGIN + dtx, -BASE_MARGIN + dty)

    def _render_base_cache(self, scale: float, tx: float, ty: float) -> None:
        """Render the full base map into the oversized surface at the current
        projection, offset by BASE_MARGIN so viewport (0,0) maps to (M, M)."""
        bc = self._base_surface.getCanvas()
        bc.clear(BG)
        bc.save()
        bc.translate(BASE_MARGIN, BASE_MARGIN)
        self._paint_base(bc)
        bc.restore()
        self._base_cache = self._base_surface.makeImageSnapshot()
        self._cache_scale, self._cache_tx, self._cache_ty = scale, tx, ty

    def _paint_base(self, c: skia.Canvas) -> None:
        """Draw graticule + land using the current projection. No background
        clear (callers clear first) so it composes into the oversized cache."""
        proj = self.proj.project
        grat = skia.Paint(Color=GRAT, Style=skia.Paint.kStroke_Style,
                          StrokeWidth=0.5, AntiAlias=True)
        grat.setAlphaf(0.5)
        for lon in range(-180, 181, 20):
            path = skia.Path()
            for k, lat in enumerate(range(-90, 91, 4)):
                x, y = proj(lon, lat)
                path.moveTo(x, y) if k == 0 else path.lineTo(x, y)
            c.drawPath(path, grat)
        for lat in range(-80, 81, 20):
            path = skia.Path()
            for k, lon in enumerate(range(-180, 181, 4)):
                x, y = proj(lon, lat)
                path.moveTo(x, y) if k == 0 else path.lineTo(x, y)
            c.drawPath(path, grat)
        if self.world:
            fill = skia.Paint(Color=LAND, Style=skia.Paint.kFill_Style, AntiAlias=True)
            edge = skia.Paint(Color=LAND_EDGE, Style=skia.Paint.kStroke_Style,
                              StrokeWidth=0.6, AntiAlias=True)
            for poly in self.world:
                path = skia.Path()
                for ring in poly:
                    for k, (lon, lat) in enumerate(ring):
                        x, y = proj(lon, lat)
                        path.moveTo(x, y) if k == 0 else path.lineTo(x, y)
                    path.close()
                c.drawPath(path, fill)
                c.drawPath(path, edge)

    @staticmethod
    def _plane_glyph(size: float) -> skia.Path:
        """Port of drawPlaneGlyph — nose points up (north) before rotation."""
        p = skia.Path()
        p.moveTo(0, -size * 0.6)          # nose
        p.lineTo(size * 0.42, size * 0.05)   # right wingtip
        p.lineTo(size * 0.12, size * 0.12)   # right wing root
        p.lineTo(size * 0.18, size * 0.5)    # right tail
        p.lineTo(0, size * 0.32)             # tail notch
        p.lineTo(-size * 0.18, size * 0.5)   # left tail
        p.lineTo(-size * 0.12, size * 0.12)  # left wing root
        p.lineTo(-size * 0.42, size * 0.05)  # left wingtip
        p.close()
        return p

    def _draw_markers(self, c: skia.Canvas, now: float) -> None:
        for m in self.tracked.values():
            lat, lon = self._marker_pos(m, now)
            x, y = self.proj.project(lon, lat)
            if m["focus"]:
                color = FOCUS_COLOR_EMERGENCY if m["emergency"] else FOCUS_COLOR_NORMAL
                ring_radius = FOCUS_RADIUS
                size = PLANE_LENGTH * 1.4
            else:
                color = MUTED_COLOR
                ring_radius = NORMAL_MARKER_RADIUS
                size = PLANE_LENGTH

            if m["focus"] and m["emergency"]:
                phase = (now / 900) % 1
                pr = ring_radius + phase * 14
                pa = 0.55 * (1 - phase)
                ring = skia.Paint(Color=_c("#e0584a", int(pa * 255)),
                                  Style=skia.Paint.kStroke_Style, StrokeWidth=1.5, AntiAlias=True)
                c.drawCircle(x, y, pr, ring)
            elif m["focus"]:
                ring = skia.Paint(Color=_c("#3ddc84", int(0.6 * 255)),
                                  Style=skia.Paint.kStroke_Style, StrokeWidth=1.5, AntiAlias=True)
                c.drawCircle(x, y, ring_radius + 5, ring)

            c.save()
            c.translate(x, y)
            c.rotate(math.degrees(math.radians(m["track"] or 0)))
            c.drawPath(self._plane_glyph(size),
                       skia.Paint(Color=color, Style=skia.Paint.kFill_Style, AntiAlias=True))
            c.restore()

    def _load_photo(self, path: Optional[str]) -> Optional[skia.Image]:
        """Load a photo from disk, caching by path. Returns None on any failure."""
        if not path:
            return None
        if path in self._photo_image_cache:
            return self._photo_image_cache[path]
        try:
            raw = Path(path).read_bytes()
            img = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(raw))
            self._photo_image_cache[path] = img
            return img
        except Exception:
            self._photo_image_cache[path] = None
            return None

    # ── chrome (HTML/CSS overlays ported to Skia draws) ──────────────
    def _draw_chrome(self, c: skia.Canvas, now: float) -> None:
        st = self.latest_state
        f_badge = _font(13, bold=True)
        f_view = _font(12)
        f_small = _font(11)
        f_label = _font(9, bold=True)
        f_chip = _font(14, bold=True)
        f_caption = _font(15)
        f_desk = _font(13, bold=True)

        # ---- LIVE badge (top-right) ----
        viewers = st.get("viewers") or 0
        vtext = f"{viewers:,} watching"
        live_txt, gap = "Live", 8
        dot_r = 4
        pad_x, pad_y, h = 12, 6, 26
        w = (10 + dot_r * 2 + gap + f_badge.measureText(live_txt) + gap + 1 + 8
             + f_view.measureText(vtext) + pad_x)
        bx = WIDTH - 18 - w
        by = 18
        c.drawRoundRect(skia.Rect.MakeXYWH(bx, by, w, h), 4, 4, skia.Paint(Color=LIVE_RED, AntiAlias=True))
        cx = bx + 10
        c.drawCircle(cx + dot_r, by + h / 2, dot_r, skia.Paint(Color=WHITE, AntiAlias=True))
        cx += dot_r * 2 + gap
        c.drawString(live_txt, cx, by + 18, f_badge, skia.Paint(Color=WHITE, AntiAlias=True))
        cx += f_badge.measureText(live_txt) + gap
        c.drawLine(cx, by + 6, cx, by + h - 6, skia.Paint(Color=_c("#ffffff", 90), AntiAlias=True))
        c.drawString(vtext, cx + 8, by + 18, f_view, skia.Paint(Color=_c("#ffffff", 235), AntiAlias=True))

        # ---- Tracking panel (top-right, below badge) ----
        t = st.get("tracking")
        if t and t.get("callsign"):
            px, py, pw = WIDTH - 18 - 260, 58, 260

            # Measure dynamic height before drawing background.
            has_photo = bool(t.get("photo_path"))
            has_reg = bool(t.get("registration"))
            has_age = bool(t.get("built_year"))
            has_route = bool(t.get("route"))
            has_emergency = bool(t.get("emergency"))
            data_rows = [
                ("Altitude", f"{t['alt']:,} ft" if t.get("alt") is not None else "—"),
                ("Speed", f"{t['speed']} kt" if t.get("speed") is not None else "—"),
                ("Squawk", t.get("squawk") or "—"),
            ]
            ph = (16 + 20 + 14       # tag + callsign + type
                  + 96               # photo box + gap
                  + (14 if (has_reg or has_age) else 0)   # badges row
                  + (12 if t.get("photo_credit") else 0)  # photo credit
                  + 8                # section divider gap
                  + (16 * 2 if has_route else 0)           # two route rows (same spacing as data rows)
                  + len(data_rows) * 16
                  + (20 if has_emergency else 0)
                  + 12)              # bottom padding

            c.drawRoundRect(skia.Rect.MakeXYWH(px, py, pw, ph), 4, 4, skia.Paint(Color=PANEL_BG, AntiAlias=True))
            c.drawRect(skia.Rect.MakeXYWH(px, py, 3, ph), skia.Paint(Color=RED))

            ix, iy = px + 12, py + 14
            # tag
            c.drawString("TRACKING NOW", ix, iy, f_label, skia.Paint(Color=TEXT_DIM, AntiAlias=True))
            iy += 18
            # callsign
            c.drawString(_safe(t["callsign"]), ix, iy, _font(16, bold=True), skia.Paint(Color=WHITE, AntiAlias=True))
            iy += 18
            # type (may overlap operator — prefer operator when available)
            type_str = _safe(t.get("operator") or t.get("type") or "Unknown type")
            c.drawString(type_str, ix, iy, f_small, skia.Paint(Color=TEXT_DIM, AntiAlias=True))
            iy += 14

            # Photo box (90px tall, full panel width minus side padding)
            photo_rect = skia.Rect.MakeXYWH(ix, iy, pw - 24, 90)
            photo_img = self._load_photo(t.get("photo_path"))
            if photo_img:
                # Scale-to-fill crop into photo_rect
                iw, ih = photo_img.width(), photo_img.height()
                box_w, box_h = pw - 24, 90
                scale = max(box_w / iw, box_h / ih)
                sw, sh = iw * scale, ih * scale
                src_rect = skia.Rect.MakeXYWH(0, 0, iw, ih)
                dst_rect = skia.Rect.MakeXYWH(
                    ix + (box_w - sw) / 2, iy + (box_h - sh) / 2, sw, sh
                )
                c.save()
                c.clipRRect(skia.RRect.MakeRectXY(photo_rect, 3, 3))
                c.drawImageRect(photo_img, src_rect, dst_rect,
                                skia.SamplingOptions(skia.FilterMode.kLinear), None)
                c.restore()
            else:
                # Placeholder: dark box with registration or fallback text
                c.drawRoundRect(photo_rect, 3, 3,
                                skia.Paint(Color=_c("#141b2d"), AntiAlias=True))
                placeholder = _safe(t.get("registration") or t.get("callsign") or "—")
                pw_txt = _font(12, bold=True).measureText(placeholder)
                c.drawString(placeholder, ix + (pw - 24 - pw_txt) / 2, iy + 42,
                             _font(12, bold=True), skia.Paint(Color=TEXT_DIM, AntiAlias=True))
                no_ph = "No photo found"
                no_w = f_small.measureText(no_ph)
                c.drawString(no_ph, ix + (pw - 24 - no_w) / 2, iy + 58,
                             f_small, skia.Paint(Color=_c("#4a5568"), AntiAlias=True))
            iy += 94

            # Photo credit
            credit = t.get("photo_credit")
            if credit:
                credit_str = _safe(f"Photo: {credit}")
                c.drawString(credit_str, ix, iy, _font(9), skia.Paint(Color=_c("#4a5568"), AntiAlias=True))
                iy += 12

            # Reg + age badges
            if has_reg or has_age:
                badge_x = ix
                badge_h = 16
                f_badge_sm = _font(9, bold=True)
                for badge_txt in filter(None, [
                    t.get("registration"),
                    f"Est. {t['built_year']}" if has_age else None,
                ]):
                    badge_txt = _safe(badge_txt)
                    btw = f_badge_sm.measureText(badge_txt)
                    bw = btw + 12
                    c.drawRoundRect(skia.Rect.MakeXYWH(badge_x, iy - 11, bw, badge_h),
                                    3, 3, skia.Paint(Color=_c("#1e2d42"), AntiAlias=True))
                    c.drawString(badge_txt, badge_x + 6, iy, f_badge_sm,
                                 skia.Paint(Color=TEXT, AntiAlias=True))
                    badge_x += bw + 6
                iy += 14

            # Divider gap
            iy += 8

            # Route rows — same label-left / value-right layout as data rows.
            if has_route:
                route_safe = _safe(t["route"])
                route_parts = route_safe.split("->", 1)
                f_route = _font(10)
                max_val_w = pw - 24 - f_small.measureText("From") - 8
                if len(route_parts) == 2:
                    for lbl, name in [("From", route_parts[0].strip()),
                                      ("To",   route_parts[1].strip())]:
                        # Truncate name until it fits the right-aligned value
                        # slot. Guard against a non-converging loop: shrink by
                        # one char at a time and stop once we can't shrink
                        # further, so a tiny/negative max_val_w (narrow panel or
                        # unexpected font metrics) can never spin forever.
                        while (len(name) > 1
                               and f_route.measureText(name) > max_val_w):
                            name = name[:-1]
                        c.drawString(lbl, ix, iy, f_small,
                                     skia.Paint(Color=TEXT_DIM, AntiAlias=True))
                        nw = f_route.measureText(name)
                        c.drawString(name, px + pw - 12 - nw, iy, f_route,
                                     skia.Paint(Color=TEXT, AntiAlias=True))
                        iy += 16
                else:
                    c.drawString(route_safe, ix, iy, f_small,
                                 skia.Paint(Color=TEXT, AntiAlias=True))
                    iy += 16

            # Data rows (altitude / speed / squawk)
            for label, val in data_rows:
                val = _safe(val)
                c.drawString(label, ix, iy, f_small, skia.Paint(Color=TEXT_DIM, AntiAlias=True))
                vw = f_small.measureText(val)
                c.drawString(val, px + pw - 12 - vw, iy, f_small, skia.Paint(Color=TEXT, AntiAlias=True))
                iy += 16

            if has_emergency:
                c.drawString("EMERGENCY IN PROGRESS", ix, iy + 2, _font(11, bold=True),
                             skia.Paint(Color=RED, AntiAlias=True))

        # ---- Stat chips (bottom-left) ----
        chips = [("AIRBORNE", f"{st['airborne']:,}" if st.get("airborne") is not None else "—"),
                 ("BUSIEST", _safe(st.get("busiest") or "—"))]
        chx, chy, chh = 18, HEIGHT - 84 - 40, 40
        for label, val in chips:
            cw = max(120, f_chip.measureText(val) + 20)
            c.drawRoundRect(skia.Rect.MakeXYWH(chx, chy, cw, chh), 4, 4, skia.Paint(Color=PANEL_BG, AntiAlias=True))
            c.drawString(label, chx + 10, chy + 14, f_label, skia.Paint(Color=TEXT_DIM, AntiAlias=True))
            c.drawString(val, chx + 10, chy + 32, f_chip, skia.Paint(Color=WHITE, AntiAlias=True))
            chx += cw + 8

        # ---- Narrating indicator (bottom-left, above chips) ----
        ny = HEIGHT - 128
        narrating = bool(st.get("caption"))
        label = "NARRATING" if narrating else "STANDING BY"
        bars_x = 18
        for i, bh in enumerate((4, 10, 6)):
            if narrating:
                bh = int(4 + (8 * abs(math.sin(now / 250 + i * 0.7))))
            c.drawRect(skia.Rect.MakeXYWH(bars_x + i * 5, ny - bh, 3, bh),
                       skia.Paint(Color=CYAN if narrating else TEXT_DIM, AntiAlias=True))
        c.drawString(label, bars_x + 20, ny, f_small, skia.Paint(Color=TEXT_DIM, AntiAlias=True))

        # ---- Caption strip (lower third) ----
        strip_h = 38
        strip_y = HEIGHT - 28 - strip_h
        desk = "Flight desk"
        desk_w = f_desk.measureText(desk) + 32
        c.drawRect(skia.Rect.MakeXYWH(0, strip_y, desk_w, strip_h), skia.Paint(Color=LIVE_RED))
        c.drawString(desk, 16, strip_y + 25, f_desk, skia.Paint(Color=WHITE, AntiAlias=True))
        c.drawRect(skia.Rect.MakeXYWH(desk_w, strip_y, WIDTH - desk_w, strip_h), skia.Paint(Color=CAPTION_BG))
        caption = _safe(st.get("caption") or "Standing by for live traffic.")
        c.drawString(caption, desk_w + 16, strip_y + 25, f_caption, skia.Paint(Color=WHITE, AntiAlias=True))

        # ---- Alerts ticker (very bottom, scrolling) ----
        ty0 = HEIGHT - 28
        c.drawRect(skia.Rect.MakeXYWH(0, ty0, WIDTH, 28), skia.Paint(Color=TICKER_BG))
        tag = "ALERTS"
        tag_w = f_label.measureText(tag) + 20
        c.drawRect(skia.Rect.MakeXYWH(0, ty0, tag_w, 28), skia.Paint(Color=CYAN))
        c.drawString(tag, 10, ty0 + 18, f_label, skia.Paint(Color=BG, AntiAlias=True))
        alerts = st.get("alerts") or []
        content = _safe("     ·     ".join(alerts) if alerts else "No active alerts.")
        seg_w = f_view.measureText(content) + 80
        offset = (now / 25) % seg_w if seg_w > 0 else 0
        c.save()
        c.clipRect(skia.Rect.MakeXYWH(tag_w, ty0, WIDTH - tag_w, 28))
        base_x = tag_w + 12 - offset
        paint = skia.Paint(Color=TEXT, AntiAlias=True)
        c.drawString(content, base_x, ty0 + 19, f_view, paint)
        c.drawString(content, base_x + seg_w, ty0 + 19, f_view, paint)
        c.restore()

    # ── public: produce one frame ────────────────────────────────────
    def render_frame(self, now_ms: Optional[float] = None, quality: int = 75) -> bytes:
        now = time.monotonic() * 1000 if now_ms is None else now_ms
        self._update_camera(now)
        # chase-cam: once zoom-in completes, pin focus aircraft to center
        if self.chase_key and self.chase_scale and self.transition_start is None:
            fm = self.tracked.get(self.chase_key)
            if fm:
                fp_lat, fp_lon = self._marker_pos(fm, now)
                self.proj.set_camera(fp_lon, fp_lat, self.chase_scale)
                self.current_view = {"lon": fp_lon, "lat": fp_lat, "scale": self.chase_scale}
            else:
                self.chase_key = None

        c = self.surface.getCanvas()
        self._draw_base(c)
        self._draw_markers(c, now)
        self._draw_chrome(c, now)
        img = self.surface.makeImageSnapshot()
        return bytes(img.encodeToData(skia.kJPEG, quality))


if __name__ == "__main__":
    # Smoke test: render the shared sample state to a PNG for eyeballing.
    from .render import _build_sample_state, _write_state, STATE_JSON

    state = _build_sample_state()
    _write_state(state)
    r = NativeRenderer()
    r.apply_state(state, now=0.0)
    # advance a bit so the intro camera tween settles
    for step in range(8):
        r.render_frame(now_ms=step * 250)
    jpeg = r.render_frame(now_ms=2000)
    out = SCENE_DIR / "native_frame.png"
    r.surface.makeImageSnapshot().save(str(out), skia.kPNG)
    print(f"wrote {out} ({len(jpeg)} bytes as jpeg)")
