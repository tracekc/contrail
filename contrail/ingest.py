"""ADS-B ingestion. Pulls aircraft states from a community feed.

adsb.fi / adsb.lol / airplanes.live all share the `re-api` v2 shape, so one
client covers them via a base-URL swap. OpenSky uses a different (OAuth2) API
and is left as a Phase-1 stub.
"""

from __future__ import annotations

import logging
import time

import requests

from .models import Aircraft

log = logging.getLogger(__name__)

# Be polite to free community feeds: minimum spacing between requests, and
# back off on HTTP 429 rather than hammering (which keeps you rate-limited).
MIN_REQUEST_SPACING_S = 1.2
MAX_RETRIES = 3

_BASE_URLS = {
    "adsbfi": "https://opendata.adsb.fi/api/v2",
    "adsblol": "https://api.adsb.lol/v2",
    "airplaneslive": "https://api.airplanes.live/v2",
}


class AdsbClient:
    def __init__(self, source: str = "adsblol", timeout: float = 20.0) -> None:
        if source == "opensky":
            raise NotImplementedError(
                "OpenSky uses OAuth2 and is not wired up in Phase 1; "
                "use ADSB_SOURCE=adsblol (keyless)."
            )
        if source not in _BASE_URLS:
            raise ValueError(f"Unknown ADSB_SOURCE: {source!r}")
        # Failover order: the chosen source first, then the other re-api mirrors.
        # They share the v2 response shape, so a region/squawk query works
        # against any of them — if one is down or rate-limiting, we try the next.
        self._sources = [source] + [s for s in _BASE_URLS if s != source]
        self._bases = [_BASE_URLS[s] for s in self._sources]
        self.base = self._bases[0]  # primary; kept for logging/back-compat
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "contrail/0.1 (skywatch)"})
        self._last_request = 0.0

    def _respect_spacing(self) -> None:
        wait = MIN_REQUEST_SPACING_S - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _get_one(self, base: str, path: str) -> list[Aircraft]:
        """Query a single source. Returns parsed aircraft on a 200; raises on
        network error, bad status, or being rate-limited past MAX_RETRIES."""
        url = f"{base}{path}"
        for attempt in range(MAX_RETRIES):
            self._respect_spacing()
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code == 429:
                back = float(resp.headers.get("Retry-After", 2 ** attempt))
                log.warning("rate limited on %s%s; backing off %.1fs", base, path, back)
                time.sleep(min(back, 10))
                continue
            resp.raise_for_status()
            payload = resp.json()
            raw = payload.get("aircraft") or payload.get("ac") or []
            out = []
            for d in raw:
                try:
                    out.append(Aircraft.from_adsbfi(d))
                except Exception as exc:  # never let one bad record kill the tick
                    log.debug("parse skip: %s", exc)
            return out
        raise RuntimeError(f"rate-limited past {MAX_RETRIES} retries on {base}{path}")

    def _get(self, path: str) -> list[Aircraft]:
        """Try each source in failover order; return the first success ([] is a
        valid success). Only an actual failure rolls over to the next source."""
        last_exc: Exception | None = None
        for i, base in enumerate(self._bases):
            try:
                return self._get_one(base, path)
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last_exc = exc
                more = i + 1 < len(self._bases)
                log.warning("source %s failed for %s: %s; %s", self._sources[i],
                            path, exc, "trying next source" if more else "no more sources")
        log.warning("all sources failed for %s (last: %s)", path, last_exc)
        return []

    def get_squawk(self, code: str) -> list[Aircraft]:
        """Global lookup by transponder squawk (e.g. 7700). Not region-limited."""
        return self._get(f"/squawk/{code}")

    def get_region(self, lat: float, lon: float, radius_nm: float) -> list[Aircraft]:
        return self._get(f"/lat/{lat:.4f}/lon/{lon:.4f}/dist/{radius_nm:.0f}")

    def get_military(self) -> list[Aircraft]:
        return self._get("/mil")
