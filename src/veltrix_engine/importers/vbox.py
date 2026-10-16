"""
Racelogic VBOX (.vbo).

A .vbo is text: bracketed sections naming the channels, then space-separated
data. Three of its columns are in the format's own encoding, which no unit
conversion can express, so they are decoded here:

  time       HHMMSS.SS (UTC)        → seconds from the first sample
  latitude   minutes, north +ve     → degrees
  longitude  minutes, WEST +ve      → degrees, east +ve

Everything else is passed through as written. When the file carries a
start/finish line ([laptiming]), the times the car crossed it are worked out
from the position and handed on as lap beacons.
"""

from __future__ import annotations

import io
import re

import numpy as np
import pandas as pd

from .table import RawTable

EARTH_RADIUS = 6_371_000.0
_SHORT = {"sats": "satellites", "lat": "latitude", "long": "longitude", "velocity": "velocity kmh",
          "vert-vel": "vertical velocity kmh"}
_UNITS = {"velocity kmh": "km/h", "vertical velocity kmh": "km/h", "heading": "deg", "height": "m",
          "latitude": "deg", "longitude": "deg", "time": "s"}
_STANDARD = ("satellites", "time", "latitude", "longitude", "velocity kmh", "heading", "height",
             "vertical velocity kmh")


def _sections(text: str) -> dict:
    out, name = {"": []}, ""
    for line in text.splitlines():
        m = re.match(r"^\s*\[(.+?)\]\s*$", line)
        if m:
            name = m.group(1).strip().lower()
            out[name] = []
            if name == "data":
                break
        elif line.strip() or name == "channel units":     # a blank units line is a channel without a unit
            out[name].append(line.rstrip())
    while out.get("channel units") and not out["channel units"][-1]:
        out["channel units"].pop()
    return out


def _clock_seconds(hhmmss: np.ndarray) -> np.ndarray:
    h = np.floor(hhmmss / 10000)
    m = np.floor((hhmmss - h * 10000) / 100)
    s = h * 3600 + m * 60 + (hhmmss - h * 10000 - m * 100)
    s = s + 86400 * np.cumsum(np.r_[False, np.diff(s) < -43200])     # a log that runs past midnight
    return s - s[0]


def _gate_crossings(t, x, y, gate) -> np.ndarray:
    """Times at which the path (x, y) crosses the segment `gate`, in the
    direction most crossings go."""
    (ax, ay), (bx, by) = gate
    gx, gy = bx - ax, by - ay
    side = gx * (y - ay) - gy * (x - ax)
    i = np.flatnonzero((side[:-1] * side[1:] < 0) & np.isfinite(side[:-1]) & np.isfinite(side[1:]))
    if not len(i):
        return np.array([])
    f = side[i] / (side[i] - side[i + 1])                    # fraction of the step at which the line is met
    px, py = x[i] + f * (x[i + 1] - x[i]), y[i] + f * (y[i + 1] - y[i])
    along = ((px - ax) * gx + (py - ay) * gy) / (gx * gx + gy * gy)
    hit = (along >= -0.5) & (along <= 1.5)                   # allow for a gate drawn narrower than the track
    i, f = i[hit], f[hit]
    if not len(i):
        return np.array([])
    forward = side[i + 1] > 0
    keep = forward if forward.sum() >= (~forward).sum() else ~forward
    return t[i[keep]] + f[keep] * (t[i[keep] + 1] - t[i[keep]])


