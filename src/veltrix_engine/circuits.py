"""
Circuit definitions: official corner numbers and names.

The engine can find corners from the data, but how a circuit *numbers* them is
a convention no data can settle: one long corner may be counted as two turns,
a kink may or may not have a number, and Turn 1 depends on where the start
line is. A circuit file states the convention. When one fits the session, its
corners are used instead of detected ones.

A circuit file is JSON in `veltrix_engine/tracks/`:

    {
      "name": "Silverstone GP",
      "venues": ["silverstone"],          matched against the file's venue, lower-case
      "lap_length_m": 5787,               on the distance channel the file was made from
      "start_line": "old pit straight, before Copse",
      "corners": [
        {"number": 9, "name": "Copse", "direction": "R", "start_m": 258, "end_m": 556},
        ...
      ]
    }

Distances are from the start/finish line of the session the file was made
from. Before a file is used it is checked against the session: the lap length
must agree, and the car must actually be turning the stated way at each
corner. A file made for a different layout or start line fails that check and
is ignored, with a warning.

`draft_from_session` writes a first version from detected corners, to be
edited by hand: rename, renumber, split or merge.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

TRACK_DIR = Path(__file__).with_name("tracks")
LENGTH_TOLERANCE = 0.03       # lap length may differ by this much (distance channels are not surveys)
MIN_AGREEMENT = 0.8           # share of corners where the car must be turning the stated way


def _norm(text) -> str:
    return "".join(ch for ch in str(text).lower() if ch.isalnum())


def load(path: str | Path) -> dict:
    """Read and validate a circuit file."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("name", "lap_length_m", "corners"):
        if key not in data:
            raise ValueError(f"{path.name}: missing '{key}'.")
    last = -1.0
    for c in data["corners"]:
        for key in ("number", "direction", "start_m", "end_m"):
            if key not in c:
                raise ValueError(f"{path.name}: a corner is missing '{key}'.")
        if c["direction"] not in ("L", "R"):
            raise ValueError(f"{path.name}: corner {c['number']} direction must be 'L' or 'R'.")
        if not (last <= c["start_m"] < c["end_m"] <= data["lap_length_m"]):
            raise ValueError(f"{path.name}: corner {c['number']} is out of order or outside the lap "
                             "(corners must be listed in the order they are driven).")
        last = c["end_m"]
    data["path"] = str(path)
    return data


def available(directory: str | Path | None = None) -> list:
    """Every circuit file in the library (files that fail validation are skipped)."""
    out = []
    for path in sorted(Path(directory or TRACK_DIR).glob("*.json")):
        try:
            out.append(load(path))
        except (ValueError, json.JSONDecodeError):
            continue
    return out


def for_venue(venue: str, directory: str | Path | None = None) -> list:
    """Circuit files whose venue names match the session's venue."""
    key = _norm(venue)
    if not key:
        return []
    return [t for t in available(directory)
            if any(_norm(v) and (_norm(v) in key or key in _norm(v)) for v in t.get("venues", []))]


def fit(track: dict, lap_length: float, grid, signal) -> tuple[bool, str]:
    """Does this circuit file describe the lap in the session?
    grid, signal: the session's cornering signal along the lap (+ve left)."""
    length = float(track["lap_length_m"])
    if abs(lap_length / length - 1) > LENGTH_TOLERANCE:
        return False, f"lap length {lap_length:.0f} m against {length:.0f} m in the file"
    scale = lap_length / length
    level = 0.2 * np.nanpercentile(np.abs(signal), 95)
    agree = 0
    for c in track["corners"]:
        a = int(np.searchsorted(grid, c["start_m"] * scale))
        b = int(np.searchsorted(grid, c["end_m"] * scale))
        part = signal[a:max(b, a + 1)]
        want = 1 if c["direction"] == "L" else -1
        if len(part) and np.nanmean(part) * want > level:
            agree += 1
    share = agree / max(len(track["corners"]), 1)
    if share < MIN_AGREEMENT:
        return False, (f"the car turns the stated way in only {agree} of {len(track['corners'])} corners "
                       "(different layout or start line?)")
    return True, f"{agree} of {len(track['corners'])} corners line up"


MATCH_SLACK_M = 30.0          # an inferred corner within this of an official turn's span is that turn


def validate(track: dict, lap_length: float, inferred) -> dict:
    """Compare the corners found in the data with a circuit file's turns.

    inferred: the corner table detected from the data alone (corners.find_corners
    with track=None). Returns {"turns": [...], "extra": [...], "found", "total",
    "merged"}: for every official turn whether the data shows a corner there, which
    inferred corner, and how far its start, apex and end lie from the file's span;
    the inferred corners no official turn accounts for; and the turns the data sees
    as one corner (a chicane taken in one movement)."""
    scale = lap_length / float(track["lap_length_m"])
    rows, used = [], {}
    for c in track["corners"]:
        a, b = c["start_m"] * scale, c["end_m"] * scale
        best, overlap = None, 0.0
        for r in inferred.itertuples():
            o = min(b + MATCH_SLACK_M, r.end_m) - max(a - MATCH_SLACK_M, r.start_m)
            if o > overlap:
                best, overlap = r, o
        rec = {"turn": f"T{c['number']}", "name": c.get("name", ""), "direction": c["direction"],
               "start_m": round(a, 1), "end_m": round(b, 1), "found": best is not None, "inferred": None}
        if best is not None:
            rec.update({"inferred": best.corner, "direction_agrees": best.direction == c["direction"],
                        "start_offset_m": round(best.start_m - a, 1), "end_offset_m": round(best.end_m - b, 1),
                        "apex_inside": bool(a - MATCH_SLACK_M <= best.apex_m <= b + MATCH_SLACK_M)})
            used.setdefault(best.corner, []).append(rec["turn"])
        rows.append(rec)
    extra = [r.corner for r in inferred.itertuples() if r.corner not in used]
    merged = {k: v for k, v in used.items() if len(v) > 1}
    found = sum(r["found"] and r.get("direction_agrees", False) for r in rows)
    return {"turns": rows, "extra": extra, "merged": merged, "found": found, "total": len(rows)}


def draft_from_session(session, corners, name: str | None = None) -> dict:
    """A first circuit file from detected corners, for editing by hand."""
    venue = str(session.raw.info.get("Venue", "")).strip()
    return {
        "name": name or (venue.title() if venue else "Unnamed circuit"),
        "venues": [venue.lower()] if venue else [],
        "lap_length_m": round(float(session.lap_length), 1),
        "start_line": "as in the session this file was drafted from",
        "note": "Drafted from detected corners. Numbers follow the order driven from the start line; "
                "edit number and name to match the circuit's own, and split or merge corners as needed.",
        "corners": [{"number": i + 1, "name": "", "direction": c.direction,
                     "start_m": round(float(c.start_m)), "end_m": round(float(c.end_m))}
                    for i, c in enumerate(corners.itertuples())],
    }


def save(track: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {k: v for k, v in track.items() if k not in ("corners", "path")}
    lines = json.dumps(body, indent=2, ensure_ascii=False)[:-2] + ',\n  "corners": [\n'
    lines += ",\n".join("    " + json.dumps(c, ensure_ascii=False) for c in track["corners"]) + "\n  ]\n}\n"
    path.write_text(lines, encoding="utf-8")
    return path
