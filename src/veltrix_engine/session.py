"""
Session: one imported telemetry file, normalised and ready for analysis.

The session is built as a canonical model (model.py): raw, normalised,
corrected and calculated channels, each on its own timestamps with its
provenance. `Session.model` is that model. The rest of the Session is the
model's *analysis view* — the channels laid on one uniform clock, which is what
the lap, corner and balance calculations work on:

  raw       the file as read (RawTable) — never modified
  channels  the analysis view of the normalised channels: schema ids, schema
            units and signs, one uniform time base, plus `lap` and `lap_distance`
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
from .importers import RawTable, read_file
from .mapping import auto_map, validate_mapping
from .model import SessionModel
from .model_build import build_model
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
    coarse: dict = field(default_factory=dict)    # {channel id: step} recorded too coarsely
    rebuilt: dict = field(default_factory=dict)   # {signal: how it was rebuilt}
    model: SessionModel | None = None             # the canonical model this session is a view of

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
                "samples": len(self.channels), "mapping": self.mapping, "units": self.units,
                "coarse": self.coarse, "rebuilt": self.rebuilt}


def load_session(path: str | Path, dbc=None, channels=None, progress=None, cancel=None, **kwargs) -> Session:
    """Read a telemetry file of any supported format and build a Session.

    dbc: the DBC file (or files) a CAN log is decoded with; left out, the .dbc
         files beside the log are used.
    channels, progress, cancel: for lazily read formats (MDF) — see read_file.
    See build_session for the other options."""
    return build_session(read_file(path, dbc=dbc, channels=channels, progress=progress, cancel=cancel), **kwargs)


def build_session(raw: RawTable, vehicle: VehicleParams | None = None, mapping: dict | None = None,
                  rate_hz: float | None = None, flip: tuple = (),
                  start_finish_index: int | None = None, units: dict | None = None) -> Session:
    """Normalise a RawTable into a Session.

    mapping: {channel id: source column} to override the automatic mapping
             (channels not named keep their automatic match).
    rate_hz: uniform sample rate for the normalised channels. Default: the
             file's own median rate.
    flip:    channel ids whose sign is opposite to the schema's convention in
             this file (e.g. ("steering", "acc_lat")).
    units:   {source column: unit} to state or correct a column's unit. A unit
             given here is trusted: it replaces the file's units row for that
             column and switches off the engine's own unit checks for it.
    start_finish_index: source row at which the car is on the start/finish
             line, for timing laps from position when the file has no lap
             channel, beacons or lap distance.
    """
    vehicle = vehicle or VehicleParams()
    notes: list = []
    note = lambda level, msg: notes.append((level, msg))
    if raw.format != "CSV":
        note("info", f"Read as {raw.format}.")
    notes += [tuple(n) for n in raw.notes]

    # ── map ────────────────────────────────────────────────────────────────
    mapped = auto_map(raw.data.columns)
    decided_by = {cid: "name match" for cid in mapped}
    for cid, col in raw.hints.items():           # channels the file format itself identifies
        if col in raw.data.columns and cid in CHANNELS:
            mapped = {c: k for c, k in mapped.items() if k != col}
            mapped[cid] = col
            decided_by[cid] = "the file format"
    if mapping:
        validate_mapping(mapping, raw.data.columns)
        mapped = {c: col for c, col in mapped.items() if col not in mapping.values()}
        mapped.update(mapping)
        decided_by.update({cid: "chosen at import" for cid in mapping})
    missing = [c for c in REQUIRED if c not in mapped and not (c == "time" and rate_hz)]
    if missing:
        raise ValueError(
            f"{raw.source or 'file'}: required channel(s) not found: {missing}. "
            f"Columns: {list(raw.data.columns)[:20]}. Pass mapping={{channel: column}} to set them.")

    # ── units ──────────────────────────────────────────────────────────────
    stated = dict(units or {})
    unknown = [c for c in stated if c not in raw.data.columns]
    if unknown:
        raise ValueError(f"units= names column(s) that are not in the file: {unknown}")
    raw.units = {**raw.units, **stated}
    cdf, units, unit_source = _to_canonical(raw, mapped, note)
    unit_source.update({cid: "stated" for cid, col in mapped.items() if col in stated})
    _glitch_check(cdf, raw, mapped, note)
    _speed_spikes(cdf, raw, mapped, note)
    unit_source.update({cid: "plausibility" for cid in _accel_unit_check(cdf, raw, mapped, stated, note)})
    coarse = _resolution_check(cdf, mapped, units, note)
    if "time" not in cdf:
        cdf["time"] = np.arange(len(cdf)) / rate_hz
        units["time"], unit_source["time"] = "s", "assumed"
        note("info", f"No time channel — assumed a uniform {rate_hz:g} Hz.")
    for cid in flip:
        if cid in cdf:
            cdf[cid] = -cdf[cid]
            note("info", f"{cid}: sign flipped to match the schema convention.")

    # ── time base ──────────────────────────────────────────────────────────
    as_logged = cdf            # schema units, the file's own rows: what the raw layer is timed against
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
        # a log on a steady clock is analysed on that clock, sample for sample; only a log with no
        # single rate (jitter, distance-based sampling) is brought to a standard one
        rate_hz = _steady_rate(t) or _standard_rate(1.0 / dt_src)
    note("info", f"{len(cdf):,} samples logged at about {1 / dt_src:,.1f} Hz; "
                 f"normalised to {rate_hz:g} Hz.")
    channels = _resample(cdf, t, rate_hz, lap_table, info["distance"],
                         lap_engine.unwrap_distance(d))
    units["distance"] = units["lap_distance"] = "m"
    units["lap"] = "-"

    lap_table = lap_table.drop(columns=["start_pos", "end_pos"])
    lap_table = _stints(lap_table, channels, rate_hz, note)
    lap_table = _lap_length_check(lap_table, note)
    _sign_check(channels, note)
    derived, derived_notes, rebuilt = compute_derived(channels, rate_hz, vehicle, coarse)
    notes += [("warning" if "cannot be rebuilt" in m else "info", m) for m in derived_notes]

    model = build_model(raw, mapped, units, unit_source, as_logged, cdf, channels, derived, lap_table,
                        notes, rate_hz, coarse, rebuilt, tuple(flip))
    model.mapping = [{"column": str(col), "channel": cid, "basis": decided_by.get(cid, "name match"), "confirmed": False,
                      "unit": raw.units.get(col) or "unknown", "unit_source": unit_source.get(cid, "none"), "problems": []}
                     for cid, col in mapped.items()]
    return Session(raw=raw, channels=channels, derived=derived, laps=lap_table, mapping=mapped,
                   units=units, rate_hz=rate_hz, lap_length=lap_length, vehicle=vehicle,
                   notes=notes, lap_source=source, crossing_method=info["crossing"],
                   coarse=coarse, rebuilt=rebuilt, model=model)


# ── steps ─────────────────────────────────────────────────────────────────────

STANDARD_RATES = (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000)


STEADY_TOLERANCE = 0.01       # sample intervals within this share of their median: a steady clock
STEADY_SHARE = 0.995          # ... for at least this share of the steps (a few gaps do not unsteady it)


def _steady_rate(t: np.ndarray) -> float | None:
    """The file's own rate if it logs on a steady clock, else None."""
    dt = np.diff(t)
    dt = dt[dt > 0]
    if len(dt) < 10:
        return None
    step = float(np.median(dt))
    if np.mean(np.abs(dt - step) <= STEADY_TOLERANCE * step) < STEADY_SHARE:
        return None
    return float(f"{1.0 / step:.6g}")


