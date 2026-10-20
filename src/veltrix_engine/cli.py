"""
Command line: inspect a telemetry file with the engine. Every command takes any
supported format (CSV, MoTeC .ld, iRacing .ibt, VBOX .vbo, AiM .xrk, MDF / MF4,
MAT, CAN logs with a DBC); `veltrix formats` lists them.

  veltrix app session.csv                 the desktop workspace: channel browser, charts, shared cursor
  veltrix workspace my_project            open the workspace in the browser
  veltrix engineer session.csv            the engineering report: nine sections, every conclusion with its evidence
  veltrix formats
  veltrix inspect bus.asc --dbc car.dbc
  veltrix inspect session.csv
  veltrix inspect session.csv --ref 5 --compare 11
  veltrix inspect session.csv --flip steering --wheelbase 2.463 --steer-ratio 12.5
  veltrix inspect session.csv --unit G_LAT=g G_LON=g
  veltrix inspect session.csv --export out/
  veltrix report session.csv -o session.html
  veltrix batch season/ --report --engineer every file in a folder, with an index of them
  veltrix library scan ~/Telemetry          the session library: see cli_library.py
  veltrix importer check my_importer:IMPORTER sample.mlg    a plug-in importer against the test kit

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
from .importers import supported_formats
from .session import load_session
from .track import track_map
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
    for field in ("wheelbase_m", "steer_ratio", "mass_kg", "grip_limit_g", "front_weight_dist",
                  "wheel_rate_front_N_mm", "wheel_rate_rear_N_mm"):
        value = getattr(args, field, None)
        if value is not None:
            vehicle.set(field, value, "user")
    try:
        units = dict(item.split("=", 1) for item in (args.unit or ()))
    except ValueError:
        raise ValueError("--unit expects COLUMN=UNIT, for example --unit G_LAT=g")
    cars = None
    if not getattr(args, "no_app_data", False):
        from .cars import CarLibrary
        from .user_data import data_dir
        cars = CarLibrary(Path(getattr(args, "app_data", None) or data_dir()) / "cars")
    return load_session(args.file, vehicle=vehicle, flip=tuple(args.flip or ()), rate_hz=args.rate,
                        units=units, dbc=args.dbc or None, cars=cars)


def export(args) -> int:
    from .exports import export_session
    s = _load(args)
    out = export_session(s, args.output, args.channels)
    print(f"Written {out} ({out.stat().st_size / 1024:.0f} KB, {len(s.channels):,} samples)")
    return 0


def document(args) -> int:
    from .report import documents
    if args.spec:
        spec = documents.load_spec(args.spec)
    else:
        if not args.template:
            raise ValueError("Name a template (debrief, driver-card, comparison) or give --spec.")
        spec = documents.make_spec(args.template, args.files, args.title, args.change, not args.no_app_data)
    page = documents.build(spec)
    out = Path(args.output or f"{file_stem(spec['files'][0])}_{spec['template']}.html")
    out.write_text(page, encoding="utf-8")
    print(f"Written to {out}")
    if args.save_spec:
        print(f"Spec saved to {documents.save_spec(spec, args.save_spec)}")
    if args.pdf:
        from .report.pdf import html_to_pdf
        try:
            print(f"PDF written to {html_to_pdf(page, args.pdf)}")
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
    return 0


def coach(args) -> int:
    from .analysis import coaching
    s = _load(args)
    result = coaching.cards(s)
    text = coaching.to_markdown(result, f"Driver coaching — {Path(args.file).name}")
    print(text)
    if args.markdown:
        Path(args.markdown).write_text(text, encoding="utf-8")
    if args.html:
        Path(args.html).write_text(coaching.to_html(result, f"Driver coaching — {Path(args.file).name}"), encoding="utf-8")
        print(f"Cards written to {args.html}")
    return 0


def compare(args) -> int:
    from .analysis import ab
    a = _load(args)
    other = argparse.Namespace(**{**vars(args), "file": args.other})
    b = _load(other)
    result = ab.compare_sessions(a, b, args.change)
    text = ab.to_markdown(result)
    print(text)
    if args.markdown:
        Path(args.markdown).write_text(text, encoding="utf-8")
        print(f"Written to {args.markdown}")
    return 0 if result["comparable"] else 2


def car(args) -> int:
    from .cars import PARAMETERS, CarLibrary
    from .user_data import data_dir
    library = CarLibrary(Path(args.app_data or data_dir()) / "cars")
    if args.action == "list":
        names = library.names()
        print("\n".join(names) if names else "The car library is empty. Add a car: veltrix car set NAME --wheelbase …")
        return 0
    if not args.name:
        raise ValueError(f"veltrix car {args.action} needs the car's name.")
    if args.action == "remove":
        print(f"Removed {args.name}." if library.remove(args.name) else f"{args.name} is not in the car library.")
        return 0
    if args.action == "set":
        given = {p: getattr(args, p) for p in PARAMETERS if getattr(args, p, None) is not None}
        if not given and not args.alias:
            raise ValueError("Give at least one parameter, e.g. --wheelbase 2.575, or an --alias.")
        print(f"Saved to {library.save(args.name, given, args.alias, args.note)}")
    entry = library.get(args.name)
    if entry is None:
        print(f"{args.name} is not in the car library.")
        return 1
    print(entry["name"] + (f"  (also: {', '.join(entry['aliases'])})" if entry["aliases"] else ""))
    for p in PARAMETERS:
        if p in entry["parameters"]:
            print(f"  {p:<24} {entry['parameters'][p]:g}")
    if entry.get("note"):
        print(f"  note: {entry['note']}")
    return 0


def file_stem(path) -> str:
    """A telemetry file's name without its extension ("run.csv.gz" -> "run")."""
    name = Path(path).name
    if name.lower().endswith(".gz"):
        name = name[:-3]
    return Path(name).stem or name


