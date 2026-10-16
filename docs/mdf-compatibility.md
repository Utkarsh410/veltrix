# MDF / MF4 compatibility (Phase 4)

How MDF files are read, what was evaluated, and what is and is not supported.

## Library: asammdf

| Question | Finding |
|---|---|
| Version evaluated | 8.8.27 |
| MDF versions it reads | 2.00, 2.10, 2.14, 3.00, 3.10, 3.20, 3.30, 4.00, 4.10, 4.11, 4.20, 4.30 |
| Licence | LGPL-3.0-or-later. Used as an unmodified, separately installed library, which the licence allows; a packaged app must keep it replaceable (a normal Python environment inside the bundle does) |
| Python | 3.10 and later; this project runs 3.12 |
| Apple Silicon | arm64 wheel; its compiled part (`cutils.abi3.so`) is a native arm64 binary |
| Dependencies | numpy ≥ 2.0, pandas ≥ 2.2.2, numexpr, lxml, lz4, zstd, chardet, canmatrix, python-dateutil. No conflict with this project's numpy 2.5, pandas 3.0, scipy or pyarrow; the full suite passes with it installed |
| Memory | opens lazily from a path: only block structure is read until a channel is asked for (measured below) |
| Alternative considered | writing a reader: rejected. MDF 4 has compressed and chained data blocks, variable-length signals, invalidation bits and conversion rules; asammdf already handles them and is the reference open-source implementation |

One wart: when asammdf fails to open a damaged file it also prints an
"exception ignored in `MDF4.__del__`" line. The import still fails cleanly with
this engine's own message; the extra line is asammdf's.

## Pipeline

```
validate   first bytes say "MDF"; a file asammdf cannot open → "could not be read as MDF: … cut short or damaged"
metadata   inspect_file(path): version, groups, sizes, rates, channel names and units — no samples read
discover   every channel that is a measurement (not a group's time channel, not a bus-logging field)
timestamps each group keeps its own; first and last stamp give its rate and duration
units      taken from the file; a channel without one is "unknown" (listed in the diagnostics)
mapping    mapping_profiles.decide(): metadata > verified profile > plausibility > user > unmapped
model      one Signal per channel on its own clock → canonical model (native rates kept)
analysis   the same engine as every other format; nothing is duplicated for MDF
```

`read_file(path, channels=[...], progress=fn, cancel=fn)` reads only the
channels named, one at a time, calls `progress(done, total)` after each, and
stops with `ImportCancelled` when `cancel()` returns true. The file handle is
closed whether the import finishes, fails or is cancelled.

## Supported

| Content | State |
|---|---|
| MDF 4.10 written by asammdf, numeric channels in several groups at different rates | tested (`tests/test_formats.py`) |
| Raw CAN bus logging (`CAN_DataFrame` groups) decoded with a DBC | tested |
| Selective loading, progress, cancel, handle cleanup | tested |
| Truncated or damaged file | tested: fails with a message naming the file |
| MDF 2.x / 3.x, MDF 4.11–4.30 | **not tested** — asammdf reads them; no file of those versions has been through this engine |
| Files written by a real logger (Vector, ETAS, CSS, MoTeC export …) | **not tested** — none was available |

## Not supported

- Text, byte-array and array (matrix) channels: skipped and listed in the import notes.
- LIN, FlexRay and Ethernet bus logging: reported by `inspect_file` as not decoded.
- Events, attachments and embedded DBCs inside the MDF.
- Invalidation bits are applied by asammdf; the engine does not expose them separately.
- Lap markers: MDF has no standard for them. Laps come from a lap channel in
  the file, if there is one.

## Measured

Apple M5, 16 GB, macOS 27.0.1, Python 3.12. The file is the reference session
(79 minutes, 951,694 samples) rewritten as MF4 4.10 by asammdf: 56 channels in
three groups at 200, 50 and 10 Hz, compressed (5 MB — the source is a
simulator export in whole numbers, which compresses far better than real
sensor data would).

| Step | Time | Peak memory of the process |
|---|---|---|
| `inspect_file` (structure only) | 0.10 s | 191 MB (the Python libraries themselves) |
| Read five channels | 0.53 s | 503 MB |
| Full import to an analysed session, all 56 channels | 4.5 s | 2.6 GB |

The full import is slower than the CSV's 2.4 s because the three groups are
laid on one 200 Hz table for the mapping step. The model keeps the 50 Hz group
at 50 Hz (`model.describe()["rates_hz"]` reports 50 and 200 for the mapped
channels). No lap was found in this file, correctly: the CSV's beacon markers
live in its header block, which an MF4 has no place for, and the file has no
lap channel.

**Limit of this benchmark:** the file was generated, not logged. It measures
this engine's path through asammdf at realistic size, not compatibility with
any logger's files.
