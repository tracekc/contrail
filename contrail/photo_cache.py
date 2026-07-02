"""Local photo cache: download aircraft photos, evict oldest when over limit.

Photos stored as contrail/renderer/photos/<hex>.jpg.
Max 200 files; when exceeded, the oldest-accessed files are deleted.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

PHOTO_DIR = Path(__file__).parent / "renderer" / "photos"
MAX_PHOTOS = 200


def get_photo_path(hex_: str, photo_url: Optional[str]) -> Optional[Path]:
    """Return local path to the aircraft photo, downloading if not cached.

    Downloads in the calling thread with a 10s timeout. Returns None if
    photo_url is absent or the download fails.
    """
    if not photo_url:
        return None

    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    dest = PHOTO_DIR / f"{hex_.lower()}.jpg"

    if dest.exists():
        return dest

    try:
        import requests
        resp = requests.get(
            photo_url, timeout=10,
            headers={"User-Agent": "contrail-skywatch/1.0"},
        )
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        _evict()
        return dest
    except Exception as exc:
        log.debug("photo download failed for %s: %s", hex_, exc)
        return None


def _evict() -> None:
    photos = sorted(PHOTO_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
    for p in photos[: max(0, len(photos) - MAX_PHOTOS)]:
        try:
            p.unlink()
        except OSError:
            pass
