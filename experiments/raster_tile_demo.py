#!/usr/bin/env python3
"""Standalone raster-tile basemap prototype — NOT wired into the live stream.

Renders two short clips with identical camera motion and plane markers:
  * raster : real dark map tiles (CARTO dark) composited under the overlay
  * flat   : the current-style solid dark background under the same overlay

then reports render time and (constant-quality) encoded bitrate for each, so we
can judge both the visual look and the performance/bandwidth cost of adding a
real basemap — without touching contrail/ or the production stream.

Tiles: CARTO dark basemap (© OpenStreetMap contributors, © CARTO), free for
light use with attribution. Fetched once and cached on disk; a run reuses them.

Usage:  .venv/bin/python experiments/raster_tile_demo.py [--zoom 9] [--seconds 20]
Outputs: experiments/out_raster.mp4, experiments/out_flat.mp4
"""
from __future__ import annotations

import argparse
import math
import os
import random
import subprocess
import time
from pathlib import Path

import requests
import skia

WIDTH, HEIGHT = 1280, 720
TILE = 256
HERE = Path(__file__).parent
CACHE = HERE / "tile_cache"
UA = "contrail-maptest/0.1 (+github.com/tracekc/contrail)"
TILE_URL = "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"
_SUBDOMAINS = "abc"

BG = skia.Color(10, 14, 26)          # #0a0e1a — current flat background
PLANE = skia.Color(207, 214, 228)    # muted white
FOCUS = skia.Color(61, 220, 132)     # green


# ── web-mercator tile math ────────────────────────────────────────────────────
def _lon_to_gx(lon: float, z: float) -> float:
    return (lon + 180.0) / 360.0 * (2 ** z) * TILE


def _lat_to_gy(lat: float, z: float) -> float:
    s = math.asinh(math.tan(math.radians(lat)))
    return (1.0 - s / math.pi) / 2.0 * (2 ** z) * TILE


# ── tile fetch + cache ────────────────────────────────────────────────────────
def _tile_bytes(z: int, x: int, y: int) -> bytes | None:
    n = 2 ** z
    if not (0 <= x < n and 0 <= y < n):
        return None
    p = CACHE / f"{z}_{x}_{y}.png"
    if p.exists():
        return p.read_bytes()
    url = TILE_URL.format(s=_SUBDOMAINS[(x + y) % 3], z=z, x=x, y=y)
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": UA})
        if r.status_code == 200:
            CACHE.mkdir(parents=True, exist_ok=True)
            p.write_bytes(r.content)
            time.sleep(0.05)  # be polite to the tile server
            return r.content
    except Exception as e:
        print(f"  tile {z}/{x}/{y} failed: {e}")
    return None


def _load_tiles(z: int, xr: range, yr: range) -> dict[tuple[int, int], skia.Image]:
    imgs: dict[tuple[int, int], skia.Image] = {}
    missing = 0
    for x in xr:
        for y in yr:
            b = _tile_bytes(z, x, y)
            if b:
                img = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(b))
                if img:
                    imgs[(x, y)] = img
                    continue
            missing += 1
    if missing:
        print(f"  ({missing} tiles missing — will show dark gaps)")
    return imgs


# ── overlay: fake traffic ─────────────────────────────────────────────────────
class Plane:
    def __init__(self, lon, lat, hdg, focus=False):
        self.lon, self.lat, self.hdg, self.focus = lon, lat, hdg, focus

    def step(self, dt):
        spd = 0.02  # deg/sec-ish
        self.lon += math.sin(math.radians(self.hdg)) * spd * dt
        self.lat += math.cos(math.radians(self.hdg)) * spd * dt


_GLYPH = skia.Path()
_GLYPH.moveTo(0, -7); _GLYPH.lineTo(5, 6); _GLYPH.lineTo(0, 3); _GLYPH.lineTo(-5, 6); _GLYPH.close()


def _draw_planes(c, planes, cx_lon, cx_lat, z):
    ox = _lon_to_gx(cx_lon, z) - WIDTH / 2
    oy = _lat_to_gy(cx_lat, z) - HEIGHT / 2
    for pl in planes:
        x = _lon_to_gx(pl.lon, z) - ox
        y = _lat_to_gy(pl.lat, z) - oy
        if not (-20 <= x <= WIDTH + 20 and -20 <= y <= HEIGHT + 20):
            continue
        color = FOCUS if pl.focus else PLANE
        if pl.focus:
            c.drawCircle(x, y, 10, skia.Paint(Color=color, Style=skia.Paint.kStroke_Style,
                                              StrokeWidth=1.5, AntiAlias=True))
        c.save(); c.translate(x, y); c.rotate(pl.hdg)
        c.drawPath(_GLYPH, skia.Paint(Color=color, AntiAlias=True))
        c.restore()


