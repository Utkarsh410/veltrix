"""
An importer written against Veltrix's importer interface, outside the engine.

It reads the ".elg" files of an imaginary logger, the "Example Logger": a
binary format typical of real ones —

    b"ELOG" + version byte 1
    uint32 little-endian: length of the JSON header that follows
    JSON header: {"rate_hz": 100, "venue": ..., "vehicle": ..., "driver": ...,
                  "beacons": [lap-marker times, s],
                  "channels": [{"name": "vCar", "unit": "km/h", "scale": 0.01}, ...]}
    rows of int16 little-endian, one value per channel; value = raw × scale

There is no time column in the rows: the clock is the sample rate. Channel
names are the logger's own (vCar, gLat…), so the importer says which schema
channel each one is through `hints`.

Only the public interface is used: Importer and RawTable from
veltrix_engine.importers. Installed, the entry point in pyproject.toml makes
the engine read .elg files with it; `write` makes sample files for its tests.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np
import pandas as pd

from veltrix_engine.importers import Importer, RawTable

MAGIC = b"ELOG\x01"
__version__ = "1.0.0"

# the logger's channel names -> the engine's channel ids
HINTS = {"vCar": "speed", "nEngine": "rpm", "nGear": "gear", "rThrottle": "throttle", "pBrake": "brake",
         "aSteer": "steering", "gLat": "acc_lat", "gLong": "acc_long"}


def sniff(name: str, head: bytes) -> bool:
    return head.startswith(MAGIC)


def read(path: Path) -> RawTable:
    content = Path(path).read_bytes()
    if not content.startswith(MAGIC):
        raise ValueError("not an Example Logger file (no ELOG signature)")
    if len(content) < 9:
        raise ValueError("the file ends inside its header")
    (size,) = struct.unpack_from("<I", content, 5)
    if 9 + size > len(content):
        raise ValueError(f"the header says it is {size} bytes long, but the file ends before that")
    try:
        header = json.loads(content[9:9 + size].decode("utf-8"))
        channels = header["channels"]
        rate = float(header["rate_hz"])
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as e:
        raise ValueError(f"the header is damaged ({e})") from None
    if not channels or not rate > 0:
        raise ValueError("the header names no channels or no sample rate")
    body = content[9 + size:]
    width = 2 * len(channels)
    rows = len(body) // width
    if rows == 0:
        raise ValueError("the file holds no samples")
    values = np.frombuffer(body[: rows * width], dtype="<i2").reshape(rows, len(channels))
    data = {"Time": np.arange(rows) / rate}
    units = {"Time": "s"}
    for k, ch in enumerate(channels):
        data[ch["name"]] = values[:, k] * float(ch.get("scale", 1.0))
        if ch.get("unit"):
            units[ch["name"]] = ch["unit"]
    notes = []
    if len(body) % width:
        notes.append(("warning", f"The last {len(body) % width} bytes are not a whole row and were left out "
                                 "(the recording was cut short)."))
    beacons = np.asarray(header.get("beacons", []), float)
    return RawTable(
        pd.DataFrame(data), units=units, format="Example Logger", notes=notes,
        info={k: v for k, v in (("Venue", header.get("venue")), ("Vehicle", header.get("vehicle")),
                                ("Driver", header.get("driver"))) if v},
        beacons=beacons if len(beacons) else None,
        hints={cid: col for col, cid in HINTS.items() if col in data})


IMPORTER = Importer(key="example_logger", name="Example Logger", extensions=(".elg",), read=read, sniff=sniff,
                    version=__version__, remark="an example of the importer interface")


def write(path, frame: pd.DataFrame, columns: dict, rate_hz: float, beacons=(), **info) -> Path:
    """Write an .elg file: columns {logger name: (source column, unit, scale)}."""
    header = {"rate_hz": rate_hz, "beacons": [float(b) for b in beacons], **info,
              "channels": [{"name": n, "unit": u, "scale": s} for n, (_, u, s) in columns.items()]}
    raw = np.column_stack([np.clip(np.round(frame[c].to_numpy(float) / s), -32768, 32767)
                           for c, _, s in columns.values()]).astype("<i2")
    blob = json.dumps(header).encode()
    path = Path(path)
    path.write_bytes(MAGIC + struct.pack("<I", len(blob)) + blob + raw.tobytes())
    return path
