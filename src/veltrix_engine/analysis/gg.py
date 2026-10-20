"""G-G analysis: grip envelope, utilisation and driving-phase split."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..derived import best_lat

BRAKING_G = -0.3      # longitudinal g below this is the braking phase
TRACTION_G = 0.2      # longitudinal g above this is the traction phase


def _lat_lon(session, laps=None):
    frame = session.frame()
    lat_id = best_lat(frame)
    if lat_id is None:
        raise ValueError("G-G analysis needs a lateral acceleration channel.")
    lon_id = "acc_long_calc" if "acc_long_calc" in frame else "acc_long"
    frame = session.frame(["lap", lat_id, lon_id])
    if laps is not None:
        frame = frame[frame["lap"].isin(laps)]
    return frame[lat_id].to_numpy(float), frame[lon_id].to_numpy(float)


def gg_envelope(lat, lon, bins: int = 36, quantile: float = 0.98) -> pd.DataFrame:
    """Outer edge of the G-G cloud: the `quantile` of combined g in each of
    `bins` direction sectors. Columns: angle_deg (0 = pure left cornering,
    90 = pure acceleration), radius_g, lat_g, lon_g."""
    lat, lon = np.asarray(lat, float), np.asarray(lon, float)
    ok = np.isfinite(lat) & np.isfinite(lon)
    lat, lon = lat[ok], lon[ok]
    angle = np.degrees(np.arctan2(lon, lat)) % 360
    radius = np.hypot(lat, lon)
    width = 360 / bins
    which = np.minimum((angle // width).astype(int), bins - 1)
    rows = []
    for k in range(bins):
        r = radius[which == k]
        mid = (k + 0.5) * width
        rg = float(np.quantile(r, quantile)) if len(r) >= 5 else np.nan
        rows.append({"angle_deg": mid, "radius_g": rg,
                     "lat_g": rg * np.cos(np.radians(mid)), "lon_g": rg * np.sin(np.radians(mid))})
    return pd.DataFrame(rows)


HELD_S = 0.25                 # headline peaks are held this long (rolling median), so a kerb strike cannot set them


def gg_summary(session, laps=None) -> dict:
    """Headline G-G numbers against the vehicle's reference envelope. The peaks
    are held values — the highest acceleration kept up for a quarter of a second,
    from the measured accelerometer where there is one — with the single highest
    samples alongside as *_instant_g."""
    lat, lon = _lat_lon(session, laps)
    combined = np.hypot(lat, lon)
    util = combined / session.vehicle.grip_limit_g
    window = max(int(round(HELD_S * session.rate_hz)), 1)
    held = lambda x: pd.Series(x).rolling(window, center=True, min_periods=1).median().to_numpy()
    frame = session.frame(["lap", "acc_long"])
    if laps is not None:
        frame = frame[frame["lap"].isin(laps)]
    measured = frame["acc_long"].to_numpy(float) if "acc_long" in frame and frame["acc_long"].notna().any() else lon
    hl, hm = held(lat), held(measured)
    return {
        "samples": int(len(lat)),
        "peak_lat_g": float(np.nanmax(np.abs(hl))),
        "peak_brake_g": float(-np.nanmin(hm)),
        "peak_drive_g": float(np.nanmax(hm)),
        "peak_combined_g": float(np.nanmax(np.hypot(hl, held(lon)))),
        "peak_lat_instant_g": float(np.nanmax(np.abs(lat))),
        "peak_brake_instant_g": float(-np.nanmin(lon)),
        "mean_util": float(np.nanmean(util)),
        "frac_above_75": float(np.nanmean(util > 0.75)),
        "frac_above_90": float(np.nanmean(util > 0.90)),
    }


def phase_split(session, laps=None) -> dict:
    """Share of samples in each driving phase, by longitudinal g."""
    _, lon = _lat_lon(session, laps)
    return {"braking": float(np.mean(lon < BRAKING_G)),
            "cornering": float(np.mean((lon >= BRAKING_G) & (lon <= TRACTION_G))),
            "traction": float(np.mean(lon > TRACTION_G))}
