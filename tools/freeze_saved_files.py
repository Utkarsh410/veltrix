"""
Write one file of every kind Veltrix saves, as this release writes it, into
tests/fixtures/saved_release_<n>/. Later releases must open them unchanged
(tests/test_saved_files.py). Run once per release whose formats change:

    .venv/bin/python tools/freeze_saved_files.py 1
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from veltrix_engine import ProjectStore, events, load_session, thresholds  # noqa: E402
from veltrix_engine.maths import MathChannel, MathSet  # noqa: E402
from veltrix_engine.mapping_profiles import Profile  # noqa: E402
from veltrix_engine.user_data import AnnotationStore, annotate  # noqa: E402
from veltrix_app.video_sync import VideoLink, VideoStore  # noqa: E402
from veltrix_app.workspace import default_layout  # noqa: E402

GOLDEN = ROOT / "datasets" / "golden" / "logger_layout_3lap_20hz.csv"


def main(release: str) -> None:
    out = ROOT / "tests" / "fixtures" / f"saved_release_{release}"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    s = load_session(GOLDEN)
    sha = s.raw.sha256

    store = ProjectStore(out / "project")
    store.import_file(GOLDEN)
    store.close()

    ms = MathSet()
    ms.parameters["k"] = 2.0
    ms.channels["SpeedTimesK"] = MathChannel("SpeedTimesK", "speed * $k", unit="km/h", description="speed doubled")
    ms.save(out / "maths.json")
    rules = events.default_rules()
    rules.parameters["lock_on"] = 14.0
    rules.save(out / "event_rules.json")
    Profile(name="Logger layout").save(out / "profile.json")
    ws = default_layout(s.model)
    ws.name = "saved workspace"
    ws.save(out / "workspace.json")
    thresholds.save(out / "thresholds.json", {"engineering.LAPS_HIGH": 6})

    note = annotate(s.model, "kerb on exit", time=float(s.model.laps[1].t_start + 10), category="driver")
    AnnotationStore(out / "annotations").save(sha, [note])
    found, _ = events.detect(s.model, fit=False)
    victim = found[0] if found else events.Event(kind="Possible wheelspin", t_start=1.0, t_end=1.5, rule="wheelspin")
    events.ReviewStore(out / "reviews").put(sha, victim, "rejected", "a kerb", "tester")
    VideoStore(out / "videos").save(sha, [VideoLink(path="onboard.mp4", camera="roll hoop", offset=12.5)])
    (out / "SOURCE").write_text(f"{GOLDEN.name}\n{sha}\n", encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "1")
