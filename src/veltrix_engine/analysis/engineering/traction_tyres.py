"""Sections 5 and 6: traction and tyres."""

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


# ── 5. traction ───────────────────────────────────────────────────────────────

def _traction(c: _Ctx) -> list:
    out, m, summary, ch = [], c.metrics, c.summary, c.ch
    late, gain = [], 0.0
    for k in summary["corner"]:
        best, typical, part = c.best_and_typical(k)
        d = typical["throttle_on_m"] - best["throttle_on_m"]
        d_exit = best["exit_speed"] - typical["exit_speed"]
        avail = float(summary.loc[summary["corner"] == k, "available_s"].iloc[0])
        if np.isfinite(d) and d >= THROTTLE_M and avail >= MIN_GAIN_S:
            late.append((k, d, typical["throttle_on_m"], best["throttle_on_m"], d_exit, avail,
                         float(part["throttle_on_m"].std(ddof=0)), int(best["lap"])))
            gain += avail
    calc = (BEST_VS_TYPICAL + f"Throttle pick-up = the first point after the apex where throttle is back above "
            f"{corner_engine.THROTTLE_ON:.0f}%. Exit speed = speed at the end of the corner. A pick-up "
            f"{THROTTLE_M:.0f} m or more later than the fastest pass is raised, where the zone holds at least {MIN_GAIN_S:g} s.")
    if late:
        late.sort(key=lambda r: -r[5])
        out.append(_finding(
            f"The throttle goes on later than the driver's best out of {_join(r[0] for r in late)}",
            [f"{k}: throttle typically back on at {typ:.0f} m, on the fastest pass (lap {lap}) at {best:.0f} m — {d:.0f} m "
             f"earlier, {d_exit:+.0f} km/h at the exit; {avail:.2f} s available in the zone"
             for k, d, typ, best, d_exit, avail, _, lap in late],
            calc,
            c.conf(effect=float(np.mean([r[1] for r in late])), scatter=float(np.mean([r[6] for r in late])),
                   caps=[("medium", "the earlier throttle went with a faster zone; that does not prove it caused it")]),
            "Get to the throttle earlier: " + "; ".join(f"{k} by about {d:.0f} m" for k, d, *_ in late)
            + ". If the car will not take it, that is a traction or balance limit — see the incidents and setup "
              "hypotheses for those corners.", _join(r[0] for r in late), at_stake_s=gain))
    elif m["throttle_on_m"].notna().any() and c.n >= 2:
        out.append(_finding(
            "Throttle pick-up matches the driver's best out of every corner",
            [f"No corner has its typical pick-up {THROTTLE_M:.0f} m or more after its fastest pass with {MIN_GAIN_S:g} s available"],
            calc, c.conf(), "Exit technique is repeatable. Any gain on exit has to come from the car.", "all corners"))

    ramp = (m["full_throttle_m"] - m["apex_m"]).groupby(m["corner"], sort=False).median().dropna()
    if len(ramp) and ramp.max() >= 30.0:
        longest = ramp.sort_values(ascending=False).head(3)
        kind_of = c.corners.set_index("corner")["kind"]
        out.append(_finding(
            f"Full throttle comes latest after the apex in {_join(longest.index)}",
            [f"{k} ({kind_of[k]}): {v:.0f} m from the apex to full throttle" for k, v in longest.items()]
            + [f"Shortest: {ramp.idxmin()}, {ramp.min():.0f} m"],
            f"Distance from the point of lowest speed to the first point at {corner_engine.THROTTLE_FULL:.0f}% throttle, "
            "median over flying laps.",
            c.conf(caps=[("medium", "a long corner needs a long throttle application whatever the car's traction")]),
            f"These are the exits where traction is asked for longest. If the car is traction-limited anywhere it will "
            f"be here: watch {_join(longest.index)} when testing rear-end changes.", _join(longest.index)))

    wheels = [f"wheel_speed_{w}" for w in WHEELS]
    hard = c.fly_rows & (ch["throttle"].to_numpy(float) >= 50) & (ch["speed"].to_numpy(float) > COAST_MIN_KMH) \
        if "throttle" in ch else None
    power_os = None
    if c.handling is not None:
        ev = c.handling["events"]
        power_os = ev[ev["cause"].str.startswith("power oversteer")] if len(ev) else ev
    if hard is not None and all(w in ch for w in wheels) and hard.any():
        slip = (ch[["wheel_speed_rl", "wheel_speed_rr"]].mean(axis=1) - ch[["wheel_speed_fl", "wheel_speed_fr"]].mean(axis=1)).to_numpy(float)
        spin = hard & (slip >= WHEELSPIN_KMH)
        share = float(spin.sum() / hard.sum())
        zone = c.zone_of(ch["lap_distance"].to_numpy(float))
        names = c.corners["corner"].to_numpy()
        by_zone = (pd.Series(spin.astype(float) * c.dt).groupby(zone).sum() / c.n).sort_values(ascending=False)
        worst = [(names[int(k)], v) for k, v in by_zone.head(3).items() if v >= 0.05]
        evidence = [f"Rear wheels at least {WHEELSPIN_KMH:.0f} km/h faster than the fronts for {share:.0%} of the time "
                    f"above half throttle ({spin.sum() * c.dt / c.n:.2f} s a lap)"]
        if worst:
            evidence.append("Most wheelspin per lap: " + ", ".join(f"{k} {v:.2f} s" for k, v in worst))
        if power_os is not None:
            evidence.append(f"{len(power_os)} power-oversteer incidents in the session")
        calc = ("Wheelspin = mean rear wheel speed − mean front wheel speed, counted on flying laps at 50% throttle or "
                f"more above {COAST_MIN_KMH:.0f} km/h. {WHEELSPIN_SHARE:.0%} of that time or more is raised.")
        conf = c.conf(caps=[("medium", "front and rear tyre diameters are assumed equal; a difference shifts the whole figure")])
        if share >= WHEELSPIN_SHARE and worst:
            out.append(_finding(
                f"The car is traction-limited out of {_join(k for k, _ in worst)}", evidence, calc, conf,
                f"Feed the throttle in more progressively out of {_join(k for k, _ in worst)}, and treat rear traction as "
                "a setup target: see the setup hypotheses.", _join(k for k, _ in worst)))
        else:
            out.append(_finding("Wheelspin is not limiting the exits", evidence, calc, conf,
                                "The rear tyres are putting the power down. Nothing to change.", "all exits"))
    elif power_os is not None:
        n_laps = power_os["lap"].nunique() if len(power_os) else 0
        out.append(_finding(
            (f"{len(power_os)} power-oversteer incidents on {n_laps} laps" if len(power_os) else
             "No power oversteer was detected on exit"),
            [f"{len(power_os)} incidents with the car oversteering on throttle"
             + (f", in {_join(sorted(set(power_os['corner'])))}" if len(power_os) else ""),
             "Wheelspin itself is not measured: the file has no wheel speeds for all four wheels"],
            "Power oversteer = an oversteer incident from the steering model (see Vehicle balance) while the throttle "
            "is applied.",
            c.conf(uses=("acc_lat",), caps=[("medium", "traction is inferred from the steering, not from wheel speeds")]),
            ("See the incidents and setup hypotheses for those corners." if len(power_os) else
             "Log all four wheel speeds to measure wheelspin directly."), "corner exits"))
    return out