def _standard_rate(native_hz: float) -> float:
    """The lowest standard logging rate that does not undersample the file
    (logs sampled by distance or with jitter have no single exact rate)."""
    return float(next((r for r in STANDARD_RATES if r >= 0.95 * native_hz), STANDARD_RATES[-1]))


def _unit_key(unit) -> str:
    return (unit or "").strip().lower().replace(" ", "").replace("²", "^2")


FRACTION_SLACK = 1e-3          # a 0–1 fraction stored as a 32-bit float can read 1.00000012 at full scale


def _is_fraction(x) -> bool:
    """True when a pedal-like channel runs 0 to 1 rather than 0 to 100 — allowing
    for the rounding of 32-bit floats, and for a channel that never got above 1 %
    being implausible for a whole session."""
    lo, hi = np.nanmin(x), np.nanmax(x)
    return bool(lo >= -0.01 and 0.0 < hi <= 1.0 + FRACTION_SLACK)


def _to_canonical(raw: RawTable, mapped: dict, note_all) -> tuple[pd.DataFrame, dict, dict]:
    """Mapped channels as float columns in schema units. Every conversion and
    every assumption is recorded as a note. Also returns, per channel, where
    its unit came from: "file" (the file's units row), "plausibility" (worked
    out from the values), "assumed" (nothing to go on but the schema's unit)
    or "none" (the channel has no unit)."""
    df = raw.data
    cdf, units, unit_source = pd.DataFrame(index=df.index), {}, {}
    for cid, col in mapped.items():
        cdef = CHANNELS[cid]
        said = []

        def note(level, msg, said=said):
            said.append(msg)
            note_all(level, msg)
        if cid == "lap":
            cdf[cid] = df[col].to_numpy()
            unit_source[cid] = "none"
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
            if unit != "%" and _is_fraction(x):
                note("info", f"{col} is a 0–1 fraction in the file — converted to %.")
                x = x * 100
        elif q == "brake":
            target = "%"
            if unit in UNIT_FACTORS["pressure"]:
                x, target = x * UNIT_FACTORS["pressure"][unit], "bar"
            elif unit != "%" and _is_fraction(x):
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
        inferred = any("fraction" in m for m in said) or (not unit and any("converted" in m for m in said))
        unit_source[cid] = ("plausibility" if inferred else
                            "file" if unit and not any("not recognised" in m for m in said) else
                            "none" if not q and not unit else "assumed")

    note = note_all
    if "speed" in cdf:
        col = mapped["speed"]
        unit = _unit_key(raw.units.get(col))
        table = UNIT_FACTORS["speed"]
        unit_source["speed"] = "file"
        if unit not in table:
            unit = _speed_unit_from_distance(cdf) if {"time", "distance"} <= set(cdf) else None
            unit_source["speed"] = "plausibility" if unit else "assumed"
            note("info" if unit else "warning", f"{col} has no unit in the file — " + (
                f"its distance over time matches the distance channel as {unit}." if unit
                else "assumed km/h."))
        if unit and table[unit] != 1:
            cdf["speed"] = cdf["speed"] * table[unit]
            note("info", f"{col} converted from {unit} to km/h.")
        units["speed"] = "km/h"
    return cdf, units, unit_source


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


