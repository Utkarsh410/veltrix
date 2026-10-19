"""Section 8: incidents — recorded, inferred, peaks, corner zones and data."""

from __future__ import annotations

import json
import re

import numpy as np
import pandas as pd

from ... import circuits
from ... import events as event_rules
from ... import corners as corner_engine
from ... import laps as lap_engine
from ...derived import best_lat
from ...schema import CORNERS as WHEELS
from ...schema import tyre_temp_channel
from .. import advice, balance, handling, plausibility, tyres

from .base import (SIM_INCIDENT, BEST_VS_TYPICAL, BRAKE_M, BRAKE_SCATTER_M, COAST_FLAG_S, COAST_MIN_KMH, COAST_THROTTLE, CONSISTENT_S, DECEL_GAP_G, DECEL_SHARE, DEG_R2, ESTIMATE, HEAVY_BRAKE_KMH, LAPS_HIGH, LAPS_MEDIUM, LAT_SHARE, LEVELS, LOOSE_S, MEASURED, MIN_GAIN_S, NATURE_TEXT, OVERLAP_FLAG_S, PHASE_DEG, PRESSURE_SPLIT_BAR, RATIO_HIGH, RATIO_MEDIUM, SECTIONS, SPEED_KMH, THROTTLE_M, WHEELSPIN_KMH, WHEELSPIN_SHARE, ZONES_CALC, _Ctx, _finding, _join, fmt_time)  # noqa: F401


# ── 8. incidents ──────────────────────────────────────────────────────────────

def _peak_check(c: _Ctx) -> list:
    """Every acceleration peak the report uses, checked: held, confirmed by an
    independent measurement, within the car's stated grip."""
    p = c.peaks
    if p is None or not len(p):
        return []
    flagged = p[p["verdict"] != "ok"].sort_values("peak_g", ascending=False)
    by = flagged["verdict"].value_counts()
    words = {"kerb": "kerb strikes", "bump": "bumps", "spike": "single-sample spikes", "jolt": "jolts not held",
             "unsupported": "held but not supported by an independent measurement", "beyond": "beyond the stated grip limit"}
    evidence = [f"{len(p) - len(flagged)} of {len(p)} corner peaks (lateral and braking, flying laps) are held for a quarter "
                "of a second" + (" and agree with an independent measurement" if p["independent_g"].notna().any() else "")]
    if len(flagged):
        evidence.append("Set aside: " + ", ".join(f"{n} {words.get(k, k)}" for k, n in by.items()))
        for r in flagged.head(6).itertuples():
            vert = f", vertical {r.vertical_g:.2f} g" if np.isfinite(r.vertical_g) else ""
            evidence.append(f"Lap {r.lap}, {r.corner} {r.kind} at {r.m:.0f} m: {r.peak_g:.2f} g for an instant, "
                            f"{r.held_g:.2f} g held{vert} — {r.reason}")
    calc = (f"For each corner pass the instantaneous peak is set against the same acceleration held for a quarter of a "
            f"second (rolling median). More than {plausibility.TRANSIENT_G:g} g above it is a jolt: a kerb strike when the "
            f"simulator's surface says kerb, a bump when vertical g is more than {plausibility.BUMP_G:g} g from 1, a "
            "spike when one sample stands alone. The held value is then checked against an independent measurement — "
            "yaw rate × speed for lateral g, d(speed)/dt for deceleration — and more than "
            f"{plausibility.CORROBORATE_G:g} g above it is not supported. Peak figures in this report are held values; "
            "unsupported ones are left out.")
    unsupported = int(by.get("unsupported", 0) + by.get("beyond", 0))
    return [_finding(
        (f"Acceleration peaks hold up: {len(p) - len(flagged)} of {len(p)} are held and corroborated" if not len(flagged) else
         f"{len(flagged)} of {len(p)} acceleration peaks are not the car's grip"
         + (f", {unsupported} of them left out of the peak figures" if unsupported else
            " — jolts the held peak figures already leave out")),
        evidence, calc, {"level": "high", "basis": "checked against the held value and an independent measurement"},
        ("Peak g figures here can be read as the car's." if not len(flagged) else
         "Read peak g figures as held values. A kerb strike or bump that keeps recurring in one corner is worth "
         "checking on video: it is load the suspension takes, even though it is not grip."), "every corner")]


