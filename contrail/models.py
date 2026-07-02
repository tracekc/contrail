"""Core data structures shared across the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


def _clean_callsign(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    cs = raw.strip()
    return cs or None


@dataclass
class Aircraft:
    """A single parsed aircraft state from an ADS-B feed."""

    hex: str
    callsign: Optional[str]
    registration: Optional[str]
    type_code: Optional[str]
    type_desc: Optional[str]
    altitude: Optional[int]          # feet; None when on ground
    on_ground: bool
    ground_speed: Optional[float]    # knots
    track: Optional[float]           # degrees
    vertical_rate: Optional[int]     # feet/min (baro)
    squawk: Optional[str]
    emergency: Optional[str]         # feed's emergency field; "none" when normal
    category: Optional[str]
    lat: Optional[float]
    lon: Optional[float]
    distance_nm: Optional[float]     # from the region-query center, when applicable

    # Filled in by the enricher:
    airline: Optional[str] = None
    flags: list[str] = field(default_factory=list)

    @classmethod
    def from_adsbfi(cls, d: dict[str, Any]) -> "Aircraft":
        """Parse the adsb.fi / adsb.lol / airplanes.live `re-api` aircraft object."""
        alt_raw = d.get("alt_baro")
        on_ground = alt_raw == "ground"
        altitude = None if on_ground or alt_raw is None else _as_int(alt_raw)

        return cls(
            hex=str(d.get("hex", "")).lower(),
            callsign=_clean_callsign(d.get("flight")),
            registration=d.get("r"),
            type_code=d.get("t"),
            type_desc=d.get("desc"),
            altitude=altitude,
            on_ground=on_ground,
            ground_speed=_as_float(d.get("gs")),
            track=_as_float(d.get("track")),
            vertical_rate=_as_int(d.get("baro_rate")),
            squawk=d.get("squawk"),
            emergency=d.get("emergency"),
            category=d.get("category"),
            lat=_as_float(d.get("lat")),
            lon=_as_float(d.get("lon")),
            distance_nm=_as_float(d.get("dst")),
        )

    @property
    def label(self) -> str:
        """Best human identifier available."""
        parts = []
        if self.callsign:
            parts.append(self.callsign)
        if self.registration and self.registration != self.callsign:
            parts.append(f"({self.registration})")
        if self.type_desc:
            parts.append(f"· {self.type_desc}")
        elif self.type_code:
            parts.append(f"· {self.type_code}")
        return " ".join(parts) or self.hex


@dataclass
class StoryCandidate:
    """Something the detector thinks is worth narrating."""

    kind: str                    # e.g. "emergency_7700", "rare_type"
    priority: int                # base 0-100
    headline: str
    aircraft: Optional[Aircraft]
    detail: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0
    sensitive: bool = False      # e.g. military / hijack — suppressed by default


def _as_int(v: Any) -> Optional[int]:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _as_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
