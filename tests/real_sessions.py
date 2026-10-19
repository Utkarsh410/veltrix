"""
The real sessions the regression check runs on, and what is frozen from each.

The files are the user's and stay on this machine (the repo ignores them); so
do their frozen outputs in tests/real_expected/. They are looked for in
VELTRIX_REAL_DATA (folders separated by the path separator), the repo's
datasets/ folder and ~/Downloads.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = Path(__file__).parent / "real_expected"
CORPUS = {
    "spa_gr86_ibt": "toyotagr86_spa 2024 up 2026-10-03 01-28-28.ibt",
    "hungaroring_amg_ld": "Hungaroring-mercedes_amg_gt3-32-2026.04.02-15.05.29.ld",
    "nurburgring_amg_csv": "nurburgring-mercedes_amg_gt3-10-2026.04.08-16.26.51.csv",
}


def find(name: str) -> Path | None:
    folders = [Path(p) for p in os.environ.get("VELTRIX_REAL_DATA", "").split(os.pathsep) if p]
    folders += [ROOT / "datasets", Path.home() / "Downloads"]
    return next((f / name for f in folders if (f / name).exists()), None)


def summary(path: Path) -> dict:
    """What a release must keep giving for this file: laps, sample counts,
    corners, every finding with its confidence, peaks and recorded incidents."""
    from veltrix_engine import corners as corner_engine, load_session
    from veltrix_engine.analysis import engineering, gg
    s = load_session(path)
    corners = corner_engine.find_corners(s)
    report = engineering.analyse(s)
    flying = list(s.laps.loc[s.laps["status"] == "flying", "lap"])
    try:
        peaks = {k: round(v, 3) for k, v in gg.gg_summary(s, flying or None).items() if k.startswith("peak")}
    except ValueError:
        peaks = {}
    return {
        "counts": s.counts,
        "laps": [[str(r.lap), round(float(r.lap_time_s), 3), r.status] for r in s.laps.itertuples()],
        "ideal_lap_s": report["summary"]["ideal_lap_s"] and round(report["summary"]["ideal_lap_s"], 3),
        "corners": [[c.corner, c.direction, c.kind, getattr(c, "basis", "")] for c in corners.itertuples()],
        "findings": [[sec["id"], f["conclusion"], f["confidence"]["level"]]
                     for sec in report["sections"] for f in sec["findings"]],
        "not_assessed": [sec["id"] for sec in report["sections"] if sec["not_assessed"]],
        "peaks": peaks,
        "warnings": s.warnings(),
    }
