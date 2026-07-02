"""Curated rotation regions for the channel.

The show cycles through these every ~10-15 minutes (zooming out to the globe and
back in between them). They're chosen for DENSE ADS-B receiver coverage so the
map is always busy — sparse/ocean regions would look empty. Each is a center
point plus a radius (nm); the feed query is a circle, capped at 250nm.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    name: str          # on-air label ("London", "Tokyo")
    lat: float
    lon: float
    radius_nm: float = 250.0

    @property
    def tuple(self) -> tuple[float, float, float]:
        return (self.lat, self.lon, self.radius_nm)


# Ordered so consecutive hops cross the globe (Europe -> US -> back -> Gulf ->
# Asia), which makes the zoom-out/zoom-in travel beat feel like a real journey.
REGIONS: list[Region] = [
    Region("London", 51.47, -0.45),
    Region("New York", 40.64, -73.78),
    Region("Los Angeles", 33.94, -118.41),
    Region("Frankfurt", 50.04, 8.56),
    Region("Dubai", 25.25, 55.36),
    Region("Tokyo", 35.55, 139.78),
]
