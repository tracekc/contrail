"""Disk-backed enrichment cache for aircraft details and routes.

Keyed by hex (aircraft metadata + photo) and callsign (route).
Pruned on load to drop entries not seen in 30 days.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

CACHE_PATH = Path(__file__).parent / "renderer" / "enrichment_cache.json"
_PRUNE_DAYS = 30
_ROUTE_TTL = 86400  # routes stale after 24h (schedules change day to day)


class EnrichmentCache:
    def __init__(self, path: Path = CACHE_PATH) -> None:
        self._path = path
        self._data: dict = {"aircraft": {}, "routes": {}}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._data = {
                "aircraft": raw.get("aircraft", {}),
                "routes": raw.get("routes", {}),
            }
            self._prune()
        except Exception as exc:
            log.warning("enrichment cache load failed: %s", exc)

    def _prune(self) -> None:
        cutoff = time.time() - _PRUNE_DAYS * 86400
        before = sum(len(v) for v in self._data.values())
        for section in ("aircraft", "routes"):
            self._data[section] = {
                k: v for k, v in self._data[section].items()
                if v.get("last_seen", 0) >= cutoff
            }
        pruned = before - sum(len(v) for v in self._data.values())
        if pruned:
            log.info("enrichment cache: pruned %d stale entries", pruned)
            self._save()

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("enrichment cache save failed: %s", exc)

    def get_aircraft(self, hex_: str) -> Optional[dict]:
        entry = self._data["aircraft"].get(hex_.lower())
        if entry:
            entry["last_seen"] = time.time()
        return entry

    def set_aircraft(self, hex_: str, data: dict[str, Any]) -> None:
        self._data["aircraft"][hex_.lower()] = {**data, "last_seen": time.time()}
        self._save()

    def get_route(self, callsign: str) -> Optional[dict]:
        entry = self._data["routes"].get(callsign.upper())
        if not entry:
            return None
        if time.time() - entry.get("cached_at", 0) > _ROUTE_TTL:
            return None
        entry["last_seen"] = time.time()
        return entry

    def set_route(self, callsign: str, data: dict[str, Any]) -> None:
        self._data["routes"][callsign.upper()] = {
            **data,
            "cached_at": time.time(),
            "last_seen": time.time(),
        }
        self._save()
