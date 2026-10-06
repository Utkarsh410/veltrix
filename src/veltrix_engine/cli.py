"""
Command line: inspect a telemetry file with the engine.

  veltrix inspect session.csv
  veltrix inspect session.csv --ref 5 --compare 11
  veltrix inspect session.csv --flip steering --wheelbase 2.463 --steer-ratio 12.5
  veltrix inspect session.csv --unit G_LAT=g G_LON=g
  veltrix inspect session.csv --export out/
  veltrix report session.csv -o session.html

Prints what the engine read, every assumption it made, the lap table, sectors,
a lap comparison and the engineering summaries the file's channels allow.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import corners as corner_engine
from . import laps as lap_engine
from .analysis import advice, balance, gg, handling, tyres
from .schema import CORNERS, tyre_temp_channel
from .session import load_session
from .track import track_xy
from .vehicle import VehicleParams


def fmt_time(seconds: float) -> str:
    if not np.isfinite(seconds):
        return "—"
    m, s = divmod(float(seconds), 60.0)
    return f"{int(m)}:{s:06.3f}"


def heading(text: str) -> None:
    print(f"\n{text}\n" + "─" * len(text))


def _load(args):
    """Load the file named on the command line with the shared import options."""
    vehicle = VehicleParams()
    for field in ("wheelbase_m", "steer_ratio", "mass_kg", "grip_limit_g"):
        value = getattr(args, field)
        if value is not None:
            setattr(vehicle, field, value)
    try:
        units = dict(item.split("=", 1) for item in (args.unit or ()))
    except ValueError:
        raise ValueError("--unit expects COLUMN=UNIT, for example --unit G_LAT=g")
    return load_session(args.file, vehicle=vehicle, flip=tuple(args.flip or ()), rate_hz=args.rate,
                        units=units)


def report(args) -> int:
    from .report import write_report
    s = _load(args)
    stem = Path(args.file).name.split(".csv")[0]
    out = write_report(s, args.output or f"{stem}_report.html", title=args.title)
    warnings = len(s.warnings())
    print(f"Report written to {out}  ({out.stat().st_size / 1024:.0f} KB, {len(s.laps)} laps, "
          f"{warnings} warning{'s' if warnings != 1 else ''} from the import)")
    return 0


def inspect(args) -> int:
    t0 = time.perf_counter()
    s = _load(args)
    elapsed = time.perf_counter() - t0
    frame = s.frame()

    # ── file ───────────────────────────────────────────────────────────────
    heading(f"{Path(args.file).name}")
    info = s.raw.info
    for key in ("Venue", "Vehicle", "Driver", "Log Date", "Log Time"):
        if info.get(key):
            print(f"  {key:<10} {info[key]}")
    print(f"  Loaded     {len(s.raw.data):,} rows × {s.raw.data.shape[1]} columns in {elapsed:.1f} s")
    print(f"  Normalised {len(frame):,} samples at {s.rate_hz:g} Hz, "
          f"{frame['time'].iloc[-1] - frame['time'].iloc[0]:,.0f} s")
    if s.lap_length:
        print(f"  Lap length {s.lap_length:,.0f} m")

    heading("Channels")
    print(f"  Mapped {len(s.mapping)} of {s.raw.data.shape[1]} columns:")
    for cid, col in s.mapping.items():
        print(f"    {cid:<22} ← {col:<22} [{s.units.get(cid, '')}]")
    unmapped = [c for c in s.raw.data.columns if c not in s.mapping.values()]
    if unmapped:
        print(f"  Not used ({len(unmapped)}): {', '.join(map(str, unmapped))}")
    print(f"  Derived: {', '.join(s.derived.columns) or 'none'}")

    heading("Notes from the import")
    for level, message in s.notes:
        print(f"  {'!' if level == 'warning' else '·'} {message}")

    # ── laps ───────────────────────────────────────────────────────────────
    heading("Laps")
    best = lap_engine.best_lap(s)
    best_time = float(s.laps.loc[s.laps["lap"] == best, "lap_time_s"].iloc[0])
    print(f"  {'Lap':>4} {'Stint':>5} {'Time':>10} {'Δ best':>8} {'Dist m':>8}  Status")
    for lap in s.laps.itertuples():
        delta = lap.lap_time_s - best_time
        mark = "  ← best" if lap.lap == best else ""
        print(f"  {str(lap.lap):>4} {lap.stint:>5} {fmt_time(lap.lap_time_s):>10} "
              f"{delta:>+8.3f} {lap.dist_m:>8.0f}  {lap.status}{mark}")

    sectors = None
    if s.lap_length and len(s.laps) > 2:
        sectors = lap_engine.sector_times(s, n_sectors=args.sectors)
        cols = [c for c in sectors.columns if c.startswith("s") and c[1:].isdigit()]
        flying = sectors[sectors["status"] == "flying"]
        heading(f"Sectors ({args.sectors} equal lengths, flying laps)")
        print("  " + f"{'Lap':>4} " + " ".join(f"{c.upper():>9}" for c in cols) + f" {'Lap time':>10}")
        for row in flying.itertuples():
            cells = []
            for c in cols:
                v = getattr(row, c)
                cells.append(f"{v:>8.3f}{'*' if v == flying[c].min() else ' '}")
            print(f"  {str(row.lap):>4} " + " ".join(cells) + f" {fmt_time(row.lap_time_s):>10}")
        if len(flying):
            theo = lap_engine.theoretical_best(sectors)
            print(f"  * best sector.  Theoretical best {fmt_time(theo)} "
                  f"({theo - best_time:+.3f} s vs best lap {best})")

    # ── corners ────────────────────────────────────────────────────────────
    corner_result = None
    if s.lap_length and len(s.laps) > 2:
        try:
            corner_result = corner_engine.analyse(s)
        except ValueError as e:
            print(f"\nCorners: not available ({e})")
    if corner_result:
        cs, summary = corner_result["corners"], corner_result["summary"].set_index("corner")
        heading(f"Corners ({len(cs)} found from {cs.attrs['source']}; flying laps)")
        print(f"  {'':<4} {'':>3} {'Kind':<7} {'From m':>7} {'Apex m':>7} {'To m':>7} {'Best s':>8} {'Lap':>4} "
              f"{'Mean s':>8} {'Spread':>7} {'On table':>9} {'Min km/h':>9}")
        for c in cs.itertuples():
            if c.corner not in summary.index:
                continue
            r = summary.loc[c.corner]
            print(f"  {c.corner:<4} {c.direction:>3} {c.kind:<7} {c.start_m:>7.0f} {c.apex_m:>7.0f} {c.end_m:>7.0f} "
                  f"{r.best_s:>8.3f} {str(r.best_lap):>4} {r.mean_s:>8.3f} {r.spread_s:>7.3f} "
                  f"{r.available_s:>9.3f} {r.min_speed_mean:>9.0f}")
        ideal = corner_result["ideal_lap_s"]
        top = summary.sort_values("available_s", ascending=False).head(3)
        print(f"  Ideal lap (best time through every corner): {fmt_time(ideal)} ({ideal - best_time:+.3f} s vs best lap {best})")
        print("  Most time on the table: " + ", ".join(f"{k} {v.available_s:.2f} s" for k, v in top.iterrows()))

    # ── understeer / oversteer ─────────────────────────────────────────────
    handling_result = None
    if corner_result:
        try:
            handling_result = handling.analyse(s, corner_result["corners"])
        except ValueError as e:
            print(f"\nUndersteer / oversteer: not available ({e})")
    if handling_result:
        ev, model = handling_result["events"], handling_result["model"]
        n_us, n_os = int((ev["kind"] == "understeer").sum()), int((ev["kind"] == "oversteer").sum())
        heading(f"Understeer and oversteer ({n_us} understeer, {n_os} oversteer incidents)")
        print(f"  Lateral g {model['lateral_source']}; a lap must differ from the norm by at least "
              f"{model['threshold_deg']:.0f} deg of steering for 0.25 s, more where laps naturally vary.")
        busy = handling_result["by_corner"]
        busy = busy[(busy["understeer"] + busy["oversteer"]) > 0]
        if len(busy):
            print(f"  {'':<4} {'Under':>6} {'Over':>5} {'Severe':>7} {'Laps':>7}  Usually")
            for r in busy.itertuples():
                print(f"  {r.corner:<4} {r.understeer:>6} {r.oversteer:>5} {r.severe:>7} "
                      f"{r.laps_affected:>3}/{r.laps:<3}  {r.usual_phase}, {r.usual_cause}")
        worst = ev[ev["severity"] == "severe"]
        if len(worst):
            print("  Severe incidents:")
            for e in worst.itertuples():
                lock = f", {e.opposite_lock_deg:.0f} deg opposite lock" if e.counter_steer else ""
                cost = f", {e.cost_s:+.2f} s in the corner" if np.isfinite(e.cost_s) else ""
                print(f"    lap {e.lap:>2} {e.corner:<4} {e.phase:<5} {e.peak_m:>5.0f} m  {e.cause}{lock}, "
                      f"{e.duration_s:.1f} s at {e.speed:.0f} km/h{cost}")
        elif not len(ev):
            print("  No incidents found.")

    # ── suggestions ────────────────────────────────────────────────────────
    suggestions = advice.suggest(s, corner_result, handling_result) if corner_result else None
    if suggestions:
        for area, title in (("driver", "Suggestions for the driver"), ("setup", "Setup directions to test")):
            heading(title)
            if not suggestions[area]:
                print("  Nothing in this session's data points to a change.")
            for n, item in enumerate(suggestions[area], 1):
                gain = f"  (up to {item['gain_s']:.2f} s a lap)" if item["gain_s"] else ""
                print(f"  {n}. {item['title']}{gain}")
                print(f"     Where: {item['where']}  ·  confidence: {item['confidence']}")
                print(f"     Do:    {item['action']}")
                for line in item["evidence"]:
                    print(f"     - {line}")
        print("\n  These are suggestions to test, one at a time. The engine does not know the car's current setup.")

    # ── comparison ─────────────────────────────────────────────────────────
    cmp_ = None
    if s.lap_length and len(s.laps) > 2:
        ref = args.ref if args.ref is not None else best
        if args.compare is not None:
            other = args.compare
        else:
            pool = s.laps[(s.laps["status"] == "flying") & (s.laps["lap"] != ref)]
            other = pool.loc[pool["lap_time_s"].idxmin(), "lap"] if len(pool) else None
        if other is not None:
            channels = [c for c in ("speed", "throttle", "brake", "steering", "gear") if c in frame]
            cmp_ = lap_engine.compare_laps(s, ref, other, channels=channels)
            delta, grid = cmp_["delta_t"].to_numpy(), cmp_.index.to_numpy()
            heading(f"Lap {other} against reference lap {ref}")
            print(f"  Delta at the line: {delta[-1]:+.3f} s  (+ve = lap {other} is slower)")
            swing = lap_engine.biggest_swing(delta, grid)
            if swing:
                (a, b, loss), (c, d, gain) = swing
                print(f"  Biggest loss: {loss:+.3f} s between {a:,.0f} m and {b:,.0f} m")
                print(f"  Biggest gain: {gain:+.3f} s between {c:,.0f} m and {d:,.0f} m")
            if "speed_ref" in cmp_:
                k = int(np.argmax(np.abs(cmp_["speed_cmp"] - cmp_["speed_ref"]).to_numpy()))
                print(f"  Largest speed difference: {cmp_['speed_cmp'].iloc[k] - cmp_['speed_ref'].iloc[k]:+.1f} "
                      f"km/h at {grid[k]:,.0f} m")
            if corner_result and {ref, other} <= set(corner_result["metrics"]["lap"]):
                by_corner = corner_engine.compare_corners(corner_result["metrics"], ref, other)
                print(f"\n  Corner by corner (+ve time = lap {other} slower):")
                for row in by_corner.itertuples():
                    print(f"    {row.corner:<4} {row.d_time_s:>+7.3f} s   {row.finding}")

    # ── engineering ────────────────────────────────────────────────────────
    coarse = "acc_lat" in s.coarse and "acc_lat" not in s.rebuilt
    caveat = ("  ! Not reliable for this file: lateral g is recorded in coarse steps and could not "
              "be rebuilt (see the import notes).")
    rebuilt_line = None
    if s.rebuilt:
        parts = []
        if "acc_lat" in s.rebuilt:
            parts.append("lateral g rebuilt from damper roll")
        if "acc_long_calc" in s.rebuilt:
            parts.append(f"longitudinal g from speed at {s.rebuilt['acc_long_calc']['bandwidth_hz']:g} Hz")
        rebuilt_line = "  · Uses " + " and ".join(parts) + " (logged channels too coarse)."
    if "oversteer_metric" in frame or ("acc_lat" in frame and not coarse):
        heading("Grip (against the reference envelope)")
        if rebuilt_line:
            print(rebuilt_line)
        g = gg.gg_summary(s)
        print(f"  Peak lateral {g['peak_lat_g']:.2f} g · braking {g['peak_brake_g']:.2f} g · "
              f"drive {g['peak_drive_g']:.2f} g · combined {g['peak_combined_g']:.2f} g")
        print(f"  Mean utilisation {g['mean_util']:.0%} of {s.vehicle.grip_limit_g:g} g; "
              f"above 75%: {g['frac_above_75']:.0%} of samples")

        per_lap = balance.balance_by_lap(s)
        if per_lap is not None and len(per_lap):
            heading("Balance (oversteer metric while cornering: −ve understeer, +ve oversteer)")
            if rebuilt_line:
                print(rebuilt_line)
            flying_laps = s.laps.loc[s.laps["status"] == "flying", "lap"]
            vals = per_lap.reindex(flying_laps).dropna()
            if len(vals):
                print(f"  Flying laps: mean {vals.mean():+.3f} g, first {vals.iloc[0]:+.3f} g, last {vals.iloc[-1]:+.3f} g")
            sc = balance.steering_characteristic(s)
            if sc:
                print(f"  Steering ratio from the data: {sc['ratio']:.1f} (configured {s.vehicle.steer_ratio:g}, "
                      f"for a {s.vehicle.wheelbase_m:g} m wheelbase)")
                print(f"  Understeer gradient: {sc['understeer_deg_per_g']:+.2f} deg/g at the road wheels "
                      f"({sc['steer_deg_per_g']:+.1f} deg/g at the steering wheel)")
                for stint in sorted(s.laps["stint"].unique()):
                    in_stint = s.laps[(s.laps["stint"] == stint) & (s.laps["status"] == "flying")]["lap"]
                    part = balance.steering_characteristic(s, laps=list(in_stint), ratio=sc["ratio"]) \
                        if len(in_stint) else None
                    if part:
                        print(f"    stint {stint}: {part['understeer_deg_per_g']:+.2f} deg/g")
            if "lltd_front" in frame and frame["lltd_front"].notna().any():
                print(f"  Front share of lateral load transfer: {frame['lltd_front'].mean():.1%} "
                      f"(uses wheel rates {s.vehicle.wheel_rate_front_N_mm:g} / "
                      f"{s.vehicle.wheel_rate_rear_N_mm:g} N/mm)")

    elif coarse:
        heading("Grip and balance")
        print(caveat)

    corners = [c for c in CORNERS if tyre_temp_channel(frame.columns, c)]
    if corners:
        heading("Tyres")
        summary = tyres.lap_summary(s)
        for stint, rows in summary.groupby("stint"):
            temps = "  ".join(f"{c.upper()} {rows[f'temp_{c}_mean'].mean():.0f}" for c in corners)
            print(f"  Stint {stint}: {len(rows)} laps, mean temperature °C  {temps}")
        events = tyres.detect_anomalies(s)
        print(f"  Temperature anomalies: {len(events)}")
        for e in events.itertuples():
            where = " (pit lap — tyre change)" if e.lap_status == "pit" else ""
            print(f"    lap {e.lap} {e.corner.upper()} {e.delta_C:+.1f} °C to {e.temp_C:.0f} °C{where}")

    heading("Degradation (lap time against laps since the stint began)")
    for stint in sorted(s.laps["stint"].unique()):
        deg = tyres.fit_degradation(s, stint=int(stint))
        if np.isfinite(deg["slope"]):
            trend = "" if deg["r_squared"] >= 0.3 else " — no clear trend"
            print(f"  Stint {stint}: {deg['slope'] * 1000:+.0f} ms/lap over {len(deg['ages'])} laps "
                  f"(R² {deg['r_squared']:.2f}){trend}")
        else:
            print(f"  Stint {stint}: fewer than three usable laps")

    try:
        xy = track_xy(s, lap=best)
        print(f"\nTrack map for lap {best}: {len(xy):,} points, source: {xy.attrs['source']}")
    except ValueError:
        xy = None
        print("\nTrack map: not available (no position, or no speed and lateral g)")

    # ── export ─────────────────────────────────────────────────────────────
    if args.export:
        out = Path(args.export)
        out.mkdir(parents=True, exist_ok=True)
        s.laps.to_csv(out / "laps.csv", index=False)
        if sectors is not None:
            sectors.to_csv(out / "sectors.csv", index=False)
        if cmp_ is not None:
            cmp_.to_csv(out / "lap_comparison.csv")
        if corner_result:
            corner_result["corners"].to_csv(out / "corners.csv", index=False)
            corner_result["metrics"].to_csv(out / "corner_metrics.csv", index=False)
            corner_result["summary"].to_csv(out / "corner_summary.csv", index=False)
        if handling_result:
            handling_result["events"].to_csv(out / "balance_incidents.csv", index=False)
            handling_result["by_corner"].to_csv(out / "balance_by_corner.csv", index=False)
            handling_result["tendency"].to_csv(out / "balance_tendency.csv", index=False)
        if suggestions:
            rows = [{**i, "evidence": " | ".join(i["evidence"])} for area in ("driver", "setup") for i in suggestions[area]]
            pd.DataFrame(rows, columns=["area", "title", "where", "action", "evidence", "confidence", "gain_s"]) \
                .to_csv(out / "suggestions.csv", index=False)
        if xy is not None:
            xy.to_csv(out / "track_map.csv", index=False)
        pd.DataFrame(s.notes, columns=["level", "message"]).to_csv(out / "import_notes.csv", index=False)
        print(f"\nTables written to {out}/")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="veltrix", description="Veltrix Data analysis engine")
    sub = ap.add_subparsers(dest="command", required=True)
    def import_options(p):
        p.add_argument("file")
        p.add_argument("--flip", nargs="*", metavar="CHANNEL",
                       help="channels whose sign is opposite to the schema in this file")
        p.add_argument("--unit", nargs="*", metavar="COLUMN=UNIT",
                       help="state a column's unit, overriding the file (e.g. G_LAT=g)")
        p.add_argument("--rate", type=float, help="resample rate in Hz (default: the file's own)")
        p.add_argument("--wheelbase", dest="wheelbase_m", type=float)
        p.add_argument("--steer-ratio", dest="steer_ratio", type=float)
        p.add_argument("--mass", dest="mass_kg", type=float)
        p.add_argument("--grip-limit", dest="grip_limit_g", type=float)

    r = sub.add_parser("report", help="write a self-contained HTML report of a telemetry file")
    import_options(r)
    r.add_argument("-o", "--output", metavar="FILE", help="where to write the page (default: <file>_report.html)")
    r.add_argument("--title", help="page title (default: venue and vehicle from the file)")
    r.set_defaults(func=report)

    p = sub.add_parser("inspect", help="load a telemetry file and print what the engine makes of it")
    import_options(p)
    p.add_argument("--ref", type=int, help="reference lap (default: best flying lap)")
    p.add_argument("--compare", type=int, help="lap to compare with the reference (default: next best)")
    p.add_argument("--sectors", type=int, default=3, help="number of equal-length sectors")
    p.add_argument("--export", metavar="DIR", help="also write the tables as CSV files")
    p.set_defaults(func=inspect)
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, KeyError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
