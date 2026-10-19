"""
A session written out for other tools: CSV, Parquet or MF4 (ASAM MDF 4).

What is written is the analysis view: every normalised and derived channel on
the session's clock, in schema units, with `time`, `lap` and `lap_distance`.
The format comes from the file's extension. Each export names its units — a
second header row in CSV (the layout the CSV importer reads back), column
metadata in Parquet, channel units in MF4 — and a CSV or MF4 export imports
back to the same laps.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

FORMATS = {".csv": "CSV", ".parquet": "Parquet", ".mf4": "MF4"}


def _frame(session, channels=None) -> tuple[pd.DataFrame, dict]:
    frame = session.frame()
    keep = ["time", "lap", "lap_distance"] + [c for c in frame.columns if c not in ("time", "lap", "lap_distance")]
    if channels:
        missing = sorted(set(channels) - set(frame.columns))
        if missing:
            raise ValueError(f"Not in this session: {', '.join(missing)}.")
        keep = ["time", "lap", "lap_distance"] + [c for c in channels if c not in ("time", "lap", "lap_distance")]
    keep = [c for c in dict.fromkeys(keep) if c in frame.columns]
    out = frame[keep].copy()
    units = {c: session.units.get(c) or (session.model.get(c).unit if session.model and session.model.has(c) else "")
             for c in keep}
    units["time"], units["lap"] = "s", "-"
    return out, units


def export_session(session, path: str | Path, channels: list | None = None) -> Path:
    """Write the session to `path`; the extension (.csv, .parquet, .mf4) picks the format."""
    path = Path(path)
    kind = FORMATS.get(path.suffix.lower())
    if kind is None:
        raise ValueError(f"Export to {', '.join(FORMATS)} — not '{path.suffix or path.name}'.")
    frame, units = _frame(session, channels)
    if kind == "CSV":
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(",".join(frame.columns) + "\n")
            f.write(",".join(str(units.get(c) or "") for c in frame.columns) + "\n")
            frame.to_csv(f, header=False, index=False, float_format="%.6g", na_rep="")
    elif kind == "Parquet":
        import pyarrow as pa
        import pyarrow.parquet as pq
        table = pa.Table.from_pandas(frame, preserve_index=False)
        fields = [f.with_metadata({"unit": str(units.get(f.name) or "")}) for f in table.schema]
        meta = {"veltrix_source": session.raw.source, "veltrix_sha256": session.raw.sha256,
                "veltrix_rate_hz": str(session.rate_hz)}
        table = table.cast(pa.schema(fields, metadata=meta))
        pq.write_table(table, path)
    else:
        try:
            import asammdf
        except ImportError:
            raise RuntimeError("An MF4 export needs asammdf (pip install 'veltrix-engine[mdf]').") from None
        t = frame["time"].to_numpy(float)
        signals = [asammdf.Signal(frame[c].to_numpy(float), t, name=c, unit=str(units.get(c) or ""))
                   for c in frame.columns if c != "time" and np.issubdtype(frame[c].dtype, np.number)]
        mdf = asammdf.MDF(version="4.10")
        mdf.append(signals, comment=f"Veltrix export of {session.raw.source}")
        mdf.save(path, overwrite=True)
        mdf.close()
    return path
