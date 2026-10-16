"""
AiM logger files (.xrk, .xrz, .drk).

AiM does not publish the format. These files are read through the open-source
`libxrk` package, which is optional: without it the importer says how to get
it, and RaceStudio's CSV export is read by the CSV importer either way.

Each channel keeps its own rate; lap boundaries come from the logger's own lap
table and are handed on as beacons.
"""

from __future__ import annotations

import numpy as np

from .table import RawTable, Signal, signals_to_table

_INFO_KEYS = (("Venue", ("venue", "track")), ("Vehicle", ("vehicle", "car")), ("Driver", ("driver", "racer")),
              ("Log Date", ("log date", "date")), ("Log Time", ("log time", "time")),
              ("Session", ("session",)), ("Event", ("championship", "series", "event")))


def looks_like_xrk(head: bytes) -> bool:
    return head[:2] == b"<h" and head[2:5].isalpha()


def parse_xrk(data: bytes) -> RawTable:
    try:
        from libxrk import ChannelMetadata, aim_xrk
    except ImportError:
        raise ValueError("Reading AiM .xrk / .xrz / .drk files needs the libxrk package: "
                         "pip install 'veltrix-engine[aim]'  (or export the session from RaceStudio as CSV).") from None
    try:
        log = aim_xrk(data)
    except Exception as e:                       # the parser raises its own error types
        raise ValueError(f"The AiM file could not be read: {e}") from e

    signals = []
    for name, table in log.channels.items():
        if table.num_rows < 2 or name not in table.column_names:
            continue
        y = table.column(name).to_numpy()
        if y.dtype.kind not in "biuf":
            continue
        meta = ChannelMetadata.from_channel_table(table)
        t = table.column("timecodes").to_numpy().astype(float) / 1000.0
        signals.append(Signal(name, t, y, meta.units, None if meta.interpolate else True))

    info = {}
    for key, value in (log.metadata or {}).items():
        text = str(value).strip()
        if not text or isinstance(value, (dict, list, tuple, bytes)):
            continue
        target = next((k for k, words in _INFO_KEYS if any(w == str(key).lower() or w in str(key).lower().split()
                                                           for w in words)), None)
        info.setdefault(target or str(key), text)

    beacons, notes = None, []
    laps = log.laps
    if laps is not None and laps.num_rows > 1:
        starts = np.sort(laps.column("start_time").to_numpy().astype(float)) / 1000.0
        beacons = starts[1:]
        notes.append(("info", f"{laps.num_rows} laps in the logger's own lap table; its lap starts are used as lap markers."))
    return signals_to_table(signals, "AiM .xrk", info=info, beacons=beacons, notes=notes)