# ── 6. tyres ──────────────────────────────────────────────────────────────────

def _tyres(c: _Ctx) -> tuple[list, str | None]:
    s, ch, out = c.session, c.ch, []
    fly = ch[c.fly_rows]
    have = [w for w in WHEELS if tyre_temp_channel(ch.columns, w)]
    missing = []
    if len(have) == 4 and c.n:
        window = tyres.COMPOUND_WINDOWS[tyres.DEFAULT_COMPOUND]
        ts = tyres.lap_summary(s)
        ts = ts[ts["status"] == "flying"]
        mean = {w: float(ts[f"temp_{w}_mean"].mean()) for w in WHEELS}
        inside = {w: float(ts[f"in_window_{w}"].mean()) for w in WHEELS}
        cold = [w for w in WHEELS if mean[w] < window[w][0]]
        hot = [w for w in WHEELS if mean[w] > window[w][1]]
        evidence = ["Mean temperature on flying laps: " + ", ".join(f"{w.upper()} {mean[w]:.0f} °C" for w in WHEELS),
                    "Share of flying-lap time inside the window: " + ", ".join(f"{w.upper()} {inside[w]:.0f}%" for w in WHEELS),
                    f"Window used: front {window['fl'][0]}–{window['fl'][1]} °C, rear {window['rl'][0]}–{window['rl'][1]} °C"]
        calc = ("Each tyre's representative temperature (the middle of the tread when three zones are logged, else the "
                "single sensor) is averaged over flying laps and set against an operating window; the share is the "
                "fraction of samples inside it.")
        conf = c.conf(caps=[("low", f"the window is the engine's generic '{tyres.DEFAULT_COMPOUND}' default, not this "
                                    "tyre's: set the real one before acting on this")])
        if cold or hot:
            parts = ([f"{_join(w.upper() for w in cold)} below"] if cold else []) + ([f"{_join(w.upper() for w in hot)} above"] if hot else [])
            out.append(_finding(
                "Tyres outside the operating window: " + ", ".join(parts), evidence, calc, conf,
                (("Cold tyres: raise starting pressures slightly, close brake ducts if they heat the rims, or work the "
                  "tyres harder on the out-lap. ") if cold else "")
                + (("Hot tyres: lower pressures within the allowed range, reduce sliding (see incidents), or open "
                    "cooling. ") if hot else "")
                + "Check the window against the tyre maker's figure first.", _join(w.upper() for w in cold + hot)))
        else:
            out.append(_finding("All four tyres run inside the operating window", evidence, calc, conf,
                                "Nothing to change on temperature, if the window used here is right for this tyre.",
                                "all four tyres"))

        front, rear = (mean["fl"] + mean["fr"]) / 2, (mean["rl"] + mean["rr"]) / 2
        left, right = (mean["fl"] + mean["rl"]) / 2, (mean["fr"] + mean["rr"]) / 2
        if abs(rear - front) >= advice.TEMP_SPLIT_C or abs(left - right) >= advice.TEMP_SPLIT_C:
            axle = abs(rear - front) >= abs(left - right)
            hot_end = ("rear" if rear > front else "front") if axle else ("left" if left > right else "right")
            out.append(_finding(
                f"The {hot_end} tyres run hotter",
                [f"Front {front:.0f} °C, rear {rear:.0f} °C; left {left:.0f} °C, right {right:.0f} °C (flying-lap means)"],
                f"Mean of the two tyres on each axle and each side; a difference of {advice.TEMP_SPLIT_C:.0f} °C or more is raised.",
                c.conf(caps=[("medium", "a circuit with more corners one way loads one side whatever the setup")]),
                (f"The {hot_end} axle is doing more of the work. If the balance agrees (see Vehicle balance), shift "
                 f"roll stiffness or aero toward the other axle." if axle else
                 f"Expected on a circuit that turns mostly one way. Set pressures per side so the hot ones land on target."),
                f"{hot_end} tyres"))
    else:
        missing.append("tyre temperatures for all four wheels")

    if all(f"tyre_temp_{w}_inner" in ch and f"tyre_temp_{w}_outer" in ch for w in WHEELS) and c.n:
        lo, hi = advice.CAMBER_SPREAD_C
        spread = {w: float((fly[f"tyre_temp_{w}_inner"] - fly[f"tyre_temp_{w}_outer"]).mean()) for w in WHEELS}
        off = {w: v for w, v in spread.items() if v > hi or v < lo}
        evidence = ["Inner edge minus outer edge, flying laps: " + ", ".join(f"{w.upper()} {v:+.0f} °C" for w, v in spread.items())]
        calc = (f"Mean of inner-zone minus outer-zone surface temperature per tyre over flying laps. {lo:.0f} to {hi:.0f} °C "
                "hotter on the inside is the usual target.")
        conf = c.conf(caps=[("medium", "the target spread is a common rule of thumb, not this tyre's own figure")])
        if off:
            out.append(_finding(
                "Camber looks off on " + _join(w.upper() for w in off), evidence, calc, conf,
                "; ".join(f"{w.upper()}: {'reduce' if v > hi else 'add'} negative camber" for w, v in off.items())
                + ". Change one axle at a time and re-read the spread.", _join(w.upper() for w in off)))
        else:
            out.append(_finding("Temperature across the tread is in the usual camber range on all four tyres",
                                evidence, calc, conf, "Nothing to change on camber.", "all four tyres"))

    if all(f"tyre_press_{w}" in ch for w in WHEELS) and c.n:
        hot_p = {w: float(fly[f"tyre_press_{w}"].mean()) for w in WHEELS}
        first, last = ch[ch["lap"] == c.flying[0]], ch[ch["lap"] == c.flying[-1]]
        rise = {w: float(last[f"tyre_press_{w}"].mean() - first[f"tyre_press_{w}"].mean()) for w in WHEELS}
        split = {"front": hot_p["fl"] - hot_p["fr"], "rear": hot_p["rl"] - hot_p["rr"]}
        uneven = {k: v for k, v in split.items() if abs(v) >= PRESSURE_SPLIT_BAR}
        evidence = ["Mean pressure on flying laps: " + ", ".join(f"{w.upper()} {v:.2f} bar" for w, v in hot_p.items()),
                    f"Change from lap {c.flying[0]} to lap {c.flying[-1]}: "
                    + ", ".join(f"{w.upper()} {v:+.2f}" for w, v in rise.items()) + " bar"]
        calc = (f"Mean of each pressure channel over flying laps; left minus right per axle, {PRESSURE_SPLIT_BAR:g} bar or "
                "more raised. The engine does not know the target hot pressure.")
        conf = c.conf(caps=[("medium", "no target pressure is known, so only the left-right balance is judged")])
        if uneven:
            out.append(_finding(
                "Hot pressures differ left to right on the " + _join(uneven) + " axle", evidence, calc, conf,
                "; ".join(f"{k}: {'left' if v > 0 else 'right'} is {abs(v):.2f} bar higher" for k, v in uneven.items())
                + ". Adjust cold pressures so both sides reach the target hot pressure.", _join(uneven) + " axle"))
        else:
            out.append(_finding("Hot pressures are even left to right", evidence, calc, conf,
                                "Compare the means above with the target hot pressure for this tyre.", "all four tyres"))
    else:
        missing.append("tyre pressures")

    for k in sorted(s.laps["stint"].unique()):
        in_stint = s.laps[(s.laps["stint"] == k) & (s.laps["status"] == "flying")]
        if len(in_stint) < 3:
            continue
        deg = tyres.fit_degradation(s, stint=int(k))
        n = len(deg["ages"])
        if not np.isfinite(deg["slope"]):
            continue
        trend = deg["r_squared"] >= DEG_R2 and deg["slope"] > 0
        resid = float(np.std(deg["lap_times"] - (deg["intercept"] + deg["slope"] * deg["ages"])))
        out.append(_finding(
            (f"Stint {k}: lap time falls away by {deg['slope'] * 1000:.0f} ms a lap" if trend else
             f"Stint {k}: no degradation trend over {n} laps"),
            [f"Laps {_join(deg['laps'])}: {', '.join(fmt_time(t) for t in deg['lap_times'])}",
             f"Fitted slope {deg['slope'] * 1000:+.0f} ms per lap, R² {deg['r_squared']:.2f}, scatter about the line ±{resid:.2f} s"],
            "Least-squares line of lap time against laps since the stint began, flying laps only. A slope is called a "
            f"trend when it is positive and explains at least {DEG_R2:.0%} of the variation (R²).",
            c.conf(n=n, effect=deg["slope"] * max(n - 1, 1), scatter=resid,
                   caps=[("medium", "fuel burn-off makes the car faster as the stint goes on and hides part of the tyre loss")]),
            ((f"Expect about {deg['slope'] * 10:.1f} s over ten laps. " + (f"At this rate the loss passes "
              f"{tyres.LOSS_THRESHOLD_S:g} s a lap after {deg['threshold_age']} laps." if deg["threshold_age"] else ""))
             if trend else "The stint is too short or too scattered to show tyre wear. A run of ten laps or more at "
             "steady pace is needed."), f"stint {k}"))
    reason = None
    if missing:
        reason = ("the file has no " + _join(missing) + (" and no stint of three or more flying laps" if not out else "")
                  + ". Log " + _join(m.replace(" for all four wheels", " (ideally inner, middle and outer)") for m in missing)
                  + " to assess them")
    return out, reason
