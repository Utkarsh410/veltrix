"""
MoTeC log files (.ld), with lap beacons from the .ldx file beside them.

This is also how Assetto Corsa telemetry arrives: Assetto Corsa Competizione
writes .ld / .ldx pairs itself, and Assetto Corsa does through the ACTI plugin.

The .ld layout is not published; this reader follows the layout worked out by
the open-source `ldparser` project: a fixed header, then a linked list of
channel records, each pointing at a block of 16- or 32-bit samples logged at
the channel's own rate. Channels in an encoding the reader does not know are
left out and reported.
"""

from __future__ import annotations

import re
import struct
import xml.etree.ElementTree as ET

import numpy as np

from .table import RawTable, Signal, signals_to_table

LD_MARKER = 0x40
_HEAD = struct.Struct("<I4xII20xI24xHHHI8sHHI4x16s16x16s16x64s64s64x64s64x1024xI66x64s126x")
_EVENT = struct.Struct("<64s64s1024sH")
_VENUE = struct.Struct("<64s1034xH")
_VEHICLE = struct.Struct("<64s128xI4s32s32s")
_CHANNEL = struct.Struct("<IIIIHHHHhhhh32s8s12s")

_INT, _FLOAT = (0x00, 0x03, 0x05), (0x07,)
_DTYPES = {("int", 2): "<i2", ("int", 4): "<i4", ("float", 2): "<f2", ("float", 4): "<f4"}


def _text(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("latin-1", errors="replace").strip()


def looks_like_ld(head: bytes) -> bool:
    return len(head) >= _HEAD.size and struct.unpack_from("<I", head)[0] == LD_MARKER


def parse_ld(data: bytes, ldx: bytes | None = None) -> RawTable:
    if not looks_like_ld(data):
        raise ValueError("Not a MoTeC .ld file (the file does not start with the .ld marker).")
    (_, meta_ptr, _data_ptr, event_ptr, _, _, _, _serial, device, _, _, _n,
     date, clock, driver, vehicle, venue, _, comment) = _HEAD.unpack_from(data)

    info = {k: v for k, v in (("Venue", _text(venue)), ("Vehicle", _text(vehicle)), ("Driver", _text(driver)),
                              ("Log Date", _text(date)), ("Log Time", _text(clock)),
                              ("Device", _text(device)), ("Comment", _text(comment))) if v}
    try:
        if event_ptr:
            name, session, _long, venue_ptr = _EVENT.unpack_from(data, event_ptr)
            info.update({k: v for k, v in (("Event", _text(name)), ("Session", _text(session))) if v})
            if venue_ptr:
                vname, vehicle_ptr = _VENUE.unpack_from(data, venue_ptr)
                info.setdefault("Venue", _text(vname))
                if vehicle_ptr:
                    info.setdefault("Vehicle", _text(_VEHICLE.unpack_from(data, vehicle_ptr)[0]))
    except struct.error:
        pass
    info = {k: v for k, v in info.items() if v}

    signals, skipped, seen = [], [], set()
    ptr = meta_ptr
    while ptr and ptr not in seen and ptr + _CHANNEL.size <= len(data):
        seen.add(ptr)
        (_prev, nxt, data_ptr, n, _, kind, size, freq, shift, mul, scale, dec,
         name, _short, unit) = _CHANNEL.unpack_from(data, ptr)
        ptr = nxt
        name = _text(name)
        dtype = _DTYPES.get(("float" if kind in _FLOAT else "int" if kind in _INT else "", size))
        if dtype is None or not freq or not scale:
            skipped.append(name)
            continue
        n = min(n, (len(data) - data_ptr) // size)
        if n < 2:
            continue
        raw = np.frombuffer(data, dtype, n, data_ptr).astype(float)
        y = (raw / scale * 10.0 ** -dec + shift) * mul
        signals.append(Signal(name, np.arange(n) / float(freq), y, _text(unit)))
    if not signals:
        raise ValueError("The .ld file has no readable channels.")

    notes = []
    if skipped:
        notes.append(("warning", f"{len(skipped)} channel(s) are stored in an encoding this reader does not know "
                                 f"and were left out: {', '.join(skipped[:10])}{' …' if len(skipped) > 10 else ''}."))
    duration = max(s.t[-1] for s in signals)
    beacons = None
    if ldx:
        beacons = _ldx_beacons(ldx, duration, info)
        notes.append(("info", f"{len(beacons)} lap beacon(s) read from the .ldx file." if beacons is not None
                      else "The .ldx file has no usable lap beacons."))
    else:
        notes.append(("info", "No .ldx file beside the .ld, so there are no lap beacons from it. Laps then come from "
                              "a lap timer or lap channel in the log, if it has one."))
    return signals_to_table(signals, "MoTeC .ld", info=info, beacons=beacons, notes=notes, zero_time=False)


def _ldx_beacons(ldx: bytes, duration: float, info: dict):
    """Beacon times [s] from an .ldx file. Marker times are in microseconds."""
    try:
        root = ET.fromstring(ldx.decode("utf-8-sig", errors="ignore").strip())
    except ET.ParseError:
        return None
    for el in root.iter("String"):
        if el.get("Id") in ("Total Laps", "Fastest Time", "Fastest Lap") and el.get("Value"):
            info[el.get("Id")] = el.get("Value")
    times = []
    for group in root.iter("MarkerGroup"):
        if "beacon" not in (group.get("Name") or "").lower():
            continue
        for marker in group.iter("Marker"):
            try:
                times.append(float(marker.get("Time")) / 1e6)
            except (TypeError, ValueError):
                pass
    times = np.array(sorted(t for t in times if 0 < t < duration))
    return times if len(times) else None
