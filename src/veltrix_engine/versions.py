"""
Versions of the files Veltrix saves, and how an older file is brought up to date.

Every saved file — maths sets, event rules, mapping profiles, workspaces,
thresholds, notes, reviews, video links, the session model, the project
database — carries a format version. Reading one goes through `upgrade`:

  - a file from a newer release is refused with a clear message, never read
    half-understood (and so never overwritten with less than it held);
  - a file from an older release is brought up to date one step at a time by
    the migrations registered for its kind, then read as current.

A store that writes a file checks `refuse_overwrite` first, so a newer file
written by another release is left alone.
"""

from __future__ import annotations

import json
from pathlib import Path

from .errors import VersionError


def upgrade(kind: str, data: dict, current: int, steps: dict | None = None, key: str = "version",
            default: int = 1) -> dict:
    """`data` at version `current`. steps: {from version: function(data) -> data at the next version}.
    A file with no version is taken to be `default` (the first format of its kind)."""
    found = int(data.get(key, default))
    if found > current:
        raise VersionError(f"This {kind} was saved by a newer release of Veltrix (format {found}); this release reads "
                           f"up to format {current}. Update Veltrix to open it — it has not been changed.")
    steps = steps or {}
    while found < current:
        if found not in steps:
            raise VersionError(f"This {kind} is in format {found}, and there is no way to bring it to format {current}.")
        data = steps[found](dict(data))
        found += 1
        data[key] = found
    return data


def refuse_overwrite(path: str | Path, kind: str, current: int, key: str = "version") -> None:
    """Raise rather than overwrite a file of this kind that a newer release wrote."""
    path = Path(path)
    if not path.exists():
        return
    try:
        found = int(json.loads(path.read_text(encoding="utf-8")).get(key, 1))
    except (ValueError, TypeError, AttributeError, OSError):
        return                                 # not readable as ours: the caller's own error handling decides
    if found > current:
        raise VersionError(f"{path.name} holds a {kind} saved by a newer release (format {found}); it was not "
                           "overwritten. Update Veltrix to change it.")