# Coarsest step each channel may be recorded in before analysis suffers
MAX_STEP = {"speed": 0.5, "acc_lat": 0.02, "acc_long": 0.02, "yaw_rate": 0.01}
PIT_SPEED_KMH, PIT_STOP_S = 5.0, 3.0      # stationary this long = a pit stop
SHORT_LAP_FRACTION = 0.004                # a lap this much shorter than the rest is flagged


def _resolution_check(cdf, mapped, units, note) -> dict:
    """Find channels recorded or exported in steps too coarse to analyse directly
    (typically an export with too few decimal places). Returns {channel id: step}."""
    coarse, found = [], {}
    for cid, limit in MAX_STEP.items():
        if cid not in cdf:
            continue
        values = np.unique(cdf[cid].to_numpy(float))
        values = values[np.isfinite(values)]
        if len(values) < 2:
            continue
        step = float(np.min(np.diff(values)))
        if step > limit:
            found[cid] = step
            coarse.append(f"{mapped[cid]} ({step:.3g} {units[cid]}, {len(values)} distinct values)")
    if coarse:
        note("warning", "Recorded in coarse steps: " + "; ".join(coarse) + ". These channels are not "
                        "used directly where a better signal can be rebuilt — see the notes that follow.")
    return found


def _position_sign_check(channels, note) -> None:
    """The way the path turns must agree with steering and lateral g. A
    disagreement means the position plane is mirrored or the other channel's
    sign is opposite to the schema; the engine reports it and changes nothing."""
    if not {"pos_x", "pos_y", "speed", "time"} <= set(channels.columns):
        return
    dt = float(np.median(np.diff(channels["time"].to_numpy())))
    n = max(int(round(0.5 / dt)), 1)                       # position is often logged in coarse time steps
    x = channels["pos_x"].rolling(n, center=True, min_periods=1).mean().to_numpy()
    y = channels["pos_y"].rolling(n, center=True, min_periods=1).mean().to_numpy()
    heading = np.unwrap(np.arctan2(np.gradient(y), np.gradient(x)))
    rate = pd.Series(np.gradient(heading) / dt).rolling(n, center=True, min_periods=1).mean().to_numpy()
    turning = (np.abs(rate) > 0.05) & (np.abs(rate) < 2.0) & (channels["speed"].to_numpy(float) > 40)
    if turning.sum() < 200:
        return
    for cid in ("steering", "acc_lat"):
        if cid not in channels:
            continue
        v = channels[cid].to_numpy(float)[turning]
        if np.nanstd(v) == 0:
            continue
        r = float(np.corrcoef(v, rate[turning])[0, 1])
        if r < -0.3:
            note("warning", f"{cid} and the position channels disagree about left and right (r = {r:.2f}): either "
                            f"{cid} has the opposite sign to the schema, or the position plane is mirrored. The map "
                            "and corner directions follow position as logged. Import with "
                            f"flip=(\"{cid}\",) or flip=(\"pos_y\",) once you know which.")


