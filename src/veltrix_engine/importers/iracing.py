"""
iRacing telemetry (.ibt).

An .ibt file is the iRacing SDK's memory layout written to disk: a header, a
table describing every variable, the session information as YAML text, then one
fixed-size record per tick. Every variable is logged on every tick, so the file
is already a table.

iRacing labels throttle, brake, clutch and its other fractions "%" although
the values run 0 to 1, and stores them as 32-bit floats, so a pedal held
flat reads 1.00000012. They are scaled to 0-100 here, where the format is
known, rather than left for the engine to recognise from the values.
Array variables (one value per car, for example) are left out.
"""

from __future__ import annotations

import re
import struct

import numpy as np
import pandas as pd

from .table import RawTable

_HEADER = struct.Struct("<10i")           # ver status tickRate infoUpdate infoLen infoOffset numVars varHeaderOffset numBuf bufLen
_VAR_BUF_OFFSET = 48                      # first of four {tickCount, bufOffset, pad, pad}
_DISK_HEADER = struct.Struct("<qddii")    # startDate startTime endTime lapCount recordCount, at byte 112
_VAR = struct.Struct("<iii?3x32s64s32s")
_TYPES = {0: "S1", 1: "?", 2: "<i4", 3: "<u4", 4: "<f4", 5: "<f8"}

# tyre carcass temperature across the tread: L / M / R as seen from behind the car
_ZONES = {"lf": ("outer", "mid", "inner"), "lr": ("outer", "mid", "inner"),
          "rf": ("inner", "mid", "outer"), "rr": ("inner", "mid", "outer")}
_CORNER = {"lf": "fl", "rf": "fr", "lr": "rl", "rr": "rr"}


def _text(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("latin-1", errors="replace").strip()


def looks_like_ibt(head: bytes) -> bool:
    if len(head) < 144:
        return False
    ver, _, tick, _, info_len, info_off, n_vars, var_off, n_buf, buf_len = _HEADER.unpack_from(head)
    return (ver in (1, 2) and 1 <= tick <= 1000 and 0 < n_vars < 5000 and var_off == 144
            and 0 < buf_len < 1_000_000 and 1 <= n_buf <= 4 and info_off >= 144 and info_len >= 0)


def _yaml_value(yaml: str, key: str, after: str = "") -> str:
    start = yaml.find(after) if after else 0
    m = re.search(rf"^\s*-?\s*{key}:[ \t]*(.*)$", yaml[max(start, 0):], re.M)
    return m.group(1).strip().strip("'\"") if m else ""


def parse_ibt(data: bytes) -> RawTable:
    if not looks_like_ibt(data[:144]):
        raise ValueError("Not an iRacing .ibt file.")
    _, _, tick, _, info_len, info_off, n_vars, var_off, _, buf_len = _HEADER.unpack_from(data)
    buf_off = struct.unpack_from("<i", data, _VAR_BUF_OFFSET + 4)[0]
    start_date = _DISK_HEADER.unpack_from(data, 112)[0]

    fields, units, arrays, fractions = [], {}, 0, []
    for i in range(n_vars):
        kind, offset, count, _, name, _desc, unit = _VAR.unpack_from(data, var_off + i * _VAR.size)
        name, unit = _text(name), _text(unit)
        if count != 1 or kind not in _TYPES or kind == 0:
            arrays += 1
            continue
        fields.append((name, _TYPES[kind], offset))
        if unit == "%" and kind in (4, 5):
            fractions.append(name)
        if unit:
            units[name] = unit
    n = (len(data) - buf_off) // buf_len
    if n < 2 or not fields:
        raise ValueError("The .ibt file has no telemetry records.")
    dtype = np.dtype({"names": [f[0] for f in fields], "formats": [f[1] for f in fields],
                      "offsets": [f[2] for f in fields], "itemsize": buf_len})
    rec = np.frombuffer(data, dtype, n, buf_off)
    df = pd.DataFrame({name: rec[name].astype(float) for name, _, _ in fields})
    for name in fractions:                # 0-1 in the file, "%" on the label: make the label true
        df[name] = df[name] * 100.0

    yaml = data[info_off:info_off + info_len].decode("latin-1", errors="replace")
    info = {}
    venue = _yaml_value(yaml, "TrackDisplayName") or _yaml_value(yaml, "TrackName")
    config = _yaml_value(yaml, "TrackConfigName")
    if venue:
        info["Venue"] = f"{venue} {config}".strip() if config and config.lower() != "null" else venue
    idx = _yaml_value(yaml, "DriverCarIdx")
    if idx:
        me = yaml.find(f"CarIdx: {idx}\n", max(yaml.find("Drivers:"), 0))
        if me >= 0:
            info["Driver"] = _yaml_value(yaml[me:], "UserName")
            info["Vehicle"] = _yaml_value(yaml[me:], "CarScreenName")
    session = _yaml_value(yaml, "SessionType", "Sessions:")
    if session:
        info["Session"] = session
    if start_date > 0:
        stamp = pd.Timestamp(start_date, unit="s")
        info["Log Date"], info["Log Time"] = stamp.strftime("%d/%m/%Y"), stamp.strftime("%H:%M:%S")
    info = {k: v for k, v in info.items() if v}

    hints = {cid: col for cid, col in (("time", "SessionTime"), ("distance", "LapDist"), ("lap", "Lap"),
                                       ("gps_lat", "Lat"), ("gps_lon", "Lon")) if col in df}
    for wheel, cid in _CORNER.items():
        for tag, zone in zip(("CL", "CM", "CR"), _ZONES[wheel]):
            if f"{wheel.upper()}temp{tag}" in df:
                hints[f"tyre_temp_{cid}_{zone}"] = f"{wheel.upper()}temp{tag}"
        for cid_base, col in ((f"wheel_speed_{cid}", f"{wheel.upper()}speed"),
                              (f"susp_travel_{cid}", f"{wheel.upper()}shockDefl"),
                              (f"tyre_press_{cid}", f"{wheel.upper()}pressure")):
            if col in df:
                hints[cid_base] = col

    notes = [("info", f"{n_vars - arrays} variables at {tick} Hz" + (f"; {arrays} array or text variable(s) left out." if arrays else "."))]
    if fractions:
        shown = ", ".join(n for n in ("Throttle", "Brake", "Clutch") if n in fractions) or fractions[0]
        notes.append(("info", f"{len(fractions)} variable(s) iRacing labels % but stores as 0–1 ({shown}, …) were "
                              "scaled to 0–100."))
    if "IsOnTrack" in df and not df["IsOnTrack"].all():
        off = 1 - float(df["IsOnTrack"].mean())
        notes.append(("info", f"The car is off track (garage or replay) for {off:.0%} of the file."))
    return RawTable(df, units, info, format="iRacing .ibt", notes=notes, hints=hints)