def formats(args) -> int:
    """List the file formats the engine reads."""
    rows = supported_formats()
    width = max(len(r["format"]) for r in rows)
    for r in rows:
        state = "ready" if r["ready"] else f"needs: {r['install']}"
        print(f"  {r['format']:<{width}}  {r['extensions']:<22} {state}")
        if r["remark"]:
            print(f"  {'':<{width}}  {r['remark']}")
    return 0


def batch(args) -> int:
    """Import every telemetry file in a folder; write the outputs asked for and an index."""
    from .batch import find_files, run_batch, write_index
    folder = Path(args.folder)
    files = find_files(folder, recursive=not args.no_recursive)
    if not files:
        print(f"No telemetry files in {folder} (veltrix formats lists what is read).")
        return 1
    out_dir = Path(args.output or f"{folder.resolve()}_veltrix")
    outputs = (("report",) if args.report else ()) + (("engineer",) if args.engineer else ()) \
        + tuple(e if e.startswith(".") else f".{e}" for e in (args.export or ()))
    cars_dir = None
    if not args.no_app_data:
        from .user_data import data_dir
        cars_dir = Path(args.app_data or data_dir()) / "cars"
    print(f"{len(files)} file(s) in {folder}" + (f", {args.jobs} at a time" if args.jobs > 1 else ""))

    def progress(done, total, row):
        best = f", best {fmt_time(row.best_lap_s)} (lap {row.best_lap})" if row.best_lap_s else ""
        state = {"ok": f"{row.format}, {row.laps} laps{best}", "duplicate": row.error}.get(row.status, f"FAILED — {row.error}")
        print(f"  [{done}/{total}] {row.file}: {state}")
    rows = run_batch(files, root=folder, out_dir=out_dir, outputs=outputs, jobs=args.jobs, progress=progress,
                     cars_dir=cars_dir, dbc=args.dbc or None, thresholds=args.thresholds)
    index = write_index(rows, out_dir)
    failed = sum(r.status == "failed" for r in rows)
    print(f"\n{len(rows) - failed} read, {failed} failed. Index: {index[1]}")
    return 1 if failed else 0


