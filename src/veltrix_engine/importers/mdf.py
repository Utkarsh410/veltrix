"""
ASAM MDF measurement files (.mf4, .mdf, .dat), read through the optional
`asammdf` package.

Two kinds of content are handled:

  channels      already-decoded measurements, each group on its own time base
  bus logging   raw CAN frames (CAN_DataFrame groups, as written by CAN
                loggers), decoded here with a DBC

A file can hold both; the result is one table.

A file on disk is opened lazily: `inspect_mdf` reads only the block structure
(names, units, group sizes, first and last timestamp of each group), and
`parse_mdf` reads only the channels asked for, one at a time, reporting
progress and stopping when told to. Each group keeps its own timestamps.
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np

from . import dbc as dbc_mod
from .dbc import CanFrames
from .table import RawTable, Signal, signals_to_table

_BUS_PREFIXES = ("CAN_DataFrame", "CAN_ErrorFrame", "CAN_RemoteFrame", "CAN_OverloadFrame", "LIN_", "FLX_", "ETH_")


def looks_like_mdf(head: bytes) -> bool:
    return head[:3] == b"MDF" and head[3:8].strip() == b""


def _bus_frames(mdf) -> CanFrames | None:
    parts = []
    for i, group in enumerate(mdf.groups):
        names = {ch.name for ch in group.channels}
        if "CAN_DataFrame.ID" not in names or "CAN_DataFrame.DataBytes" not in names:
            continue
        ids = mdf.get("CAN_DataFrame.ID", group=i)
        if not len(ids.samples):
            continue
        payload = np.atleast_2d(mdf.get("CAN_DataFrame.DataBytes", group=i).samples).astype(np.uint8)
        raw_id = ids.samples.astype(np.uint64)
        ext = (raw_id & np.uint64(dbc_mod.EXT_FLAG)) != 0
        if "CAN_DataFrame.IDE" in names:
            ext |= mdf.get("CAN_DataFrame.IDE", group=i).samples.astype(bool)
        dlc = np.full(len(raw_id), payload.shape[1])
        if "CAN_DataFrame.DataLength" in names:
            dlc = np.minimum(mdf.get("CAN_DataFrame.DataLength", group=i).samples.astype(int), payload.shape[1])
        parts.append((ids.timestamps.astype(float), (raw_id & np.uint64(dbc_mod.ID_MASK)).astype(np.uint32),
                      ext, dlc, payload))
    if not parts:
        return None
    width = max(p[4].shape[1] for p in parts)
    data = np.vstack([np.pad(p[4], ((0, 0), (0, width - p[4].shape[1]))) for p in parts])
    return CanFrames(*(np.concatenate([p[k] for p in parts]) for k in range(4)), data)


class ImportCancelled(ValueError):
    """The caller asked for the import to stop."""


def _open(source):
    try:
        from asammdf import MDF
    except ImportError:
        raise ValueError("Reading MDF / MF4 files needs the asammdf package: "
                         "pip install 'veltrix-engine[mdf]'") from None
    try:
        return MDF(str(source)) if isinstance(source, (str, Path)) else MDF(io.BytesIO(source))
    except Exception as e:                       # asammdf raises many types for a damaged file
        name = Path(source).name if isinstance(source, (str, Path)) else "The MDF file"
        raise ValueError(f"{name} could not be read as MDF: {type(e).__name__}: {e}. The file may be cut short or "
                         "damaged, or not an MDF file at all.") from e


def _measurements(mdf) -> list:
    """(group, index, channel) of every channel that is a measurement: not a
    group's own time channel, not part of a bus-logging frame."""
    out = []
    for g, group in enumerate(mdf.groups):
        for j, ch in enumerate(group.channels):
            if getattr(ch, "channel_type", 0) in (2, 3) or ch.name.startswith(_BUS_PREFIXES):
                continue
            out.append((g, j, ch))
    return out


