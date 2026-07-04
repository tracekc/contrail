"""Tests for contrail.enrich photo handling — the negative-cache-avoidance fix."""

from __future__ import annotations

import requests

import contrail.enrich as enrich
from contrail.enrich_cache import EnrichmentCache


class _FakeResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_fetch_photo_200_with_photo(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(
        200, {"photos": [{"thumbnail_large": {"src": "http://x/p.jpg"},
                          "photographer": "Jane"}]}))
    fields, resolved = enrich._fetch_photo("abc123")
    assert resolved is True
    assert fields["photo_url"] == "http://x/p.jpg"
    assert fields["photo_credit"] == "Jane"


def test_fetch_photo_403_is_not_resolved(monkeypatch):
    """A 403 must NOT count as resolved, so it gets retried rather than cached
    permanently as 'no photo' — the root cause of the 7% hit rate."""
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(403))
    fields, resolved = enrich._fetch_photo("abc123")
    assert resolved is False
    assert fields == {}


def test_fetch_photo_200_empty_is_resolved(monkeypatch):
    """A genuine 'no photo on file' (200, empty list) is a real answer -> resolved."""
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(200, {"photos": []}))
    fields, resolved = enrich._fetch_photo("abc123")
    assert resolved is True
    assert fields == {}


def test_fetch_photo_network_error_not_resolved(monkeypatch):
    def _boom(*a, **k):
        raise requests.RequestException("timeout")
    monkeypatch.setattr(requests, "get", _boom)
    fields, resolved = enrich._fetch_photo("abc123")
    assert resolved is False
    assert fields == {}


def test_update_aircraft_merges_without_dropping_metadata(tmp_path):
    c = EnrichmentCache(path=tmp_path / "e.json")
    c.set_aircraft("ABC", {"registration": "G-X", "operator": "Acme",
                           "photo_resolved": False})
    c.update_aircraft("ABC", {"photo_url": "u", "photo_credit": "Sam",
                              "photo_resolved": True})
    e = c.get_aircraft("abc")
    assert e["registration"] == "G-X"   # preserved through the photo retry
    assert e["operator"] == "Acme"
    assert e["photo_url"] == "u"
    assert e["photo_resolved"] is True
