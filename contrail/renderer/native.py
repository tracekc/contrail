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

import json
import math
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

import skia

WIDTH, HEIGHT = 1280, 720
SCENE_DIR = Path(__file__).parent
WORLD_CACHE = SCENE_DIR / "countries-110m.json"
WORLD_URL = "https://cdn.jsdelivr.net/npm/world-atlas@2/countries-110m.json"

# ── constants mirrored from scene.html ───────────────────────────────
MAP_PADDING = 20
CAMERA_DURATION_MS = 1600.0
VIEW_EPSILON = 0.01
CORRECT_MS = 800.0
MAX_EXTRAP_S = 120.0
PLANE_LENGTH = 11
FOCUS_RADIUS = 5
NORMAL_MARKER_RADIUS = 3


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


def _typeface(bold: bool = False) -> skia.Typeface:
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


class NativeRenderer:
    def __init__(self) -> None:
        self.world = _load_world()
        self.proj = _Projection()
        self.surface = skia.Surface(WIDTH, HEIGHT)

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
    def _draw_base_map(self, c: skia.Canvas) -> None:
        c.clear(BG)
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
            px, py, pw = WIDTH - 18 - 240, 58, 240
            rows = [("Route", t.get("route") or "—"),
                    ("Altitude", f"{t['alt']:,} ft" if t.get("alt") is not None else "—"),
                    ("Speed", f"{t['speed']} kt" if t.get("speed") is not None else "—"),
                    ("Squawk", t.get("squawk") or "—")]
            ph = 62 + len(rows) * 16 + (18 if t.get("emergency") else 0)
            c.drawRoundRect(skia.Rect.MakeXYWH(px, py, pw, ph), 4, 4, skia.Paint(Color=PANEL_BG, AntiAlias=True))
            c.drawRect(skia.Rect.MakeXYWH(px, py, 3, ph), skia.Paint(Color=RED))
            ix, iy = px + 12, py + 16
            c.drawString("TRACKING NOW", ix, iy, f_label, skia.Paint(Color=TEXT_DIM, AntiAlias=True))
            iy += 18
            c.drawString(_safe(t["callsign"]), ix, iy, _font(16, bold=True), skia.Paint(Color=WHITE, AntiAlias=True))
            iy += 16
            c.drawString(_safe(t.get("type") or "Unknown type"), ix, iy, f_small, skia.Paint(Color=TEXT_DIM, AntiAlias=True))
            iy += 16
            for label, val in rows:
                val = _safe(val)
                c.drawString(label, ix, iy, f_small, skia.Paint(Color=TEXT_DIM, AntiAlias=True))
                vw = f_small.measureText(val)
                c.drawString(val, px + pw - 12 - vw, iy, f_small, skia.Paint(Color=TEXT, AntiAlias=True))
                iy += 16
            if t.get("emergency"):
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
        self._draw_base_map(c)
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