def importer(args) -> int:
    """List plug-in importers, or check one against the importer test kit."""
    from .importers import plugins
    if args.action == "list":
        found, broken = plugins.registered(), plugins.failed()
        if not found and not broken:
            print("No plug-in importers installed. See docs/importers.md to write one.")
        for p in found:
            print(f"  {p.key:<20} {p.name} {p.version}  {' '.join(p.extensions)}  (interface {p.api})")
        for name, why in broken.items():
            print(f"  {name:<20} NOT LOADED — {why}")
        return 0
    if not args.importer:
        raise ValueError("veltrix importer check MODULE[:NAME] SAMPLE...: name the importer and give sample files")
    import importlib
    from .importers.testkit import check_importer
    module, _, attr = args.importer.partition(":")
    sys.path.insert(0, str(Path.cwd()))        # a module beside the user, as written on the command line
    target = getattr(importlib.import_module(module), attr or "IMPORTER")
    result = check_importer(target() if callable(target) and not isinstance(target, plugins.Importer) else target,
                            args.samples)
    print(result.text())
    return 0 if result.ok else 1


def engineer(args) -> int:
    """Print the engineering report of a telemetry file."""
    from .analysis import engineering
    s = _load(args)
    result = engineering.analyse(s)
    print(f"{Path(args.file).name} — {len(s.laps)} laps, {result['summary']['flying_laps']} flying, "
          f"{result['summary']['findings']} findings\n")
    print(engineering.to_text(result))
    if args.json:
        import json
        Path(args.json).write_text(json.dumps(result, indent=1), encoding="utf-8")
        print(f"\nReport data written to {args.json}")
    if args.markdown:
        Path(args.markdown).write_text(engineering.to_markdown(result, f"Performance engineering report — {Path(args.file).name}"),
                                       encoding="utf-8")
        print(f"\nReport written to {args.markdown}")
    return 0


def app(args) -> int:
    """Open the desktop workspace window."""
    try:
        from veltrix_app.window import main as app_main
    except ImportError as e:
        raise ValueError(f"The desktop app needs PySide6 and pyqtgraph: pip install 'veltrix-engine[ui]'  ({e})") from None
    return app_main([args.file] if args.file else [])


def list_thresholds(args) -> int:
    """Print every analysis threshold and its value (with any --thresholds file applied)."""
    from . import thresholds as tuning
    for key, value in tuning.snapshot().items():
        print(f"  {key:<42} {value}")
    return 0


def workspace(args) -> int:
    from .workspace import serve
    serve(args.folder, port=args.port, open_browser=not args.no_browser)
    return 0


