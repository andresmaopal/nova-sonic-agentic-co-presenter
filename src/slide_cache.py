"""Slide cache — persist preprocessed slides to disk for fast restarts.

Caches the list of SlideData objects as a JSON file keyed by the source
file's name and modification time. If the file hasn't changed, the cached
version is loaded instantly instead of re-running LibreOffice + pdf2image.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import List, Optional

from src.models import SlideData

logger = logging.getLogger(__name__)

CACHE_DIR = Path(".slide_cache")


def _cache_key(file_path: str) -> str:
    """Build a cache key from filename + size + mtime."""
    p = Path(file_path)
    stat = p.stat()
    raw = f"{p.name}:{stat.st_size}:{stat.st_mtime}"
    return hashlib.md5(raw.encode()).hexdigest()


def _cache_path(file_path: str) -> Path:
    """Return the path to the cached JSON file."""
    return CACHE_DIR / f"{_cache_key(file_path)}.json"


def load_cached(file_path: str) -> Optional[List[SlideData]]:
    """Load cached slides if available and still valid.

    Returns None if no cache exists or the file has changed.
    """
    cp = _cache_path(file_path)
    if not cp.exists():
        return None

    try:
        data = json.loads(cp.read_text())
        slides = [
            SlideData(
                index=s["index"],
                image_base64=s["image_base64"],
                speaker_notes=s.get("speaker_notes", ""),
            )
            for s in data["slides"]
        ]
        logger.info("Loaded %d slides from cache (%s)", len(slides), cp.name)
        return slides
    except Exception as e:
        logger.warning("Cache load failed, will reprocess: %s", e)
        return None


def save_cache(file_path: str, slides: List[SlideData]) -> None:
    """Save preprocessed slides to disk cache."""
    CACHE_DIR.mkdir(exist_ok=True)
    cp = _cache_path(file_path)

    data = {
        "source": str(file_path),
        "slides": [
            {
                "index": s.index,
                "image_base64": s.image_base64,
                "speaker_notes": s.speaker_notes,
            }
            for s in slides
        ],
    }

    cp.write_text(json.dumps(data))
    logger.info("Cached %d slides to %s", len(slides), cp.name)
