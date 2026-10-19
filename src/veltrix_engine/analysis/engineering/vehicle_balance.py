"""Section 7: vehicle balance."""

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


# ── 7. vehicle balance ────────────────────────────────────────────────────────

def _balance(c: _Ctx) -> tuple[list, str | None]:
    s, out = c.session, []
    if "steering" not in c.ch or not c.usable_lat:
        return out, ("it needs steering and a usable lateral acceleration channel"
                     + (" (lateral g here is recorded too coarsely and there is no damper travel to rebuild it from)"
                        if c.lat_id else ""))
    sc = balance.steering_characteristic(s)
    if sc:
        k = sc["understeer_deg_per_g"]
        read = "understeers" if k > 0.2 else "oversteers" if k < -0.2 else "is close to neutral"
        evidence = [f"Understeer gradient {k:+.2f} deg/g at the road wheels ({sc['steer_deg_per_g']:+.1f} deg/g at the steering wheel)",
                    f"Steering ratio from the same fit {sc['ratio']:.1f} (configured {s.vehicle.steer_ratio:g}), "
                    f"from {sc['samples']:,} cornering samples"]
        per_stint = []
        for stint in sorted(s.laps["stint"].unique()):
            laps = s.laps[(s.laps["stint"] == stint) & (s.laps["status"] == "flying")]["lap"]
            part = balance.steering_characteristic(s, laps=list(laps), ratio=sc["ratio"]) if len(laps) >= 3 else None
            if part:
                per_stint.append((int(stint), part["understeer_deg_per_g"]))
        if len(per_stint) >= 2:
            evidence.append("By stint: " + ", ".join(f"stint {a} {b:+.2f} deg/g" for a, b in per_stint))
        out.append(_finding(
            f"In steady cornering the car {read} ({k:+.2f} deg/g)", evidence,
            "Steady-state cornering: road-wheel angle = L·ay/V² + K·ay. Steering per g of lateral acceleration is "
            f"regressed on L·g/V² over flying-lap samples above {balance.MIN_LAT_G:g} g and {balance.MIN_SPEED_KMH:.0f} km/h; "
            "the slope is the steering ratio and the intercept gives K, the understeer gradient. Only ratio × "
            f"wheelbase is identified, so K rests on the configured wheelbase of {s.vehicle.wheelbase_m:g} m.",
            c.conf(uses=("acc_lat", "speed"),
                   caps=[("medium", "the wheelbase is the configured value; set the real one for a firm figure")]),
            ("A positive gradient is normal and stable. Use it as the baseline: the same figure after a setup change "
             "shows which way the balance moved." if k >= -0.2 else
             "A negative gradient means the car needs less steering as it loads up — check it against the driver's "
             "feel before trusting it, then look at rear grip."), "whole lap"))

    if c.handling is not None:
        t = c.handling["tendency"]
        t = t[np.isfinite(t["overall_deg"])]
        model = c.handling["model"]
        if len(t):
            traits, evidence = [], []
            for kind, part in t.groupby("kind"):
                row = {p: float(part[f"{p}_deg"].mean()) for p in ("entry", "apex", "exit")}
                evidence.append(f"{kind} corners ({_join(part['corner'])}): entry {row['entry']:+.1f}, apex {row['apex']:+.1f}, "
                                f"exit {row['exit']:+.1f} deg of steering against the car's own norm")
                for p, v in row.items():
                    if np.isfinite(v) and abs(v) >= PHASE_DEG:
                        traits.append((kind, p, v))
            calc = ("A steering model is fitted to this session: steering = |ay|·(a·L·g/V² + b + c·|ay|). The residual "
                    "(steering applied minus steering expected) is positive for understeer, negative for oversteer. "
                    "Its median over the flying laps at each point of the lap is the car's habit there; it is averaged "
                    f"over the entry, apex and exit of each corner. {PHASE_DEG:g} deg or more is called a trait. "
                    f"Model fitted on {model['samples']:,} samples, scatter ±{model['sigma_deg']:.1f} deg.")
            conf = c.conf(uses=("acc_lat", "speed"),
                          effect=float(np.mean([abs(v) for *_, v in traits])) if traits else None,
                          scatter=float(model["sigma_deg"]),
                          caps=[("medium", "the norm is this car on this day: it shows where balance differs, not "
                                           "whether the whole car is balanced")])
            if traits:
                text = _join(f"{'understeer' if v > 0 else 'oversteer'} at {p} of {kind} corners ({v:+.0f} deg)" for kind, p, v in traits)
                # an oversteer trait read from the steering is checked against the car actually travelling sideways
                slip = advice.exit_slip(s, c.corners, list(t["corner"]))
                verdict = ("These are habits of the car, the same every lap. They are the first place to aim a setup "
                           "change: see the setup hypotheses.")
                if slip and any(v < 0 for *_, v in traits):
                    exits = {k: v[1] for k, v in slip.items() if np.isfinite(v[1])}
                    worst = max(exits.values()) if exits else np.nan
                    evidence.append("Body slip at corner exit, median over flying laps: "
                                    + ", ".join(f"{k} {v:.1f} deg" for k, v in sorted(exits.items(), key=lambda kv: -kv[1])[:6]))
                    if np.isfinite(worst) and worst < advice.HABIT_SLIP_DEG:
                        verdict = (f"The oversteer read here is not the car sliding: body slip stays under "
                                   f"{advice.HABIT_SLIP_DEG:g} deg at every exit. It is how this session's steering model "
                                   "fits the car in those phases (a residual against the car's own norm, not a balance "
                                   "measurement). Do not change the setup for it; use the figures as a baseline to compare "
                                   "a later session with.")
                        conf = {"level": conf["level"], "basis": conf["basis"] + "; checked against measured body slip"}
                out.append(_finding(f"Balance by phase: {text}", evidence, calc, conf, verdict,
                                    _join(sorted({k for k, *_ in traits})) + " corners"))
            else:
                out.append(_finding(
                    "The balance is the same through entry, apex and exit in every type of corner", evidence, calc, conf,
                    "No phase of the corner stands out. Balance changes should aim at the whole-lap gradient above.",
                    "all corners"))

    per_lap = balance.balance_by_lap(s)
    if per_lap is not None:
        vals = per_lap.reindex(c.flying).dropna()
        if len(vals) >= 4:
            x = np.arange(len(vals))
            slope = float(np.polyfit(x, vals.to_numpy(), 1)[0])
            drift = slope * (len(vals) - 1)
            resid = float(np.std(vals.to_numpy() - np.polyval(np.polyfit(x, vals.to_numpy(), 1), x)))
            moving = abs(drift) >= 0.03 and abs(drift) >= 2 * resid
            out.append(_finding(
                (f"The balance moves toward {'oversteer' if drift > 0 else 'understeer'} as the laps go by" if moving else
                 "The balance does not drift over the flying laps"),
                [f"Mean oversteer metric per flying lap: {', '.join(f'{v:+.3f}' for v in vals)} g",
                 f"Fitted change first to last {drift:+.3f} g, scatter ±{resid:.3f} g"],
                "Oversteer metric = |lateral g| − |lateral g a neutral-steer car would make for the steering applied "
                "(V²·δ / (g·L))|, averaged over samples above 0.3 g on each flying lap; a straight line is fitted through "
                "the laps in order. A change of 0.03 g and twice the scatter is called a drift.",
                c.conf(uses=("acc_lat",), effect=drift, scatter=resid),
                (("The rear tyres are going away first: protect them on exit, or soften the rear." if drift > 0 else
                  "The front tyres are going away first: protect them on entry, or soften the front.")
                 if moving else "The balance is stable over a run. Tyre wear is not shifting it."), "whole lap"))

    if all(f"susp_travel_{w}" in c.ch for w in WHEELS) and "lltd_front" in c.frame and c.frame["lltd_front"].notna().any():
        v = s.vehicle
        static = (f"Static front weight {v.front_weight_dist:.1%} ({v.origin('front_weight_dist')})"
                  if v.known("front_weight_dist") else "Static front weight not known (no setup in the file, none stated)")
        lat = c.frame["acc_lat"] if "acc_lat" in c.frame else c.frame.get("acc_lat_calc")
        roll = []
        if lat is not None:
            ay = lat.to_numpy(float)
            cornering = c.fly_rows & (np.abs(ay) > 0.5)
            for axle, (left, right) in (("front", ("fl", "fr")), ("rear", ("rl", "rr"))):
                diff = (c.ch[f"susp_travel_{right}"] - c.ch[f"susp_travel_{left}"]).to_numpy(float) / 2.0
                with np.errstate(divide="ignore", invalid="ignore"):
                    per_g = np.abs(diff[cornering] / ay[cornering])
                if np.isfinite(per_g).any():
                    roll.append((axle, float(np.nanmedian(per_g))))
        roll_text = [f"Roll at the {a} dampers: {mm:.1f} mm of left–right difference (half) per g" for a, mm in roll]
        if v.known("wheel_rate_front_N_mm") and v.known("wheel_rate_rear_N_mm"):
            lltd = float(c.frame.loc[c.fly_rows, "lltd_front"].mean())
            out.append(_finding(
                f"The front axle carries {lltd:.0%} of the lateral load transfer",
                [f"Front share of lateral load transfer {lltd:.1%} above 0.5 g on flying laps", static] + roll_text,
                "Load transfer per axle = wheel rate × (left − right damper travel) ÷ 2, front as a share of front plus "
                f"rear. Wheel rates: front {v.wheel_rate_front_N_mm:g} N/mm ({v.origin('wheel_rate_front_N_mm')}), "
                f"rear {v.wheel_rate_rear_N_mm:g} N/mm ({v.origin('wheel_rate_rear_N_mm')}).",
                c.conf(),
                ("A front share above the static front weight pushes the car toward understeer, below it toward oversteer."
                 if v.known("front_weight_dist") else
                 "Compare it with the car's static front weight once that is known: above it pushes toward understeer, "
                 "below it toward oversteer."), "whole lap"))
        elif roll:
            out.append(_finding(
                "The front/rear split of lateral load transfer cannot be stated without this car's wheel rates",
                roll_text + [static],
                "Damper roll is measured: half the left–right difference in damper travel, per g of lateral acceleration, "
                "median over flying laps above 0.5 g. Turning it into load needs each axle's roll wheel rate (spring rate "
                "× motion ratio² plus the anti-roll bar), which neither the file nor the import gives. With assumed rates "
                "the split would only repeat the assumption, so none is given.",
                {"level": "high", "basis": "measured damper travel; the split itself is withheld"},
                "Enter the front and rear roll wheel rates (--wheel-rate-front / --wheel-rate-rear, or the car settings) "
                "to get the split. The damper roll above is still useful on its own: compare it between setups.",
                "whole lap"))
    return out, None
