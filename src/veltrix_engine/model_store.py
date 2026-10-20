"""
The canonical model on disk: metadata in one JSON file, arrays in Parquet.

  model/manifest.json        every entity and every channel's metadata and provenance
  model/group_<n>.parquet    the arrays, one file per set of channels sharing timestamps

Channels that share a timestamp array are stored together with that array
once, so a multi-rate log becomes a few files, each at its own rate, and
nothing is resampled on the way to disk. Raw channels are not stored: the
source file is kept byte-for-byte beside the model and is their storage; the
manifest keeps their metadata. The analysis view is likewise not repeated
here (the store keeps it as channels.parquet).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from .model import (MODEL_VERSION, Annotation, Channel, Event, Lap, Provenance, SessionModel, SourceFile, Stint)

MANIFEST = "manifest.json"


def save_model(model: SessionModel, folder: str | Path) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    manifest = model.manifest()
    groups: dict = {}                              # digest of a timestamp array -> (file name, timestamps, [channels])
    where = {}
    for c in model.channels():
        if c.layer == "raw":
            continue
        key = hashlib.sha1(np.ascontiguousarray(c.timestamps, float).tobytes()).hexdigest()     # same clock, same file
        if key not in groups:
            groups[key] = (f"group_{len(groups)}.parquet", c.timestamps, [])
        groups[key][2].append(c)
        where[c.ref] = groups[key][0]
    import pyarrow as pa
    import pyarrow.parquet as pq
    for name, timestamps, chans in groups.values():
        # Arrow columns straight from the arrays: going through a DataFrame first copied every channel.
        # NaN is written as missing, as pandas writes it.
        columns = {"time": pa.array(np.asarray(timestamps, float), from_pandas=True)}
        for c in chans:
            values = c.values
            columns[c.ref] = (pa.array(values.astype(str), type=pa.large_string()) if values.dtype == object
                              else pa.array(values, from_pandas=True))
            if c.valid is not None:
                columns[f"valid|{c.ref}"] = pa.array(c.valid)
        pq.write_table(pa.table(columns), folder / name, compression="zstd")
    for entry in manifest["channels"]:
        ref = f"{entry['layer']}:{entry['id']}"
        entry["file"] = where.get(ref)             # None: a raw channel, read again from the source file
    (folder / MANIFEST).write_text(json.dumps(manifest, default=_plain), encoding="utf-8")
    return folder


def _plain(o):
    return o.item() if hasattr(o, "item") else str(o)


def read_manifest(folder: str | Path) -> dict:
    from .versions import upgrade
    manifest = json.loads((Path(folder) / MANIFEST).read_text(encoding="utf-8"))
    return upgrade("session", manifest, MODEL_VERSION, key="model_version")


def load_model(folder: str | Path, channels: list | None = None) -> SessionModel:
    """Read a stored model. `channels` limits which arrays are read (ids or
    "layer:id"); every channel's metadata is in `manifest_channels` either way.
    Raw channels come back as metadata only."""
    folder = Path(folder)
    manifest = read_manifest(folder)
    model = SessionModel([SourceFile(**s) for s in manifest["sources"]])
    model.laps = [Lap(**x) for x in manifest["laps"]]
    model.stints = [Stint(**x) for x in manifest["stints"]]
    model.events = [Event(**{**e, "channels": tuple(e["channels"])}) for e in manifest["events"]]
    model.annotations = [Annotation(**a) for a in manifest["annotations"]]
    model.notes = [tuple(n) for n in manifest["notes"]]
    model.mapping = [dict(m) for m in manifest.get("mapping", [])]
    model.manifest_channels = manifest["channels"]

    wanted = None if channels is None else set(channels)
    by_file: dict = {}
    for entry in manifest["channels"]:
        ref = f"{entry['layer']}:{entry['id']}"
        if entry.get("file") and (wanted is None or ref in wanted or entry["id"] in wanted):
            by_file.setdefault(entry["file"], []).append(entry)
    for name, entries in by_file.items():
        refs = [f"{e['layer']}:{e['id']}" for e in entries]
        import pyarrow.parquet as pq
        stored = pq.ParquetFile(folder / name)
        present = set(stored.schema_arrow.names)
        # column by column: reading the file into one frame held every array twice while it was converted
        column = lambda ref: stored.read(columns=[ref]).column(0).to_numpy(zero_copy_only=False)
        t = column("time")
        for e, ref in zip(entries, refs):
            prov = e["provenance"]
            values = column(ref)
            if e["id"] == "lap" and values.dtype == object:
                values = np.array([int(v) if v.lstrip("-").isdigit() else v for v in values], dtype=object)
            model.add(Channel(
                id=e["id"], layer=e["layer"], label=e["label"], timestamps=t, values=values, unit=e["unit"],
                name=e["name"], source_name=e["source_name"], description=e["description"], category=e["category"],
                quantity=e["quantity"], discrete=e["discrete"],
                valid=column(f"valid|{ref}") if f"valid|{ref}" in present else None,
                formula=e["formula"], dependencies=tuple(e["dependencies"]), quality=list(e["quality"]),
                provenance=Provenance(tuple(prov["inputs"]), prov["algorithm"], prov["parameters"], prov["unit_source"],
                                      prov["engine_version"], tuple(prov["notes"])),
                version=e["version"], user=e["user"]))
    return model
