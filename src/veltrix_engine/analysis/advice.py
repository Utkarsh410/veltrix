"""
Suggestions: what the driver could do differently, and which setup directions
are worth testing — each one tied to numbers from the session.

How the suggestions are made
----------------------------
Driver. For every corner the driver's *typical* lap is compared with their own
*best* pass through that corner. Where the two differ in braking point, minimum
speed or throttle pick-up, and there is time on the table, that difference is
the suggestion: it is something this driver has already done in this car.
Repeated oversteer or understeer in a corner adds a suggestion about how the
car is being driven there.

Setup. Patterns across corners — the same imbalance in the same phase, in
several corners, on many laps — point to the car rather than to one corner.
Each pattern maps to the standard directions a race engineer would test,
listed from least to most invasive.

What these are not
------------------
They are suggestions to test, not instructions. The engine does not know the
car's current setup, its adjustment ranges or the regulations, and a time
difference between two laps does not prove its cause. Change one thing at a
time and check the result against the same numbers.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .. import corners as corner_engine
from ..schema import CORNERS as WHEELS
from . import balance, handling, tyres

MIN_CORNER_GAIN_S = 0.08      # a corner needs at least this much on the table to be worth a suggestion
MAX_DRIVER_ITEMS = 6
BRAKE_M, SPEED_KMH, THROTTLE_M = 5.0, 2.0, 5.0     # differences worth acting on
BRAKE_SCATTER_M = 8.0         # braking point varying by more than this (std) is inconsistency
REPEAT_SHARE = 0.25           # an imbalance on this share of laps in a corner is a pattern
PATTERN_MIN_EVENTS = 5        # ... and across the car, at least this many incidents
BRAKE_TEMP_WATCH_C = 750.0    # peak disc temperature worth checking against the pad/disc range
TEMP_SPLIT_C = 8.0            # front-rear or left-right tyre temperature split worth acting on
HABIT_SLIP_DEG = 2.0          # body slip at exit this large confirms an oversteer habit read from the steering
CAMBER_SPREAD_C = (5.0, 15.0)     # inner-minus-outer surface temperature: outside this, look at camber


def _item(area, title, where, evidence, action, confidence, gain_s=None, priority=0.0) -> dict:
    return {"area": area, "title": title, "where": where, "evidence": evidence, "action": action,
            "confidence": confidence, "gain_s": None if gain_s is None else float(gain_s),
            "priority": float(priority)}


def _fmt_list(names) -> str:
    names = list(names)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _channel_share(session, channel, mask) -> float | None:
    """Share of the masked samples in which a 0/1 channel is on."""
    ch = session.channels
    if channel not in ch or not mask.any():
        return None
    return float((ch.loc[mask, channel] > 0).mean())


# ── driver ────────────────────────────────────────────────────────────────────

def _driver_items(session, corners, metrics, summary, events) -> list:
    flying = set(session.laps.loc[session.laps["status"] == "flying", "lap"])
    m = metrics[metrics["lap"].isin(flying)] if flying else metrics
    items = []
    total_available = float(summary["available_s"].sum()) if len(summary) else 0.0

    for row in summary.sort_values("available_s", ascending=False).itertuples():
        if row.available_s < MIN_CORNER_GAIN_S or row.laps < 3:
            continue
        part = m[m["corner"] == row.corner]
        best = part.loc[part["zone_time_s"].idxmin()]
        typical = part.median(numeric_only=True)
        evidence = [f"Typical time through the corner {row.mean_s:.2f} s, best {row.best_s:.2f} s (lap {row.best_lap}); "
                    f"lap-to-lap spread {row.spread_s:.2f} s"]
        actions = []

        d_brake = best["brake_m"] - typical["brake_m"]            # +ve: best braked later
        d_min = best["min_speed"] - typical["min_speed"]
        d_thr = typical["throttle_on_m"] - best["throttle_on_m"]  # +ve: typically later on throttle
        d_exit = best["exit_speed"] - typical["exit_speed"]
        if np.isfinite(d_brake) and d_brake >= BRAKE_M:
            actions.append(f"brake about {d_brake:.0f} m later")
            evidence.append(f"Braking point typically {corner_engine.before_corner(typical['brake_before_m'])}, "
                            f"on the best pass {corner_engine.before_corner(best['brake_before_m'])}")
        elif np.isfinite(d_brake) and d_brake <= -BRAKE_M and d_min >= SPEED_KMH:
            actions.append(f"brake about {-d_brake:.0f} m earlier and carry about {d_min:.0f} km/h more through the apex "
                           "(the best pass braked earlier and was faster there)")
            evidence.append(f"Braking point typically {corner_engine.before_corner(typical['brake_before_m'])}, "
                            f"on the best pass {corner_engine.before_corner(best['brake_before_m'])}")
            evidence.append(f"Minimum speed typically {typical['min_speed']:.0f} km/h, on the best pass {best['min_speed']:.0f} km/h")
            d_min = 0.0                                   # already said
        if d_min >= SPEED_KMH:
            actions.append(f"carry about {d_min:.0f} km/h more minimum speed")
            evidence.append(f"Minimum speed typically {typical['min_speed']:.0f} km/h, on the best pass {best['min_speed']:.0f} km/h")
        if np.isfinite(d_thr) and d_thr >= THROTTLE_M:
            actions.append(f"get back on throttle about {d_thr:.0f} m earlier")
            evidence.append(f"Throttle typically back on at {typical['throttle_on_m']:.0f} m, on the best pass at "
                            f"{best['throttle_on_m']:.0f} m")
        if d_exit >= SPEED_KMH and not any("throttle" in a for a in actions):
            evidence.append(f"Exit speed typically {typical['exit_speed']:.0f} km/h, on the best pass {best['exit_speed']:.0f} km/h")
        scatter = float(part["brake_m"].std(ddof=0)) if part["brake_m"].notna().sum() >= 3 else np.nan
        if np.isfinite(scatter) and scatter >= BRAKE_SCATTER_M:
            actions.append(f"fix a braking reference (the braking point moves by ±{scatter:.0f} m from lap to lap)")
        if not actions:
            actions.append("repeat the best pass (no single input explains the difference, so compare the two "
                           "laps in the lap comparison)")
        c = corners[corners["corner"] == row.corner].iloc[0]
        items.append(_item(
            "driver", f"{row.corner}: {actions[0][0].upper() + actions[0][1:].split(' (')[0]}",
            f"{row.corner} ({'left' if c['direction'] == 'L' else 'right'}, {c['kind']}, {c['start_m']:.0f}–{c['end_m']:.0f} m)",
            evidence, _fmt_list(actions).capitalize() + ".",
            "medium" if row.laps >= 5 else "low", gain_s=row.available_s, priority=row.available_s))
        if len(items) >= MAX_DRIVER_ITEMS:
            break

    # How the car is being driven where it repeatedly steps out or pushes
    if events is not None and len(events):
        n_laps = max(metrics["lap"].nunique(), 1)
        for corner, ev in events[events["severity"] != "mild"].groupby("corner"):
            laps_hit = ev["lap"].nunique()
            if laps_hit / n_laps < REPEAT_SHARE or laps_hit < 2:
                continue
            kind = ev["kind"].mode().iloc[0]
            cause = ev["cause"].mode().iloc[0]
            part = m[m["corner"] == corner]
            hit, clean = part[part["lap"].isin(ev["lap"])], part[~part["lap"].isin(ev["lap"])]
            cost = float(hit["zone_time_s"].mean() - clean["zone_time_s"].mean()) if len(hit) and len(clean) else np.nan
            evidence = [f"{len(ev)} {kind} incidents rated moderate or severe, on {laps_hit} of {n_laps} laps; mostly at "
                        f"{ev['phase'].mode().iloc[0]}"]
            if ev["counter_steer"].any():
                evidence.append(f"Opposite lock up to {ev['opposite_lock_deg'].max():.0f} deg")
            slip = ev["rear_slip_kmh"].dropna()
            if len(slip) and slip.median() >= handling.WHEELSPIN_KMH:
                evidence.append(f"Rear wheels typically {slip.median():.0f} km/h faster than the fronts during them")
            if np.isfinite(cost):
                evidence.append(f"Laps with an incident were {cost:+.2f} s through the corner against laps without")
            if kind == "oversteer" and cause.startswith("power"):
                action = ("Feed the throttle in more progressively on the way out: reach full throttle later, once the "
                          "steering is unwinding.")
            elif kind == "oversteer" and "braking" in cause:
                action = "Release the brake more gradually into the corner, so the rear stays loaded as the car turns."
            elif kind == "oversteer":
                action = "Avoid a sudden lift mid-corner; keep a little throttle on to hold the rear."
            elif "braking" in cause:
                action = "Finish more of the braking in a straight line and turn in with less brake."
            elif cause.startswith("power"):
                action = "Wait for the car to rotate before applying throttle; early throttle is pushing the front wide."
            else:
                action = "Lower the entry speed slightly so the front bites, then carry speed from the apex."
            if np.isfinite(cost) and cost < 0.02:
                action += " It is not costing lap time here, so the gain is in tyre life and margin, not pace."
            items.append(_item("driver", f"{corner}: {cause.split(',')[0]} on {laps_hit} of {n_laps} laps",
                               corner, evidence, action, "medium",
                               gain_s=cost if np.isfinite(cost) and cost > 0 else None,
                               priority=max(cost, 0.0) if np.isfinite(cost) else 0.0))

    short = session.laps[session.laps["status"] == "short"]
    if len(short):
        items.append(_item("driver", "Track limits", "laps " + _fmt_list(str(l) for l in short["lap"]),
                           [f"Lap {l.lap} covered {l.dist_m:.0f} m against a lap length of {session.lap_length:.0f} m"
                            for l in short.itertuples()],
                           "These laps cut part of the track and are left out of the analysis.", "high"))
    if total_available > 0 and len(summary):
        best_lap_time = session.laps.loc[session.laps["status"] == "flying", "lap_time_s"].min()
        ideal = summary.attrs.get("ideal_lap_s", np.nan)
        if np.isfinite(ideal) and np.isfinite(best_lap_time) and best_lap_time - ideal >= 0.05:
            items.insert(0, _item(
                "driver", f"Put the best corners together: {best_lap_time - ideal:.2f} s",
                "whole lap",
                [f"Best lap {best_lap_time:.3f} s; best pass through every corner added up {ideal:.3f} s",
                 f"A typical lap leaves {total_available:.2f} s against those bests"],
                "The pace is already in the driving: the gain is in repeating the best pass at each corner on one "
                "lap. Start with the corners listed below.", "medium",
                gain_s=best_lap_time - ideal, priority=1e6))
    return sorted(items, key=lambda i: -i["priority"])


# ── setup ─────────────────────────────────────────────────────────────────────

def exit_slip(session, corners, names: list) -> dict:
    """Median |body slip| at the apex and in the exit of each named corner over
    the flying laps: {corner: (apex_deg, exit_deg)}. Empty without body slip."""
    frame = session.frame()
    if "body_slip" not in frame or not frame["body_slip"].notna().any() or corners is None:
        return {}
    flying = session.laps.loc[session.laps["status"] == "flying", "lap"]
    on = frame["lap"].isin(flying).to_numpy()
    d, slip = frame["lap_distance"].to_numpy(), np.abs(frame["body_slip"].to_numpy())
    out = {}
    for c in corners[corners["corner"].isin(names)].itertuples():
        apex = getattr(c, "apex_m", (c.start_m + c.end_m) / 2)
        span = max(c.end_m - c.start_m, 40.0) * 0.15
        a = on & (np.abs(d - apex) <= span)
        e = on & (d > apex + span) & (d <= c.end_m)
        out[c.corner] = (float(np.nanmedian(slip[a])) if a.any() else np.nan,
                         float(np.nanmedian(slip[e])) if e.any() else np.nan)
    return out


def _setup_items(session, corners, handling_result) -> list:
    items = []
    frame = session.frame()
    events = handling_result["events"] if handling_result else None
    tendency = handling_result["tendency"] if handling_result else None
    kind_of = corners.set_index("corner")["kind"]

    if events is not None and len(events):
        strong = events[events["severity"] != "mild"]
        n_laps = max(events["lap"].nunique(), 1)

        def where_text(ev):
            counts = ev["corner"].value_counts()
            kinds = sorted({kind_of[c] for c in counts.index})
            return f"{_fmt_list(counts.index[:5])} ({_fmt_list(kinds)} corners)", kinds

        # Power oversteer on exit
        power = strong[strong["cause"].str.startswith("power oversteer")]
        if len(power) >= PATTERN_MIN_EVENTS and power["corner"].nunique() >= 2:
            where, kinds = where_text(power)
            evidence = [f"{len(power)} power-oversteer incidents rated moderate or severe, in {power['corner'].nunique()} corners, "
                        f"on {power['lap'].nunique()} of {n_laps} laps"]
            slip = power["rear_slip_kmh"].dropna()
            if len(slip):
                evidence.append(f"Rear wheels typically {slip.median():.0f} km/h faster than the fronts during them")
            habit = tendency[(tendency["read"] == "oversteer")]["corner"].tolist() if tendency is not None else []
            if habit:
                evidence.append(f"Exit oversteer is the norm, not the exception, in {_fmt_list(habit)}")
            actions = []
            if "tc_active" in frame:
                ch = session.channels
                mask = np.zeros(len(ch), bool)
                for e in power.itertuples():
                    mask |= ((ch["lap"] == e.lap) & ch["lap_distance"].between(e.start_m, e.end_m)).to_numpy()
                during = _channel_share(session, "tc_active", mask)
                overall = _channel_share(session, "tc_active", (ch["throttle"] >= 50).to_numpy()) if "throttle" in ch else None
                if during is not None:
                    evidence.append(f"Traction control was intervening for {during:.0%} of the time in those incidents"
                                    + (f" ({overall:.0%} of all running above half throttle)" if overall is not None else ""))
                    actions.append("raise the traction control setting one step" if during < 0.5 else
                                   "traction control is already working hard, so look for mechanical grip rather than more TC")
            actions += ["soften the rear anti-roll bar", "soften the rear springs or run a little less rear rebound",
                        "lower the rear tyre pressures slightly if they are above target when hot",
                        "if the differential is adjustable, reduce locking on power"]
            if "fast" in kinds:
                actions.append("add rear wing for the fast corners")
            items.append(_item("setup", "Rear traction on corner exit", where, evidence,
                               "Directions to test, least invasive first: " + "; ".join(actions) + ".",
                               "medium" if len(power) >= 2 * PATTERN_MIN_EVENTS else "low", priority=len(power)))

        # Oversteer on entry / under braking
        entry_os = strong[(strong["kind"] == "oversteer") & strong["cause"].str.contains("braking|lift-off")]
        if len(entry_os) >= PATTERN_MIN_EVENTS:
            where, _ = where_text(entry_os)
            items.append(_item(
                "setup", "Rear stability on corner entry", where,
                [f"{len(entry_os)} oversteer incidents under braking or on a lift, on {entry_os['lap'].nunique()} of {n_laps} laps"],
                "Directions to test: move the brake balance forward; reduce engine braking if adjustable; soften the "
                "front anti-roll bar or stiffen the rear slightly less; add rear wing if it happens in fast corners.",
                "medium" if len(entry_os) >= 2 * PATTERN_MIN_EVENTS else "low", priority=len(entry_os)))

        # Understeer
        us = strong[strong["kind"] == "understeer"]
        if len(us) >= PATTERN_MIN_EVENTS and us["corner"].nunique() >= 2:
            where, kinds = where_text(us)
            phase = us["phase"].mode().iloc[0]
            actions = ["soften the front anti-roll bar", "add front camber if tyre temperatures allow",
                       "lower the front tyre pressures slightly if they are above target when hot"]
            if phase == "entry":
                actions.insert(0, "move the brake balance rearward a little")
            if "fast" in kinds:
                actions.append("reduce rear wing or add front aero for the fast corners")
            items.append(_item("setup", f"Front grip at corner {phase}", where,
                               [f"{len(us)} understeer incidents, in {us['corner'].nunique()} corners, on "
                                f"{us['lap'].nunique()} of {n_laps} laps; mostly at {phase}"],
                               "Directions to test: " + "; ".join(actions) + ".",
                               "medium" if len(us) >= 2 * PATTERN_MIN_EVENTS else "low", priority=len(us)))

    # Exit oversteer that is the norm rather than an incident. The steering residual is measured
    # against a model fitted to this same session, so a residual that is the same every lap may be
    # how the model fits the car rather than the car's balance. It becomes a setup hypothesis only
    # when the car is measured travelling sideways there: body slip at those exits.
    habit = tendency[tendency["read"] == "oversteer"] if tendency is not None else []
    slip = exit_slip(session, corners, list(habit["corner"])) if len(habit) else {}
    confirmed = [r for r in habit.itertuples() if slip.get(r.corner, (np.nan, np.nan))[1] >= HABIT_SLIP_DEG] if len(habit) else []
    if len(confirmed) >= 2 and not any(i["title"] == "Rear traction on corner exit" for i in items):
        items.append(_item(
            "setup", "Oversteer is the car's norm in several corners", _fmt_list([r.corner for r in confirmed]),
            ["Typical steering against the car's own norm at exit: "
             + ", ".join(f"{r.corner} {r.exit_deg:+.0f} deg" for r in confirmed if np.isfinite(r.exit_deg)),
             "Body slip at exit, median over flying laps: "
             + ", ".join(f"{r.corner} {slip[r.corner][1]:.1f} deg (apex {slip[r.corner][0]:.1f})" for r in confirmed)],
            "Directions to test: soften the rear anti-roll bar; soften the rear springs; lower the rear tyre "
            "pressures slightly if they are above target when hot; raise the traction control setting one step.",
            "low", priority=len(habit)))


    # Steady-state balance and how it moves through the session
    usable_lat = not ("acc_lat" in session.coarse and "acc_lat" not in session.rebuilt)
    sc = balance.steering_characteristic(session) if usable_lat else None
    if sc:
        per_stint = []
        for k in sorted(session.laps["stint"].unique()):
            fly = session.laps[(session.laps["stint"] == k) & (session.laps["status"] == "flying")]["lap"]
            part = balance.steering_characteristic(session, laps=list(fly), ratio=sc["ratio"]) if len(fly) >= 3 else None
            if part:
                per_stint.append((int(k), part["understeer_deg_per_g"]))
        if len(per_stint) >= 2:
            first, last = per_stint[0][1], per_stint[-1][1]
            steps = np.diff([v for _, v in per_stint])
            one_way = bool((steps > 0).all() or (steps < 0).all())      # a trend, not scatter between stints
            if one_way and abs(first) > 0.05 and abs(last - first) / abs(first) >= 0.15:
                more = last > first
                items.append(_item(
                    "setup", f"Balance moves toward {'understeer' if more else 'oversteer'} over the session", "whole lap",
                    ["Understeer gradient by stint: " + ", ".join(f"stint {k} {v:+.2f} deg/g" for k, v in per_stint)],
                    ("The front tyres are giving up first. " if more else "The rear tyres are giving up first. ")
                    + "If this matches the driver's feel late in a run, test "
                    + ("a softer front anti-roll bar or slightly lower front pressures" if more else
                       "a softer rear anti-roll bar or slightly lower rear pressures")
                    + ". Fuel load and a different tyre set can also move this, so compare like with like.",
                    "low", priority=2))

    # Tyres
    ch = session.channels
    if all(f"tyre_temp_{w}_inner" in ch and f"tyre_temp_{w}_outer" in ch for w in WHEELS):
        fly = ch[ch["lap"].isin(session.laps.loc[session.laps["status"] == "flying", "lap"])]
        for w in WHEELS:
            spread = float((fly[f"tyre_temp_{w}_inner"] - fly[f"tyre_temp_{w}_outer"]).mean())
            if spread > CAMBER_SPREAD_C[1] or spread < CAMBER_SPREAD_C[0]:
                too_much = spread > CAMBER_SPREAD_C[1]
                items.append(_item(
                    "setup", f"{w.upper()} camber", f"{w.upper()} tyre",
                    [f"Inner edge runs {spread:+.0f} °C against the outer edge on flying laps"],
                    f"{'Reduce' if too_much else 'Add'} negative camber on the {w.upper()}: the usual target is the inner "
                    f"edge {CAMBER_SPREAD_C[0]:.0f}–{CAMBER_SPREAD_C[1]:.0f} °C hotter than the outer.", "medium", priority=3))
    try:
        ts = tyres.lap_summary(session)
        ts = ts[ts["status"] == "flying"]
        temps = {w: float(ts[f"temp_{w}_mean"].mean()) for w in WHEELS if f"temp_{w}_mean" in ts}
    except Exception:
        temps = {}
    if len(temps) == 4:
        front, rear = (temps["fl"] + temps["fr"]) / 2, (temps["rl"] + temps["rr"]) / 2
        if abs(rear - front) >= TEMP_SPLIT_C:
            hot = "rear" if rear > front else "front"
            items.append(_item(
                "setup", f"{hot.capitalize()} tyres running hotter", "all four tyres",
                [f"Mean on flying laps: front {front:.0f} °C, rear {rear:.0f} °C"],
                f"The {hot} axle is doing more work. Check its pressures against target and consider shifting balance "
                f"toward the {'front' if hot == 'rear' else 'rear'} (roll stiffness or aero).", "low", priority=1))

    # Brakes
    discs = {w: f"brake_temp_{w}" for w in WHEELS if f"brake_temp_{w}" in ch}
    if len(discs) == 4:
        peak = {w: float(ch[c].max()) for w, c in discs.items()}
        front_peak, rear_peak = max(peak["fl"], peak["fr"]), max(peak["rl"], peak["rr"])
        if max(front_peak, rear_peak) >= BRAKE_TEMP_WATCH_C:
            axle = "front" if front_peak >= rear_peak else "rear"
            items.append(_item(
                "setup", f"{axle.capitalize()} brake temperatures", f"{axle} discs",
                [f"Peak disc temperature: front {front_peak:.0f} °C, rear {rear_peak:.0f} °C"],
                f"Check the {axle} peak against the working range of the disc and pad fitted. If it is above it, open "
                f"the {axle} brake ducts; if the rears are well inside their range, a little more rear brake balance "
                "also unloads the fronts.", "low", priority=1))
    if "abs_active" in ch and "brake" in ch:
        braking = (ch["brake"] > corner_engine.BRAKE_ON_FRACTION * float(ch["brake"].max())).to_numpy()
        share = _channel_share(session, "abs_active", braking)
        if share is not None and share >= 0.30:
            items.append(_item(
                "setup", "ABS working through much of the braking", "braking zones",
                [f"ABS intervening for {share:.0%} of the time on the brakes"],
                "The tyres are being asked for more than they can give under braking. Test a little less peak brake "
                "pressure from the driver, a lower ABS setting only if stopping distances allow, and the brake balance "
                "in whichever direction stops the locking axle.", "low", priority=1))
    return sorted(items, key=lambda i: -i["priority"])


def suggest(session, corner_result: dict | None = None, handling_result: dict | None = None) -> dict:
    """Driver and setup suggestions for a session.

    Returns {"driver": [...], "setup": [...]} — each a list of items, most
    worthwhile first. An item has: title, where, evidence (list of sentences
    with the numbers), action, confidence ("high" / "medium" / "low") and
    gain_s (seconds per lap, where the data supports an estimate)."""
    if corner_result is None:
        corner_result = corner_engine.analyse(session)
    if handling_result is None:
        try:
            handling_result = handling.analyse(session, corner_result["corners"])
        except ValueError:
            handling_result = None
    events = handling_result["events"] if handling_result else None
    driver = _driver_items(session, corner_result["corners"], corner_result["metrics"],
                           corner_result["summary"], events)
    setup = _setup_items(session, corner_result["corners"], handling_result)
    for item in driver + setup:
        item.pop("priority")
    return {"driver": driver, "setup": setup}
