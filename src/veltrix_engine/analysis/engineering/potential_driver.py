"""Sections 1 and 2: lap potential and driver performance."""

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


# ── 1. lap potential ──────────────────────────────────────────────────────────

def _lap_potential(c: _Ctx) -> list:
    """What the driver did, kept apart from what the pieces of different laps add
    up to. Actual lap times are measured; an ideal lap by corner or by sector is a
    composite estimate, never a lap that was driven or shown to be repeatable."""
    s, out = c.session, []
    times = s.laps.loc[s.laps["status"] == "flying", ["lap", "lap_time_s"]]
    summary = c.summary.sort_values("available_s", ascending=False)
    zones = len(summary)

    # 1. the laps as driven
    if c.n >= 1:
        t = times["lap_time_s"].to_numpy(float)
        sd, median = (float(np.std(t)), float(np.median(t))) if c.n >= 2 else (np.nan, float(t[0]))
        label = ("consistent" if sd < CONSISTENT_S else "loose" if sd > LOOSE_S else "moderately consistent") if c.n >= 3 else ""
        out.append(_finding(
            f"Best lap {fmt_time(c.best_time)} on lap {c.best_lap}"
            + (f"; lap times are {label}, ±{sd:.2f} s over {c.n} flying laps" if label else f", from {c.n} flying lap(s)"),
            [f"Flying laps: {', '.join(f'lap {r.lap} {fmt_time(r.lap_time_s)}' for r in times.itertuples())}",
             f"Median {fmt_time(median)}, {median - c.best_time:.2f} s off the best"],
            "Lap times as timed at the start/finish line. Out-laps, in-laps, pit laps, laps marked slow and laps that "
            f"cut the track are left out. Below {CONSISTENT_S:g} s standard deviation is called consistent, above "
            f"{LOOSE_S:g} s loose.",
            c.conf(),
            ("The lap time is repeatable, so differences between laps of this size mean something when a change is "
             "tested." if c.n >= 3 and sd < CONSISTENT_S else
             f"Treat a setup or technique change as real only if it moves the lap time by more than about {2 * sd:.1f} s, "
             "or judge it on the corner it targets instead of the whole lap." if c.n >= 3 else
             "Run more laps before comparing lap times: one or two laps do not show how repeatable the pace is."),
            "whole lap", nature=MEASURED))

    # 2. the ideal lap by corner — an estimate, built from named laps
    gap = c.best_time - c.ideal
    split = summary.dropna(subset=["gap_on_best_lap_s"]) if "gap_on_best_lap_s" in summary else summary.iloc[0:0]
    from_laps = summary.groupby("best_lap", sort=True)["corner"].apply(list)
    evidence = [f"Fastest pass through each of the {zones} corner zones, added together: {fmt_time(c.ideal)}, "
                f"{gap:.3f} s under the best lap"]
    evidence.append("Built from " + "; ".join(
        f"lap {lap} ({len(cs)} corner{'s' if len(cs) > 1 else ''}: {_join(cs)})" for lap, cs in from_laps.items()))
    if len(split):
        owed = split[split["gap_on_best_lap_s"] > 0.005].sort_values("gap_on_best_lap_s", ascending=False)
        evidence.append("Where the gap is, against the best lap: " + ", ".join(
            f"{r.corner} {r.gap_on_best_lap_s:.2f} s ({r.gap_on_best_lap_s / gap:.0%}, best pass on lap {r.best_lap})"
            for r in owed.head(6).itertuples()) + (f", and {len(owed) - 6} more" if len(owed) > 6 else ""))
        level = split[split["gap_on_best_lap_s"] <= 0.005]["corner"]
        if len(level):
            evidence.append(f"The best lap is already the fastest pass in {_join(level)}")
    wide = c.metrics.pivot_table(index="lap", columns="corner", values="zone_time_s")
    second = float(np.sort(wide.to_numpy(), axis=0)[1].sum()) if c.n >= 3 and not wide.isna().any().any() else np.nan
    if np.isfinite(second):
        evidence.append(f"Second-fastest pass through each zone, added together: {fmt_time(second)} "
                        f"({c.best_time - second:+.3f} s against the best lap) — also an estimate")
    calc = (ZONES_CALC + "Ideal lap by corner = the sum over zones of each zone's fastest time on any flying lap. A "
            "corner's part of the gap = the best lap's time in that zone minus the fastest time there; the parts add "
            "up to the gap. Second-fastest sum = the same with each zone's second-lowest time, leaving out its single "
            "luckiest pass.")
    caps = [("medium", "a sum of best passes is optimistic: every minimum includes its share of favourable scatter, "
                       "and no lap combined them")]
    top = split.sort_values("gap_on_best_lap_s", ascending=False).head(3) if len(split) else summary.head(3)
    if gap < MIN_GAIN_S:
        out.append(_finding(
            f"Estimate: the best pass through every corner adds up to within {max(gap, 0):.2f} s of the best lap",
            evidence, calc, c.conf(uses=("speed",), caps=caps),
            "The best lap already holds nearly every best corner. More pace has to come from doing something "
            "differently — technique or the car — not from repeating what has already been done.",
            "whole lap", 0.0, nature=ESTIMATE))
    else:
        where = ", ".join(f"{r.corner} ({r.gap_on_best_lap_s:.2f} s)" for r in top.itertuples()) if len(split) else ""
        out.append(_finding(
            f"Estimate: the best pass through every corner adds up to {fmt_time(c.ideal)}, {gap:.2f} s under the best "
            f"lap — built from {len(from_laps)} different laps, not driven",
            evidence, calc, c.conf(uses=("speed",), caps=caps),
            "No lap has put these corners together, so this is a measure of what the driver has shown in pieces, not a "
            "lap time to expect"
            + (f"; the second-fastest passes ({fmt_time(second)}) are a steadier guide to what is within reach. "
               if np.isfinite(second) and second < c.best_time else ". ")
            + (f"The best lap gives most of the gap away in {where}: start there." if where else ""),
            "whole lap", nature=ESTIMATE))

    # 3. the ideal lap by sector — a second, coarser estimate, kept apart
    if c.n >= 2:
        sectors = lap_engine.sector_times(s)
        theoretical = float(lap_engine.theoretical_best(sectors))
        parts = lap_engine.sector_breakdown(sectors, c.best_lap)
        out.append(_finding(
            f"Estimate: the best of three equal sectors adds up to {fmt_time(theoretical)}, "
            f"{c.best_time - theoretical:.2f} s under the best lap",
            [f"{r.sector}: fastest {r.best_s:.3f} s on lap {r.best_lap}; the best lap {r.on_best_lap_s:.3f} s "
             f"({r.gap_on_best_lap_s:+.3f} s)" for r in parts.itertuples()]
            + [f"By corner the same idea gives {fmt_time(c.ideal)}: more, shorter pieces each catch a little more luck, "
               "so the corner figure is usually the lower of the two"],
            "The lap is cut into three sectors of equal length from the start/finish line; each sector's fastest time "
            "on any flying lap is added up. Sector parts of the gap = the best lap's sector time minus the fastest.",
            c.conf(caps=[("medium", "a composite of different laps; no lap combined these sectors")]),
            "Use it beside the corner figure, not instead of it: the two estimates bracket what the pieces add up to. "
            "Neither has been driven.", "whole lap", nature=ESTIMATE))

    # 4. where a typical lap gives time away — against each corner's best pass
    total = float(summary["available_s"].sum())
    if len(summary) and float(summary["available_s"].max()) >= MIN_GAIN_S:
        top_typ = summary.head(3)
        by_kind = summary.groupby("kind")["available_s"].sum()
        share = float(top_typ["available_s"].sum() / total)
        out.append(_finding(
            f"{share:.0%} of the time a typical lap gives away is in {_join(top_typ['corner'])}",
            [f"A typical lap is {total:.2f} s slower than the best pass through every zone"]
            + [f"{r.corner} ({r.kind}): typical {r.mean_s:.2f} s, best {r.best_s:.2f} s on lap {r.best_lap}, "
               f"{r.available_s:.2f} s available, spread ±{r.spread_s:.2f} s" for r in top_typ.itertuples()]
            + ["By corner type: " + ", ".join(f"{k} {v:.2f} s" for k, v in by_kind.items())],
            ZONES_CALC + "Available time in a zone = mean zone time over flying laps − fastest zone time. Corner type "
            f"by minimum speed: slow below {corner_engine.SLOW_KMH:.0f} km/h, fast above {corner_engine.FAST_KMH:.0f} km/h.",
            c.conf(uses=("speed",), effect=float(top_typ["available_s"].mean()), scatter=float(top_typ["spread_s"].mean())),
            f"Work on {_join(top_typ['corner'])} first, in that order. Braking, cornering and traction below say what "
            "differed between the best pass and the typical one in each.",
            _join(top_typ["corner"]), at_stake_s=float(top_typ["available_s"].sum())))
    return out


