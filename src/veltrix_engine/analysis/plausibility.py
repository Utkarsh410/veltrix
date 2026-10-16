"""
Is an acceleration peak the car, or something else?

Every peak the report leans on — the highest lateral g through each corner and
the hardest deceleration into it — is checked three ways before it is used:

  held       is it held for a quarter of a second, or a jolt? A peak far above
             its own sustained value is a kerb strike (the simulator's surface
             says kerb), a bump (vertical g far from 1), or a single bad sample.
  confirmed  does an independent measurement agree? Lateral g against yaw rate
             × speed (the turn the car is making at that speed); deceleration
             against d(speed)/dt. A peak neither of them supports is a sensor
             or mounting effect, not grip.
  possible   when the car's grip limit is stated, is it beyond it?

Peaks that fail are listed with the reason and kept out of the peak-g figures;
peaks that pass are reported as corroborated.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..vehicle import G

TRANSIENT_G = 0.30            # a peak this far above its own quarter-second value is a jolt, not grip
CORROBORATE_G = 0.35          # ... and this far above the independent estimate is not supported by it
BUMP_G = 0.50                 # vertical g this far from 1 at the peak: a bump or a compression jolt
GRIP_MARGIN = 1.15            # above the stated grip limit by this factor: beyond what the car can do
WINDOW_S = 0.15               # look for a kerb or a bump this close to the peak


def _rad_s(values: np.ndarray, unit: str) -> np.ndarray:
    return np.radians(values) if unit in ("deg/s", "°/s") else values


def check_peaks(session, corners, laps=None) -> pd.DataFrame:
    """One row per corner pass and kind of peak ("lateral", "braking"):
    lap, corner, kind, t, m, peak_g (instantaneous), held_g (quarter-second),
    independent_g (the other measurement, or NaN), vertical_g, on_kerb,
    verdict ("ok" or a reason), reason (plain words)."""
    f = session.frame()
    if corners is None or not len(corners) or "lap_distance" not in f:
        return pd.DataFrame()
    fs = session.rate_hz
    half = max(int(round(WINDOW_S * fs)), 1)
    hold = lambda x: pd.Series(x).rolling(max(int(round(0.25 * fs)), 1), center=True, min_periods=1).median().to_numpy()
    lap, d, t = f["lap"].to_numpy(), f["lap_distance"].to_numpy(), f["time"].to_numpy()
    v = f["speed"].to_numpy(float) / 3.6
    vert = f["acc_vert"].to_numpy(float) if "acc_vert" in f else None
    kerb = f["on_kerb"].to_numpy(float) > 0 if "on_kerb" in f else None
    signals = {}
    lat_id = "acc_lat_rebuilt" if "acc_lat_rebuilt" in f else "acc_lat" if "acc_lat" in f else None
    if lat_id:
        lat = f[lat_id].to_numpy(float)
        kin = None
        if "yaw_rate" in f:
            kin = hold(np.abs(_rad_s(f["yaw_rate"].to_numpy(float), session.units.get("yaw_rate", "")) * v) / G)
        signals["lateral"] = (np.abs(lat), hold(np.abs(lat)), kin)
    if "acc_long" in f and f["acc_long"].notna().any():
        dec = np.clip(-f["acc_long"].to_numpy(float), 0, None)
        kin = hold(np.clip(-f["acc_long_calc"].to_numpy(float), 0, None)) if "acc_long_calc" in f else None
        signals["braking"] = (dec, hold(dec), kin)
    elif "acc_long_calc" in f:                 # no accelerometer: the speed-derived deceleration, checked for jolts only
        dec = np.clip(-f["acc_long_calc"].to_numpy(float), 0, None)
        signals["braking"] = (dec, hold(dec), None)
    if not signals:
        return pd.DataFrame()
    limit = session.vehicle.grip_limit_g if session.vehicle.known("grip_limit_g") else None
    laps = laps if laps is not None else sorted(session.laps.loc[session.laps["status"] == "flying", "lap"])
    rows = []
    for L in laps:
        on = lap == L
        for c in corners.itertuples():
            for kind, (x, held, kin) in signals.items():
                a, b = (c.zone_start_m, c.apex_m) if kind == "braking" else (c.start_m, c.end_m)
                idx = np.flatnonzero(on & (d >= a) & (d <= b) & np.isfinite(x))
                if not len(idx):
                    continue
                i = idx[np.argmax(x[idx])]
                w = slice(max(i - half, 0), i + half + 1)
                rec = {"lap": int(L), "corner": c.corner, "kind": kind, "t": float(t[i]), "m": float(d[i]),
                       "peak_g": float(x[i]), "held_g": float(np.nanmax(held[idx])),
                       "independent_g": float(np.nanmax(kin[idx])) if kin is not None else np.nan,
                       "vertical_g": float(vert[i]) if vert is not None else np.nan,
                       "on_kerb": bool(kerb[w].any()) if kerb is not None else False}
                jolt = rec["peak_g"] - held[i]
                single = (len(x) > i + 1 and i > 0 and x[i] - max(x[i - 1], x[i + 1]) > TRANSIENT_G)
                if jolt > TRANSIENT_G:
                    bump = vert is not None and np.nanmax(np.abs(vert[w] - 1.0)) > BUMP_G
                    rec["verdict"], rec["reason"] = (("kerb", "a kerb strike: the jolt on the kerb, not grip") if rec["on_kerb"]
                                                     else ("bump", "a bump: vertical g far from 1 at the peak") if bump
                                                     else ("spike", "a single sample far from its neighbours") if single
                                                     else ("jolt", "not held for a quarter of a second"))
                elif np.isfinite(rec["independent_g"]) and rec["held_g"] - rec["independent_g"] > CORROBORATE_G:
                    other = "yaw rate × speed" if kind == "lateral" else "d(speed)/dt"
                    rec["verdict"], rec["reason"] = "unsupported", f"held, but {other} says only {rec['independent_g']:.2f} g"
                elif limit and rec["held_g"] > GRIP_MARGIN * limit:
                    rec["verdict"], rec["reason"] = "beyond", f"beyond the stated grip limit of {limit:g} g"
                else:
                    rec["verdict"], rec["reason"] = "ok", ""
                rows.append(rec)
    return pd.DataFrame(rows)