def _sign_check(channels, note) -> None:
    """Steering, lateral g and yaw rate must agree in sign while cornering.
    The engine does not choose which one is wrong — it reports the conflict."""
    _position_sign_check(channels, note)
    if "acc_lat" not in channels:
        return
    ay = channels["acc_lat"].to_numpy(float)
    corner = np.abs(ay) > 0.3
    if corner.sum() < 100:
        return
    for cid in ("steering", "yaw_rate"):
        if cid not in channels:
            continue
        x = channels[cid].to_numpy(float)[corner]
        if np.nanstd(x) == 0:
            continue
        r = np.corrcoef(x, ay[corner])[0, 1]
        if r < -0.3:
            note("warning", f"{cid} and acc_lat have opposite signs while cornering (r = {r:.2f}). "
                            "One of them does not follow the schema convention (+ve to the left) — "
                            "import with flip=(...) for the channel that is wrong.")


GLITCH_CHANNELS = ("acc_lat", "acc_long", "acc_vert", "yaw_rate")
SPEED_SPIKE_KMH = 40.0        # a speed sample this far from its neighbours on both sides is not the car
GLITCH_FACTOR = 5.0           # this many times beyond the channel's own 99.9th percentile is not a measurement


def _speed_spikes(cdf, raw: RawTable, mapped, note) -> None:
    """Blank isolated speed samples that jump away from their neighbours and
    back — a dropped sample reading zero, a wheel-speed spike. Judged against
    the median of the five samples around each one, so a real change of speed,
    however sharp, is never touched, and neither is a channel logged in coarse
    steps. The blanked samples are bridged when the channel is resampled."""
    if "speed" not in cdf or len(cdf) < 20:
        return
    v = cdf["speed"].to_numpy(float)
    around = pd.Series(v).rolling(5, center=True, min_periods=3).median().to_numpy()
    bad = np.isfinite(v) & np.isfinite(around) & (np.abs(v - around) > SPEED_SPIKE_KMH)
    if bad.any():
        worst = float(np.max(np.abs(v[bad] - around[bad])))
        cdf.loc[bad, "speed"] = np.nan
        note("warning", f"{mapped['speed']}: {int(bad.sum())} sample(s) jump up to {worst:.0f} km/h away from the samples "
                        "either side and straight back. Treated as glitches and left out.")


def _glitch_check(cdf, raw: RawTable, mapped, note) -> None:
    """Blank the few samples that sit far outside everything else a channel
    reads — a crash, a reset to the pits or a logger hiccup can write values
    hundreds of times larger than any real acceleration. Left in, a dozen such
    samples are enough to wreck every fit and correlation made on the channel.
    The blanked samples are bridged when the channel is resampled."""
    for cid in GLITCH_CHANNELS:
        if cid not in cdf:
            continue
        x = cdf[cid].to_numpy(float)
        size = np.abs(x[np.isfinite(x)])
        if len(size) < 1000:
            continue
        usual = float(np.percentile(size, 99.9))
        if usual <= 0:
            continue
        bad = np.abs(x) > GLITCH_FACTOR * usual
        if bad.any():
            # quote the numbers as the file has them: units may not be settled yet
            written = pd.to_numeric(raw.data[mapped[cid]], errors="coerce").to_numpy(float)
            worst = float(np.nanmax(np.abs(written[bad])))
            typical = float(np.nanpercentile(np.abs(written), 99.9))
            cdf.loc[bad, cid] = np.nan
            note("warning", f"{mapped[cid]}: {int(bad.sum())} samples read up to {worst:,.0f} in the file, far outside the "
                            f"rest of the channel (99.9% of it is within {typical:.3g}). Treated as glitches and left out.")


