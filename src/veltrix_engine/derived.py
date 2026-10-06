"""
Derived (maths) channels computed from normalised channels.

Each channel declares what it needs in the schema (`requires`); a channel whose
inputs are missing is skipped and reported, never filled with zeros.

Formulas are documented inline with their assumptions. All inputs follow the
schema's units and sign conventions.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.signal import butter, filtfilt

from .schema import CORNERS
from .vehicle import G, VehicleParams

CORNERING_G = 0.3     # oversteer metric is only defined above this lateral g
LLTD_MIN_G = 0.5      # load transfer split is only reported above this lateral g


def lowpass(x: np.ndarray, cutoff_hz: float, fs_hz: float, order: int = 4) -> np.ndarray:
    """Zero-phase Butterworth low-pass. The cutoff is capped below Nyquist so
    low-rate logs still filter; NaNs are bridged before filtering and restored."""
    x = np.asarray(x, float)
    cutoff = min(cutoff_hz, 0.4 * fs_hz)
    b, a = butter(order, cutoff / (0.5 * fs_hz), btype="low")
    if len(x) <= 3 * max(len(a), len(b)):
        return x.copy()
    bad = ~np.isfinite(x)
    if bad.all():
        return x.copy()
    if bad.any():
        idx = np.arange(len(x))
        x = np.interp(idx, idx[~bad], x[~bad])
    y = filtfilt(b, a, x)
    y[bad] = np.nan
    return y


def compute_derived(channels: pd.DataFrame, fs_hz: float,
                    vehicle: VehicleParams | None = None) -> tuple[pd.DataFrame, list]:
    """Compute every derived channel whose inputs are present.

    channels: normalised channels on a uniform time base (schema ids as columns).
    Returns (derived frame, notes) — notes say what was skipped or assumed."""
    p = vehicle or VehicleParams()
    dt = 1.0 / fs_hz
    out, notes = pd.DataFrame(index=channels.index), []
    has = lambda *ids: all(c in channels for c in ids)
    arr = lambda cid: channels[cid].to_numpy(float)

    if has("speed"):
        v = arr("speed") / 3.6                                   # m/s
        # Longitudinal g: dV/dt, filtered
        out["acc_long_calc"] = lowpass(np.gradient(v, dt) / G, 15.0, fs_hz)
    else:
        notes.append("No speed channel — no derived channels that depend on speed.")

    if has("steering"):
        out["steering_rate"] = lowpass(np.gradient(arr("steering"), dt), 10.0, fs_hz)

    if has("speed", "steering"):
        # Bicycle model, neutral steer: ay = V² δ / (g L),
        # δ = steering wheel angle / steering ratio.
        road_wheel = np.radians(arr("steering") / p.steer_ratio)
        out["acc_lat_calc"] = np.clip(
            lowpass(v ** 2 * road_wheel / (G * p.wheelbase_m), 15.0, fs_hz), -4.0, 4.0)

    if has("acc_lat"):
        ay = arr("acc_lat")
        ay_f = lowpass(ay, 15.0, fs_hz)

        if has("speed"):
            # Steady-state yaw rate: r = ay g / V (undefined when nearly stopped)
            with np.errstate(divide="ignore", invalid="ignore"):
                yaw = np.where(v > 5.0, ay * G / v, 0.0)
            out["yaw_rate_est"] = lowpass(yaw, 15.0, fs_hz)

        if "acc_lat_calc" in out:
            # Magnitudes make the sign independent of corner direction:
            # -ve = more steering than a neutral car needs (understeer)
            # +ve = more lateral g than the steering asks for (oversteer)
            os_raw = np.where(np.abs(ay_f) > CORNERING_G,
                              np.abs(ay_f) - np.abs(out["acc_lat_calc"].to_numpy()), 0.0)
            out["oversteer_metric"] = lowpass(os_raw, 5.0, fs_hz)

        # Lateral load transfer. The rigid-body total is m ay g h / t; how it
        # splits front/rear depends on roll stiffness, which accelerometers
        # cannot see. With damper travel it is measured per axle:
        #   ΔFz = wheel rate × (right − left travel) / 2
        t_mean = 0.5 * (p.track_front_m + p.track_rear_m)
        llt_total = p.mass_kg * ay * G * p.cg_height_m / t_mean
        susp = [f"susp_travel_{c}" for c in CORNERS]
        if has(*susp):
            fl, fr, rl, rr = (lowpass(arr(c), 10.0, fs_hz) for c in susp)
            llt_f = p.wheel_rate_front_N_mm * (fr - fl) / 2.0
            llt_r = p.wheel_rate_rear_N_mm * (rr - rl) / 2.0
        else:
            llt_f = llt_r = 0.5 * llt_total
            notes.append("No damper travel on all four corners — load transfer is the rigid-body "
                         "total split evenly, so lltd_front carries no information.")
        out["llt_front"], out["llt_rear"] = llt_f, llt_r
        total = np.abs(llt_f) + np.abs(llt_r)
        with np.errstate(divide="ignore", invalid="ignore"):
            out["lltd_front"] = np.where((np.abs(ay) > LLTD_MIN_G) & (total > 1.0),
                                         np.abs(llt_f) / total, np.nan)

        if "acc_long_calc" in out:
            # Filtered dV/dt rather than the raw accelerometer, so pitch and
            # noise do not inflate the number.
            out["grip_util"] = np.hypot(ay_f, out["acc_long_calc"].to_numpy()) / p.grip_limit_g
    else:
        notes.append("No lateral acceleration channel — balance, load transfer and grip "
                     "utilisation are not computed.")
    return out, notes