def _corner_check(c: _Ctx) -> list:
    """Where the corner zones every per-corner finding uses came from, and how
    they compare with the circuit's own definition when there is one."""
    corners = c.corners
    if corners is None or not len(corners):
        return []
    attrs, source = corners.attrs, corners.attrs.get("source", "")
    check = attrs.get("validation")
    calc = ("Each official turn in the circuit file is looked for among the corners found in the data alone "
            f"(the cornering signal, with no circuit file): a found corner overlaps the turn's span to within "
            f"{circuits.MATCH_SLACK_M:.0f} m and turns the same way. Offsets are the found corner's start and end "
            "against the file's, after scaling the file to this session's lap length.")
    if check and "turns" in check:
        missing = [r["turn"] for r in check["turns"] if not (r["found"] and r.get("direction_agrees"))]
        evidence = [f"{check['found']} of {check['total']} turns of '{attrs.get('track')}' are found in the data"]
        offs = [r for r in check["turns"] if r["found"]]
        if offs:
            worst = max(offs, key=lambda r: abs(r["start_offset_m"]) + abs(r["end_offset_m"]))
            evidence.append(f"Largest offset: {worst['turn']} — the data's corner starts {worst['start_offset_m']:+.0f} m "
                            f"and ends {worst['end_offset_m']:+.0f} m from the file's")
        if missing:
            evidence.append("Not found in the data: " + _join(missing))
        for found, turns in check["merged"].items():
            evidence.append(f"{_join(turns)} are one corner in the data (taken in one movement)")
        if check["extra"]:
            evidence.append("Corners in the data that no turn of the file accounts for: " + _join(check["extra"]))
        good = not missing and not check["extra"]
        return [_finding(
            (f"Corner zones are the circuit's own, and the data confirms all {check['total']} turns" if good else
             f"Corner zones are the circuit's own; the data confirms {check['found']} of {check['total']} turns"),
            evidence, calc, {"level": "high" if good else "medium", "basis": "official turns checked against the data"},
            ("Corner numbers and names in this report are the circuit's." if good else
             "Corner numbers and names are the circuit's, but check the turns listed above on the track map before "
             "relying on findings about them: the file and this session's data disagree there."), "every corner")]
    tried = attrs.get("tried") or []
    evidence = [f"{len(corners)} corners found from {source}"]
    if attrs.get("track_note"):
        evidence.append(attrs["track_note"])
    elif not tried:
        venue = c.session.raw.info.get("Venue", "")
        evidence.append(f"No circuit file for {venue!r}" if venue else "The file names no venue, so no circuit file could be chosen")
    return [_finding(
        "Corner zones are inferred from the data, not checked against a circuit definition", evidence,
        "Corners are where the cornering signal rises clear of the straights, numbered T1, T2, … in lap order from the "
        "start/finish line. They are not the circuit's official numbers or names, and a chicane may be one corner or "
        "two depending on how it is driven.",
        {"level": "medium", "basis": "inferred from this session's data"},
        "Read corner numbers as this report's own. To use the circuit's, add a circuit file for the venue "
        "(`veltrix circuit FILE` drafts one from this session for you to number and name).", "every corner")]