def parse_vbo(text: str) -> RawTable:
    sec = _sections(text)
    if "data" not in sec or ("column names" not in sec and "header" not in sec):
        raise ValueError("Not a VBOX .vbo file: no [column names] / [data] sections.")
    short = " ".join(sec.get("column names", [])).split()
    header = [h.strip() for h in sec.get("header", [])]
    names = header if header and (not short or len(header) == len(short)) else [_SHORT.get(s, s) for s in short]
    names = [_SHORT.get(n, n) for n in names]

    body = text[text.lower().index("[data]") + len("[data]"):]
    df = pd.read_csv(io.StringIO(body), sep=r"\s+", header=None, engine="c", on_bad_lines="skip")
    df = df.apply(pd.to_numeric, errors="coerce")
    if df.shape[1] != len(names):
        n = min(df.shape[1], len(names))
        df, names = df.iloc[:, :n], names[:n]
    df.columns = names
    df = df.dropna(how="all").reset_index(drop=True)
    if len(df) < 2:
        raise ValueError("The .vbo file has no data rows.")

    notes, units = [], {n: u for n, u in _UNITS.items() if n in df}
    extra = [n for n in names if n not in _STANDARD]
    stated = [u.strip() for u in sec.get("channel units", [])]
    if stated and len(stated) == len(extra):
        units.update({n: u for n, u in zip(extra, stated) if u})
    for n in names:
        if n.endswith(" g") and n not in units:
            units[n] = "g"
    if "time" in df:
        df["time"] = _clock_seconds(df["time"].to_numpy(float))
        notes.append(("info", "time decoded from the VBOX clock (HHMMSS.SS) to seconds from the first sample."))
    if {"latitude", "longitude"} <= set(df.columns):
        df["latitude"] = df["latitude"] / 60.0
        df["longitude"] = -df["longitude"] / 60.0
        notes.append(("info", "latitude and longitude decoded from VBOX minutes (west positive) to degrees (east positive)."))

    info = {}
    m = re.search(r"File created on (\S+)(?:\s+(?:at|@)\s+(\S+))?", text[:300])
    if m:
        info["Log Date"] = m.group(1)
        if m.group(2):
            info["Log Time"] = m.group(2)
    for line in sec.get("comments", []):
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip()] = v.strip()
        elif "Logger" not in info and re.search(r"vbox|racelogic|performancebox|driftbox", line, re.I) \
                and "(c)" not in line.lower():
            info["Logger"] = line.strip()

    beacons = None
    start = next((ln for ln in sec.get("laptiming", []) if ln.lower().startswith("start")), None)
    if start and {"latitude", "longitude", "time"} <= set(df.columns):
        v = [float(x) for x in re.findall(r"[+-]?\d+\.\d+", start)[:4]]
        lat, lon = df["latitude"].to_numpy(float), df["longitude"].to_numpy(float)
        fix = np.isfinite(lat) & np.isfinite(lon) & ((lat != 0) | (lon != 0))
        if len(v) == 4 and fix.sum() > 10:
            lat0, lon0 = np.mean(lat[fix]), np.mean(lon[fix])
            k = np.cos(np.radians(lat0))
            xy = lambda la, lo: (np.radians(lo - lon0) * EARTH_RADIUS * k, np.radians(la - lat0) * EARTH_RADIUS)
            x, y = xy(np.where(fix, lat, np.nan), np.where(fix, lon, np.nan))
            best = None
            # the line is written as two points in minutes; take the reading of the four numbers that lies on the path
            for order in ((1, 0, 3, 2), (0, 1, 2, 3)):
                la1, lo1, la2, lo2 = (v[j] for j in order)
                gate = (xy(la1 / 60, -lo1 / 60), xy(la2 / 60, -lo2 / 60))
                mid = (0.5 * (gate[0][0] + gate[1][0]), 0.5 * (gate[0][1] + gate[1][1]))
                off = float(np.nanmin(np.hypot(x - mid[0], y - mid[1])))
                if best is None or off < best[0]:
                    best = (off, gate)
            if best[0] < 100:
                c = _gate_crossings(df["time"].to_numpy(float), x, y, best[1])
                if len(c):
                    beacons = c
                    notes.append(("info", f"The file's start/finish line was crossed {len(c)} time(s); "
                                          "those crossings are used as lap markers."))
            else:
                notes.append(("warning", f"The file's start/finish line is {best[0]:,.0f} m from the logged path "
                                         "and was not used."))

    hints = {cid: col for cid, col in (("time", "time"), ("speed", "velocity kmh"), ("gps_lat", "latitude"),
                                       ("gps_lon", "longitude")) if col in df}
    return RawTable(df, units, info, beacons, sep=" ", format="VBOX .vbo", notes=notes, hints=hints)
