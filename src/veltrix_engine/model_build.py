"""
Builds the canonical model (model.py) from what the import pipeline works out.

  raw          every source column, untouched, on the logger's own timestamps
  normalised   every mapped channel in schema units, on its own timestamps
  corrected    channels the engine rebuilt (lateral g from damper roll)
  calculated   the engine's derived channels, on the analysis view's clock

The uniform frame the analysis runs on is recorded as the view "analysis",
with the lookup policy used for each channel.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .importers import RawTable
from .model import (CALCULATED, MEASURED, RECONSTRUCTED, UNKNOWN_UNIT, Channel, Provenance, SessionModel, SourceFile,
                    View, laps_from_table)
from .schema import CHANNELS

ENGINE_VERSION = "0.1.0"
GLITCH_FACTOR = 5.0           # same rule as session._glitch_check, applied to a signal on its own clock
_FLAG = {"assumed": "unit assumed — the file gives none", "plausibility": "unit worked out from the values, not stated in the file"}


def _linear_map(src: np.ndarray, out: np.ndarray) -> tuple[float, float] | None:
    """The scale and offset that take a source column to its normalised
    values, read off the two columns themselves."""
    ok = np.isfinite(src) & np.isfinite(out)
    if ok.sum() < 1:
        return None
    a, b = src[ok], out[ok]
    lo, hi = int(np.argmin(a)), int(np.argmax(a))
    if a[hi] == a[lo]:
        return 1.0, float(b[lo] - a[lo])
    scale = float((b[hi] - b[lo]) / (a[hi] - a[lo]))
    return scale, float(b[lo] - scale * a[lo])


def build_model(raw: RawTable, mapped: dict, units: dict, unit_source: dict, as_logged: pd.DataFrame,
                clean: pd.DataFrame, view: pd.DataFrame, derived: pd.DataFrame, lap_table: pd.DataFrame,
                notes: list, rate_hz: float, coarse: dict, rebuilt: dict, flipped: tuple = ()) -> SessionModel:
    """as_logged: mapped channels in schema units on the file's own rows.
    clean: the same after unusable, repeated and out-of-order rows were dealt with.
    view / derived: the uniform analysis frame and the channels computed on it."""
    model = SessionModel([SourceFile(raw.source, raw.sha256, raw.format, {k: str(v) for k, v in raw.info.items()},
                                     int(raw.data.shape[1]), int(len(raw.data)))])
    model.notes = [tuple(n) for n in notes]
    native = raw.signals or {}

    # ── raw: the file's columns, as they are ───────────────────────────────
    t_logged = as_logged["time"].to_numpy(float)
    for col in raw.data.columns:
        sig = native.get(col)
        values = sig.y if sig is not None else raw.data[col].to_numpy()
        numeric = values.dtype.kind in "biuf"
        model.add(Channel(
            id=str(col), layer="raw", source_name=str(col), label=MEASURED,
            timestamps=sig.t if sig is not None else t_logged, values=values,
            unit=raw.units.get(col) or UNKNOWN_UNIT, discrete=not numeric or bool(sig is not None and sig.step),
            provenance=Provenance(inputs=(raw.source,), algorithm=f"read as {raw.format}",
                                  unit_source="file" if raw.units.get(col) else "none", engine_version=ENGINE_VERSION)))

    # ── normalised: schema id, unit and sign; still the logger's timestamps ─
    t_clean = clean["time"].to_numpy(float)
    reordered = len(clean) != len(as_logged) or not np.array_equal(t_clean, t_logged)
    for cid, col in mapped.items():
        cdef = CHANNELS[cid]
        sig = native.get(col)
        src = pd.to_numeric(raw.data[col], errors="coerce").to_numpy(float) if cid != "lap" else None
        fit = _linear_map(src, as_logged[cid].to_numpy(float)) if src is not None and cid in as_logged else None
        params, flags, said = {}, [], []
        if fit and fit != (1.0, 0.0):
            params["scale"], params["offset"] = fit
        if cid in flipped:
            params["sign_flipped"] = True
        if sig is not None and cid != "time":
            ts = sig.t
            values = sig.y.astype(float) * (fit[0] if fit else 1.0) + (fit[1] if fit else 0.0) if cid != "lap" else sig.y
            if cid != "lap" and len(values) >= 1000:
                size = np.abs(values[np.isfinite(values)])
                usual = float(np.percentile(size, 99.9)) if len(size) else 0.0
                bad = np.abs(values) > GLITCH_FACTOR * usual if usual > 0 and cid in ("acc_lat", "acc_long", "acc_vert", "yaw_rate") else None
                if bad is not None and bad.any():
                    values = np.where(bad, np.nan, values)
                    flags.append(f"{int(bad.sum())} glitch samples blanked")
        elif cid in clean:
            ts, values = t_clean, clean[cid].to_numpy()
            if reordered:
                said.append("rows without a usable time were dropped; repeated and out-of-order timestamps were resolved")
            blanked = int(np.isnan(values.astype(float)).sum() - np.isnan(as_logged[cid].to_numpy(float)).sum()) \
                if cid != "lap" and not reordered else 0
            if blanked > 0:
                flags.append(f"{blanked} glitch samples blanked")
        else:
            continue
        if cid in coarse:
            flags.append(f"recorded in coarse steps of {coarse[cid]:g} {units.get(cid, '')}".rstrip())
        how = unit_source.get(cid, "none")
        if how in _FLAG:
            flags.append(_FLAG[how])
        if cid == "time" and sig is None and native:
            said.append("the clock of the table the importer laid multi-rate channels on")
        model.add(Channel(
            id=cid, layer="normalised", label=MEASURED, timestamps=ts, values=values,
            unit=units.get(cid) or cdef.unit or UNKNOWN_UNIT,
            name=cdef.description, source_name=str(col), description=cdef.description, quantity=cdef.quantity,
            category=_category(cid), discrete=cdef.discrete, quality=flags,
            provenance=Provenance(inputs=(f"raw:{col}",), algorithm="unit and sign brought to the schema",
                                  parameters=params, unit_source=how, engine_version=ENGINE_VERSION, notes=tuple(said))))

    # ── corrected and calculated: on the analysis view's clock ─────────────
    t_view = view["time"].to_numpy(float)
    # When the file logs on a steady clock the analysis view is that clock, sample for sample, and what is
    # computed on it is computed on the logger's own timestamps. Otherwise it is a resampling, and says so.
    own_clock = not native and len(t_view) == len(t_clean) and np.allclose(t_view, t_clean, rtol=0, atol=1e-6)
    on_view = {"timebase": "the logger's own clock" if own_clock else "analysis view (resampled)", "rate_hz": float(rate_hz)}
    for col in derived.columns:
        cdef = CHANNELS[col]
        is_rebuilt = col == "acc_lat_rebuilt"
        deps = tuple(f"normalised:{r}" for r in cdef.requires if model.has(r, "normalised"))
        flags = ["stands in for a channel recorded too coarsely to use"] if col in rebuilt and not is_rebuilt else []
        model.add(Channel(
            id=col, layer="corrected" if is_rebuilt else "calculated",
            label=RECONSTRUCTED if is_rebuilt else CALCULATED, timestamps=t_view, values=derived[col].to_numpy(),
            unit=cdef.unit, name=cdef.description, description=cdef.description, category=_category(col),
            formula=cdef.description, dependencies=deps, quality=flags,
            provenance=Provenance(inputs=deps, algorithm=cdef.description,
                                  parameters={**on_view, **{k: v for k, v in rebuilt.get("acc_lat" if is_rebuilt else col, {}).items()
                                                            if isinstance(v, (int, float, str, bool))}},
                                  unit_source="schema", engine_version=ENGINE_VERSION)))
    for col, formula, deps in (("lap_distance", "distance from the start/finish line, scaled to the common lap length",
                                ("normalised:distance",) if "distance" in mapped else ("normalised:speed",)),
                               ("distance", "∫ speed dt over the logged timestamps", ("normalised:speed",))):
        if col in view and not (col == "distance" and "distance" in mapped):
            model.add(Channel(id=col, layer="calculated", label=CALCULATED, timestamps=t_view, values=view[col].to_numpy(),
                              unit="m", name=CHANNELS[col].description, description=CHANNELS[col].description,
                              category="lap", formula=formula, dependencies=deps,
                              provenance=Provenance(inputs=deps, algorithm=formula, parameters=dict(on_view),
                                                    unit_source="schema", engine_version=ENGINE_VERSION)))

    for col in view.columns:
        if col in CHANNELS and not model.has(col) and col not in ("time", "lap"):
            deps = tuple(f"normalised:{c}" for c in ("gps_lat", "gps_lon") if model.has(c, "normalised"))
            model.add(Channel(id=col, layer="calculated", label=CALCULATED, timestamps=t_view, values=view[col].to_numpy(),
                              unit=CHANNELS[col].unit, name=CHANNELS[col].description, description=CHANNELS[col].description,
                              category=_category(col), formula="flat projection of GPS latitude and longitude about their mean",
                              dependencies=deps,
                              provenance=Provenance(inputs=deps, algorithm="equirectangular projection", parameters=dict(on_view),
                                                    unit_source="schema", engine_version=ENGINE_VERSION)))

    policy = {c: ("hold" if c in CHANNELS and CHANNELS[c].discrete else "linear") for c in view.columns if c != "time"}
    model.views["analysis"] = View(
        "analysis", float(rate_hz), policy, view,
        ("The logger's own samples: the file logs on a steady clock, so nothing was resampled. " if own_clock else "")
        + "Normalised channels on one uniform clock for lap, corner and balance analysis: continuous channels "
        "linearly interpolated over their valid samples, discrete channels held; lap and lap_distance rebuilt "
        "from the lap table so they are exact at the line.")
    model.laps, model.stints = laps_from_table(lap_table)
    return model


def _category(cid: str) -> str:
    if cid.startswith(("tyre_", "brake_temp")):
        return "tyres and brakes"
    if cid.startswith(("susp_", "wheel_speed", "llt")):
        return "chassis"
    if cid in ("time", "lap", "distance", "lap_distance", "pos_x", "pos_y", "gps_lat", "gps_lon"):
        return "lap and position"
    if cid in ("throttle", "brake", "steering", "gear", "steering_rate"):
        return "driver"
    if cid in ("rpm", "tc_active", "abs_active"):
        return "powertrain and aids"
    return "vehicle dynamics"
