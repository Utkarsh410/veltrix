"""Where the app keeps what it remembers between runs — maths channels, event
rules, mapping profiles, notes and video links. No Qt here, so the command
line reads the same files the app writes."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def data_dir() -> Path:
    """The app's data folder. VELTRIX_DATA_DIR overrides it."""
    override = os.environ.get("VELTRIX_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Veltrix Data"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "veltrix-data"
