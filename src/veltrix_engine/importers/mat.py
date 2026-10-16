"""
MATLAB data files (.mat).

A .mat file has no telemetry layout of its own, so the reader looks for the
shapes telemetry is usually saved in:

  one struct per channel   Speed.Time, Speed.Value, Speed.Units — the MATLAB
                           export of logger software; each channel on its own clock
  Simulink logging         a struct with `time` and `signals.values`
  a table of vectors       variables (or fields of one struct) that are all
                           the same length, one of them time
  a matrix with names      an n × m matrix beside a list of m channel names

Version 7.3 files (HDF5) need the optional `h5py` package.
"""

from __future__ import annotations

import io

import numpy as np
import pandas as pd

from .table import RawTable, Signal, signals_to_table

_TIME = ("time", "t", "timestamps", "timestamp", "time_s", "sessiontime", "session_time", "tout")
_VALUE = ("value", "values", "data", "signal", "y")
_UNIT = ("units", "unit")
_NAMES = ("names", "name", "header", "headers", "channels", "channelnames", "channel_names", "labels", "columns")
MAX_DEPTH = 6


def looks_like_mat(head: bytes) -> bool:
    return head[:6] == b"MATLAB"


def _load_v5(data: bytes) -> dict:
    from scipy.io import loadmat
    try:
        tree = loadmat(io.BytesIO(data), simplify_cells=True)
    except Exception as e:
        raise ValueError(f"The .mat file could not be read: {e}") from e
    return {k: v for k, v in tree.items() if not k.startswith("__")}


def _load_v73(data: bytes) -> dict:
    try:
        import h5py
    except ImportError:
        raise ValueError("This is a version 7.3 .mat file, which needs the h5py package: "
                         "pip install 'veltrix-engine[mat]'  (or save it from MATLAB with -v7).") from None

    def walk(node, depth=0):
        out = {}
        for key, item in node.items():
            if key.startswith("#") or depth > MAX_DEPTH:
                continue
            if isinstance(item, h5py.Group):
                out[key] = walk(item, depth + 1)
            elif item.dtype.kind in "biuf":
                value = np.squeeze(item[()].T)
                if item.attrs.get("MATLAB_class") == b"char":
                    value = "".join(map(chr, np.atleast_1d(value).astype(int)))
                out[key] = value
        return out

    with h5py.File(io.BytesIO(data), "r") as f:
        return walk(f)


def _vector(x):
    """x as a 1-D numeric array of more than one sample, or None."""
    if not isinstance(x, np.ndarray) or x.dtype.kind not in "biuf":
        return None
    x = np.squeeze(x)
    return x if x.ndim == 1 and len(x) > 1 else None


def _get(d: dict, keys):
    lower = {str(k).lower(): k for k in d}
    for k in keys:
        if k in lower:
            return d[lower[k]]
    return None


def _clock(x):
    v = _vector(x)
    return v.astype(float) if v is not None and np.all(np.diff(v) >= 0) and v[-1] > v[0] else None


def _collect(node, name, signals, tables, depth=0):
    if depth > MAX_DEPTH:
        return
    if isinstance(node, (list, tuple)) or (isinstance(node, np.ndarray) and node.dtype == object):
        for i, item in enumerate(np.ravel(np.asarray(node, dtype=object))):
            label = _get(item, ("name", "label", "blockname")) if isinstance(item, dict) else None
            _collect(item, label if isinstance(label, str) and label else f"{name}_{i + 1}", signals, tables, depth + 1)
        return
    if not isinstance(node, dict):
        return

    t, unit = _clock(_get(node, _TIME)), _get(node, _UNIT)
    unit = unit if isinstance(unit, str) else ""
    if t is not None:
        y = _vector(_get(node, _VALUE))
        if y is not None and len(y) == len(t):
            signals.append(Signal(name, t, y, unit))                  # one struct per channel
            return
        logged = _get(node, ("signals",))
        if logged is not None:                                        # Simulink "structure with time"
            for i, item in enumerate(logged if isinstance(logged, (list, np.ndarray)) else [logged]):
                values = _get(item, ("values",)) if isinstance(item, dict) else None
                if not isinstance(values, np.ndarray) or values.dtype.kind not in "biuf" or len(values) != len(t):
                    continue
                label = _get(item, ("label", "blockname", "name"))
                label = label if isinstance(label, str) and label else f"{name}_{i + 1}"
                columns = values.reshape(len(t), -1)
                for j in range(columns.shape[1]):
                    signals.append(Signal(label if columns.shape[1] == 1 else f"{label}_{j + 1}", t, columns[:, j]))
            return

    vectors = {str(k): _vector(v) for k, v in node.items()}
    vectors = {k: v for k, v in vectors.items() if v is not None}
    if vectors:
        length = pd.Series([len(v) for v in vectors.values()]).mode().iloc[0]
        same = {k: v for k, v in vectors.items() if len(v) == length}
        if len(same) >= 2:
            units = _get(node, _UNIT)
            tables.append((name, same, units if isinstance(units, dict) else {}))

    names = _get(node, _NAMES)
    if isinstance(names, (list, np.ndarray)) and all(isinstance(n, str) for n in np.ravel(names)):
        names = [n.strip() for n in np.ravel(names)]
        for key, value in node.items():
            if isinstance(value, np.ndarray) and value.dtype.kind in "biuf" and value.ndim == 2:
                m = value if value.shape[1] == len(names) else value.T if value.shape[0] == len(names) else None
                if m is not None and len(m) > 1:
                    tables.append((str(key), dict(zip(names, m.T)), {}))

    for key, value in node.items():
        if isinstance(value, (dict, list, tuple)) or (isinstance(value, np.ndarray) and value.dtype == object):
            _collect(value, str(key), signals, tables, depth + 1)


def parse_mat(data: bytes) -> RawTable:
    tree = _load_v73(data) if b"MATLAB 7.3" in data[:32] else _load_v5(data)
    signals, tables = [], []
    _collect(tree, "", signals, tables)

    if signals:
        # vectors saved beside a clock in the same struct ride on that clock
        for _, columns, units in tables:
            key = next((k for k in columns if k.lower() in _TIME), None)
            t = _clock(columns[key]) if key else None
            if t is not None:
                signals += [Signal(k, t, v, str(units.get(k, ""))) for k, v in columns.items() if k != key]
        return signals_to_table(signals, "MATLAB .mat", zero_time=False,
                                notes=[("info", f"{len(signals)} channels read from per-channel structs.")])
    if not tables:
        top = ", ".join(list(tree)[:12])
        raise ValueError("No telemetry was found in the .mat file: it has no channel structs (Time / Value) and no "
                         f"set of equal-length vectors. Variables: {top or 'none'}.")
    name, columns, units = max(tables, key=lambda tb: len(tb[1]) * len(next(iter(tb[1].values()))))
    df = pd.DataFrame({k: np.asarray(v, float) for k, v in columns.items()})
    where = f"the struct '{name}'" if name else "the top-level variables"
    notes = [("info", f"{df.shape[1]} channels of {len(df):,} samples read from {where}.")]
    clock = next((c for c in df.columns if c.lower() in _TIME), None)
    if clock is None:
        notes.append(("warning", "No time vector was found among them — give the sample rate when importing."))
    return RawTable(df, {k: str(u) for k, u in units.items() if k in df and isinstance(u, str)},
                    format="MATLAB .mat", notes=notes, hints={"time": clock} if clock else {})
