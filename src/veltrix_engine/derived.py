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
SLIP_MIN_KMH = 30.0   # wheel slip is only worked out above this speed
SUSTAIN_S = 0.25      # sustained acceleration: the median over this long, centred
COARSE_SPEED_CUTOFF_HZ = 2.0    # d(speed)/dt filter when speed is recorded in coarse steps
ROLL_CUTOFF_HZ = 5.0            # bandwidth of lateral g rebuilt from damper roll
ROLL_MIN_SPEED_KMH = 60.0       # calibrate above this speed, away from pit-lane and kerb crawling
ROLL_MIN_R = 0.8                # weakest roll-to-lateral-g correlation accepted for a rebuild
MAX_LONG_G = 6.0                # beyond this, d(speed)/dt is a jump in the speed channel, not the car


def best_lat(frame) -> str | None:
    """The lateral acceleration channel to analyse: the rebuilt one when the
    logged channel was too coarse, otherwise the logged one."""
    for cid in ("acc_lat_rebuilt", "acc_lat"):
        if cid in frame:
            return cid
    return None


def rebuild_lateral_from_roll(channels: pd.DataFrame, fs_hz: float, step_g: float):
    """Lateral g from damper roll, for files where acc_lat is recorded in steps
    too coarse to use (typically whole g).

    Body roll is proportional to lateral acceleration, and damper travel is
    recorded finely. The roll signal (right minus left travel, both axles) is
    fitted to the logged lateral g by least squares: rounding error in the
    logged channel is unbiased, so over many samples the fit recovers the scale.

    Returns (signal, info) or (None, reason). info holds the fit and how its
    leftover error compares with what rounding alone would leave
    (step / sqrt(12)): when the two are equal, the rebuilt signal explains
    everything the logged channel is able to show."""
    susp = [f"susp_travel_{c}" for c in CORNERS]
    if not all(c in channels for c in susp):
        return None, "no damper travel on all four corners"
    fl, fr, rl, rr = (channels[c].to_numpy(float) for c in susp)
    roll = lowpass((fr - fl) + (rr - rl), ROLL_CUTOFF_HZ, fs_hz)
    ay = channels["acc_lat"].to_numpy(float)
    use = np.isfinite(roll) & np.isfinite(ay)
    if "speed" in channels:
        use &= channels["speed"].to_numpy(float) > ROLL_MIN_SPEED_KMH
    if use.sum() < 2000 or np.std(roll[use]) == 0 or np.std(ay[use]) == 0:
        return None, "too few samples at speed to calibrate damper roll"
    r = float(np.corrcoef(roll[use], ay[use])[0, 1])
    if r < ROLL_MIN_R:
        return None, f"damper roll does not track the logged lateral g well enough (r = {r:.2f})"
    slope, offset = np.polyfit(roll[use], ay[use], 1)
    rebuilt = slope * roll + offset
    info = {"method": "damper roll", "slope_g_per_mm": float(slope), "offset_g": float(offset),
            "r": r, "samples": int(use.sum()),
            "residual_g": float(np.std(ay[use] - rebuilt[use])),
            "rounding_g": float(step_g / np.sqrt(12)), "bandwidth_hz": ROLL_CUTOFF_HZ}
    return rebuilt, info


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


