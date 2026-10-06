"""
Engine benchmarks on the golden datasets and on a long synthetic session.

  python benchmarks/bench_engine.py

Times are the best of three runs. Use them to spot regressions and, later, as
the baseline the native core has to beat.
"""

import tempfile
import time
from pathlib import Path

from veltrix_engine import ProjectStore, laps as lap_engine, load_session
from veltrix_engine.derived import compute_derived
from veltrix_engine.importers import read_csv
from veltrix_engine.synth import generate_session

GOLDEN = Path(__file__).resolve().parents[1] / "datasets" / "golden"


def best_of(fn, n=3):
    times, result = [], None
    for _ in range(n):
        t0 = time.perf_counter()
        result = fn()
        times.append(time.perf_counter() - t0)
    return min(times) * 1000, result


def bench_file(path: Path) -> None:
    ms_read, raw = best_of(lambda: read_csv(path))
    ms_load, s = best_of(lambda: load_session(path))
    ms_derived, _ = best_of(lambda: compute_derived(s.channels, s.rate_hz, s.vehicle))
    full = s.laps[s.laps["start_crossed"] & s.laps["end_crossed"]]["lap"].tolist()
    ms_delta, _ = best_of(lambda: lap_engine.compare_laps(s, full[0], full[1]))
    ms_sector, _ = best_of(lambda: lap_engine.sector_times(s))
    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(Path(tmp) / "p")
        t0 = time.perf_counter()
        sid = store.add_session(s, source_path=path)
        ms_store = (time.perf_counter() - t0) * 1000
        ms_read_back, _ = best_of(lambda: store.channels(sid))
        ms_read_two, _ = best_of(lambda: store.channels(sid, columns=["time", "speed"]))
        size = sum(f.stat().st_size for f in (Path(tmp) / "p" / "sessions" / str(sid)).glob("*.parquet"))
        store.close()
    rows, cols = len(s.channels), s.channels.shape[1] + s.derived.shape[1]
    print(f"\n{path.name}  —  {rows:,} samples × {cols} channels, {len(s.laps)} laps")
    for label, ms in [("read file", ms_read), ("full import pipeline", ms_load),
                      ("  of which derived channels", ms_derived),
                      ("lap delta on 1 m grid", ms_delta), ("sector times, all laps", ms_sector),
                      ("store (Parquet + SQLite)", ms_store), ("read back all channels", ms_read_back),
                      ("read back 2 channels", ms_read_two)]:
        print(f"  {label:<30} {ms:8.1f} ms")
    print(f"  {'Parquet size':<30} {size / 1e6:8.2f} MB")


if __name__ == "__main__":
    for name in ("logger_layout_3lap_20hz.csv", "monza_ac_gt3.csv.gz", "synthetic_gt3_5lap.csv.gz"):
        bench_file(GOLDEN / name)
    # A long stint: 30 laps at 100 Hz, about 40 minutes of running
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "synthetic_30lap.csv"
        generate_session(lap_count=30).to_csv(path, index=False, float_format="%.3f")
        bench_file(path)
