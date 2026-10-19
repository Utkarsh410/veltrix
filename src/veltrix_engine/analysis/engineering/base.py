"""The report's building blocks: thresholds, the finding record, the shared context."""

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


SIM_INCIDENT = event_rules.SIM_INCIDENT       # iRacing's incident kinds, as the event engine names them


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


MEASURED, ESTIMATE = "measured", "estimate"
NATURE_TEXT = {MEASURED: "Measured: times the car actually did.",
               ESTIMATE: "Estimate: put together from several laps — no lap was driven like this."}


def _finding(conclusion, evidence, calculation, confidence, recommendation, where="", gain_s=None,
             at_stake_s=None, nature=None) -> dict:
    """gain_s: seconds a lap this finding alone is worth. at_stake_s: the time
    available in the zones it concerns — shared with every other finding about
    those zones, so the two must not be added up across findings. nature:
    MEASURED or ESTIMATE where a reader could take one for the other."""
    num = lambda x: None if x is None or not np.isfinite(x) else round(float(x), 3)
    out = {"conclusion": conclusion, "where": where, "evidence": list(evidence), "calculation": calculation,
           "confidence": confidence, "recommendation": recommendation, "gain_s": num(gain_s),
           "at_stake_s": num(at_stake_s)}
    if nature:
        out["nature"] = nature
    return out


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
