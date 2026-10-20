"""
The importer test kit: what an importer must do before the engine relies on
it, checked the same way for every importer.

    from veltrix_engine.importers.testkit import assert_importer
    def test_my_importer():
        assert_importer(IMPORTER, [Path("tests/data/short_session.mlg")])

`check_importer` returns a KitResult: `problems` (each one fails the kit) and
`remarks` (worth knowing, not failures). It checks that

  1. the importer can be registered (plugins.validate);
  2. each sample is detected as this format, through the engine's own
     detection, and small CSV, text and binary files of other kinds are not
     claimed by it;
  3. each sample reads through `read_file` into a table that keeps the
     contract (plugins.check_table), the same twice over;
  4. each sample builds an analysed session: a time base, a rate and laps;
  5. damaged copies of each sample (empty, cut short, garbage after the
     first bytes) either read or fail with a FileFormatError — never another
     exception — within TIME_LIMIT_S.

`expect` adds what a sample must give, per sample file name:
    {"short_session.mlg": {"rows": 12000, "laps": 3, "channels": ["speed", "throttle"]}}
"""

from __future__ import annotations

import random
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..errors import FileFormatError
from . import plugins

TIME_LIMIT_S = 30.0          # a damaged file must be refused, not read for ever


@dataclass
class KitResult:
    importer: str
    problems: list = field(default_factory=list)
    remarks: list = field(default_factory=list)
    checked: list = field(default_factory=list)     # what was run, in order

    @property
    def ok(self) -> bool:
        return not self.problems

    def text(self) -> str:
        lines = [f"Importer test kit — {self.importer}: " + ("passed" if self.ok else f"{len(self.problems)} problem(s)")]
        lines += [f"  ✗ {p}" for p in self.problems] + [f"  · {r}" for r in self.remarks]
        lines += [f"  ✓ {c}" for c in self.checked]
        return "\n".join(lines)


@contextmanager
def _registered(importer):
    """The importer registered for the kit's run, and the registry as it was afterwards."""
    before = plugins._registered.get(importer.key)
    plugins.register(importer, replace=True)
    try:
        yield
    finally:
        if before is None:
            plugins.unregister(importer.key)
        else:
            plugins._registered[importer.key] = before


def check_importer(importer, samples, expect: dict | None = None) -> KitResult:
    from . import FORMATS, detect_format, read_file
    from ..session import build_session

    result = KitResult(getattr(importer, "key", repr(importer)))
    problem, remark, done = result.problems.append, result.remarks.append, result.checked.append
    try:
        plugins.validate(importer, {e for f in FORMATS for e in f.extensions} | {".gz", ".ldx"})
    except ValueError as e:
        problem(f"cannot be registered: {e}")
        return result
    done("can be registered")
    samples = [Path(s) for s in samples]
    if not samples:
        problem("no sample files given: the kit needs at least one real file of the format")
        return result
    missing = [s.name for s in samples if not s.is_file()]
    if missing:
        problem("sample files not found: " + ", ".join(missing))
        return result
    expect = expect or {}

    with _registered(importer), tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # ── detection ─────────────────────────────────────────────────────
        for s in samples:
            kind = detect_format(s.name, s.read_bytes()[:65536])
            if kind != f"plugin:{importer.key}":
                problem(f"{s.name} is detected as '{kind}', not as this importer's")
        for name, content in _other_formats().items():
            (tmp / name).write_bytes(content)
            try:
                kind = detect_format(name, content[:65536])
            except ValueError:
                kind = "refused"
            if kind == f"plugin:{importer.key}":
                problem(f"claims a file that is not its own: {name} (its sniff is too loose)")
        done("detects its samples and only its own files")

        # ── read, contract, determinism, session ──────────────────────────
        for s in samples:
            try:
                first, second = read_file(s), read_file(s)
            except Exception as e:
                problem(f"{s.name} does not read: {type(e).__name__}: {e}")
                continue
            if not (first.data.equals(second.data) and first.units == second.units and first.info == second.info):
                problem(f"{s.name} reads differently the second time")
            if not first.units:
                remark(f"{s.name}: no units given — the engine will assume schema units and say so")
            try:
                session = build_session(first)
            except Exception as e:
                problem(f"{s.name} reads, but no session can be built from it: {type(e).__name__}: {e}")
                continue
            if not session.rate_hz > 0 or len(session.channels) < 10:
                problem(f"{s.name}: the session has no usable time base")
            if len(session.laps) == 0:
                problem(f"{s.name}: the session has no laps")
            want = expect.get(s.name, {})
            if "rows" in want and len(first.data) != want["rows"]:
                problem(f"{s.name}: {len(first.data)} rows, expected {want['rows']}")
            if "laps" in want and len(session.laps) != want["laps"]:
                problem(f"{s.name}: {len(session.laps)} laps, expected {want['laps']}")
            lacking = [c for c in want.get("channels", ()) if c not in session.mapping]
            if lacking:
                problem(f"{s.name}: channels not mapped: {', '.join(lacking)}")
            unmapped = [c for c in first.data.columns if c not in session.mapping.values()]
            if unmapped and len(unmapped) == len(first.data.columns) - 1:
                remark(f"{s.name}: only the time column was mapped — consider `hints` for the channels the format defines")
        done("reads each sample to a valid table, the same each time, and builds a session with laps")

        # ── damaged files ─────────────────────────────────────────────────
        for s in samples:
            for label, content in _damaged(s.read_bytes()).items():
                bad = tmp / f"{label}{s.suffix}"
                bad.write_bytes(content)
                started = time.perf_counter()
                try:
                    read_file(bad)
                except FileFormatError:
                    pass
                except Exception as e:
                    problem(f"{s.name} {label}: raised {type(e).__name__} instead of a FileFormatError: {e}")
                took = time.perf_counter() - started
                if took > TIME_LIMIT_S:
                    problem(f"{s.name} {label}: took {took:.0f} s to deal with")
        done("refuses damaged files with a reason")
    return result


def assert_importer(importer, samples, expect: dict | None = None) -> KitResult:
    """check_importer, failing with every problem listed (for pytest)."""
    result = check_importer(importer, samples, expect)
    assert result.ok, result.text()
    return result


def _damaged(content: bytes) -> dict:
    rng = random.Random(len(content))
    keep = content[:64]
    garbage = bytes(rng.getrandbits(8) for _ in range(min(max(len(content) - 64, 256), 200_000)))
    return {"empty": b"", "first 100 bytes": content[:100], "cut in half": content[: len(content) // 2],
            "garbage after the header": keep + garbage, "header only": keep}


def _other_formats() -> dict:
    """Small files of formats the engine reads itself, which no plug-in should claim."""
    t = np.arange(0, 2, 0.1)
    rows = "\n".join(f"{x:.1f},{50 + x:.1f},{x * 10:.0f}" for x in t)
    return {"plain.csv": f"Time,Speed,Throttle\ns,km/h,%\n{rows}\n".encode(),
            "semicolon.txt": ("Time;Speed\n" + "\n".join(f"{x:.1f};{x:.1f}" for x in t)).encode(),
            "notes.csv": b"lap,comment\n1,out lap\n2,push\n",
            "binary.bin": bytes(range(256)) * 4}