def compute_derived(channels: pd.DataFrame, fs_hz: float, vehicle: VehicleParams | None = None,
                    coarse: dict | None = None) -> tuple[pd.DataFrame, list, dict]:
    """Compute every derived channel whose inputs are present.

    channels: normalised channels on a uniform time base (schema ids as columns).
    coarse:   {channel id: step} for channels recorded in steps too coarse to
              analyse directly. Longitudinal g is then filtered harder, and
              lateral g is rebuilt from damper roll where the dampers allow.
    Returns (derived frame, notes, rebuilt) — notes say what was skipped,
    assumed or rebuilt; rebuilt holds the details of each rebuilt signal."""
    p = vehicle or VehicleParams()
    coarse = coarse or {}
    dt = 1.0 / fs_hz
    out, notes, rebuilt = pd.DataFrame(index=channels.index), [], {}
    has = lambda *ids: all(c in channels for c in ids)
    arr = lambda cid: channels[cid].to_numpy(float)

    if has("speed"):
        v = arr("speed") / 3.6                                   # m/s
        # Longitudinal g: dV/dt, filtered. Coarse speed is a staircase: smooth it
        # before differentiating, or every step becomes a spike.
        if "speed" in coarse:
            fc = COARSE_SPEED_CUTOFF_HZ
            out["acc_long_calc"] = np.gradient(lowpass(v, fc, fs_hz), dt) / G
            rebuilt["acc_long_calc"] = {"method": "d(speed)/dt", "bandwidth_hz": fc,
                                        "speed_step_kmh": float(coarse["speed"])}
            notes.append(f"acc_long_calc uses a {fc:g} Hz low-pass instead of 15 Hz because speed is "
                         f"recorded in {coarse['speed']:.3g} km/h steps. Expect about 0.1 g of error; "
                         "events shorter than half a second are smoothed out.")
        else:
            out["acc_long_calc"] = lowpass(np.gradient(v, dt) / G, 15.0, fs_hz)
    else:
        notes.append("No speed channel — no derived channels that depend on speed.")

    if has("vel_x", "vel_y"):
        # Body slip from the car-frame velocities: how far the car travels sideways of where it points
        vx, vy = arr("vel_x"), arr("vel_y")
        with np.errstate(invalid="ignore"):
            out["body_slip"] = np.where(vx > 5.0, np.degrees(np.arctan2(vy, vx)), np.nan)

    if "acc_long_calc" in out:
        # A reset to the pits or a dropped sample makes speed jump; that is not an acceleration
        jump = out["acc_long_calc"].abs() > MAX_LONG_G
        if jump.any():
            out.attrs.setdefault("bridged", {})["acc_long_calc"] = int(jump.sum())
            out.loc[jump, "acc_long_calc"] = np.nan
            out["acc_long_calc"] = out["acc_long_calc"].interpolate(limit_direction="both")
            notes.append(f"acc_long_calc: {int(jump.sum())} samples above {MAX_LONG_G:g} g come from jumps in the speed "
                         "channel (a reset or a dropped sample) and are bridged from the samples either side.")

    if has("steering"):
        out["steering_rate"] = lowpass(np.gradient(arr("steering"), dt), 10.0, fs_hz)

    if has("speed", "steering"):
        # Bicycle model, neutral steer: ay = V² δ / (g L),
        # δ = steering wheel angle / steering ratio.
        road_wheel = np.radians(arr("steering") / p.steer_ratio)
        out["acc_lat_calc"] = np.clip(
            lowpass(v ** 2 * road_wheel / (G * p.wheelbase_m), 15.0, fs_hz), -4.0, 4.0)

    lat_ok = has("acc_lat")
    if lat_ok and "acc_lat" in coarse:
        signal, info = rebuild_lateral_from_roll(channels, fs_hz, coarse["acc_lat"])
        if signal is None:
            lat_ok = False
            notes.append(f"Lateral g is recorded in {coarse['acc_lat']:.3g} g steps and cannot be rebuilt "
                         f"({info}) — balance, load-transfer split and grip utilisation are not computed.")
        else:
            out["acc_lat_rebuilt"] = signal
            rebuilt["acc_lat"] = info
            notes.append(
                f"acc_lat_rebuilt: lateral g rebuilt from damper roll because the logged channel is in "
                f"{coarse['acc_lat']:.3g} g steps (fit r = {info['r']:.2f}; leftover error {info['residual_g']:.3f} g "
                f"against {info['rounding_g']:.3f} g expected from rounding alone). Used for balance, "
                f"grip and the track map; {info['bandwidth_hz']:g} Hz bandwidth.")

    if lat_ok:
        if "acc_lat_rebuilt" in out:
            ay = ay_f = out["acc_lat_rebuilt"].to_numpy()
        else:
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
    elif not has("acc_lat"):
        notes.append("No lateral acceleration channel — balance, load transfer and grip "
                     "utilisation are not computed.")
    # ── vehicle dynamics ──────────────────────────────────────────────────────
    if has("speed"):
        v_kmh = arr("speed")
        for c in CORNERS:
            if has(f"wheel_speed_{c}"):
                with np.errstate(divide="ignore", invalid="ignore"):
                    out[f"slip_ratio_{c}"] = np.where(v_kmh > SLIP_MIN_KMH, (arr(f"wheel_speed_{c}") - v_kmh) / v_kmh, np.nan)
    for c in CORNERS:
        if has(f"susp_travel_{c}") and not has(f"damper_vel_{c}"):
            out[f"damper_vel_{c}_est"] = lowpass(np.gradient(arr(f"susp_travel_{c}"), dt), 20.0, fs_hz)
    if has(*[f"susp_travel_{c}" for c in CORNERS]):
        fl, fr, rl, rr = (lowpass(arr(f"susp_travel_{c}"), 10.0, fs_hz) for c in CORNERS)
        out["roll_front_deg"] = np.degrees(np.arctan((fr - fl) / 1000.0 / p.track_front_m))
        out["roll_rear_deg"] = np.degrees(np.arctan((rr - rl) / 1000.0 / p.track_rear_m))
        out["pitch_deg"] = np.degrees(np.arctan(((fl + fr) - (rl + rr)) / 2.0 / 1000.0 / p.wheelbase_m))
    if has("vel_x", "vel_y", "yaw_rate", "steering"):
        vx, vy, r = arr("vel_x"), arr("vel_y"), arr("yaw_rate")
        a = p.wheelbase_m * (1.0 - p.front_weight_dist)            # CG to the front axle
        b = p.wheelbase_m * p.front_weight_dist                    # CG to the rear axle
        delta = np.radians(arr("steering") / p.steer_ratio)
        moving = vx > 5.0
        out["slip_angle_front_deg"] = np.where(moving, np.degrees(delta - np.arctan2(vy + a * r, vx)), np.nan)
        out["slip_angle_rear_deg"] = np.where(moving, np.degrees(-np.arctan2(vy - b * r, vx)), np.nan)

    lat_id = "acc_lat_rebuilt" if "acc_lat_rebuilt" in out else "acc_lat" if has("acc_lat") else None
    if lat_id:
        # what the tyres hold, not the jolt of a kerb: a quarter of a second's median
        lat = out[lat_id] if lat_id in out else channels[lat_id]
        out["acc_lat_sustained"] = lat.rolling(max(int(round(SUSTAIN_S * fs_hz)), 1), center=True, min_periods=1).median()
    return out, notes, rebuilt