# ── 2. driver performance ─────────────────────────────────────────────────────

def _driver(c: _Ctx) -> list:
    out, ch = [], c.ch
    summary = c.summary
    worst = summary.sort_values("spread_s", ascending=False).head(3)
    if c.n >= 3 and len(worst):
        steady = summary.sort_values("spread_s").iloc[0]
        loose = worst[worst["spread_s"] >= 0.05]
        if len(loose):
            out.append(_finding(
                f"The driving varies most in {_join(loose['corner'])}",
                [f"{r.corner}: zone time ±{r.spread_s:.2f} s from lap to lap (typical {r.mean_s:.2f} s)"
                 for r in loose.itertuples()]
                + [f"Most repeatable: {steady.corner}, ±{steady.spread_s:.2f} s"],
                ZONES_CALC + "Spread = standard deviation of a zone's time over the flying laps.",
                c.conf(uses=("speed",)),
                f"Pick fixed references (a braking board, a kerb, a gear) for {_join(loose['corner'])} and drive them the "
                "same way for several laps before changing anything: a corner that varies this much hides the effect "
                "of any change.", _join(loose["corner"])))
        else:
            out.append(_finding(
                "Every corner is driven the same way lap after lap",
                [f"Largest lap-to-lap spread in any zone: {worst.iloc[0].corner}, ±{worst.iloc[0].spread_s:.2f} s"],
                ZONES_CALC + "Spread = standard deviation of a zone's time over the flying laps.",
                c.conf(uses=("speed",)),
                "The driver is a steady reference: use them to judge car changes.", "all corners"))

    brake_on = c.brake_on()
    if "throttle" in ch and brake_on is not None:
        thr, v = ch["throttle"].to_numpy(float), ch["speed"].to_numpy(float)
        coast = c.fly_rows & (thr < COAST_THROTTLE) & ~brake_on & (v > COAST_MIN_KMH)
        both = c.fly_rows & (thr >= corner_engine.THROTTLE_ON) & brake_on & (v > COAST_MIN_KMH)
        full = c.fly_rows & (thr >= corner_engine.THROTTLE_FULL)
        lap_col = ch["lap"].to_numpy()
        per_lap = lambda mask: np.array([mask[lap_col == lap].sum() * c.dt for lap in c.flying])
        coast_s, both_s = per_lap(coast), per_lap(both)
        zone = c.zone_of(ch["lap_distance"].to_numpy(float))
        by_zone = pd.Series(coast.astype(float) * c.dt).groupby(zone).sum() / c.n
        names = c.corners["corner"].to_numpy()
        ranked = by_zone.sort_values(ascending=False)
        zone_text = ", ".join(f"{names[int(k)]} {val:.2f} s" for k, val in ranked.head(3).items() if val >= 0.05)
        best_i = c.flying.index(c.best_lap)
        lap_s = float(np.mean([float(c.session.lap_row(lap).lap_time_s) for lap in c.flying]))
        evidence = [f"Coasting {coast_s.mean():.2f} s a lap on average ({coast_s.mean() / lap_s:.1%} of the lap); "
                    f"{coast_s[best_i]:.2f} s on the best lap",
                    f"Full throttle for {full.sum() * c.dt / c.n / lap_s:.0%} of the lap"]
        if zone_text:
            evidence.append(f"Most coasting per lap: {zone_text}")
        calc = (f"Coasting = time on flying laps above {COAST_MIN_KMH:.0f} km/h with throttle below {COAST_THROTTLE:.0f}% "
                "and the brake off. The brake counts as on above "
                + (f"{corner_engine.BRAKE_ON_FRACTION:.0%} of its session maximum" if "brake" in ch
                   else f"{-corner_engine.BRAKE_ON_G:g} g of deceleration (the file has no brake channel)")
                + f". Full throttle = {corner_engine.THROTTLE_FULL:.0f}% or more. Seconds are samples × {c.dt:g} s.")
        conf = c.conf(effect=float(coast_s.mean()), scatter=float(coast_s.std()) if c.n > 1 else None)
        if coast_s.mean() >= COAST_FLAG_S and zone_text:
            first = names[int(ranked.index[0])]
            out.append(_finding(
                f"{coast_s.mean():.1f} s of every lap is spent neither braking nor accelerating", evidence, calc, conf,
                f"Close the gap between throttle and brake into {first} first: stay on the throttle to the braking "
                "point, or brake later by the distance now covered coasting. Coasting through a fast corner to settle "
                "the car is a choice; coasting on the way into a braking zone is time given away.",
                first))
        else:
            out.append(_finding(
                "Little time is lost coasting between throttle and brake", evidence, calc, conf,
                "Nothing to change here.", "whole lap"))
        if both_s.mean() >= OVERLAP_FLAG_S:
            out.append(_finding(
                f"Throttle and brake are applied together for {both_s.mean():.1f} s a lap",
                [f"Overlap per flying lap: {', '.join(f'{x:.1f}' for x in both_s)} s"],
                f"Time on flying laps with throttle at {corner_engine.THROTTLE_ON:.0f}% or more while the brake is on.",
                c.conf(effect=float(both_s.mean()), scatter=float(both_s.std()) if c.n > 1 else None),
                "If this is deliberate left-foot braking to steady the car, check it against brake temperatures and "
                "fuel use. If it is not deliberate, it is drag on every lap: release the brake fully before the throttle.",
                "whole lap"))

    for item in advice._driver_items(c.session, c.corners, c.all_metrics, c.summary,
                                     c.handling["events"] if c.handling else None):
        if " laps" in item["title"] and item["where"] in set(c.corners["corner"]):
            out.append(_finding(
                item["title"], item["evidence"],
                "Incidents come from the steering model in Vehicle balance; only those rated moderate or severe are "
                f"counted, and a corner is raised when they occur on at least {advice.REPEAT_SHARE:.0%} of laps. The "
                "cost is the mean zone time of laps with an incident minus that of laps without.",
                c.conf(caps=[(item["confidence"], "a repeated pattern, but its cause is read from the pedals, not measured")]),
                item["action"], item["where"], item["gain_s"]))
    return out
