"""Sections 3 and 4: braking and cornering."""

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


# ── 3. braking ────────────────────────────────────────────────────────────────

def _braking(c: _Ctx) -> list:
    out, m = [], c.metrics
    how = ("where the brake first rises above "
           f"{corner_engine.BRAKE_ON_FRACTION:.0%} of its session maximum" if "brake" in c.ch else
           f"where deceleration first passes {-corner_engine.BRAKE_ON_G:g} g (the file has no brake channel)")
    point_calc = (f"Braking point = {how}, in the braking phase that takes off the most speed before the corner's "
                  "apex. That phase may start before the corner's zone does, and a release shorter than "
                  f"{corner_engine.BRAKE_GAP_S:g} s does not end it. It is given in metres before the corner's turn-in, "
                  "the distance a trackside marker board counts down to. Turn-in is one fixed point per corner: on the "
                  f"typical lap, where lateral g first builds to {corner_engine.TURN_IN_SHARE:.0%} of the corner's peak. ")
    zones = [k for k, part in m.groupby("corner", sort=False) if part["brake_m"].notna().mean() >= 0.6]
    if not zones:
        return out

    scatter = {k: float(m.loc[m["corner"] == k, "brake_m"].std(ddof=0)) for k in zones}
    loose = {k: v for k, v in scatter.items() if v >= BRAKE_SCATTER_M}
    if c.n >= 3:
        ranked = sorted(scatter.items(), key=lambda kv: -kv[1])
        where = {k: float(m.loc[m["corner"] == k, "brake_before_m"].median()) for k in zones}
        evidence = [f"{k}: braking point moves by ±{v:.0f} m" for k, v in ranked[:4]]
        # every braking zone's reference, as the driver finds it on track
        evidence.append("Typical braking points: " + "; ".join(f"{k} {corner_engine.before_corner(where[k])}" for k in zones))
        if loose:
            out.append(_finding(
                f"The braking point is not repeatable into {_join(loose)}", evidence,
                point_calc + f"Scatter = standard deviation over the flying laps; {BRAKE_SCATTER_M:.0f} m or more is raised.",
                c.conf(caps=[] if "brake" in c.ch else [("medium", "braking is inferred from deceleration, not a pedal channel")]),
                f"Fix a braking reference for {_join(loose)} before looking for a later braking point — "
                + "; ".join(f"{k}: brake at {corner_engine.before_corner(where[k])} every lap" for k in loose)
                + f". At these speeds ±{max(loose.values()):.0f} m is more than the gain being chased.", _join(loose)))
        else:
            out.append(_finding(
                f"Braking points repeat to within ±{ranked[0][1]:.0f} m in all {len(zones)} braking zones", evidence,
                point_calc + "Scatter = standard deviation over the flying laps.",
                c.conf(caps=[] if "brake" in c.ch else [("medium", "braking is inferred from deceleration, not a pedal channel")]),
                "The references are working. Any gain in the braking zones comes from moving the point, not steadying it.",
                "all braking zones"))

    later, gain = [], 0.0
    for k in zones:
        best, typical, part = c.best_and_typical(k)
        d = best["brake_m"] - typical["brake_m"]
        avail = float(c.summary.loc[c.summary["corner"] == k, "available_s"].iloc[0])
        if np.isfinite(d) and d >= BRAKE_M and avail >= MIN_GAIN_S:
            later.append((k, d, typical["brake_before_m"], best["brake_before_m"], avail, scatter[k], int(best["lap"])))
            gain += avail
    if later:
        later.sort(key=lambda r: -r[4])
        out.append(_finding(
            f"The fastest pass braked later into {_join(r[0] for r in later)}",
            [f"{k}: typically brakes {corner_engine.before_corner(typ)}, on the fastest pass (lap {lap}) "
             f"{corner_engine.before_corner(best)} — {d:.0f} m later; "
             f"{avail:.2f} s available in the zone" for k, d, typ, best, avail, _, lap in later],
            point_calc + BEST_VS_TYPICAL + f"A difference of {BRAKE_M:.0f} m or more is raised, where the zone holds at "
            f"least {MIN_GAIN_S:g} s. The time is the whole zone's, so braking explains part of it, not all.",
            c.conf(effect=float(np.mean([r[1] for r in later])), scatter=float(np.mean([r[5] for r in later])),
                   caps=[("medium", "the later braking point went with a faster zone; that does not prove it caused it")]),
            "Move the braking point later in steps of about 5 m: "
            + "; ".join(f"{k} toward {corner_engine.before_corner(best)}" for k, _, _, best, *_ in later)
            + ". Keep the step only if minimum speed and exit speed hold.",
            _join(r[0] for r in later), at_stake_s=gain))

    heavy = []
    for k in zones:
        part = m[m["corner"] == k]
        drop = float((part["entry_speed"] - part["min_speed"]).median())
        peak = part["peak_brake_g"].dropna()
        if drop >= HEAVY_BRAKE_KMH and len(peak) >= 2:
            heavy.append((k, float(peak.median()), float(peak.max()), float(peak.std(ddof=0))))
    if heavy:
        soft = [h for h in heavy if h[1] < DECEL_SHARE * h[2] and h[2] - h[1] >= DECEL_GAP_G]
        hardest = max(heavy, key=lambda h: h[2])
        evidence = [f"{k}: typical peak {med:.2f} g, best {top:.2f} g ({med / top:.0%})" for k, med, top, _ in heavy[:6]]
        source = ("the measured longitudinal acceleration" if "acc_long" in c.ch and c.ch["acc_long"].notna().any()
                  else "d(speed)/dt, low-pass filtered")
        calc = (f"Peak deceleration = the largest value of {source} held over {corner_engine.PEAK_G_SAMPLES} samples "
                "of the distance grid, within the braking phase. "
                "Each zone is compared with its own best, not with the hardest stop of the lap, because downforce "
                f"makes a fast stop harder than a slow one. Zones shedding less than {HEAVY_BRAKE_KMH:.0f} km/h are left out.")
        conf = c.conf(uses=("speed",), effect=float(np.mean([h[2] - h[1] for h in heavy])),
                      scatter=float(np.mean([h[3] for h in heavy])))
        if soft:
            out.append(_finding(
                f"The first hit of the brakes is softer than the driver's own best into {_join(h[0] for h in soft)}",
                evidence, calc, conf,
                f"Build peak pressure faster in {_join(h[0] for h in soft)}: the car has already shown "
                f"{', '.join(f'{h[2]:.2f} g' for h in soft)} there. Harder initial braking allows a later braking point "
                "for the same corner entry speed.", _join(h[0] for h in soft)))
        else:
            out.append(_finding(
                f"Peak braking is used consistently, up to {hardest[2]:.2f} g into {hardest[0]}", evidence, calc, conf,
                "Nothing to change in how hard the brakes are applied.", "heavy braking zones"))

    ch = c.ch
    brake_on = c.brake_on()
    if "abs_active" in ch and brake_on is not None and (brake_on & c.fly_rows).any():
        share = float((ch.loc[brake_on & c.fly_rows, "abs_active"] > 0).mean())
        out.append(_finding(
            f"ABS is working for {share:.0%} of the time on the brakes",
            [f"{share:.0%} of braking samples on flying laps have the ABS channel active"],
            "Share of samples with the brake on in which the ABS channel is non-zero.",
            c.conf(n=None, caps=[("medium", "what the ABS channel reports depends on the car")]),
            ("The tyres are being asked for more than they can give under braking: ease the peak pressure slightly, or "
             "move the brake balance toward the axle that is not locking." if share >= 0.30 else
             "ABS is a backstop here, not a habit. Nothing to change."), "braking zones"))
    return out


