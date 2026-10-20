"""
Session report: one self-contained HTML page showing what the engine made of a
telemetry file — lap times, a track map and lap comparison sharing one cursor, grip,
corner-by-corner analysis, understeer and oversteer incidents, stints, sectors and
every note from the import.

  from veltrix_engine.report import write_report
  write_report(session, "session.html")

The page holds its own data and scripts, so it opens offline. (Fonts come from
Google Fonts when online and fall back to system fonts otherwise.)

`report_data` returns everything the page shows as a plain dict, which is also
the place to look for the numbers behind a chart.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .. import corners as corner_engine
from .. import laps as lap_engine
from ..analysis import advice, balance, engineering, gg, handling, tyres
from ..derived import best_lat
from ..schema import CORNERS, tyre_temp_channel
from ..track import track_map

GRID_STEP_M = 5.0          # distance step of the lap traces in the page
GG_POINTS = 3200           # points drawn in the G-G diagram
TRACE_STATUSES = ("flying", "short", "slow", "whole session")
_TEMPLATE = Path(__file__).with_name("template.html")


def _r(x, n=3):
    if x is None:
        return None
    x = float(x)
    return round(x, n) if np.isfinite(x) else None


def report_data(session, title: str | None = None) -> dict:
    """Everything the report page shows, as JSON-ready values."""
    s = session
    frame, laps, info = s.frame(), s.laps, s.raw.info
    best = lap_engine.best_lap(s)
    flying = list(laps.loc[laps["status"] == "flying", "lap"])
    has_length = bool(s.lap_length and np.isfinite(s.lap_length) and len(laps) > 1)

    sectors, theoretical, traces = [], None, {}
    if has_length:
        sec = lap_engine.sector_times(s)
        fly_sec = sec[sec["status"] == "flying"] if len(sec) else sec
        sectors = [{"lap": x.lap, "s": [_r(x.s1), _r(x.s2), _r(x.s3)], "time": _r(x.lap_time_s)}
                   for x in fly_sec.itertuples()]
        if len(fly_sec):
            theoretical = _r(lap_engine.theoretical_best(sec))
        grid = np.arange(0.0, s.lap_length, GRID_STEP_M)
        channels = [c for c in ("speed", "throttle", "brake") if c in frame]
        complete = laps[laps["start_crossed"] & laps["end_crossed"] & laps["status"].isin(TRACE_STATUSES)]
        for lap in complete["lap"]:
            tr = lap_engine.on_distance_grid(lap_engine.lap_trace(s, lap, channels), grid)
            entry = {"t": [_r(v) for v in tr["t"]], "v": [_r(v, 1) for v in tr["speed"]]}
            if "throttle" in tr:
                entry["thr"] = [int(round(v)) for v in tr["throttle"]]
            if "brake" in tr:
                entry["brk"] = [int(round(v)) for v in tr["brake"]]
            traces[str(lap)] = entry

    # Track map, on the same distance grid as the lap traces
    track_block = None
    if traces:
        try:
            tm = track_map(s, step=GRID_STEP_M)
            track_block = {"x": [_r(v, 1) for v in tm["x"]], "y": [_r(v, 1) for v in tm["y"]],
                           "source": tm.attrs["source"], "closed": bool(tm.attrs["closed"]),
                           "gap_m": _r(tm.attrs["gap_m"], 0), "direction": tm.attrs["direction"],
                           "laps": len(tm.attrs["laps"])}
        except ValueError:
            track_block = None

    # Corners — metrics for every lap the page can compare
    corner_block = None
    if traces:
        try:
            found = corner_engine.find_corners(s)
            metrics = corner_engine.corner_metrics(s, found)
            summary = corner_engine.corner_summary(s, found, metrics)
            fields = ("zone_time_s", "entry_speed", "min_speed", "exit_speed", "brake_m", "throttle_on_m",
                      "brake_before_m", "release_after_turn_in_m", "throttle_after_apex_m", "min_throttle")
            per_lap = {}
            for lap, part in metrics.groupby("lap"):
                part = part.set_index("corner").reindex(found["corner"])
                per_lap[str(lap)] = [[_r(part.loc[c, f], 3 if f == "zone_time_s" else 1) for f in fields]
                                     for c in found["corner"]]
            corner_block = {
                "source": found.attrs["source"], "fields": list(fields),
                "track": found.attrs.get("track"), "track_note": found.attrs.get("track_note", ""),
                "list": [{"corner": c.corner, "name": c.name, "dir": c.direction, "kind": c.kind, "start": _r(c.start_m, 0),
                          "apex": _r(c.apex_m, 0), "end": _r(c.end_m, 0), "z0": _r(c.zone_start_m, 0),
                          "z1": _r(c.zone_end_m, 0), "basis": getattr(c, "basis", "inferred")}
                         for c in found.itertuples()],
                "laps": per_lap,
                "summary": [{"corner": r.corner, "best": _r(r.best_s), "best_lap": r.best_lap, "mean": _r(r.mean_s),
                             "on_best_lap": _r(getattr(r, "on_best_lap_s", np.nan)),
                             "gap_on_best_lap": _r(getattr(r, "gap_on_best_lap_s", np.nan)),
                             "spread": _r(r.spread_s), "available": _r(r.available_s),
                             "min_speed": _r(r.min_speed_mean, 0)} for r in summary.itertuples()],
                "ideal": _r(summary.attrs["ideal_lap_s"]), "best_lap": summary.attrs.get("best_lap"),
                "thresholds": corner_engine.FINDING}
        except ValueError:
            corner_block = None

    # Understeer / oversteer incidents, located by corner
    handling_block = handling_result = None
    if corner_block:
        try:
            h = handling_result = handling.analyse(s, found)
            ev = h["events"]
            handling_block = {
                "model": {k: (_r(v, 2) if isinstance(v, float) else v) for k, v in h["model"].items()},
                "events": [{"lap": e.lap, "corner": e.corner, "kind": e.kind, "phase": e.phase, "cause": e.cause,
                            "at": _r(e.peak_m, 0), "dur": _r(e.duration_s, 2), "peak": _r(e.peak_deg, 0),
                            "lock": _r(e.opposite_lock_deg, 0) if e.counter_steer else None,
                            "severity": e.severity, "speed": _r(e.speed, 0), "lat": _r(e.lat_g, 2),
                            "slip": _r(e.rear_slip_kmh, 0), "cost": _r(e.cost_s, 2), "status": e.lap_status}
                           for e in ev.itertuples()],
                "by_corner": [{"corner": r.corner, "us": int(r.understeer), "os": int(r.oversteer),
                               "severe": int(r.severe), "laps": int(r.laps_affected), "of": int(r.laps),
                               "phase": r.usual_phase, "cause": r.usual_cause} for r in h["by_corner"].itertuples()],
                "tendency": [{"corner": r.corner, "entry": _r(r.entry_deg, 0), "apex": _r(r.apex_deg, 0),
                              "exit": _r(r.exit_deg, 0), "read": r.read} for r in h["tendency"].itertuples()],
            }
        except ValueError:
            handling_block = None

    # Suggestions for the driver and the setup
    advice_block = None
    if corner_block:
        suggestions = advice.suggest(
            s, {"corners": found, "metrics": metrics, "summary": summary}, handling_result)
        advice_block = {area: [{**i, "gain_s": _r(i["gain_s"], 2)} for i in suggestions[area]]
                        for area in ("driver", "setup")}

    # The engineering report: nine sections, every conclusion with its evidence
    engineering_block = engineering.analyse(
        s, {"corners": found, "metrics": metrics, "summary": summary} if corner_block else None, handling_result)

    # Where the data came from, what was computed and detected, and what limits the report
    data_block = _data_block(s, engineering_block)

    # Grip — only when there is a usable lateral g (logged finely, or rebuilt)
    grip = None
    lat_id = best_lat(frame)
    usable_lat = lat_id is not None and not ("acc_lat" in s.coarse and "acc_lat" not in s.rebuilt)
    if usable_lat and "acc_long_calc" in frame:
        scope_laps = flying or list(laps["lap"])
        part = s.frame(["lap", lat_id, "acc_long_calc"])
        part = part[part["lap"].isin(scope_laps)]
        lat, lon = part[lat_id].to_numpy(float), part["acc_long_calc"].to_numpy(float)
        ok = np.isfinite(lat) & np.isfinite(lon)
        lat, lon = lat[ok], lon[ok]
        idx = np.random.default_rng(1).choice(len(lat), min(GG_POINTS, len(lat)), replace=False)
        env = gg.gg_envelope(lat, lon, bins=36)
        grip = {"pts": [[_r(lat[i], 2), _r(lon[i], 2)] for i in idx],
                "env": [[_r(a), _r(b)] for a, b in zip(env["lat_g"], env["lon_g"]) if np.isfinite(a)],
                "summary": {k: _r(v) for k, v in gg.gg_summary(s, laps=scope_laps).items()},
                "scope": "flying laps" if flying else "all laps"}

    sc = balance.steering_characteristic(s) if usable_lat else None
    has_lltd = "lltd_front" in frame and all(f"susp_travel_{c}" in frame for c in CORNERS) \
        and frame["lltd_front"].notna().any()
    bal = {"ratio": _r(sc["ratio"], 1) if sc else None,
           "kus": _r(sc["understeer_deg_per_g"], 2) if sc else None,
           "lltd": _r(frame["lltd_front"].mean() * 100, 1) if has_lltd else None}

    has_tyres = any(tyre_temp_channel(frame.columns, c) for c in CORNERS)
    tyre_laps = tyres.lap_summary(s) if has_tyres else None
    stints = []
    for k in sorted(laps["stint"].unique()):
        in_stint = laps[laps["stint"] == k]
        fly = in_stint[in_stint["status"] == "flying"]
        part = balance.steering_characteristic(s, laps=list(fly["lap"]), ratio=sc["ratio"]) \
            if sc and len(fly) else None
        deg = tyres.fit_degradation(s, stint=int(k))
        temps = None
        if tyre_laps is not None:
            t = tyre_laps[tyre_laps["stint"] == k]
            temps = {c: _r(t[f"temp_{c}_mean"].mean(), 0) if f"temp_{c}_mean" in t else None for c in CORNERS}
        stints.append({
            "stint": int(k), "laps": f"{in_stint['lap'].iloc[0]}–{in_stint['lap'].iloc[-1]}",
            "flying": int(len(fly)),
            "best": _r(fly["lap_time_s"].min()) if len(fly) else None,
            "mean": _r(fly["lap_time_s"].mean()) if len(fly) else None,
            "kus": _r(part["understeer_deg_per_g"], 2) if part else None,
            "deg_ms": _r(deg["slope"] * 1000, 0), "deg_r2": _r(deg["r_squared"], 2), "temps": temps})

    venue, vehicle = str(info.get("Venue", "")).strip(), str(info.get("Vehicle", "")).strip()
    if title is None:
        title = " · ".join(p for p in (venue.title() if venue.islower() else venue, vehicle) if p) \
            or Path(s.raw.source or "Session").name
    v = s.vehicle
    footer = (f"Source file: {s.raw.source or 'not recorded'}. Vehicle parameters used: wheelbase {v.wheelbase_m:g} m, "
              f"steering ratio {v.steer_ratio:g}, mass {v.mass_kg:g} kg, reference grip circle {v.grip_limit_g:g} g. "
              "These are the configured values, not measurements of this car, unless you set them.")
    data = {
        "meta": {"title": title, "file": s.raw.source, "date": str(info.get("Log Date", "")),
                 "time": str(info.get("Log Time", "")), "rows": len(s.raw.data),
                 "columns": int(s.raw.data.shape[1]), "mapped": len(s.mapping), "rate": s.rate_hz,
                 "duration": _r(frame["time"].iloc[-1] - frame["time"].iloc[0], 0),
                 "lap_length": _r(s.lap_length, 0) if has_length else None,
                 "lap_source": s.lap_source, "best_lap": best, "theoretical": theoretical,
                 "grid_step": GRID_STEP_M, "grip_limit": v.grip_limit_g,
                 "brake_unit": "%" if s.units.get("brake") == "%" else f" {s.units.get('brake', '')}".rstrip(),
                 "footer": footer},
        "laps": [{"lap": l.lap, "stint": int(l.stint), "time": _r(l.lap_time_s), "status": l.status,
                  "dist": _r(l.dist_m, 0)} for l in laps.itertuples()],
        "sectors": sectors, "traces": traces, "track": track_block, "corners": corner_block, "handling": handling_block, "advice": advice_block,
        "engineering": engineering_block, "data": data_block,
        "gg": grip, "balance": bal, "stints": stints,
        "rebuilt": {k: {a: (_r(b) if isinstance(b, float) else b) for a, b in d.items()}
                    for k, d in s.rebuilt.items()},
        "notes": [{"level": level, "text": text} for level, text in s.notes],
    }
    return json.loads(json.dumps(data, default=_plain))      # numpy scalars -> plain values


def _data_block(s, engineering_block: dict) -> dict | None:
    """Mapping decisions, maths channels, detected events and limitations, from the session's model."""
    model = getattr(s, "model", None)
    if model is None:
        return None
    mapping = [{"channel": c.id, "column": c.source_name, "unit": c.unit, "unit_source": c.provenance.unit_source,
                "rate": _r(c.sample_rate, 2), "flags": list(c.quality)} for c in model.channels("normalised")]
    maths_rows = [{"name": c.id, "formula": c.formula, "unit": c.unit, "version": c.version,
                   "invalid": int((~c.validity).sum()), "samples": int(len(c.values)),
                   "estimate": any("estimate" in q for q in c.quality)}
                  for c in model.channels("calculated") if c.user.get("maths")]
    by_kind = {}
    for e in model.events:
        by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
    # what the source recorded first, then the most severe candidates
    worst = sorted(model.events, key=lambda e: (getattr(e, "status", "candidate") != "recorded",
                                                {"severe": 0, "moderate": 1, "mild": 2}.get(e.severity, 3)))[:40]
    from .. import events as event_rules
    review_text = lambda r: ((r.get("verdict") or "note") + (f" — {r['note']}" if r.get("note") else "")
                             + (f" ({r['author']}, {r.get('at', '')[:10]})" if r.get("author") else "")) if r else ""
    events = [{"kind": e.kind, "lap": e.lap, "at": _r(e.position_m, 0), "t": _r(e.t_start, 2), "dur": _r(e.t_end - e.t_start, 2),
               "severity": e.severity, "confidence": e.confidence, "status": getattr(e, "status", "candidate"),
               "limits": event_rules.limits_text(getattr(e, "thresholds", {})),
               "review": review_text(getattr(e, "review", {}) or {}),
               "as_detected": (getattr(e, "evidence", {}) or {}).get("detected_status", "")}
              for e in sorted(worst, key=lambda e: e.t_start)]
    orphans = [{"kind": r["original"].get("kind", r.get("kind")), "lap": r.get("lap"), "t": _r(r.get("t_start"), 2),
                "detected_as": r["original"].get("status", ""), "confidence": r["original"].get("confidence", ""),
                "limits": event_rules.limits_text(r["original"].get("thresholds", {})), "review": review_text(r)}
               for r in getattr(s, "orphan_reviews", []) or []]
    from .. import thresholds as threshold_registry
    from ..analysis import engineering as eng
    confidence_rule = [
        f"High: {eng.LAPS_HIGH} or more flying laps and an effect at least {eng.RATIO_HIGH:g} times its lap-to-lap scatter.",
        f"Medium: {eng.LAPS_MEDIUM}–{eng.LAPS_HIGH - 1} flying laps, or an effect {eng.RATIO_MEDIUM:g}–{eng.RATIO_HIGH:g} times its scatter.",
        f"Low: fewer than {eng.LAPS_MEDIUM} flying laps, or an effect smaller than its scatter.",
        "Each finding takes the lowest of these, of the data quality behind it (a rebuilt or coarse channel caps it) and of "
        "any named limit, given beside it.",
        "Detected events carry their rule's own confidence and stay candidates until a record or a person confirms them."]
    analysis_thresholds = [{"name": k, "value": v} for k, v in sorted(threshold_registry.snapshot().items())]
    limits = [m for level, m in s.notes if level == "warning"]
    limits += [f"{sec['title']}: not assessed — {sec['not_assessed']}." for sec in engineering_block["sections"] if sec["not_assessed"]]
    assumed = [c.id for c in model.channels("normalised") if c.provenance.unit_source == "assumed"]
    if assumed:
        limits.append("The file gives no unit for " + ", ".join(assumed) + "; the schema unit was assumed.")
    if any(getattr(e, "status", "candidate") != "recorded" for e in model.events):
        limits.append("Detected events are candidates found by threshold rules and the steering model; only those marked "
                      "confirmed coincide with an incident the simulator recorded. The thresholds are illustrative until "
                      "tuned to the car.")
    return {"mapping": mapping, "maths": maths_rows, "event_counts": by_kind, "events": events,
            "event_total": len(model.events), "annotations": [{"lap": a.lap, "t": _r(a.time, 2), "text": a.text,
                                                               "category": a.category, "author": a.author}
                                                              for a in model.annotations],
            "limitations": limits, "counts": s.count_lines() if hasattr(s, "count_lines") else [],
            "orphan_reviews": orphans, "confidence_rule": confidence_rule, "thresholds": analysis_thresholds}


def _plain(o):
    return o.item() if hasattr(o, "item") else str(o)


def render_report(session, title: str | None = None, fragment: bool = False) -> str:
    """The report as an HTML string. fragment=True returns the page without the
    <!doctype>/<head> wrapper, for hosts that add their own."""
    data = report_data(session, title)
    payload = json.dumps(data, separators=(",", ":"), default=_plain).replace("</", "<\\/")
    safe_title = data["meta"]["title"].replace("&", "&amp;").replace("<", "&lt;")
    page = _TEMPLATE.read_text(encoding="utf-8").replace("__TITLE__", safe_title).replace("__DATA__", payload)
    if fragment:
        return page
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            "<style>body{margin:0}[hidden]{display:none!important}</style>\n</head>\n<body>\n"
            + page + "\n</body>\n</html>\n")


def write_report(session, path: str | Path, title: str | None = None, fragment: bool = False) -> Path:
    """Write the report page. Returns the path written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(session, title, fragment), encoding="utf-8")
    return path
