"""
Session: one imported telemetry file, normalised and ready for analysis.

Three layers are kept apart so imports are replaceable and calculations are
reproducible:

  raw       the file as read (RawTable) — never modified
  channels  normalised channels: schema ids, schema units and signs, uniform
            time base, plus `lap` and `lap_distance`
  derived   channels computed by the engine from `channels`

`load_session(path)` runs the whole pipeline:
  read → map → convert units → check → find laps → resample → derived channels
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from . import laps as lap_engine
from .derived import compute_derived
from .importers import RawTable, read_csv
from .mapping import auto_map, validate_mapping
from .schema import CANONICAL_UNIT, CHANNELS, UNIT_FACTORS
from .vehicle import VehicleParams

REQUIRED = ("time", "speed")
EARTH_RADIUS = 6_371_000.0


@dataclass
class Session:
    raw: RawTable
    channels: pd.DataFrame                 # normalised, uniform time base
    derived: pd.DataFrame                  # same index as channels
    laps: pd.DataFrame                     # one row per lap
    mapping: dict                          # {channel id: source column}
    units: dict                            # {channel id: unit of the normalised channel}
    rate_hz: float
    lap_length: float | None
    vehicle: VehicleParams
    notes: list = field(default_factory=list)   # [(level, message)], level: "info" | "warning"
    lap_source: str = ""
    crossing_method: str = ""

    def frame(self) -> pd.DataFrame:
        """Normalised and derived channels side by side."""
        return pd.concat([self.channels, self.derived], axis=1)

    def lap_row(self, lap):
        rows = self.laps[self.laps["lap"] == lap]
        if rows.empty:
            raise KeyError(f"Lap {lap!r} is not in the session. Laps: {list(self.laps['lap'])}")
        return next(rows.itertuples())

    def warnings(self) -> list:
        return [m for level, m in self.notes if level == "warning"]

    @property
    def meta(self) -> dict:
        return {"source": self.raw.source, "sha256": self.raw.sha256, "info": self.raw.info,
                "rate_hz": self.rate_hz, "lap_length": self.lap_length,
                "lap_source": self.lap_source, "crossing_method": self.crossing_method,
                "samples": len(self.channels), "mapping": self.mapping, "units": self.units}


def load_session(path: str | Path, **kwargs) -> Session:
    """Read a telemetry file and build a Session. See build_session for options."""
    return build_session(read_csv(path), **kwargs)


def build_session(raw: RawTable, vehicle: VehicleParams | None = None, mapping: dict | None = None,
                  rate_hz: float | None = None, flip: tuple = (),
                  start_finish_index: int | None = None) -> Session:
    """Normalise a RawTable into a Session.

    mapping: {channel id: source column} to override the automatic mapping
             (channels not named keep their automatic match).
    rate_hz: uniform sample rate for the normalised channels. Default: the
             file's own median rate.
    flip:    channel ids whose sign is opposite to the schema's convention in
             this file (e.g. ("steering", "acc_lat")).
    start_finish_index: source row at which the car is on the start/finish
             line, for timing laps from position when the file has no lap
             channel, beacons or lap distance.
    """
    vehicle = vehicle or VehicleParams()
    notes: list = []
    note = lambda level, msg: notes.append((level, msg))

    # ── map ────────────────────────────────────────────────────────────────
    mapped = auto_map(raw.data.columns)
    if mapping:
        validate_mapping(mapping, raw.data.columns)
        mapped = {c: col for c, col in mapped.items() if col not in mapping.values()}
        mapped.update(mapping)
    missing = [c for c in REQUIRED if c not in mapped and not (c == "time" and rate_hz)]
    if missing:
        raise ValueError(
            f"{raw.source or 'file'}: required channel(s) not found: {missing}. "
            f"Columns: {list(raw.data.columns)[:20]}. Pass mapping={{channel: column}} to set them.")

    # ── units ──────────────────────────────────────────────────────────────
    cdf, units = _to_canonical(raw, mapped, note)
    if "time" not in cdf:
        cdf["time"] = np.arange(len(cdf)) / rate_hz
        units["time"] = "s"
        note("info", f"No time channel — assumed a uniform {rate_hz:g} Hz.")
    for cid in flip:
        if cid in cdf:
            cdf[cid] = -cdf[cid]
            note("info", f"{cid}: sign flipped to match the schema convention.")

    # ── time base ──────────────────────────────────────────────────────────
    timer_crossings = None
    cdf = cdf[np.isfinite(cdf["time"])].reset_index(drop=True)
    t = cdf["time"].to_numpy()
    if lap_engine.time_restarts(t):
        cdf["time"], timer_crossings = lap_engine.stitch_lap_timer(t)
        note("info", "Time restarts every lap — treated as a lap timer and stitched into a session clock.")
    _time_checks(cdf, raw, note)
    cdf = (cdf.sort_values("time", kind="stable")
              .drop_duplicates(subset="time", keep="last").reset_index(drop=True))
    if len(cdf) < 10:
        raise ValueError(f"{raw.source or 'file'}: only {len(cdf)} usable samples.")
    t = cdf["time"].to_numpy()

    # ── position ───────────────────────────────────────────────────────────
    if "pos_x" not in cdf and "gps_lat" in cdf and "gps_lon" in cdf:
        lat, lon = cdf["gps_lat"].to_numpy(), cdf["gps_lon"].to_numpy()
        cdf["pos_x"] = np.radians(lon - np.nanmean(lon)) * EARTH_RADIUS * np.cos(np.radians(np.nanmean(lat)))
        cdf["pos_y"] = np.radians(lat - np.nanmean(lat)) * EARTH_RADIUS
        units["pos_x"] = units["pos_y"] = "m"
        note("info", "pos_x / pos_y projected from GPS latitude and longitude.")

    # ── distance ───────────────────────────────────────────────────────────
    if "distance" in cdf:
        d = cdf["distance"].to_numpy()
    else:
        dt_raw = np.clip(np.nan_to_num(np.diff(t, prepend=t[0])), 0, None)
        d = np.cumsum(np.nan_to_num(cdf["speed"].to_numpy()) / 3.6 * dt_raw)
        note("info", "No distance channel — distance integrated from speed.")

    # ── laps ───────────────────────────────────────────────────────────────
    runs, crossings, source, crossing_source = _lap_runs(cdf, raw, t, d, timer_crossings,
                                                         start_finish_index)
    lap_table, info = lap_engine.detect_laps(t, runs, d=d, v_kmh=cdf["speed"].to_numpy(),
                                             crossings=crossings, crossing_source=crossing_source)
    note("info", f"Laps from {source}; line crossings {info['crossing']}.")
    lap_length = info["lap_length"]

    # ── resample ───────────────────────────────────────────────────────────
    dt_src = float(np.median(np.diff(t)))
    if rate_hz is None:
        rate_hz = _standard_rate(1.0 / dt_src)
    note("info", f"{len(cdf):,} samples logged at about {1 / dt_src:,.1f} Hz; "
                 f"normalised to {rate_hz:g} Hz.")
    channels = _resample(cdf, t, rate_hz, lap_table, info["distance"],
                         lap_engine.unwrap_distance(d))
    units["distance"] = units["lap_distance"] = "m"
    units["lap"] = "-"

    lap_table = lap_table.drop(columns=["start_pos", "end_pos"])
    derived, derived_notes = compute_derived(channels, rate_hz, vehicle)
    notes += [("info", m) for m in derived_notes]

    return Session(raw=raw, channels=channels, derived=derived, laps=lap_table, mapping=mapped,
                   units=units, rate_hz=rate_hz, lap_length=lap_length, vehicle=vehicle,
                   notes=notes, lap_source=source, crossing_method=info["crossing"])


# ── steps ─────────────────────────────────────────────────────────────────────

STANDARD_RATES = (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000)


def _standard_rate(native_hz: float) -> float:
    """The lowest standard logging rate that does not undersample the file
    (logs sampled by distance or with jitter have no single exact rate)."""
    return float(next((r for r in STANDARD_RATES if r >= 0.95 * native_hz), STANDARD_RATES[-1]))


def _unit_key(unit) -> str:
    return (unit or "").strip().lower().replace(" ", "").replace("²", "^2")


def _to_canonical(raw: RawTable, mapped: dict, note) -> tuple[pd.DataFrame, dict]:
    """Mapped channels as float columns in schema units. Every conversion and
    every assumption is recorded as a note."""
    df = raw.data
    cdf, units = pd.DataFrame(index=df.index), {}
    for cid, col in mapped.items():
        cdef = CHANNELS[cid]
        if cid == "lap":
            cdf[cid] = df[col].to_numpy()
            continue
        x = pd.to_numeric(df[col], errors="coerce").to_numpy(float)
        unit_text = raw.units.get(col, "")
        unit = _unit_key(unit_text)
        q = cdef.quantity
        target = CANONICAL_UNIT.get(q, cdef.unit)

        if q == "speed":
            pass                                     # resolved below, may need distance
        elif q in UNIT_FACTORS:
            table = UNIT_FACTORS[q]
            if unit in table:
                if table[unit] != 1:
                    note("info", f"{col} converted from {unit_text} to {target}.")
                x = x * table[unit]
            elif q == "accel" and not unit and np.nanpercentile(np.abs(x), 99.5) > 4:
                note("warning", f"{col} has no unit in the file and looks like m/s² — converted to g.")
                x = x / 9.81
            elif q == "pressure" and not unit and np.nanmedian(x) > 10:
                note("warning", f"{col} has no unit in the file and looks like psi — converted to bar.")
                x = x * table["psi"]
            elif q == "angle" and not unit and np.nanmax(np.abs(x)) < 2.0:
                note("warning", f"{col} has no unit in the file and stays within ±2 — it looks like "
                                "radians or a normalised value, not degrees. Channels computed from "
                                "steering will be wrong until its unit is known.")
            elif unit:
                note("warning", f"{col}: unit '{unit_text}' not recognised — assumed {target}.")
        elif q == "percent":
            if unit != "%" and np.nanmin(x) >= -0.01 and np.nanmax(x) <= 1.0:
                note("info", f"{col} is a 0–1 fraction in the file — converted to %.")
                x = x * 100
        elif q == "brake":
            target = "%"
            if unit in UNIT_FACTORS["pressure"]:
                x, target = x * UNIT_FACTORS["pressure"][unit], "bar"
            elif unit != "%" and np.nanmin(x) >= -0.01 and np.nanmax(x) <= 1.0:
                note("info", f"{col} is a 0–1 fraction in the file — converted to %.")
                x = x * 100
            elif unit and unit != "%":
                target = unit_text
        elif q == "temperature":
            if unit in ("°f", "degf", "f") or (not unit and 120 < np.nanmedian(x) < 260):
                note("info" if unit else "warning",
                     f"{col} converted from °F to °C" + ("." if unit else " (no unit given; values look like °F)."))
                x = (x - 32) * 5 / 9
            elif unit in ("k", "kelvin"):
                x = x - 273.15
        cdf[cid], units[cid] = x, target

    if "speed" in cdf:
        col = mapped["speed"]
        unit = _unit_key(raw.units.get(col))
        table = UNIT_FACTORS["speed"]
        if unit not in table:
            unit = _speed_unit_from_distance(cdf) if {"time", "distance"} <= set(cdf) else None
            note("info" if unit else "warning", f"{col} has no unit in the file — " + (
                f"its distance over time matches the distance channel as {unit}." if unit
                else "assumed km/h."))
        if unit and table[unit] != 1:
            cdf["speed"] = cdf["speed"] * table[unit]
            note("info", f"{col} converted from {unit} to km/h.")
        units["speed"] = "km/h"
    return cdf, units


def _speed_unit_from_distance(cdf):
    """Work out the speed unit by comparing speed × time with the distance channel."""
    t, d, v = (cdf[c].to_numpy() for c in ("time", "distance", "speed"))
    dt, dd = np.diff(t), np.diff(d)
    ok = (dt > 0) & (dd >= 0) & np.isfinite(dd) & np.isfinite(v[:-1])
    by_speed = np.sum(v[:-1][ok] * dt[ok]) / 3.6  # metres, if speed is in km/h
    if not by_speed > 0:
        return None
    ratio = dd[ok].sum() / by_speed
    for unit in ("km/h", "mph", "m/s"):
        if abs(ratio / UNIT_FACTORS["speed"][unit] - 1) < 0.08:
            return unit
    return None


def _time_checks(cdf, raw: RawTable, note) -> None:
    t = cdf["time"].to_numpy()
    dt = np.diff(t)
    forward = dt[dt > 0]
    if len(forward):
        step = float(np.median(forward))
        gaps = np.flatnonzero(dt > 5 * step)
        if len(gaps):
            g = gaps[np.argmax(dt[gaps])]
            note("warning", f"{len(gaps)} gap(s) in the log longer than 5 samples — "
                            f"the longest is {dt[g]:.2f} s at {t[g]:.1f} s.")
    if (dt < 0).any():
        note("warning", f"Time goes backwards {int((dt < 0).sum())} time(s) — samples were re-ordered.")
    if (dt == 0).any():
        note("warning", f"{int((dt == 0).sum())} rows repeat the previous timestamp — the later row was kept.")
    for col, n in raw.bad_cells.items():
        note("warning", f"{n} cell(s) in '{col}' weren't numbers and were left blank.")
    if raw.skipped_rows:
        note("warning", f"{raw.skipped_rows} malformed row(s) were skipped.")
    for cid in cdf.columns:
        if cid != "lap" and np.isnan(cdf[cid].to_numpy(float)).all():
            note("warning", f"{cid} has no numeric values.")


def _lap_runs(cdf, raw: RawTable, t, d, timer_crossings, start_finish_index):
    """Pick the best lap-boundary source the file offers.
    Returns (runs, exact crossing times or None, source, crossing description)."""
    beacons = raw.beacons
    if beacons is not None:
        beacons = beacons[(beacons > t[0]) & (beacons < t[-1])]
    if "lap" in cdf:
        runs = lap_engine.lap_runs(cdf["lap"].to_numpy())
        if len(runs) > 1:
            for exact, label in ((beacons, "beacon markers"), (timer_crossings, "lap timer restarts")):
                if exact is not None and len(exact) == len(runs) - 1:
                    return runs, exact, "the lap channel", f"taken from the {label}"
            return runs, None, "the lap channel", None
    if beacons is not None and len(beacons):
        runs, c = lap_engine.runs_from_crossings(t, beacons)
        return runs, c, "beacon markers", "taken from the beacon markers"
    if timer_crossings is not None and len(timer_crossings):
        runs, c = lap_engine.runs_from_crossings(t, timer_crossings)
        return runs, c, "lap timer restarts", "taken from the lap timer restarts"
    if "distance" in cdf and lap_engine.distance_resets(d):
        return lap_engine.runs_from_distance_resets(d), None, "lap-distance resets", None
    if start_finish_index is not None and {"pos_x", "pos_y"} <= set(cdf):
        c = lap_engine.gate_crossings(t, cdf["pos_x"].to_numpy(), cdf["pos_y"].to_numpy(),
                                      int(start_finish_index))
        if len(c):
            runs, c = lap_engine.runs_from_crossings(t, c)
            return runs, c, "a position gate", "interpolated at the position gate"
    return [], None, "no lap information (whole session is one lap)", None


def _resample(cdf, t, rate_hz, lap_table, lap_d, d_cum) -> pd.DataFrame:
    """Uniform time base. Continuous channels are linearly interpolated over
    their valid samples; step channels hold the previous sample. `lap` and
    `lap_distance` are rebuilt from the lap table so they stay exact at the line."""
    dt = 1.0 / rate_hz
    n = int(np.floor((t[-1] - t[0]) / dt + 1e-6)) + 1
    t_new = t[0] + np.arange(n) * dt
    hold = np.clip(np.searchsorted(t, t_new + 1e-9, side="right") - 1, 0, len(t) - 1)

    out = {"time": t_new}
    for cid in cdf.columns:
        if cid in ("time", "lap", "distance"):
            continue
        x = cdf[cid].to_numpy(float)
        if CHANNELS[cid].discrete:
            out[cid] = x[hold]
            continue
        ok = np.isfinite(x)
        out[cid] = np.interp(t_new, t[ok], x[ok]) if ok.sum() >= 2 else np.full(n, np.nan)

    # Lap of each sample: a lap owns the time from its start crossing to its end crossing
    starts = lap_table["t_start"].to_numpy()
    which = np.clip(np.searchsorted(starts, t_new + 1e-9, side="right") - 1, 0, len(lap_table) - 1)
    labels = lap_table["lap"].to_numpy()
    out["lap"] = labels[which]

    out["distance"] = np.interp(t_new, t, d_cum - d_cum[0])
    lap_dist = np.full(n, np.nan)
    if lap_d is not None:
        for i, lap in enumerate(lap_table.itertuples()):
            sel = which == i
            s, e = lap.start_pos, lap.end_pos
            ts, ds = t[s:e + 1], lap_d[s:e + 1] * lap.d_scale
            ok = np.isfinite(ds)
            if not sel.any() or ok.sum() < 2:
                continue
            ts, ds = ts[ok], ds[ok]
            # Anchor on the line where the lap crossed it
            if lap.start_crossed and lap.t_start < ts[0]:
                ts, ds = np.r_[lap.t_start, ts], np.r_[0.0, ds]
            if lap.end_crossed and lap.t_end > ts[-1]:
                ts, ds = np.r_[ts, lap.t_end], np.r_[ds, max(ds[-1], lap.dist_m * lap.d_scale)]
            lap_dist[sel] = np.interp(t_new[sel], ts, ds)
    out["lap_distance"] = lap_dist

    frame = pd.DataFrame(out)
    for cid in frame.columns:
        if cid in CHANNELS and CHANNELS[cid].discrete and cid != "lap":
            if np.isfinite(frame[cid]).all():
                frame[cid] = frame[cid].round().astype(int)
    lead = ["time", "lap", "lap_distance", "distance"]
    return frame[lead + [c for c in frame.columns if c not in lead]]
