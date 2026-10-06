"""
Build the golden datasets in datasets/golden/.

Run once; the outputs are committed and then treated as frozen. Re-running is
only for provenance — numpy does not guarantee identical random streams across
versions, which is exactly why the files are frozen rather than regenerated.

  python tools/build_golden.py --monza-raw path/to/Monzafc-5lap.csv
"""

import argparse
import gzip
from pathlib import Path

import numpy as np
import pandas as pd

from veltrix_engine.synth import generate_session

OUT = Path(__file__).resolve().parents[1] / "datasets" / "golden"


def write_gz(df: pd.DataFrame, name: str, float_format: str) -> None:
    # mtime=0 keeps the archive byte-identical between runs
    with gzip.GzipFile(OUT / name, "wb", mtime=0) as f:
        f.write(df.to_csv(index=False, float_format=float_format).encode())
    print(f"  {name}: {len(df):,} rows, {(OUT / name).stat().st_size / 1e6:.1f} MB")


def synthetic() -> pd.DataFrame:
    """Five flying laps at 100 Hz, front-left lock-up on lap 3."""
    df = generate_session(lap_count=5, seed=42, lockup_lap=3)
    write_gz(df, "synthetic_gt3_5lap.csv.gz", "%.3f")
    return df


def logger_layout(df: pd.DataFrame) -> None:
    """Laps 1-3 of the synthetic session laid out like a logger-software CSV
    export: metadata block, channel row, units row, 20 Hz, speed in m/s,
    lateral g in m/s², no lap channel — laps come from beacon markers."""
    part = df[df["LapNumber"] <= 3].iloc[::5].reset_index(drop=True)
    beacons = [df.loc[df["LapNumber"] == k, "Time"].iloc[0] for k in (2, 3)]
    out = pd.DataFrame({
        "Time": part["Time"], "Ground Speed": part["Speed"] / 3.6,
        "Engine RPM": part["RPM"], "Gear": part["Gear"],
        "Throttle Pos": part["ThrottlePos"], "Brake Pres Front": part["BrakePress"],
        "Steered Angle": part["SteeringAngle"],
        "G Force Lat": part["LateralAcc"] * 9.81, "G Force Long": part["LongAcc"] * 9.81,
        **{f"Tyre Temp {c} Centre": part[f"TyreTemp{c}_mid"] for c in ("FL", "FR", "RL", "RR")},
    })
    units = ["s", "m/s", "rpm", "", "%", "bar", "deg", "m/s/s", "m/s/s", "C", "C", "C", "C"]
    lines = [
        '"Format","Logger CSV File",,,"Workbook",""',
        '"Venue","Synthetic Circuit",,,"Worksheet",""',
        '"Vehicle","GT3",,,"Vehicle Desc",""',
        '"Driver","Test Driver",,,"Engine ID",""',
        '"Sample Rate","20.000","Hz"',
        '"Beacon Markers","' + " ".join(f"{b:.3f}" for b in beacons) + '"',
        "",
        ",".join(f'"{c}"' for c in out.columns),
        ",".join(f'"{u}"' for u in units),
        "",
    ]
    text = "\n".join(lines) + "\n" + out.to_csv(index=False, header=False, float_format="%.3f")
    (OUT / "logger_layout_3lap_20hz.csv").write_text(text)
    print(f"  logger_layout_3lap_20hz.csv: {len(out):,} rows")


def monza(raw_path: Path) -> None:
    """Assetto Corsa GT3 at Monza (sim), laps 0-4, one row per metre of track.
    Source: 'Assetto Corsa GT3 Telemetry – Monza Full Course' by Nasim
    (nasimrajlaskar) on Kaggle, MIT licence."""
    raw = pd.read_csv(raw_path)
    raw = raw[raw["lapIndex"] <= 4].reset_index(drop=True)     # lap 5 is a bad recording
    # Per-lap timer stacked into one session clock
    t = raw["lap_time"].to_numpy(float)
    restart = np.flatnonzero(np.diff(t) < 0) + 1
    jump = np.zeros_like(t)
    jump[restart] = t[restart - 1]
    out = pd.DataFrame({
        "Time": t + np.cumsum(jump),
        "Lap": raw["lapIndex"],
        "Distance": raw["binIndex"],                   # metres from the line, resets each lap
        "Speed": raw["velocity_X"] * 3.6,              # m/s -> km/h
        "Throttle": raw["throttle"] * 100, "Brake": raw["brake"] * 100,
        "Steering": raw["steering"], "Gear": raw["gear"], "RPM": raw["rpm"],
        "PosX": raw["world_position_X"], "PosY": raw["world_position_Y"],   # Z is height
    })
    write_gz(out, "monza_ac_gt3.csv.gz", "%.3f")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--monza-raw", type=Path)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    logger_layout(synthetic())
    if args.monza_raw:
        monza(args.monza_raw)
