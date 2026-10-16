"""
Automated performance engineering: the report every session produces.

Nine sections, always in this order, always present:

  lap potential · driver performance · braking · cornering · traction ·
  tyres · vehicle balance · incidents · setup hypotheses

Each section holds findings. A finding is one conclusion and the chain that
leads to it:

  evidence        the numbers from this session, as sentences
  calculation     how those numbers were worked out, so they can be checked
  confidence      high / medium / low, with what it rests on
  recommendation  what to do about it

The same chain is also exposed as evidence → interpretation → hypothesis →
suggested investigation (`interpretation`, `hypothesis`, `investigation`). A
hypothesis is stated only in the setup section, and a setup change is only
suggested when the confidence is better than low: on thin evidence the
recommendation says what to look for first instead.

A section the data cannot support says so, and says which channels would make
it possible — it is never left out and never filled with guesses.

Confidence is a rule, not a judgement. It is the lowest of:
  laps          8 or more flying laps → high, 5 to 7 → medium, fewer → low
  size          the effect against its own lap-to-lap scatter: 2× → high, 1× → medium
  data          a channel recorded too coarsely caps it; a rebuilt channel caps it at medium
  knowledge     anything resting on a generic figure (a tyre window, a rule of
                thumb about setup) is capped, and the cap is named

Everything here is computed from what the corner, handling, tyre and balance
modules already measure; this module adds no new signal processing, it puts
their results in engineering order and makes each one answer for itself.

  from veltrix_engine.analysis import engineering
  report = engineering.analyse(session)
  print(engineering.to_text(report))
"""

from __future__ import annotations

import json
import re

import numpy as np
import pandas as pd

from .. import circuits
from .. import events as event_rules
from .. import corners as corner_engine
from .. import laps as lap_engine
from ..derived import best_lat
from ..schema import CORNERS as WHEELS
from ..schema import tyre_temp_channel
from . import advice, balance, handling, plausibility, tyres

SECTIONS = (("lap_potential", "Lap potential"), ("driver_performance", "Driver performance"),
            ("braking", "Braking"), ("cornering", "Cornering"), ("traction", "Traction"),
            ("tyres", "Tyres"), ("vehicle_balance", "Vehicle balance"), ("incidents", "Incidents"),
            ("setup_hypotheses", "Setup hypotheses"))
LEVELS = ("low", "medium", "high")

LAPS_HIGH, LAPS_MEDIUM = 8, 5         # flying laps for high / medium confidence
RATIO_HIGH, RATIO_MEDIUM = 2.0, 1.0   # effect ÷ lap-to-lap scatter for high / medium confidence
MIN_GAIN_S = 0.05                     # time on the table below this is not worth a finding
BRAKE_M, SPEED_KMH, THROTTLE_M = advice.BRAKE_M, advice.SPEED_KMH, advice.THROTTLE_M
BRAKE_SCATTER_M = advice.BRAKE_SCATTER_M
COAST_THROTTLE = 5.0                  # % throttle below which, off the brakes, the car is coasting
COAST_MIN_KMH = 40.0
COAST_FLAG_S = 1.0                    # coasting per lap worth raising
OVERLAP_FLAG_S = 0.5                  # throttle and brake together per lap worth raising
DECEL_SHARE = 0.92                    # typical peak deceleration below this share of the zone's best is soft braking
DECEL_GAP_G = 0.10
HEAVY_BRAKE_KMH = 40.0                # a braking zone that sheds at least this much speed
LAT_SHARE = 0.92                      # typical peak lateral g below this share of the class's level
CONSISTENT_S, LOOSE_S = 0.25, 0.60    # flying-lap standard deviation: below → consistent, above → loose
WHEELSPIN_KMH = handling.WHEELSPIN_KMH
WHEELSPIN_SHARE = 0.10                # share of hard-throttle time with the rears spinning worth raising
DEG_R2 = 0.30                         # a degradation fit below this explains too little to act on
PRESSURE_SPLIT_BAR = 0.07             # left-right hot pressure difference worth raising
PHASE_DEG = 3.0                       # habitual steering excess (deg) that counts as a balance trait


# ── building blocks ───────────────────────────────────────────────────────────

def fmt_time(seconds) -> str:
    if seconds is None or not np.isfinite(seconds):
        return "—"
    m, s = divmod(float(seconds), 60.0)
    return f"{int(m)}:{s:06.3f}"