def _incidents(c: _Ctx) -> list:
    s, out = c.session, []
    recorded = c.sim_incidents()
    if recorded:
        with_points = [e for e in recorded if e["points"] > 0]
        evidence = [f"Lap {e['lap']}, {e['corner']} at {e['m']:.0f} m: {e['kind']}"
                    + (f", {e['points']:g}x" if e["points"] else ", no incident points") for e in recorded[:10]]
        laps_hit = sorted({e["lap"] for e in with_points})
        out.append(_finding(
            (f"The simulator recorded {len(with_points)} incident{'s' if len(with_points) != 1 else ''}"
             + (f" — on lap{'s' if len(laps_hit) > 1 else ''} {_join(laps_hit)}" if laps_hit else "")
             if with_points else f"The simulator flagged {len(recorded)} event{'s' if len(recorded) != 1 else ''} with no incident points"),
            evidence,
            "Read from the simulator's own incident channels: a rise in the driver's incident count is an incident, "
            "named from the incident flags (low byte the kind, high byte the penalty). A flagged event without points "
            "is listed but not counted. A corner pass with an incident is never used as that corner's fastest pass.",
            {"level": "high", "basis": "recorded by the simulator, not inferred"},
            "Look at each one on the track map and video. An off-track that gained time is a corner cut to drop from "
            "comparisons; one that lost time is a mistake to understand before it is repeated.",
            _join(sorted({e["corner"] for e in recorded if e["corner"]})), None))
    if c.handling is not None:
        ev, model = c.handling["events"], c.handling["model"]
        calc = ("An incident is a stretch where the steering applied differs from what this car normally needs at "
                f"that point of the lap by more than {handling.SIGMA_LEVEL:g} robust standard deviations (at least "
                f"{model['threshold_deg']:.0f} deg) for {handling.MIN_DURATION_S:g} s or more, or opposite lock beyond "
                f"{handling.COUNTER_STEER_DEG:g} deg. More steering than normal is understeer, less is oversteer. Cost = "
                "the lap's time through that zone minus the typical time.")
        if len(ev):
            recorded = [e for e in c.sim_incidents() if e["points"] > 0 or (e["code"] & 0xFF) in SIM_INCIDENT]

            def backed(e):
                """A recorded incident on the same lap, in the same corner: the candidate is confirmed by it."""
                return next((r for r in recorded if r["lap"] == e.lap and r["corner"] == e.corner), None)
            reviewed_events = [m for m in getattr(s.model, "events", []) if m.source == "steering model" and m.review]

            def verdict(e):
                """A person's verdict on this candidate, from the app's event review."""
                m = next((m for m in reviewed_events if m.lap == e.lap and m.position_m is not None
                          and abs(m.position_m - e.peak_m) <= 10.0), None)
                return m.review if m is not None else {}
            rejected = sum(verdict(e).get("verdict") == "rejected" for e in ev.itertuples())
            n_us, n_os = int((ev["kind"] == "understeer").sum()), int((ev["kind"] == "oversteer").sum())
            n_conf = sum(backed(e) is not None or verdict(e).get("verdict") == "confirmed" for e in ev.itertuples())
            strong = ev[ev["severity"] != "mild"].sort_values("cost_s", ascending=False)
            cost = ev.groupby(["lap", "corner"])["cost_s"].first()
            total = float(cost[cost > 0].sum())
            evidence = [f"{n_us} understeer and {n_os} oversteer candidates on {ev['lap'].nunique()} laps; "
                        f"{int((ev['severity'] == 'severe').sum())} severe, {int((ev['severity'] == 'moderate').sum())} moderate"
                        + (f"; {n_conf} confirmed by a simulator record" if recorded else
                           "; none can be confirmed — the file has no incident record")]
            shown = list(strong.head(6).itertuples())
            shown += [e for e in ev.itertuples() if verdict(e) and all(e.Index != x.Index for x in shown)]   # reviewed ones always
            for e in shown:
                lock = f", {e.opposite_lock_deg:.0f} deg of opposite lock" if e.counter_steer else ""
                paid = f", {e.cost_s:+.2f} s in the zone" if np.isfinite(e.cost_s) else ""
                rec, said = backed(e), verdict(e)
                status = (f" — confirmed: {rec['kind']} recorded by the simulator" if rec else
                          f" — confirmed on review by {said.get('author') or 'a reviewer'}" if said.get("verdict") == "confirmed" else
                          f" — rejected on review by {said.get('author') or 'a reviewer'}" if said.get("verdict") == "rejected" else
                          " — candidate") + (f" (note: {said['note']})" if said.get("note") else "")
                evidence.append(f"Lap {e.lap}, {e.corner} {e.phase} at {e.peak_m:.0f} m: {e.severity} {e.cause}{lock}, "
                                f"{e.duration_s:.1f} s at {e.speed:.0f} km/h{paid}{status}")
            busiest = ev["corner"].value_counts()
            out.append(_finding(
                (f"{len(ev)} balance-incident candidates inferred from the steering, most of them in {busiest.index[0]}"
                 + (f" — {n_conf} confirmed" if n_conf else " — none confirmed")
                 + (f", {rejected} rejected on review" if rejected else "")), evidence,
                calc + " These are inferred candidates: a recorded incident (the simulator's own) on the same lap and "
                "in the same corner confirms one, as does a person's verdict in the app's event review; a candidate "
                "rejected on review is listed as such and kept, with the event as detected.",
                c.conf(uses=("acc_lat", "speed"),
                       caps=[("medium", "checked against staged incidents and one simulator session, not against a driver's notes")]),
                f"Start with {busiest.index[0]} ({busiest.iloc[0]} incidents). "
                + (f"Laps with an incident lost {total:.2f} s in those zones in total. " if total > 0 else
                   "They did not cost lap time, so the gain is in tyre life and margin. ")
                + "Driver performance says how each corner is being driven; the setup hypotheses say what to test.",
                _join(busiest.index[:3]), None))
        else:
            out.append(_finding(
                "No understeer or oversteer incident on any lap",
                [f"No stretch of any lap departed from the car's normal steering by {model['threshold_deg']:.0f} deg for "
                 f"{handling.MIN_DURATION_S:g} s, over {model['flying_laps']} flying laps"],
                calc, c.conf(uses=("acc_lat", "speed")),
                "The car was driven inside its limits throughout. A slide that happens the same way every lap is a "
                "habit, not an incident: see Vehicle balance.", "whole session"))

    short = s.laps[s.laps["status"] == "short"]
    if len(short):
        out.append(_finding(
            f"Track limits: lap{'s' if len(short) > 1 else ''} {_join(short['lap'])} cut part of the circuit",
            [f"Lap {l.lap} covered {l.dist_m:.0f} m against a lap length of {s.lap_length:.0f} m ({s.lap_length - l.dist_m:.0f} m short)"
             for l in short.itertuples()],
            "Distance per lap is measured line to line. A flying lap more than 0.4% shorter than the median flying lap "
            "is marked short.",
            c.conf(n=None, caps=[] if "distance" in s.mapping else
                   [("medium", "distance is integrated from speed, which also shortens with wheel lock or sensor error")]),
            "These laps are left out of every comparison above. If one of them was the fastest lap, it does not count.",
            "laps " + _join(short["lap"])))

    slow = s.laps[s.laps["status"] == "slow"]
    if len(slow) and c.metrics is not None:
        evidence, typical = [], c.summary.set_index("corner")["mean_s"]
        for l in slow.itertuples():
            part = c.all_metrics[c.all_metrics["lap"] == l.lap].set_index("corner")["zone_time_s"]
            loss = (part - typical).dropna().sort_values(ascending=False)
            where = f"; {loss.iloc[0]:.1f} s of it in {loss.index[0]}" if len(loss) and loss.iloc[0] > 0.3 else ""
            evidence.append(f"Lap {l.lap}: {fmt_time(l.lap_time_s)}, {l.lap_time_s - c.best_time:+.1f} s against the best{where}")
        out.append(_finding(
            f"{len(slow)} slow lap{'s' if len(slow) > 1 else ''} left out of the analysis: {_join(slow['lap'])}", evidence,
            "A complete lap is marked slow when it is far off the session's flying pace; the zone that lost the most "
            "against the typical zone time locates where.",
            c.conf(n=None), "Traffic, a mistake or a cool-down lap. Check the located zone if the cause is not known.",
            "laps " + _join(slow["lap"])))

    if any(tyre_temp_channel(c.ch.columns, w) for w in WHEELS):
        jumps = tyres.detect_anomalies(s)
        at_speed = jumps[jumps["lap_status"] != "pit"] if len(jumps) else jumps
        if len(at_speed):
            out.append(_finding(
                f"{len(at_speed)} sudden tyre temperature change{'s' if len(at_speed) > 1 else ''} at speed",
                [f"Lap {e.lap}, {e.corner.upper()}: {e.delta_C:+.0f} °C in half a second, to {e.temp_C:.0f} °C"
                 for e in at_speed.head(6).itertuples()],
                "A tyre temperature moving more than 8 °C within 0.5 s, after 0.1 s of smoothing. Changes during a pit "
                "lap (a tyre change) are left out.",
                c.conf(n=None, caps=[("medium", "a sensor glitch looks the same as a lock-up")]),
                "A sharp rise on one front tyre under braking is a lock-up: check that corner's braking on that lap, and "
                "the tyre for a flat spot.", _join(sorted({e.corner.upper() for e in at_speed.itertuples()}))))

    out += _corner_check(c)
    out += _peak_check(c)
    counted = s.count_lines() if hasattr(s, "count_lines") else []
    if counted:
        k = s.counts
        changed = (k["rows_in_file"] - k["rows_used"]) + sum(k.get("blanked", {}).values())
        out.append(_finding(
            f"{k['rows_used']:,} of {k['rows_in_file']:,} logged rows were used; {k['samples_analysed']:,} samples analysed",
            counted,
            "Counted at each step of the import, in order: rows the reader returned, rows dropped for a missing or "
            "repeated time stamp, samples blanked as glitches, then the samples on the analysis clock. Figures in this "
            "report are computed on the analysed samples.",
            {"level": "high", "basis": "counted during the import"},
            ("Nothing was changed in the logged data." if not changed else
             f"{changed:,} logged values were dropped or blanked; the findings do not rest on them. Check the logger if "
             "the share grows."), "the file"))

    warnings = s.warnings()
    if warnings:
        out.append(_finding(
            f"{len(warnings)} data problem{'s' if len(warnings) > 1 else ''} in the file limit this report",
            [w if len(w) <= 240 else w[:237] + "…" for w in warnings[:6]],
            "Warnings raised while the file was imported: units guessed, samples out of order, channels recorded too "
            "coarsely, signs that disagree.",
            {"level": "high", "basis": "read directly from the import"},
            "Fix these at the source (logger configuration or export settings) before relying on the findings they touch.",
            "the file"))
    if not out:
        out.append(_finding(
            "Nothing was flagged in this session", ["No track-limit, slow-lap or data warnings were raised"],
            "Lap statuses and import warnings were checked.", c.conf(n=None),
            "Balance incidents could not be looked for: " + (c.handling_reason or "the data does not allow it") + ".",
            "whole session"))
    return out
