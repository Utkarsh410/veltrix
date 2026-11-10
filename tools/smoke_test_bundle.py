"""
Check a built app, not the code: give it two sessions, as if they were
dropped on its icon, and wait for their reports, debriefs (with PDFs) and
coaching cards. Run by the release workflow on each platform's build:

    python tools/smoke_test_bundle.py "dist/Veltrix Data.app/Contents/MacOS/Veltrix Data"
    python tools/smoke_test_bundle.py "dist/Veltrix Data/Veltrix Data.exe"

Exit 0 when every file was read and every document written.
"""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS = [ROOT / "datasets" / "golden" / "synthetic_gt3_5lap.csv.gz",
            ROOT / "datasets" / "golden" / "monza_ac_gt3.csv.gz"]
TIMEOUT_S = 600


def smoke_test(executable: Path) -> int:
    work = Path(tempfile.mkdtemp(prefix="veltrix-smoke-"))
    data = work / "data"
    inputs = []
    for f in SESSIONS:
        shutil.copy(f, work / f.name)
        inputs.append(str(work / f.name))
    env = dict(os.environ, VELTRIX_DATA_DIR=str(data))
    index = data / "reports" / "index.csv"
    started = time.monotonic()
    app = subprocess.Popen([str(executable), *inputs], env=env)
    try:
        while not index.exists():
            if app.poll() is not None:
                print(f"The app quit (exit {app.returncode}) before its reports were written.")
                return 1
            if time.monotonic() - started > TIMEOUT_S:
                print(f"No reports after {TIMEOUT_S} s.")
                return 1
            time.sleep(2)
        time.sleep(2)                                   # the index is written last
    finally:
        app.kill()
        app.wait()
    rows = list(csv.DictReader(index.open(encoding="utf-8")))
    written = {p.name for p in (data / "reports").iterdir()}
    problems = [f"{r['file']}: {r['status']} {r['error']}" for r in rows if r["status"] != "ok"]
    for f in SESSIONS:
        stem = f.name.removesuffix(".gz").rsplit(".", 1)[0]
        for suffix in ("_report.html", "_debrief.html", "_debrief.pdf", "_coaching.html"):
            if stem + suffix not in written:
                problems.append(f"missing {stem}{suffix}")
    for r in rows:
        print(f"{r['file']}: {r['status']}, {r['laps']} laps, best {r['best_lap_s']} s")
    print(f"{len(rows)} file(s) in {time.monotonic() - started:.0f} s" + (": OK" if not problems else ""))
    for p in problems:
        print("PROBLEM:", p)
    return 1 if problems or len(rows) != len(SESSIONS) else 0


if __name__ == "__main__":
    sys.exit(smoke_test(Path(sys.argv[1])))