# ── 4. cornering ──────────────────────────────────────────────────────────────

def _cornering(c: _Ctx) -> list:
    out, m, summary = [], c.metrics, c.summary
    slow, gain = [], 0.0
    for k in summary["corner"]:
        best, typical, part = c.best_and_typical(k)
        d = best["min_speed"] - typical["min_speed"]
        avail = float(summary.loc[summary["corner"] == k, "available_s"].iloc[0])
        if d >= SPEED_KMH and avail >= MIN_GAIN_S:
            slow.append((k, d, typical["min_speed"], best["min_speed"], avail, float(part["min_speed"].std(ddof=0)), int(best["lap"])))
            gain += avail
    calc = (BEST_VS_TYPICAL + "Minimum speed = the lowest speed between the start and end of the corner. A difference "
            f"of {SPEED_KMH:.0f} km/h or more is raised, where the zone holds at least {MIN_GAIN_S:g} s.")
    if slow:
        slow.sort(key=lambda r: -r[4])
        more = len(slow) - 5
        slow = slow[:5]
        gain = sum(r[4] for r in slow)
        out.append(_finding(
            f"Minimum speed is given away in {_join(r[0] for r in slow)}"
            + (f" (and {more} more corner{'s' if more > 1 else ''} with less at stake)" if more > 0 else ""),
            [f"{k}: typically {typ:.0f} km/h at the apex, {best:.0f} km/h on the fastest pass (lap {lap}) — {d:.0f} km/h; "
             f"{avail:.2f} s available in the zone" for k, d, typ, best, avail, _, lap in slow],
            calc,
            c.conf(uses=("speed",), effect=float(np.mean([r[1] for r in slow])), scatter=float(np.mean([r[5] for r in slow])),
                   caps=[("medium", "the higher minimum speed went with a faster zone; that does not prove it caused it")]),
            "Carry more speed to the apex: " + "; ".join(f"{k} about {d:.0f} km/h" for k, d, *_ in slow)
            + ". That usually means releasing the brake earlier, not turning in faster.",
            _join(r[0] for r in slow), at_stake_s=gain))
    elif c.n >= 2:
        k = summary.loc[summary["min_speed_mean"].idxmin()]
        out.append(_finding(
            "Apex speeds match the driver's best in every corner",
            [f"No corner has its typical minimum speed {SPEED_KMH:.0f} km/h or more under its fastest pass with "
             f"{MIN_GAIN_S:g} s available",
             f"Slowest corner: {k.corner}, {k.min_speed_mean:.0f} km/h typical, {k.min_speed_best:.0f} km/h highest"],
            calc, c.conf(uses=("speed",)),
            "Mid-corner speed is repeatable. Look for time in braking and on exit, or in the car.", "all corners"))

    lat = m.dropna(subset=["peak_lat_g"])
    if c.usable_lat and len(lat):
        kind_of = c.corners.set_index("corner")["kind"]
        med = lat.groupby("corner", sort=False)["peak_lat_g"].median()
        sd = lat.groupby("corner", sort=False)["peak_lat_g"].std(ddof=0)
        by_kind = med.groupby(kind_of.reindex(med.index)).max()
        low = [(k, float(v), float(by_kind[kind_of[k]])) for k, v in med.items() if v < LAT_SHARE * by_kind[kind_of[k]]]
        evidence = ["Highest typical peak lateral g by corner type: "
                    + ", ".join(f"{k} {v:.2f} g" for k, v in by_kind.items())]
        evidence += [f"{k} ({kind_of[k]}): typical peak {v:.2f} g, {v / top:.0%} of the best {kind_of[k]} corner" for k, v, top in low[:5]]
        calc = ("Peak lateral g = the largest lateral acceleration between the start and end of the corner, median "
                "over flying laps. Corners are compared within their own type (slow, medium, fast) so that downforce "
                f"does not make every slow corner look under-driven; below {LAT_SHARE:.0%} of the type's best is raised.")
        conf = c.conf(uses=("acc_lat",), effect=float(np.mean([top - v for _, v, top in low])) if low else None,
                      scatter=float(sd.mean()) if len(sd) else None,
                      caps=[("medium", "camber, gradient, surface and a short corner all lower peak g without the "
                                       "driver leaving anything unused")])
        if low:
            out.append(_finding(
                f"{_join(k for k, *_ in low)} use less lateral grip than other corners of the same type", evidence, calc, conf,
                f"Check {_join(k for k, *_ in low)} against the track: an off-camber or bumpy corner explains it. If the "
                "track does not, the car can carry more speed there than it is being asked to.", _join(k for k, *_ in low)))
        else:
            out.append(_finding(
                "Lateral grip is used evenly across corners of the same type", evidence, calc, conf,
                "No corner stands out as under-driven on lateral g.", "all corners"))
    return out
