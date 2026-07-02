"""Runtime configuration, loaded from environment / .env."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is optional at runtime
    pass


# adsb.fi caps a single point query at this radius.
MAX_RADIUS_NM = 250
# Default region used when COVERAGE_BBOX is unset: London TMA (very strong coverage).
DEFAULT_REGION = (51.47, -0.45, 250.0)


@dataclass(frozen=True)
class Config:
    adsb_source: str
    poll_interval: int
    region: tuple[float, float, float]  # (lat, lon, radius_nm)
    log_level: str

    @classmethod
    def load(cls) -> "Config":
        return cls(
            adsb_source=os.getenv("ADSB_SOURCE", "adsblol").strip().lower(),
            poll_interval=int(os.getenv("POLL_INTERVAL_SECONDS", "15")),
            region=_parse_region(os.getenv("COVERAGE_BBOX", "")),
            log_level=os.getenv("LOG_LEVEL", "info").strip().lower(),
        )


def _parse_region(bbox: str) -> tuple[float, float, float]:
    """Accept "lat1,lon1,lat2,lon2" and convert to a center + radius (nm).

    Falls back to DEFAULT_REGION when blank or malformed.
    """
    bbox = (bbox or "").strip()
    if not bbox:
        return DEFAULT_REGION
    try:
        la1, lo1, la2, lo2 = (float(x) for x in bbox.split(","))
    except ValueError:
        return DEFAULT_REGION
    center_lat = (la1 + la2) / 2
    center_lon = (lo1 + lo2) / 2
    radius = min(_haversine_nm(la1, lo1, la2, lo2) / 2, MAX_RADIUS_NM)
    return (center_lat, center_lon, max(radius, 1.0))


def _haversine_nm(la1: float, lo1: float, la2: float, lo2: float) -> float:
    r_nm = 3440.065
    p1, p2 = math.radians(la1), math.radians(la2)
    dp = math.radians(la2 - la1)
    dl = math.radians(lo2 - lo1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r_nm * math.asin(math.sqrt(a))
