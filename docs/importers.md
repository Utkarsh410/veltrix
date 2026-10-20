# Writing an importer

A file format the engine does not read can be added from outside the engine:
a Python package that declares an importer, plus tests that run the importer
test kit. No change to the engine is needed. The interface is in
`src/veltrix_engine/importers/plugins.py` (interface version 1) and the kit
is in `src/veltrix_engine/importers/testkit.py`. A complete, working example is
`examples/importer_plugin/`.

## 1. The importer

```python
from pathlib import Path
from veltrix_engine.importers import Importer, RawTable

def sniff(name: str, head: bytes) -> bool:          # first 64 KB of the file
    return head.startswith(b"MLOG")

def read(path: Path) -> RawTable:
    ...                                             # parse the file
    return RawTable(data, units=units, info={"Venue": venue}, hints={"speed": "vCar"})

IMPORTER = Importer(key="mylogger", name="MyLogger", extensions=(".mlg",),
                    read=read, sniff=sniff, version="1.0.0")
```

| Field | |
|---|---|
| `key` | Short unique id: 2–32 lower-case letters, digits or `_`. It can't be a built-in format's key. |
| `name` | What users see in `veltrix formats`, the report and the import notes. |
| `extensions` | Lower case, with the dot. |
| `read(path)` | The file → a `RawTable`. Raise `ValueError` with a reason when the file is damaged. |
| `sniff(name, head)` | Optional. It is **required** when an extension is one the engine already reads (`.csv`, `.txt`, `.dat`…). Make it strict: it decides which files are yours. |
| `version` | Your importer's version. It is recorded in every session it reads. |
| `api` | The interface version you wrote against. Leave it at the default. |

### What `read` must return

The engine checks this on every read (`check_table`). It refuses a table that breaks
any rule and names each one:

- `data`: a pandas DataFrame with one row per sample and at least one row and one
  column. Column names must be unique, non-empty strings: the file's own channel
  names.
- A time column in seconds, named `Time` (or named in `hints` as `"time"`), when the file has a clock.
- `units`: `{column: unit}`, using the file's own unit text. The engine converts
  units and says so in the import notes; don't convert them yourself.
- `hints`: `{channel id: column}` for channels your format identifies itself. The
  ids are in `docs/CHANNEL_SCHEMA.md` (`speed`, `throttle`, `brake`, `steering`,
  `acc_lat`, …). Without hints, columns are matched by name.
- `beacons`: lap-marker times in seconds, if the file records them.
- `signals`: `{column: Signal}` when every channel has its own clock. Lay them on
  one time base with `veltrix_engine.importers.signals_to_table`.
- `info` (`Venue`, `Vehicle`, `Driver`, `Log Date`, `Log Time`), `notes`
  (`[(level, message)]`), `vehicle` and `setup` are optional and are passed through.
- Don't set `source` or `sha256`: the engine records the file's name and hash itself.

Decode only what is your format's own encoding: a clock written as HHMMSS, scaled
integers, coordinates in minutes. Units, signs, laps and resampling are the
engine's job.

### Errors

A file that can't be read should raise `ValueError("what is wrong")`. The engine
turns it into a `FileFormatError` that names the file and your importer. Anything
else your code raises (an `IndexError` on a truncated file, say) is turned into
one too, with the details in the log. Either way, the user never sees a
traceback. The kit still checks damaged files, so that a crash is found before
users find it.

## 2. Registering it

From an installed package, declare an entry point in its `pyproject.toml`:

```toml
[project.entry-points."veltrix.importers"]
mylogger = "veltrix_mylogger:IMPORTER"
```

The entry point can name an `Importer`, a list of them, or a function that returns
either. The engine loads them the first time it reads a file. A plug-in that fails
to load is left out: `veltrix importer list` and `veltrix formats` show why, and
the built-in formats keep working.

For one script, `veltrix_engine.importers.register(IMPORTER)` works too. A
registration made in code is not seen by `veltrix batch --jobs N` worker
processes; installed entry points are.

Importers are code, so they are only loaded from installed packages, never from a
data folder. `VELTRIX_NO_PLUGINS=1` leaves every plug-in out, which helps tell a
plug-in's problem from the engine's.

Detection order: first the formats that sign their files (MDF, MAT, BLF, AiM,
iRacing), then plug-ins (your `sniff`, or your extension if you have no sniff),
then the engine's guesses from names and text.

## 3. The test kit

Give the kit at least one real file of the format, a short session with a few
laps:

```python
from pathlib import Path
from veltrix_engine.importers.testkit import assert_importer
from veltrix_mylogger import IMPORTER

def test_mylogger_passes_the_kit():
    assert_importer(IMPORTER, [Path("tests/data/short_session.mlg")],
                    expect={"short_session.mlg": {"laps": 4, "channels": ["speed", "throttle", "brake"]}})
```

Or from the command line:

```bash
veltrix importer check veltrix_mylogger:IMPORTER tests/data/short_session.mlg
```

The kit checks that:

1. The importer can be registered.
2. Each sample is detected as your format by the engine's own detection, and
   small CSV, text and binary files of other kinds are not claimed.
3. Each sample reads through `read_file` into a table that keeps the contract, and
   reads the same way twice.
4. Each sample builds an analysed session with a time base and laps.
5. Damaged copies (empty, cut short, garbage after the header) are refused with a
   `FileFormatError` within 30 s, never with another exception.

`expect` adds what each sample must give: `rows`, `laps`, and `channels` that must
be mapped. Remarks (no units, only time mapped) are advice, not failures.

## The example

`examples/importer_plugin/` reads the made-up "Example Logger" `.elg` format: a
signature, a JSON header with the rate, channels, units and scales, then int16
rows. Its logger-specific channel names (`vCar`, `gLat`…) are mapped through
`hints`. It imports nothing from the engine except `Importer` and `RawTable`.
Its own test runs the kit, and the engine's test suite runs the same kit on it
(`tests/test_importer_plugins.py`).

```bash
pip install -e examples/importer_plugin
veltrix importer list                      # example_logger  Example Logger 1.0.0  .elg
```