def _join(names) -> str:
    names = [str(n) for n in names]
    if not names:
        return ""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _finding(conclusion, evidence, calculation, confidence, recommendation, where="", gain_s=None,
             at_stake_s=None) -> dict:
    """gain_s: seconds a lap this finding alone is worth. at_stake_s: the time
    available in the zones it concerns — shared with every other finding about
    those zones, so the two must not be added up across findings."""
    num = lambda x: None if x is None or not np.isfinite(x) else round(float(x), 3)
    return {"conclusion": conclusion, "where": where, "evidence": list(evidence), "calculation": calculation,
            "confidence": confidence, "recommendation": recommendation, "gain_s": num(gain_s),
            "at_stake_s": num(at_stake_s)}


class _Ctx:
    """Everything the sections share, worked out once."""

    def __init__(self, session, corner_result, handling_result):
        s = self.session = session
        self.ch, self.frame = s.channels, s.frame()
        self.incident_laps: dict = {}             # {corner: laps with an incident there}, filled once corners are known
        self.flying = list(s.laps.loc[s.laps["status"] == "flying", "lap"])
        self.n = len(self.flying)
        self.fly_rows = self.ch["lap"].isin(self.flying).to_numpy()
        self.dt = 1.0 / s.rate_hz
        self.lat_id = best_lat(self.frame)
        self.usable_lat = self.lat_id is not None and not ("acc_lat" in s.coarse and "acc_lat" not in s.rebuilt)

        self.corner_reason = self.handling_reason = None
        self.corners = self.metrics = self.summary = self.handling = None
        if corner_result is None:
            try:
                corner_result = corner_engine.analyse(s)
            except (ValueError, KeyError) as e:
                self.corner_reason = str(e)
        if corner_result is None and self.corner_reason is None:
            self.corner_reason = "no corners could be found"
        if not (s.lap_length and np.isfinite(s.lap_length)) or len(s.laps) < 2:
            corner_result, self.corner_reason = None, "the file has no lap information, so laps cannot be compared"
        elif self.n < 2:
            corner_result = corner_result if self.n else None
            if not self.n:
                self.corner_reason = "the session has no flying lap (a complete lap at racing speed)"
        if corner_result is not None:
            self.corners, self.summary = corner_result["corners"], corner_result["summary"]
            m = corner_result["metrics"]
            self.all_metrics = m
            self.metrics = m[m["lap"].isin(self.flying)] if self.flying else m
            self.ideal = float(self.summary.attrs.get("ideal_lap_s", np.nan)) if len(self.summary) else np.nan
            if handling_result is None:
                try:
                    handling_result = handling.analyse(s, self.corners)
                except (ValueError, KeyError) as e:
                    self.handling_reason = str(e)
            self.handling = handling_result
        else:
            self.handling_reason = self.corner_reason
        self.peaks = pd.DataFrame()
        if self.corners is not None:
            try:
                self.peaks = plausibility.check_peaks(s, self.corners, self.flying or None)
            except (ValueError, KeyError):
                self.peaks = pd.DataFrame()
            if len(self.peaks):
                # a held peak no other measurement supports, or one beyond the car, is not used as a peak
                bad = self.peaks[self.peaks["verdict"].isin(("unsupported", "beyond"))]
                column = {"lateral": "peak_lat_g", "braking": "peak_brake_g"}
                for r in bad.itertuples():
                    rows = (self.metrics["lap"] == r.lap) & (self.metrics["corner"] == r.corner)
                    self.metrics.loc[rows, column[r.kind]] = np.nan
            for e in self.sim_incidents():
                if e["points"] > 0 or (e["code"] & 0xFF) in SIM_INCIDENT:
                    self.incident_laps.setdefault(e["corner"], set()).add(e["lap"])
        self.best_lap = lap_engine.best_lap(s) if self.n else None
        self.best_time = float(s.laps.loc[s.laps["lap"] == self.best_lap, "lap_time_s"].iloc[0]) if self.n else np.nan

    def conf(self, n="laps", effect=None, scatter=None, uses=(), caps=()) -> dict:
        """Confidence by rule: the lowest of sample size, effect against
        scatter, data quality and any named cap."""
        s, level, basis = self.session, 2, []
        if n == "laps":
            n = self.n
        if n is not None:
            got = 2 if n >= LAPS_HIGH else 1 if n >= LAPS_MEDIUM else 0
            level = min(level, got)
            basis.append(f"{n} flying lap{'s' if n != 1 else ''}"
                         + ("" if got == 2 else f" ({LAPS_HIGH} or more for high confidence)"))
        if effect is not None and scatter is not None and np.isfinite(effect) and np.isfinite(scatter):
            if scatter > 0:
                ratio = abs(effect) / scatter
                got = 2 if ratio >= RATIO_HIGH else 1 if ratio >= RATIO_MEDIUM else 0
                level = min(level, got)
                basis.append("the effect is " + (f"{ratio:.1f}" if ratio < 10 else "more than 10")
                             + " times its lap-to-lap scatter")
            else:
                basis.append("no lap-to-lap scatter in this measure")
        for cid in uses:
            if cid == "acc_lat" and "acc_lat" in s.rebuilt:
                level = min(level, 1)
                basis.append("lateral g is rebuilt from damper roll, not measured directly")
            elif cid in s.coarse and cid not in s.rebuilt and cid != "acc_long":
                level = 0
                basis.append(f"{cid} is recorded in coarse steps ({s.coarse[cid]:g} {s.units.get(cid, '')})".rstrip())
        for cap, reason in caps:
            level = min(level, LEVELS.index(cap))
            basis.append(reason)
        return {"level": LEVELS[level], "basis": "; ".join(basis) or "direct reading of the logged data"}

    def per_corner(self, column):
        """Flying-lap values of one corner metric: {corner: Series}."""
        return {c: part[column].dropna() for c, part in self.metrics.groupby("corner", sort=False)}

    def best_and_typical(self, corner):
        """A corner's fastest pass (by zone time) and its typical (median) pass.
        A pass with a recorded incident (off track, say) is never the fastest
        pass: a corner cut is not a reference to copy."""
        part = self.metrics[self.metrics["corner"] == corner]
        clean = part[~part["lap"].isin(self.incident_laps.get(corner, ()))]
        pick = clean if len(clean) else part
        return pick.loc[pick["zone_time_s"].idxmin()], part.median(numeric_only=True), part

    def sim_incidents(self) -> list:
        """Incidents the simulator recorded: [{lap, corner, m, t, points, kind, code}], from
        a rise in the incident count, or a reported incident with no points."""
        ch = self.ch
        if "incident_count" not in ch and "incident_flags" not in ch:
            return []
        t, lap = ch["time"].to_numpy(), ch["lap"].to_numpy()
        dist = ch["lap_distance"].to_numpy() if "lap_distance" in ch else np.full(len(t), np.nan)
        count = ch["incident_count"].to_numpy(float) if "incident_count" in ch else None
        flags = ch["incident_flags"].to_numpy(float) if "incident_flags" in ch else None
        out, seen = [], set()

        def at(i, points):
            code = int(flags[i]) if flags is not None and np.isfinite(flags[i]) else 0
            if flags is not None and not code:            # the flag can trail the count by a few samples
                window = flags[max(i - 30, 0):i + 31]
                hits = window[np.isfinite(window) & (window != 0)]
                code = int(hits[0]) if len(hits) else 0
            corner = (self.corners["corner"].iloc[int(self.zone_of(np.array([dist[i]]))[0])]
                      if self.corners is not None and np.isfinite(dist[i]) else "")
            return {"lap": int(lap[i]), "corner": corner, "m": float(dist[i]), "t": float(t[i]), "points": points,
                    "kind": SIM_INCIDENT.get(code & 0xFF, f"reported as code {code & 0xFF}" if code else "incident"),
                    "code": code}

        if count is not None:
            rise = np.flatnonzero(np.diff(count) > 0) + 1
            for i in rise:
                out.append(at(i, float(count[i] - count[i - 1])))
                seen.update(range(max(i - 60, 0), i + 61))
        if flags is not None:
            new = np.flatnonzero((flags[1:] != flags[:-1]) & (flags[1:] != 0) & np.isfinite(flags[1:])) + 1
            for i in new:
                if i not in seen:
                    out.append(at(i, 0.0))
        return sorted(out, key=lambda e: e["t"])

    def zone_of(self, lap_distance: np.ndarray) -> np.ndarray:
        ends = self.corners["zone_end_m"].to_numpy()
        return np.clip(np.searchsorted(ends, lap_distance, side="right"), 0, len(ends) - 1)

    def brake_on(self) -> np.ndarray | None:
        ch = self.ch
        if "brake" in ch and float(np.nanmax(ch["brake"])) > 0:
            return (ch["brake"] > corner_engine.BRAKE_ON_FRACTION * float(np.nanmax(ch["brake"]))).to_numpy()
        if "acc_long_calc" in self.frame:
            return (self.frame["acc_long_calc"] < corner_engine.BRAKE_ON_G).to_numpy()
        return None