# ── render one clip ───────────────────────────────────────────────────────────
def render_clip(mode: str, z: int, seconds: int, fps: int,
                tiles: dict, out_path: Path) -> dict:
    surface = skia.Surface(WIDTH, HEIGHT)
    n_frames = seconds * fps
    dt = 1.0 / fps

    random.seed(7)
    cx_lon, cx_lat = -0.45, 51.47  # London
    planes = [Plane(cx_lon + random.uniform(-1.5, 1.5),
                    cx_lat + random.uniform(-1.0, 1.0),
                    random.uniform(0, 360)) for _ in range(40)]
    planes[0].focus = True

    pan_total = 1.0  # degrees lon over the whole clip (continuous chase-pan)

    cmd = [
        "ffmpeg", "-y", "-f", "image2pipe", "-vcodec", "mjpeg",
        "-video_size", f"{WIDTH}x{HEIGHT}", "-framerate", str(fps), "-i", "pipe:0",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-g", str(fps * 2), "-crf", "23",  # constant quality -> bitrate reflects content
        str(out_path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

    render_ms: list[float] = []
    for i in range(n_frames):
        lon = cx_lon + pan_total * (i / n_frames)
        for pl in planes:
            pl.step(dt)
        planes[0].lon, planes[0].lat = lon, cx_lat  # focus rides the camera

        t0 = time.perf_counter()
        c = surface.getCanvas()
        c.clear(BG)
        if mode == "raster":
            ox = _lon_to_gx(lon, z) - WIDTH / 2
            oy = _lat_to_gy(cx_lat, z) - HEIGHT / 2
            tx0, tx1 = int(ox // TILE), int((ox + WIDTH - 1) // TILE)
            ty0, ty1 = int(oy // TILE), int((oy + HEIGHT - 1) // TILE)
            for tx in range(tx0, tx1 + 1):
                for ty in range(ty0, ty1 + 1):
                    img = tiles.get((tx, ty))
                    if img:
                        c.drawImage(img, tx * TILE - ox, ty * TILE - oy)
        _draw_planes(c, planes, lon, cx_lat, z)
        img = surface.makeImageSnapshot()
        jpeg = img.encodeToData(skia.kJPEG, 92)
        render_ms.append((time.perf_counter() - t0) * 1000.0)
        try:
            proc.stdin.write(bytes(jpeg))
        except (BrokenPipeError, ValueError):
            break
    proc.stdin.close()
    proc.wait()

    render_ms.sort()
    size = out_path.stat().st_size
    return {
        "mode": mode,
        "p50_ms": render_ms[len(render_ms) // 2],
        "p99_ms": render_ms[min(len(render_ms) - 1, int(len(render_ms) * 0.99))],
        "mb": size / 1e6,
        "kbps": size * 8 / seconds / 1000.0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zoom", type=int, default=9)
    ap.add_argument("--seconds", type=int, default=20)
    ap.add_argument("--fps", type=int, default=12)
    args = ap.parse_args()

    # Tiles needed across the pan (start viewport .. end viewport), + margin.
    z, cx_lat = args.zoom, 51.47
    xr_lo = int((_lon_to_gx(-0.45, z) - WIDTH / 2) // TILE) - 1
    xr_hi = int((_lon_to_gx(-0.45 + 1.0, z) + WIDTH / 2) // TILE) + 1
    yr_lo = int((_lat_to_gy(cx_lat, z) - HEIGHT / 2) // TILE) - 1
    yr_hi = int((_lat_to_gy(cx_lat, z) + HEIGHT / 2) // TILE) + 1
    print(f"Prefetching tiles z={z}: x[{xr_lo}..{xr_hi}] y[{yr_lo}..{yr_hi}] "
          f"(~{(xr_hi-xr_lo+1)*(yr_hi-yr_lo+1)} tiles, cached after first run)")
    tiles = _load_tiles(z, range(xr_lo, xr_hi + 1), range(yr_lo, yr_hi + 1))
    print(f"  loaded {len(tiles)} tiles\n")

    results = []
    for mode in ("raster", "flat"):
        out = HERE / f"out_{mode}.mp4"
        print(f"Rendering {mode} -> {out.name} ...")
        results.append(render_clip(mode, z, args.seconds, args.fps, tiles, out))

    print("\n" + "=" * 66)
    print(f"{'mode':<8} {'render p50':>11} {'render p99':>11} {'size':>9} {'bitrate':>12}")
    print("-" * 66)
    for r in results:
        print(f"{r['mode']:<8} {r['p50_ms']:>9.1f}ms {r['p99_ms']:>9.1f}ms "
              f"{r['mb']:>7.1f}MB {r['kbps']:>9.0f}kbps")
    print("=" * 66)
    ras, flat = results[0], results[1]
    print(f"\nRaster vs flat @ equal quality (crf 23):")
    print(f"  render cost : {ras['p50_ms']/flat['p50_ms']:.2f}x")
    print(f"  bitrate     : {ras['kbps']/flat['kbps']:.2f}x  "
          f"({ras['kbps']:.0f} vs {flat['kbps']:.0f} kbps)")
    print(f"\nProduction currently caps video at 4500 kbps. If raster needs "
          f"~{ras['kbps']:.0f} kbps for good quality, that's the bandwidth/quality tradeoff.")
    print("Attribution required if productionised: © OpenStreetMap contributors, © CARTO")


if __name__ == "__main__":
    main()
