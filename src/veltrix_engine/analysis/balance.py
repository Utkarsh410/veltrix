"""Balance and steering diagnostics."""

from __future__ import annotations

import numpy as np

from ..derived import best_lat
from ..vehicle import G


MIN_SPEED_KMH = 40.0      # below this the car is in the pits or crawling over kerbs
MIN_LAT_G = 0.5           # steering per g is too noisy in gentler cornering


def _steer_per_g(session, laps=None):
    """Steering-wheel angle per g of lateral acceleration [deg/g] and the
    kinematic term L·g/V² [deg/g at the road wheels], for cornering samples."""
    frame = session.frame()
    lat_id = best_lat(frame)
    if lat_id is None or not {"speed", "steering"} <= set(frame.columns):
        return None
    if laps is not None:
        frame = session.frame(["lap", "speed", "steering", lat_id])
        frame = frame[frame["lap"].isin(laps)]
    v = frame["speed"].to_numpy(float) / 3.6
    ay = frame[lat_id].to_numpy(float)
    steer = frame["steering"].to_numpy(float)
    use = (v > MIN_SPEED_KMH / 3.6) & (np.abs(ay) > MIN_LAT_G) & (np.sign(steer) == np.sign(ay))
    if use.sum() < 500:
        return None
    return steer[use] / ay[use], np.degrees(session.vehicle.wheelbase_m * G / v[use] ** 2)


def steering_characteristic(session, laps=None, ratio: float | None = None) -> dict | None:
    """Steering ratio and understeer gradient from the data.

    Steady-state cornering:  road-wheel angle = L·ay·g/V² + K·ay, so
        steering / ay = ratio · (L·g/V²) + ratio · K
    Regressing steering per g on L·g/V² gives the ratio as the slope and
    ratio·K as the intercept: the kinematic part fades with speed, the
    understeer part does not, which is what separates them.

    laps:  restrict to these laps (default: every flying lap).
    ratio: fix the steering ratio instead of fitting it — use this to compare
           laps or stints on the same basis; K is then the mean of what is left.

    Returns {"ratio", "understeer_deg_per_g" (at the road wheels; +ve
    understeer), "steer_deg_per_g" (same thing at the steering wheel),
    "samples", "fitted_ratio"} or None without enough cornering data.
    The ratio found is tied to VehicleParams.wheelbase_m: only ratio × wheelbase
    is identified."""
    if laps is None:
        laps = list(session.laps.loc[session.laps["status"] == "flying", "lap"]) or None
    data = _steer_per_g(session, laps)
    if data is None:
        return None
    y, x = data
    if ratio is None:
        if np.ptp(x) < 0.2:           # no spread in speed: the two terms cannot be separated
            return None
        fit_ratio, intercept = np.polyfit(x, y, 1)
        if fit_ratio <= 0:
            return None
        k, fitted = intercept / fit_ratio, True
    else:
        fit_ratio, fitted = ratio, False
        k = float(np.mean(y / ratio - x))
    return {"ratio": float(fit_ratio), "understeer_deg_per_g": float(k),
            "steer_deg_per_g": float(k * fit_ratio), "samples": int(len(y)), "fitted_ratio": fitted}


def balance_by_lap(session):
    """Mean oversteer metric while cornering, per lap (-ve understeer, +ve oversteer)."""
    frame = session.frame()
    if "oversteer_metric" not in frame:
        return None
    lat_id = best_lat(frame)
    frame = session.frame(["lap", "oversteer_metric", lat_id])
    corner = frame[frame[lat_id].abs() > 0.3]
    return corner.groupby("lap")["oversteer_metric"].mean()