def _accel_unit_check(cdf, raw: RawTable, mapped, stated, note) -> list:
    """Check the longitudinal accelerometer against d(speed)/dt.

    Some exports label accelerations m/s² when the numbers are in g (or the
    reverse). The two differ by a factor of 9.81, which the speed channel
    exposes. When the evidence is clear the conversion is corrected for every
    acceleration channel that carried the same label, and a warning says so.
    Columns whose unit the caller stated are left alone."""
    if not {"time", "speed", "acc_long"} <= set(cdf) or mapped["acc_long"] in stated:
        return []
    t = cdf["time"].to_numpy(float)
    ok = np.isfinite(t) & np.isfinite(cdf["speed"].to_numpy(float)) & np.isfinite(cdf["acc_long"].to_numpy(float))
    if ok.sum() < 500 or np.any(np.diff(t[ok]) < 0):
        return []
    # 10 Hz grid, 1 s moving average: enough to compare trends, immune to noise
    grid = np.arange(t[ok][0], t[ok][-1], 0.1)
    if len(grid) < 300:
        return []
    box = np.ones(10) / 10
    v = np.convolve(np.interp(grid, t[ok], cdf["speed"].to_numpy(float)[ok]) / 3.6, box, mode="same")
    a = np.convolve(np.interp(grid, t[ok], cdf["acc_long"].to_numpy(float)[ok]), box, mode="same")
    dv = np.gradient(v, 0.1) / 9.81                      # g
    use = (v > 15) & (np.abs(dv) > 0.05)
    use[:10] = use[-10:] = False
    if use.sum() < 200 or np.std(a[use]) == 0:
        return []
    r = float(np.corrcoef(dv[use], a[use])[0, 1])
    slope = float(np.polyfit(dv[use], a[use], 1)[0])     # 1 when acc_long really is in g
    if r < 0.8:
        return []
    label = raw.units.get(mapped["acc_long"], "")
    if 0.07 < slope < 0.145:
        factor, was, now = 9.81, "m/s²", "g"
    elif 7.0 < slope < 14.0:
        factor, was, now = 1 / 9.81, "g", "m/s²"
    else:
        return []
    fixed, fixed_ids = [], []
    for cid in ("acc_long", "acc_lat", "acc_vert"):
        col = mapped.get(cid)
        if cid in cdf and col not in stated and raw.units.get(col, "") == label:
            cdf[cid] = cdf[cid] * factor
            fixed.append(col)
            fixed_ids.append(cid)
    note("warning", f"{', '.join(fixed)}: " + (f"labelled {label} in the file" if label else "no unit in the file")
         + f", but the values behave like {now} — {mapped['acc_long']} matches d(speed)/dt only when read as "
           f"{now} (fit slope {slope:.3f} as {was}, r = {r:.2f}). Read as {now}. "
           f"Pass units={{column: unit}} to override.")
    return fixed_ids


def _stints(lap_table, channels, rate_hz, note) -> pd.DataFrame:
    """Find pit stops (car stationary) and number the stints.

    A lap belongs to the stint that is running at its midpoint. Adds `stint`,
    `stint_lap` (laps since the stint began, 0 = first) and marks the lap that
    contains each stop as "pit"."""
    laps = lap_table.copy()
    t = channels["time"].to_numpy()
    stopped = channels["speed"].to_numpy(float) < PIT_SPEED_KMH
    edges = np.flatnonzero(np.diff(np.r_[False, stopped, False].astype(int)))
    stops = [(t[a], t[min(b, len(t) - 1)]) for a, b in zip(edges[::2], edges[1::2])
             if (b - a) / rate_hz >= PIT_STOP_S and t[a] > t[0] + 1.0 and b < len(t) - 1]
    stop_times = np.array([0.5 * (a + b) for a, b in stops])
    mid = 0.5 * (laps["t_start"] + laps["t_end"]).to_numpy()
    laps["stint"] = 1 + np.searchsorted(stop_times, mid)
    laps["stint_lap"] = laps.groupby("stint").cumcount()
    for ts in stop_times:
        hit = (laps["t_start"] <= ts) & (laps["t_end"] > ts)
        laps.loc[hit & laps["status"].isin(["flying", "slow"]), "status"] = "pit"
    if len(stops):
        longest = max(b - a for a, b in stops)
        note("info", f"{len(stops)} pit stop(s) found (longest {longest:.0f} s stationary) — "
                     f"{laps['stint'].nunique()} stints.")
    return laps


def _lap_length_check(laps, note) -> pd.DataFrame:
    """Mark flying laps that are clearly shorter than the rest as "short": on a
    measured distance channel that usually means the lap cut part of the track.
    Short laps do not count as flying, so they cannot become the best lap."""
    flying = laps[laps["status"] == "flying"]
    if len(flying) < 4 or not np.isfinite(flying["dist_m"]).all():
        return laps
    ref = float(flying["dist_m"].median())
    short = flying[flying["dist_m"] < ref * (1 - SHORT_LAP_FRACTION)]
    laps = laps.copy()
    for lap in short.itertuples():
        laps.loc[laps["lap"] == lap.lap, "status"] = "short"
        note("warning", f"Lap {lap.lap} covers {ref - lap.dist_m:.0f} m less than the typical "
                        f"{ref:.0f} m — marked \"short\" (likely a track cut) and left out of best-lap selection.")
    return laps


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