def inspect_mdf(source) -> dict:
    """What an MDF file holds, without reading its samples: version, groups
    with their sizes and rates, every channel's name and unit, and what the
    importer will not be able to use."""
    mdf = _open(source)
    try:
        groups, channels, unsupported = [], [], []
        for g, group in enumerate(mdf.groups):
            cycles = int(group.channel_group.cycles_nr)
            names = {ch.name for ch in group.channels}
            bus = any(n.startswith(_BUS_PREFIXES) for n in names)
            t0 = t1 = rate = None
            if cycles:
                try:
                    t0 = float(mdf.get_master(g, record_offset=0, record_count=1)[0])
                    t1 = float(mdf.get_master(g, record_offset=cycles - 1, record_count=1)[0])
                    rate = (cycles - 1) / (t1 - t0) if cycles > 1 and t1 > t0 else None
                except Exception as e:
                    unsupported.append(f"group {g}: its time channel could not be read ({type(e).__name__})")
            groups.append({"group": g, "samples": cycles, "t_start": t0, "t_end": t1,
                           "rate_hz": None if rate is None else round(rate, 4), "bus_logging": bus,
                           "comment": str(getattr(group.channel_group, "comment", "") or "")})
        for g, j, ch in _measurements(mdf):
            channels.append({"name": ch.name, "unit": str(ch.unit or ""), "group": g, "index": j,
                             "samples": groups[g]["samples"], "rate_hz": groups[g]["rate_hz"],
                             "comment": str(getattr(ch, "comment", "") or "")})
        for g in groups:
            if g["bus_logging"]:
                kinds = sorted({ch.name.split(".")[0] for ch in mdf.groups[g["group"]].channels if ch.name.startswith(_BUS_PREFIXES)})
                not_can = [k for k in kinds if not k.startswith("CAN_")]
                if not_can:
                    unsupported.append(f"group {g['group']}: {', '.join(not_can)} bus logging is not decoded (CAN is, with a DBC)")
        timed = [g for g in groups if g["t_start"] is not None]
        start = getattr(mdf.header, "start_time", None)
        return {"format": "MDF", "version": str(getattr(mdf, "version", "")), "groups": groups, "channels": channels,
                "channel_count": len(channels), "group_count": len(groups),
                "rates_hz": sorted({g["rate_hz"] for g in groups if g["rate_hz"]}),
                "duration_s": (max(g["t_end"] for g in timed) - min(g["t_start"] for g in timed)) if timed else None,
                "start_time": start.isoformat() if start is not None else None,
                "missing_units": [c["name"] for c in channels if not c["unit"]],
                "raw_can_frames": sum(g["samples"] for g in groups if g["bus_logging"]),
                "unsupported": unsupported}
    finally:
        mdf.close()


def parse_mdf(source, db=None, channels=None, progress=None, cancel=None) -> RawTable:
    """Read an MDF file (bytes, or a path — a path is read lazily).

    channels: names to read; the others are left on disk. Default: all.
    progress: called as progress(done, total) after each channel.
    cancel:   called between channels; returning True stops the import."""
    mdf = _open(source)
    try:
        wanted = None if channels is None else set(channels)
        todo = [(g, j, ch) for g, j, ch in _measurements(mdf) if wanted is None or ch.name in wanted]
        signals, notes, skipped = [], [], []
        for n, (g, j, ch) in enumerate(todo):
            if cancel is not None and cancel():
                raise ImportCancelled("The import was cancelled.")
            try:
                sig = mdf.get(group=g, index=j)
            except Exception as e:
                skipped.append(f"{ch.name} ({type(e).__name__})")
                continue
            y = np.asarray(sig.samples)
            if y.ndim == 1 and y.dtype.kind in "biuf" and len(y) >= 2:
                signals.append(Signal(sig.name, np.asarray(sig.timestamps, float), y, sig.unit or ""))
            elif y.ndim != 1 or y.dtype.kind not in "biuf":
                skipped.append(f"{ch.name} (not a numeric signal)")
            if progress is not None:
                progress(n + 1, len(todo))
        if wanted is not None:
            absent = sorted(wanted - {ch.name for _, _, ch in todo})
            if absent:
                notes.append(("warning", "Not in the file: " + ", ".join(absent) + "."))
            left = len(_measurements(mdf)) - len(todo)
            if left:
                notes.append(("info", f"{len(todo)} channel(s) were read; {left} were left on disk."))
        if skipped:
            notes.append(("warning", f"{len(skipped)} channel(s) could not be used and were left out: "
                                     + ", ".join(skipped[:8]) + (" …" if len(skipped) > 8 else "") + "."))

        frames = _bus_frames(mdf)
        if frames is not None:
            if db is None:
                message = (f"The file holds {len(frames.t):,} raw CAN frames. They need a DBC to be decoded: "
                           "pass dbc=..., or put the .dbc file beside the log.")
                if not signals:
                    raise ValueError(message)
                notes.append(("warning", message + " Only the channels already decoded in the file were read."))
            else:
                decoded, decode_notes = dbc_mod.decode(frames, db)
                signals += decoded
                notes += decode_notes

        info = {}
        start = getattr(mdf.header, "start_time", None)
        if start is not None:
            info["Log Date"], info["Log Time"] = start.strftime("%d/%m/%Y"), start.strftime("%H:%M:%S")
        for key, attr in (("Driver", "author"), ("Vehicle", "subject"), ("Event", "project"), ("Department", "department")):
            value = getattr(mdf.header, attr, "")
            if isinstance(value, str) and value.strip():
                info[key] = value.strip()
        version = getattr(mdf, "version", "")
    finally:
        mdf.close()
    return signals_to_table(signals, f"MDF {version}".strip(), info=info, notes=notes)
