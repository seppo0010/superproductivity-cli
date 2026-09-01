"""Small JSON files under STATE_DIR are this daemon's only persistence.
`_state_lock` serializes read-modify-write access across the reconciliation
loop, the webhook thread, and the Telegram polling thread."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from . import config

_state_lock = threading.Lock()


def _load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        config.log.warning("Could not read state file %s, starting fresh", path)
        return default


def _save_json(path: Path, data: dict) -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