ZONES_CALC = ("The lap is split into one zone per corner, with the boundary between two corners in the middle of "
              "the straight that joins them, so zone times add up to the lap time. ")
BEST_VS_TYPICAL = ("For each corner the fastest pass (lowest zone time on a flying lap) is set against the median of "
                   "all flying laps. ")


# ── 1. lap potential ──────────────────────────────────────────────────────────

def _lap_potential(c: _Ctx) -> list:
    s, out = c.session, []
    times = s.laps.loc[s.laps["status"] == "flying", "lap_time_s"].to_numpy(float)
    summary = c.summary.sort_values("available_s", ascending=False)
    zones = len(summary)
    gap = c.best_time - c.ideal
    wide = c.metrics.pivot_table(index="lap", columns="corner", values="zone_time_s")
    second = float(np.sort(wide.to_numpy(), axis=0)[1].sum()) if c.n >= 3 and not wide.isna().any().any() else np.nan
    evidence = [f"Best lap {fmt_time(c.best_time)} (lap {c.best_lap}), from {c.n} flying laps",
                f"Fastest pass through each of the {zones} zones added together: {fmt_time(c.ideal)} "
                f"({gap:.3f} s under the best lap), taken from laps {_join(sorted(set(summary['best_lap'])))}"]
    if np.isfinite(second):
        evidence.append(f"Second-fastest pass through each zone added together: {fmt_time(second)} "
                        f"({c.best_time - second:+.3f} s against the best lap)")
    theoretical = np.nan
    if c.n >= 2:
        sectors = lap_engine.sector_times(s)
        theoretical = float(lap_engine.theoretical_best(sectors))
        evidence.append(f"Best of three equal-length sectors added together: {fmt_time(theoretical)}")
    calc = (ZONES_CALC + "Ideal lap = Σ over zones of the minimum zone time on flying laps. Repeatable lap = Σ of the "
            "second-lowest zone time, which leaves out each zone's single luckiest pass. Sector figure = Σ of the "
            "minimum time in each of three equal-length sectors.")
    caps = [("medium", "a sum of best passes is optimistic: every minimum includes its share of favourable scatter")]
    top = summary.head(3)
    where_top = ", ".join(f"{r.corner} {r.available_s:.2f} s" for r in top.itertuples())
    if gap < MIN_GAIN_S:
        out.append(_finding(
            f"The best lap is the ideal lap to within {max(gap, 0):.2f} s", evidence, calc,
            c.conf(uses=("speed",), caps=caps),
            "The driver put their best corners together on one lap. More pace now has to come from doing something "
            "differently — technique or the car — not from repeating what has already been done.",
            "whole lap", 0.0))
    else:
        realistic = second if np.isfinite(second) and second < c.best_time else np.nan
        target = (f"Aim for {fmt_time(realistic)} next: it needs nothing the driver has not already done twice. "
                  if np.isfinite(realistic) else "")
        out.append(_finding(
            f"{gap:.2f} s is available from driving this driver's own best corners on one lap", evidence, calc,
            c.conf(uses=("speed",), caps=caps),
            target + f"{fmt_time(c.ideal)} is the ceiling with this car as it is. Start where the typical lap gives "
            f"away the most: {where_top}.", "whole lap", gap))

    total = float(summary["available_s"].sum())
    if float(summary["available_s"].max()) >= MIN_GAIN_S:
        by_kind = summary.groupby("kind")["available_s"].sum()
        share = float(top["available_s"].sum() / total)
        out.append(_finding(
            f"{share:.0%} of the time a typical lap gives away is in {_join(top['corner'])}",
            [f"A typical lap is {total:.2f} s slower than the best pass through every zone"]
            + [f"{r.corner} ({r.kind}): typical {r.mean_s:.2f} s, best {r.best_s:.2f} s on lap {r.best_lap}, "
               f"{r.available_s:.2f} s available, spread ±{r.spread_s:.2f} s" for r in top.itertuples()]
            + ["By corner type: " + ", ".join(f"{k} {v:.2f} s" for k, v in by_kind.items())],
            ZONES_CALC + "Available time in a zone = mean zone time over flying laps − fastest zone time. Corner type "
            f"by minimum speed: slow below {corner_engine.SLOW_KMH:.0f} km/h, fast above {corner_engine.FAST_KMH:.0f} km/h.",
            c.conf(uses=("speed",), effect=float(top["available_s"].mean()), scatter=float(top["spread_s"].mean())),
            f"Work on {_join(top['corner'])} first, in that order. Braking, cornering and traction below say what "
            "differed between the best pass and the typical one in each.",
            _join(top["corner"]), at_stake_s=float(top["available_s"].sum())))

    if c.n >= 3:
        sd, median = float(np.std(times)), float(np.median(times))
        label = "consistent" if sd < CONSISTENT_S else "loose" if sd > LOOSE_S else "moderately consistent"
        out.append(_finding(
            f"Lap times are {label}: ±{sd:.2f} s over {c.n} flying laps",
            [f"Flying laps: {', '.join(fmt_time(t) for t in times)}",
             f"Median {fmt_time(median)}, {median - c.best_time:.2f} s off the best"],
            "Standard deviation and median of the flying-lap times. Out-laps, in-laps, pit laps, laps marked slow and "
            f"laps that cut the track are left out. Below {CONSISTENT_S:g} s is called consistent, above {LOOSE_S:g} s loose.",
            c.conf(),
            ("The lap time is repeatable, so differences between laps of this size mean something when a change is "
             "tested." if sd < CONSISTENT_S else
             "Treat a setup or technique change as real only if it moves the lap time by more than about "
             f"{2 * sd:.1f} s, or judge it on the corner it targets instead of the whole lap."),
            "whole lap"))
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


