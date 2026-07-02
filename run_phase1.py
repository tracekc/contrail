#!/usr/bin/env python3
"""Contrail — Phase 1 runner.

Ingest -> enrich -> detect, printing scored story candidates to the console.
No audio, no video, no streaming. Proves the core hypothesis: is there always
something worth narrating? Runs keyless against adsb.fi.

    pip install -r requirements.txt
    python run_phase1.py            # loop forever
    python run_phase1.py --once     # single tick then exit
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone

from contrail.config import Config
from contrail.detect import EventDetector
from contrail.enrich import enrich_all
from contrail.ingest import AdsbClient
from contrail.models import Aircraft

EMERGENCY_SQUAWKS = ("7700", "7600")


def _dedupe(aircraft: list[Aircraft]) -> list[Aircraft]:
    seen: dict[str, Aircraft] = {}
    for ac in aircraft:
        if ac.hex and ac.hex not in seen:
            seen[ac.hex] = ac
    return list(seen.values())


def tick(client: AdsbClient, detector: EventDetector, cfg: Config) -> None:
    emergencies: list[Aircraft] = []
    for code in EMERGENCY_SQUAWKS:
        emergencies += client.get_squawk(code)

    region = client.get_region(*cfg.region)
    aircraft = enrich_all(_dedupe(emergencies + region))

    context = {"region_count": len(region), "region": cfg.region}
    candidates = detector.detect(aircraft, context)

    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    lat, lon, rad = cfg.region
    print(
        f"\n[{ts}Z] region={lat:.2f},{lon:.2f} r={rad:.0f}nm  "
        f"aircraft={len(aircraft)}  emergencies={len(emergencies)}  "
        f"candidates={len(candidates)}"
    )
    if not candidates:
        print("   (nothing above threshold this tick)")
    for c in candidates:
        marker = "‼" if c.priority >= 80 else ("•" if c.priority >= 45 else "·")
        print(f"   {marker} [{c.score:>4.0f}] {c.kind:<18} {c.headline}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Contrail Phase 1 — detect-only")
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    args = parser.parse_args()

    cfg = Config.load()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )

    client = AdsbClient(cfg.adsb_source)
    detector = EventDetector()

    print(
        f"Contrail Phase 1 — source={cfg.adsb_source} "
        f"poll={cfg.poll_interval}s region={cfg.region}"
    )

    if args.once:
        tick(client, detector, cfg)
        return

    try:
        while True:
            tick(client, detector, cfg)
            time.sleep(cfg.poll_interval)
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
