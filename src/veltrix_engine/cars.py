"""
The car library: a car's parameters entered once, used for every session of
that car.

Where a parameter comes from, best first:

  1. stated at import (command-line options, the app's car settings for a file)
  2. read from the file (an iRacing setup gives the mass and static front weight)
  3. the car library — this module
  4. the placeholder default in VehicleParams, which is not any real car's and
     is never quoted as the car's

A library entry is matched to a session by the vehicle name the file gives
("Toyota GR86"), compared ignoring case and punctuation, or by any of the
entry's aliases. One JSON file per car in <data folder>/cars/.
"""

from __future__ import annotations

import json
import re
from dataclasses import fields
from pathlib import Path

from .vehicle import VehicleParams
from .versions import refuse_overwrite, upgrade

CAR_VERSION = 1
PARAMETERS = tuple(f.name for f in fields(VehicleParams) if f.name != "source")


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).lower())


class CarLibrary:
    def __init__(self, folder: str | Path):
        self.folder = Path(folder)

    def _path(self, name: str) -> Path:
        key = _key(name)
        if not key:
            raise ValueError("A car needs a name.")
        return self.folder / f"{key}.json"

    def names(self) -> list:
        out = []
        for p in sorted(self.folder.glob("*.json")) if self.folder.exists() else []:
            try:
                out.append(self.get_file(p)["name"])
            except (ValueError, KeyError, OSError):
                continue
        return out

    def get_file(self, path: Path) -> dict:
        return upgrade("car", json.loads(Path(path).read_text(encoding="utf-8")), CAR_VERSION)

    def get(self, name: str) -> dict | None:
        """The entry for this car, by name or alias, or None."""
        path = self._path(name)
        if path.exists():
            return self.get_file(path)
        key = _key(name)
        for p in sorted(self.folder.glob("*.json")) if self.folder.exists() else []:
            try:
                entry = self.get_file(p)
            except (ValueError, OSError):
                continue
            if key in {_key(a) for a in entry.get("aliases", [])}:
                return entry
        return None

    def save(self, name: str, parameters: dict, aliases: list | None = None, note: str = "") -> Path:
        """Add or change a car. Parameters not given keep what the entry had."""
        bad = sorted(set(parameters) - set(PARAMETERS))
        if bad:
            raise ValueError(f"Not car parameters: {', '.join(bad)}. Known: {', '.join(PARAMETERS)}.")
        old = self.get(name) or {}
        entry = {"version": CAR_VERSION, "name": old.get("name", name),
                 "aliases": sorted(set(old.get("aliases", [])) | set(aliases or [])),
                 "note": note or old.get("note", ""),
                 "parameters": {**old.get("parameters", {}), **{k: float(v) for k, v in parameters.items()}}}
        path = self._path(entry["name"])
        refuse_overwrite(path, "car", CAR_VERSION)
        self.folder.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entry, indent=1), encoding="utf-8")
        return path

    def remove(self, name: str) -> bool:
        path = self._path(name)
        if path.exists():
            path.unlink()
            return True
        return False

    def apply(self, vehicle: VehicleParams, vehicle_name: str) -> list:
        """Fill in what the library knows about this car, where it was not stated
        at import or read from the file. Returns the parameters taken."""
        entry = self.get(vehicle_name) if vehicle_name else None
        if not entry:
            return []
        taken = []
        for name, value in entry["parameters"].items():
            if name in PARAMETERS and vehicle.source.get(name) not in ("user", "file"):
                vehicle.set(name, value, "library")
                taken.append(name)
        return taken
