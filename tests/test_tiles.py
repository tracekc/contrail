"""Tests for the tile provider's failure handling — no network, no real fetch.

The key guarantee: a failed tile fetch is NEVER cached permanently as dark; it
becomes a short-lived retry-after marker (the bug that produced a persistent
dark square in the basemap). Skipped where skia isn't installed."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("skia")  # tiles imports skia; skip on skia-less envs

from contrail.renderer.tiles import TileProvider, lon_to_norm, lat_to_norm, world_px_for_zoom


def test_mercator_math():
    assert abs(lon_to_norm(-180)) < 1e-9
    assert abs(lon_to_norm(180) - 1.0) < 1e-9
    assert abs(lat_to_norm(0) - 0.5) < 1e-9      # equator = middle
    assert lat_to_norm(51.5) < 0.5                # northern hemisphere = upper half
    assert world_px_for_zoom(9) == 512 * 256


def test_failed_fetch_is_not_cached_permanently(tmp_path, monkeypatch):
    tp = TileProvider(cache_dir=tmp_path / "tc")
    enqueued = []
    monkeypatch.setattr(tp, "_enqueue", lambda key: enqueued.append(key))  # no network

    key = (9, 255, 170)
    tp._remember_negative(key)                    # simulate a failed fetch
    assert tp.get(*key) is None                   # cooling down -> dark, no re-enqueue
    assert enqueued == []

    # once the cooldown has elapsed, the tile must be retried, not stuck dark
    tp._mem[key] = time.time() - 1                 # expire the negative marker
    assert tp.get(*key) is None                    # still no image yet...
    assert enqueued == [key]                        # ...but a refetch was queued
    assert key not in tp._mem                       # expired marker cleared


def test_out_of_range_tile_returns_none(tmp_path):
    tp = TileProvider(cache_dir=tmp_path / "tc")
    assert tp.get(2, 99, 0) is None               # x beyond 2**2 tiles
    assert tp.get(2, 0, -1) is None
