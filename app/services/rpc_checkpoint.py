"""Persistent block-number checkpoints for resumable scans.

Backed by a single JSON file under a configurable directory. Fully reusable —
any feature needing resumable block iteration (launchpad discovery, wallet
monitoring, whale alerts) can load/save a named checkpoint.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

_FILENAME = "checkpoints.json"


def _path() -> Path:
    return Path(settings.launchpad_checkpoint_dir) / _FILENAME


def load_checkpoint(key: str) -> int | None:
    """Return the last saved block for *key*, or None if never checkpointed."""
    p = _path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        val = data.get(key)
        return int(val) if val is not None else None
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        logger.warning("Failed to read checkpoint %r: %s", key, exc)
        return None


def save_checkpoint(key: str, block: int) -> None:
    """Persist *block* as the last completed block for *key*."""
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, int] = {}
    if p.exists():
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    data[key] = block
    try:
        p.write_text(json.dumps(data, indent=2))
    except OSError as exc:
        logger.warning("Failed to save checkpoint %r: %s", key, exc)