def report(args) -> int:
    from .report import write_report
    s = _load(args)
    extra = ""
    if not args.no_app_data:
        # the maths channels, event rules, notes and reviews made in the desktop app belong in the report too
        from .user_data import apply as apply_user_data
        done = apply_user_data(s, args.app_data)
        made = sum(r.ok for r in done["maths"].values())
        extra = f", {made} maths channel(s), {len(s.model.events)} candidate event(s), {done['notes']} note(s) from the app"
        for line in done["problems"]:
            print(f"Note: {line}", file=sys.stderr)
    out = write_report(s, args.output or f"{file_stem(args.file)}_report.html", title=args.title)
    warnings = len(s.warnings())
    print(f"Report written to {out}  ({out.stat().st_size / 1024:.0f} KB, {len(s.laps)} laps, "
          f"{warnings} warning{'s' if warnings != 1 else ''} from the import{extra})")
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
    print(f"  Format     {s.raw.format}")
    print(f"  Loaded     {len(s.raw.data):,} rows × {s.raw.data.shape[1]} columns in {elapsed:.1f} s")
    print(f"  Normalised {len(frame):,} samples at {s.rate_hz:g} Hz, "
          f"{frame['time'].iloc[-1] - frame['time'].iloc[0]:,.0f} s")
    for line in s.count_lines()[1:]:
        print(f"             {line}")
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
            print(f"  * best sector.  Best sectors added up {fmt_time(theo)} "
                  f"({theo - best_time:+.3f} s vs best lap {best}) — an estimate from several laps, not driven")

    # ── corners ────────────────────────────────────────────────────────────
    corner_result = None
    if s.lap_length and len(s.laps) > 2:
        try:
            corner_result = corner_engine.analyse(s)
        except ValueError as e:
            print(f"\nCorners: not available ({e})")
    if corner_result:
        cs, summary = corner_result["corners"], corner_result["summary"].set_index("corner")
        heading(f"Corners ({len(cs)} from {cs.attrs['source']}; flying laps)")
        if cs.attrs.get("track_note") and not cs.attrs.get("track"):
            print(f"  {cs.attrs['track_note']}")
        print(f"  {'':<4} {'':>3} {'Kind':<7} {'From m':>7} {'Apex m':>7} {'To m':>7} {'Best s':>8} {'Lap':>4} "
              f"{'Mean s':>8} {'Spread':>7} {'On table':>9} {'Min km/h':>9}")
        for c in cs.itertuples():
            if c.corner not in summary.index:
                continue
            r = summary.loc[c.corner]
            print(f"  {c.corner:<4} {c.direction:>3} {c.kind:<7} {c.start_m:>7.0f} {c.apex_m:>7.0f} {c.end_m:>7.0f} "
                  f"{r.best_s:>8.3f} {str(r.best_lap):>4} {r.mean_s:>8.3f} {r.spread_s:>7.3f} "
                  f"{r.available_s:>9.3f} {r.min_speed_mean:>9.0f}  {c.name}".rstrip())
        ideal = corner_result["ideal_lap_s"]
        top = summary.sort_values("available_s", ascending=False).head(3)
        print(f"  Ideal lap by corner (best time through every corner): {fmt_time(ideal)} ({ideal - best_time:+.3f} s vs best "
              f"lap {best}) — an estimate built from laps {', '.join(str(x) for x in sorted(set(summary['best_lap'])))}, not driven")
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
        flying_only = list(s.laps.loc[s.laps["status"] == "flying", "lap"]) or None
        g = gg.gg_summary(s, laps=flying_only)
        print(f"  Peak (held 0.25 s) lateral {g['peak_lat_g']:.2f} g · braking {g['peak_brake_g']:.2f} g · "
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
        xy = track_map(s)
        how = xy.attrs["source"]
        if how == "reconstructed":
            how += (f" from speed and lateral g, closed (ends were {xy.attrs['gap_m']:.0f} m apart before closing)"
                    if xy.attrs["closed"] else " from speed and lateral g; could not be closed into a lap")
        print(f"\nTrack map: {len(xy):,} points, {xy.attrs['direction']}, {how}")
    except ValueError:
        xy = None
        print("\nTrack map: not available (no position, or no speed and usable lateral g)")

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


def circuit(args) -> int:
    """Draft a circuit file from the corners found in a session."""
    from . import circuits
    s = _load(args)
    found = corner_engine.find_corners(s, track=None)
    draft = circuits.draft_from_session(s, found, args.name)
    stem = "".join(ch if ch.isalnum() else "_" for ch in draft["name"].lower()).strip("_") or "circuit"
    out = Path(args.output) if args.output else circuits.TRACK_DIR / f"{stem}.json"
    if out.exists() and not args.force:
        raise ValueError(f"{out} already exists (use --force to replace it, or -o to write elsewhere).")
    circuits.save(draft, out)
    print(f"{len(found)} corners found from {found.attrs['source']}; lap {draft['lap_length_m']:.0f} m.")
    print(f"Circuit file written to {out}")
    print("Edit it to give each corner the circuit's own number and name, and to split or merge corners "
          "where the circuit counts them differently. It is used automatically for files from this venue.")
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
        p.add_argument("--thresholds", metavar="FILE",
                       help="JSON file overriding analysis thresholds (veltrix thresholds lists them)")
        p.add_argument("--dbc", nargs="*", metavar="FILE",
                       help="DBC file(s) to decode a CAN log with (default: the .dbc files beside it)")
        p.add_argument("--wheelbase", dest="wheelbase_m", type=float)
        p.add_argument("--steer-ratio", dest="steer_ratio", type=float)
        p.add_argument("--mass", dest="mass_kg", type=float)
        p.add_argument("--front-weight", dest="front_weight_dist", type=float,
                       help="static front weight as a fraction, e.g. 0.53")
        p.add_argument("--wheel-rate-front", dest="wheel_rate_front_N_mm", type=float,
                       help="front roll wheel rate per wheel, N/mm (spring × motion ratio² plus anti-roll bar)")
        p.add_argument("--wheel-rate-rear", dest="wheel_rate_rear_N_mm", type=float)
        p.add_argument("--grip-limit", dest="grip_limit_g", type=float)

    w = sub.add_parser("workspace", help="open the telemetry workspace: drop in files of any format, browse sessions")
    w.add_argument("folder", nargs="?", default="veltrix_workspace", help="project folder (created if missing)")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--no-browser", action="store_true", help="do not open the browser")
    w.set_defaults(func=workspace)

    a = sub.add_parser("app", help="open the desktop workspace: channel browser, stacked charts, shared cursor")
    a.add_argument("file", nargs="?", help="a telemetry file to open")
    a.set_defaults(func=app)

    th = sub.add_parser("thresholds", help="list every analysis threshold and its value")
    th.set_defaults(func=list_thresholds)

    b = sub.add_parser("batch", help="import every telemetry file in a folder: an index, and reports or exports of each")
    b.add_argument("folder")
    b.add_argument("-o", "--output", metavar="DIR", help="where outputs and the index go (default: <folder>_veltrix)")
    b.add_argument("--report", action="store_true", help="an HTML report of each file")
    b.add_argument("--engineer", action="store_true", help="the engineering report of each file, as Markdown")
    b.add_argument("--export", nargs="*", metavar="EXT", help="export each session: csv, parquet and/or mf4")
    b.add_argument("--no-recursive", action="store_true", help="only the folder itself, not its sub-folders")
    b.add_argument("--jobs", type=int, default=1, help="files imported side by side (each takes its own memory)")
    b.add_argument("--dbc", nargs="*", metavar="FILE", help="DBC file(s) for CAN logs (default: the .dbc beside each)")
    b.add_argument("--thresholds", metavar="FILE", help="JSON file overriding analysis thresholds")
    b.add_argument("--no-app-data", action="store_true", help="ignore the app's car library")
    b.add_argument("--app-data", metavar="DIR", help="read the car library from this folder instead of the app's own")
    b.set_defaults(func=batch)

    from . import cli_library
    cli_library.add_parser(sub)

    im = sub.add_parser("importer", help="plug-in importers: list the installed ones, or check one with the test kit")
    im.add_argument("action", choices=("list", "check"))
    im.add_argument("importer", nargs="?", metavar="MODULE[:NAME]", help="for check: the Importer (default name IMPORTER)")
    im.add_argument("samples", nargs="*", help="for check: sample files of the format")
    im.set_defaults(func=importer)

    f = sub.add_parser("formats", help="list the file formats the engine reads")
    f.set_defaults(func=formats)

    r = sub.add_parser("report", help="write a self-contained HTML report of a telemetry file")
    import_options(r)
    r.add_argument("-o", "--output", metavar="FILE", help="where to write the page (default: <file>_report.html)")
    r.add_argument("--title", help="page title (default: venue and vehicle from the file)")
    r.add_argument("--no-app-data", action="store_true",
                   help="leave out the maths channels, event rules and notes made in the desktop app")
    r.add_argument("--app-data", metavar="DIR", help="read those from this folder instead of the app's own")
    r.set_defaults(func=report)

    e = sub.add_parser("engineer", help="the engineering report: lap potential, driver, braking, cornering, traction, "
                                        "tyres, balance, incidents and setup hypotheses, each with its evidence")
    import_options(e)
    e.add_argument("--json", metavar="FILE", help="also write the report as JSON")
    e.add_argument("--markdown", metavar="FILE", help="also write the report as Markdown")
    e.set_defaults(func=engineer)

    c = sub.add_parser("circuit", help="draft a circuit file (official corner numbers and names) from a session")
    import_options(c)
    c.add_argument("-o", "--output", metavar="FILE", help="where to write it (default: the engine's circuit library)")
    c.add_argument("--name", help="circuit name (default: the venue in the file)")
    c.add_argument("--force", action="store_true", help="replace an existing circuit file")
    c.set_defaults(func=circuit)

    ex = sub.add_parser("export", help="write a session for other tools: .csv, .parquet or .mf4")
    import_options(ex)
    ex.add_argument("output", help="the file to write; its extension picks the format")
    ex.add_argument("--channels", nargs="*", help="only these channels (time, lap and lap distance always come)")
    ex.add_argument("--no-app-data", action="store_true", help="ignore the app's car library")
    ex.set_defaults(func=export)

    doc = sub.add_parser("document", help="an engineering document to print or send: debrief, driver-card or comparison")
    doc.add_argument("template", nargs="?", choices=("debrief", "driver-card", "comparison"))
    doc.add_argument("files", nargs="*", help="the session (two for a comparison: before, after)")
    doc.add_argument("-o", "--output", metavar="FILE", help="the HTML page (default: <file>_<template>.html)")
    doc.add_argument("--pdf", metavar="FILE", help="also print it to a PDF")
    doc.add_argument("--title", default="")
    doc.add_argument("--change", default="", help="comparison: what was changed, if the files do not record it")
    doc.add_argument("--spec", metavar="FILE", help="build from a saved spec instead")
    doc.add_argument("--save-spec", metavar="FILE", help="save the spec, to build the same document again")
    doc.add_argument("--no-app-data", action="store_true", help="ignore the app's car library")
    doc.set_defaults(func=document)

    co = sub.add_parser("coach", help="coaching cards: per corner, what the fastest pass did and what to try")
    import_options(co)
    co.add_argument("--html", metavar="FILE", help="write the cards as a page to print or send")
    co.add_argument("--markdown", metavar="FILE", help="also write them as Markdown")
    co.add_argument("--no-app-data", action="store_true", help="ignore the app's car library")
    co.set_defaults(func=coach)

    cmp_ = sub.add_parser("compare", help="session B against session A: did one change make the car faster?")
    cmp_.add_argument("file", help="session A (before the change)")
    cmp_.add_argument("other", help="session B (after it)")
    cmp_.add_argument("--change", default="", help="what was changed, if the files do not record it")
    cmp_.add_argument("--markdown", metavar="FILE", help="also write the comparison as Markdown")
    cmp_.add_argument("--flip", nargs="*"); cmp_.add_argument("--unit", nargs="*"); cmp_.add_argument("--rate", type=float)
    cmp_.add_argument("--dbc", nargs="*"); cmp_.add_argument("--no-app-data", action="store_true")
    cmp_.set_defaults(func=compare)

    k = sub.add_parser("car", help="the car library: a car's parameters, entered once, used for every session of it")
    k.add_argument("action", choices=("list", "show", "set", "remove"))
    k.add_argument("name", nargs="?", help='the car, as the files name it, e.g. "Toyota GR86"')
    k.add_argument("--alias", nargs="*", default=[], help="other names files give the same car")
    k.add_argument("--note", default="", help="where the numbers come from")
    k.add_argument("--app-data", metavar="DIR", help="use this data folder instead of the app's own")
    for flag, dest in (("--wheelbase", "wheelbase_m"), ("--track-front", "track_front_m"), ("--track-rear", "track_rear_m"),
                       ("--mass", "mass_kg"), ("--cg-height", "cg_height_m"), ("--front-weight", "front_weight_dist"),
                       ("--steer-ratio", "steer_ratio"), ("--wheel-rate-front", "wheel_rate_front_N_mm"),
                       ("--wheel-rate-rear", "wheel_rate_rear_N_mm"), ("--grip-limit", "grip_limit_g")):
        k.add_argument(flag, dest=dest, type=float)
    k.set_defaults(func=car)

    p = sub.add_parser("inspect", help="load a telemetry file and print what the engine makes of it")
    import_options(p)
    p.add_argument("--ref", type=int, help="reference lap (default: best flying lap)")
    p.add_argument("--compare", type=int, help="lap to compare with the reference (default: next best)")
    p.add_argument("--sectors", type=int, default=3, help="number of equal-length sectors")
    p.add_argument("--export", metavar="DIR", help="also write the tables as CSV files")
    p.set_defaults(func=inspect)
    args = ap.parse_args(argv)
    from .logs import setup as setup_log
    setup_log()
    try:
        from . import thresholds as tuning
        with tuning.use(tuning.load(args.thresholds) if getattr(args, "thresholds", None) else None):
            return args.func(args)
    except (ValueError, KeyError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