SIM_INCIDENT = event_rules.SIM_INCIDENT       # iRacing's incident kinds, as the event engine names them


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
            n_us, n_os = int((ev["kind"] == "understeer").sum()), int((ev["kind"] == "oversteer").sum())
            n_conf = sum(backed(e) is not None for e in ev.itertuples())
            strong = ev[ev["severity"] != "mild"].sort_values("cost_s", ascending=False)
            cost = ev.groupby(["lap", "corner"])["cost_s"].first()
            total = float(cost[cost > 0].sum())
            evidence = [f"{n_us} understeer and {n_os} oversteer candidates on {ev['lap'].nunique()} laps; "
                        f"{int((ev['severity'] == 'severe').sum())} severe, {int((ev['severity'] == 'moderate').sum())} moderate"
                        + (f"; {n_conf} confirmed by a simulator record" if recorded else
                           "; none can be confirmed — the file has no incident record")]
            for e in strong.head(6).itertuples():
                lock = f", {e.opposite_lock_deg:.0f} deg of opposite lock" if e.counter_steer else ""
                paid = f", {e.cost_s:+.2f} s in the zone" if np.isfinite(e.cost_s) else ""
                rec = backed(e)
                status = f" — confirmed: {rec['kind']} recorded by the simulator" if rec else " — candidate"
                evidence.append(f"Lap {e.lap}, {e.corner} {e.phase} at {e.peak_m:.0f} m: {e.severity} {e.cause}{lock}, "
                                f"{e.duration_s:.1f} s at {e.speed:.0f} km/h{paid}{status}")
            busiest = ev["corner"].value_counts()
            out.append(_finding(
                (f"{len(ev)} balance-incident candidates inferred from the steering, most of them in {busiest.index[0]}"
                 + (f" — {n_conf} confirmed by a simulator record" if n_conf else " — none confirmed")), evidence,
                calc + " These are inferred candidates: a recorded incident (the simulator's own) on the same lap and "
                "in the same corner confirms one; otherwise it is for review.",
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


# ── 9. setup hypotheses ───────────────────────────────────────────────────────

_SETUP_CALC = (
    ("Rear traction on corner exit",
     f"Oversteer incidents while on throttle, rated moderate or severe, are counted across the session. At least "
     f"{advice.PATTERN_MIN_EVENTS} of them in two or more corners is treated as the car, not one corner.",
     "fewer power-oversteer incidents and an earlier throttle pick-up in the same corners"),
    ("Rear stability on corner entry",
     f"Oversteer incidents under braking or on a lift, rated moderate or severe; {advice.PATTERN_MIN_EVENTS} or more is a pattern.",
     "fewer entry-oversteer incidents and steadier braking points in the same corners"),
    ("Front grip at corner",
     f"Understeer incidents rated moderate or severe; {advice.PATTERN_MIN_EVENTS} or more in two or more corners is a pattern. "
     "The phase is the one most of them fall in.",
     "fewer understeer incidents and a higher minimum speed in the same corners"),
    ("Oversteer is the car's norm",
     "The car's habitual steering residual at corner exit (see Vehicle balance) reads oversteer in two or more corners.",
     "the exit residual in those corners moving toward zero"),
    ("Balance moves toward",
     "The understeer gradient is fitted per stint with the steering ratio held fixed; a one-way change of 15% or more is raised.",
     "a smaller change in the gradient from the first stint to the last"),
    ("camber", f"Mean inner-edge minus outer-edge temperature on flying laps, against a target of "
               f"{advice.CAMBER_SPREAD_C[0]:.0f}–{advice.CAMBER_SPREAD_C[1]:.0f} °C.",
     "the spread on that tyre moving into the target range"),
    ("tyres running hotter", f"Mean tyre temperature per axle on flying laps; {advice.TEMP_SPLIT_C:.0f} °C or more apart is raised.",
     "the front and rear means closing up"),
    ("brake temperatures", f"Peak of each brake disc temperature channel; {advice.BRAKE_TEMP_WATCH_C:.0f} °C or more is raised.",
     "a lower peak on that axle at the same braking performance"),
    ("ABS working", "Share of samples with the brake on in which the ABS channel is active; 30% or more is raised.",
     "a lower ABS share with the same or shorter stopping distances"),
)


def _setup(c: _Ctx) -> tuple[list, str | None]:
    out = []
    for item in advice._setup_items(c.session, c.corners, c.handling):
        calc, confirm = next(((a, b) for key, a, b in _SETUP_CALC if key.lower() in item["title"].lower()),
                             ("Pattern across corners in this session's measurements.", "the same measure improving"))
        conf = c.conf(uses=("acc_lat",),
                      caps=[(item["confidence"], "how often the pattern appears"),
                            ("medium", "the directions are standard rules of thumb; the engine does not know this car's "
                                       "current setup, adjustment range or regulations")])
        if conf["level"] == "low":
            # no setup-change advice on thin evidence: say what would have to be seen first
            advice_text = (f"The evidence is too thin to suggest a setup change. Investigate first: run at least "
                           f"{LAPS_MEDIUM} flying laps on one setup and see whether the pattern repeats in the same "
                           f"corners; check it against the driver's comments and, if there is one, the video. A change "
                           f"would be worth testing once it does, and would be judged by {confirm}.")
        else:
            advice_text = (item["action"] + f" Change one thing, run the same number of laps, and call it confirmed "
                           f"by {confirm}.")
        finding = _finding(
            f"Hypothesis: {item['title'][0].lower() + item['title'][1:]} is limiting the car", item["evidence"], calc,
            conf, advice_text, item["where"])
        finding["hypothesis"] = f"{item['title']} is what limits the car in {item['where']}."
        out.append(finding)
    if not out:
        evidence = []
        if c.handling is not None:
            ev = c.handling["events"]
            evidence.append(f"{int((ev['severity'] != 'mild').sum()) if len(ev) else 0} balance incidents rated moderate or "
                            f"severe (a pattern needs {advice.PATTERN_MIN_EVENTS} across two corners)")
            reads = c.handling["tendency"]["read"].value_counts()
            evidence.append("Habitual balance by corner: " + ", ".join(f"{n} {k}" for k, n in reads.items() if k != "—"))
        else:
            evidence.append("Balance could not be read: " + (c.handling_reason or "missing channels"))
        have = [n for n, ok in (("tyre temperatures", any(tyre_temp_channel(c.ch.columns, w) for w in WHEELS)),
                                ("brake temperatures", all(f"brake_temp_{w}" in c.ch for w in WHEELS)),
                                ("ABS", "abs_active" in c.ch), ("traction control", "tc_active" in c.ch)) if ok]
        if c.handling is None and not have:
            return [], ("the file has nothing a setup pattern can be read from: balance needs steering and a usable "
                        "lateral acceleration channel, and there are no tyre, brake, ABS or traction-control channels")
        evidence.append("Also checked: " + (_join(have) if have else "nothing else — no tyre, brake, ABS or TC channels in the file"))
        out.append(_finding(
            "No setup change is indicated by " + ("this session" if c.handling is not None else "the channels available"),
            evidence,
            "Every setup pattern the engine knows was tested against the session: repeated power oversteer, entry "
            "oversteer, understeer, a habitual exit slide, balance drift between stints, camber spread, axle "
            "temperature split, brake temperature and ABS use. None reached its threshold.",
            c.conf(caps=[("medium", "absence of a pattern in the channels logged is not proof the setup is optimal")]),
            "Leave the car alone and work on the driver findings above. To look for setup gains, log what is missing "
            "and run longer stints.", "whole car"))
    return out, None


# ── the report ────────────────────────────────────────────────────────────────

def _plain(value):
    return json.loads(json.dumps(value, default=lambda o: o.item() if hasattr(o, "item") else str(o)))


def analyse(session, corner_result: dict | None = None, handling_result: dict | None = None) -> dict:
    """The engineering report for a session, as plain JSON-ready values.

    Returns {"sections": [{"id", "title", "findings": [...], "not_assessed"}],
    "summary": {...}}. All nine sections are always there, in order (`note`
    says what part of a section the file could not support). A
    finding has: conclusion, where, evidence (list), calculation, confidence
    ({"level", "basis"}), recommendation, gain_s (seconds a lap this finding
    alone is worth, or None) and at_stake_s (time available in the zones it
    concerns, shared with other findings about them, or None).
    `not_assessed` is None, or the reason a section has no findings."""
    c = _Ctx(session, corner_result, handling_result)
    need_corners = "laps cannot be compared corner by corner: " + (c.corner_reason or "")
    sections = []
    for sid, title in SECTIONS:
        findings, reason = [], None
        try:
            if sid == "tyres":
                findings, reason = _tyres(c)
            elif sid == "incidents":
                findings = _incidents(c)
            elif c.metrics is None:
                reason = need_corners
            elif sid == "vehicle_balance":
                findings, reason = _balance(c)
            elif sid == "lap_potential":
                findings = _lap_potential(c)
            elif sid == "driver_performance":
                findings = _driver(c)
            elif sid == "braking":
                findings = _braking(c)
                if not findings:
                    reason = "no braking zone was found: the file has no brake channel and no usable deceleration"
            elif sid == "cornering":
                findings = _cornering(c)
            elif sid == "traction":
                if "throttle" not in c.ch:
                    reason = "it needs a throttle channel"
                else:
                    findings = _traction(c)
            elif sid == "setup_hypotheses":
                findings, reason = _setup(c)
        except (ValueError, KeyError, IndexError) as e:
            findings, reason = [], f"it could not be computed for this file ({type(e).__name__}: {e})"
        if not findings and reason is None:
            reason = "the session has too few flying laps to compare"
        for f in findings:
            # the same chain under the names the reports use: evidence → interpretation → hypothesis →
            # suggested investigation. A hypothesis is only ever stated in the setup section.
            f.setdefault("hypothesis", None)
            f["interpretation"], f["investigation"] = f["conclusion"], f["recommendation"]
            # what kind of statement each finding is (model.LABELS)
            f["label"] = ("hypothesis/recommendation" if sid == "setup_hypotheses" else
                          "recorded event" if sid == "incidents" and "simulator recorded" in f["conclusion"] else
                          "detected event (candidate)" if sid == "incidents" and "candidate" in f["conclusion"] else
                          "detected event" if sid == "incidents" and "incident" in f["conclusion"] else "interpretation")
        sections.append({"id": sid, "title": title, "findings": findings,
                         "not_assessed": reason if not findings else None,      # nothing could be said
                         "note": reason if findings else None})                 # part of it could not

    every = [(sec["title"], f) for sec in sections for f in sec["findings"]]
    priorities = []
    if c.summary is not None:
        for r in c.summary.sort_values("available_s", ascending=False).head(3).itertuples():
            if r.available_s < MIN_GAIN_S:
                continue
            said = [f"{t}: {f['conclusion']}" for t, f in every
                    if t not in ("Lap potential",) and re.search(rf"\b{r.corner}\b", f["where"])]
            priorities.append({"corner": r.corner, "kind": r.kind, "available_s": round(float(r.available_s), 3),
                               "findings": said})
    levels = {lv: sum(f["confidence"]["level"] == lv for _, f in every) for lv in LEVELS}
    summary = {"flying_laps": c.n, "best_lap": c.best_lap, "best_lap_s": c.best_time if c.n else None,
               "ideal_lap_s": getattr(c, "ideal", None), "findings": len(every), "confidence": levels,
               "assessed": [s["title"] for s in sections if s["findings"]],
               "not_assessed": [s["title"] for s in sections if not s["findings"]],
               "gain_s": (round(c.best_time - c.ideal, 3) if c.summary is not None and np.isfinite(c.best_time - c.ideal)
                          else None),
               "priorities": priorities}
    out = _plain({"sections": sections, "summary": summary})
    for key in ("best_lap_s", "ideal_lap_s"):
        v = out["summary"][key]
        out["summary"][key] = v if isinstance(v, (int, float)) and np.isfinite(v) else None
    return out


def unavailable(reason: str) -> dict:
    """A report with all nine sections present and none assessed, for a
    session the analysis could not run on at all."""
    sections = [{"id": sid, "title": title, "findings": [], "not_assessed": reason, "note": None}
                for sid, title in SECTIONS]
    return {"sections": sections,
            "summary": {"flying_laps": 0, "best_lap": None, "best_lap_s": None, "ideal_lap_s": None, "findings": 0,
                        "confidence": {lv: 0 for lv in LEVELS}, "assessed": [],
                        "not_assessed": [t for _, t in SECTIONS], "gain_s": None, "priorities": []}}


def _worth(f: dict, before: str, after: str) -> str:
    if f["gain_s"]:
        return f"{before}{f['gain_s']:.2f} s a lap{after}"
    if f["at_stake_s"]:
        return f"{before}the zones concerned hold {f['at_stake_s']:.2f} s a lap{after}"
    return ""


def to_text(report: dict) -> str:
    """The report as plain text, each finding as its chain of evidence."""
    lines = []
    for sec in report["sections"]:
        lines += ["", sec["title"].upper(), "─" * len(sec["title"])]
        if sec["not_assessed"]:
            lines.append(f"  Not assessed: {sec['not_assessed']}.")
        if sec["note"]:
            lines.append(f"  Partly assessed: {sec['note']}.")
        for n, f in enumerate(sec["findings"], 1):
            lines.append(f"  {n}. {f['conclusion']}{_worth(f, '  (', ')')}")
            lines += [f"     Evidence        {e}" if i == 0 else f"                     {e}" for i, e in enumerate(f["evidence"])]
            lines.append(f"     Calculation     {f['calculation']}")
            lines.append(f"     Confidence      {f['confidence']['level']} — {f['confidence']['basis']}")
            if f.get("hypothesis"):
                lines.append(f"     Hypothesis      {f['hypothesis']}")
            lines.append(f"     Recommendation  {f['recommendation']}")
    return "\n".join(lines).lstrip("\n")


def to_markdown(report: dict, title: str = "Performance engineering report") -> str:
    lines = [f"# {title}", ""]
    for sec in report["sections"]:
        lines += [f"## {sec['title']}", ""]
        if sec["not_assessed"] or sec["note"]:
            lines += [f"*{'Not' if sec['not_assessed'] else 'Partly'} assessed: {sec['not_assessed'] or sec['note']}.*", ""]
        for f in sec["findings"]:
            lines += [f"### {f['conclusion']}{_worth(f, ' — ', '')}", "", "**Evidence**", ""]
            lines += [f"- {e}" for e in f["evidence"]]
            lines += ["", f"**Calculation.** {f['calculation']}", "",
                      f"**Confidence: {f['confidence']['level']}.** {f['confidence']['basis'][0].upper() + f['confidence']['basis'][1:]}.", "",
                      *([f"**Hypothesis.** {f['hypothesis']}", ""] if f.get("hypothesis") else []),
                      f"**Recommendation.** {f['recommendation']}", ""]
    return "\n".join(lines)
