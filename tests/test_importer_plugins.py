"""
The importer interface (importers/plugins.py) and the importer test kit
(importers/testkit.py). The example importer in examples/importer_plugin is
written outside the engine against the public interface only; passing the kit
unchanged is phase 15's "done when".
"""

import gzip
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from veltrix_engine import load_session, read_file
from veltrix_engine.errors import FileFormatError, VersionError
from veltrix_engine.importers import (FORMATS, Importer, RawTable, detect_format, plugins, register,
                                      supported_extensions, supported_formats)
from veltrix_engine.importers.testkit import check_importer
from veltrix_engine.synth import SAMPLE_RATE_HZ, generate_session

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "importer_plugin"
sys.path.insert(0, str(EXAMPLE))
import veltrix_example_logger as elg  # noqa: E402

COLUMNS = {"vCar": ("Speed", "km/h", 0.01), "rThrottle": ("ThrottlePos", "%", 0.01),
           "pBrake": ("BrakePress", "bar", 0.01), "aSteer": ("SteeringAngle", "deg", 0.01),
           "gLat": ("LateralAcc", "g", 0.001), "gLong": ("LongAcc", "g", 0.001)}


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    """Every test starts with no plug-ins and no installed entry points."""
    monkeypatch.setattr(plugins, "_registered", {})
    monkeypatch.setattr(plugins, "_failed", {})
    monkeypatch.setattr(plugins, "_loaded", True)
    yield


@pytest.fixture(scope="module")
def frame():
    return generate_session(3)


@pytest.fixture
def sample(tmp_path, frame):
    starts = frame.groupby("LapNumber")["Time"].min().to_numpy()
    return elg.write(tmp_path / "three_laps.elg", frame, COLUMNS, SAMPLE_RATE_HZ, beacons=starts[1:], venue="Test")


def _importer(**changes):
    base = dict(key="testfmt", name="Test format", extensions=(".tst",), read=lambda p: None)
    return Importer(**{**base, **changes})


# ── the example, outside the engine ───────────────────────────────────────────

def test_the_example_importer_passes_the_kit_unchanged(sample):
    result = check_importer(elg.IMPORTER, [sample], expect={"three_laps.elg": {"rows": 24237, "laps": 3,
                                                            "channels": ["speed", "throttle", "acc_lat"]}})
    assert result.ok, result.text()
    assert len(result.checked) == 4
    assert plugins.registered() == []               # the kit leaves the registry as it found it


def test_the_example_uses_only_the_public_interface():
    source = (EXAMPLE / "veltrix_example_logger" / "__init__.py").read_text()
    engine_imports = [line.strip() for line in source.splitlines() if line.startswith(("from veltrix", "import veltrix"))]
    assert engine_imports == ["from veltrix_engine.importers import Importer, RawTable"]


def test_a_registered_importer_is_read_like_a_built_in_format(sample):
    register(elg.IMPORTER)
    assert ".elg" in supported_extensions()
    assert any(r.get("plugin") == "example_logger" for r in supported_formats())
    s = load_session(sample)
    assert s.raw.format == "Example Logger" and s.raw.source == "three_laps.elg" and len(s.raw.sha256) == 64
    assert s.mapping["speed"] == "vCar" and len(s.laps) == 3
    assert any("Example Logger importer 1.0.0" in m for _, m in s.notes)


def test_a_zipped_file_is_unpacked_for_the_importer(sample, tmp_path):
    register(elg.IMPORTER)
    zipped = tmp_path / "three_laps.elg.gz"
    zipped.write_bytes(gzip.compress(sample.read_bytes()))
    assert read_file(zipped).data.shape == read_file(sample).data.shape


def test_built_in_formats_are_untouched_by_a_plug_in(tmp_path):
    register(elg.IMPORTER)
    csv = tmp_path / "plain.csv"
    csv.write_text("Time,Speed\n" + "\n".join(f"{t / 10},{100 + t}" for t in range(50)))
    assert detect_format(csv.name, csv.read_bytes()) == "csv"


# ── entry points ──────────────────────────────────────────────────────────────

class FakeEntryPoint:
    def __init__(self, name, target):
        self.name, self.value, self._target = name, f"fake:{name}", target

    def load(self):
        if isinstance(self._target, Exception):
            raise self._target
        return self._target


def test_installed_importers_load_and_a_broken_one_is_reported(monkeypatch, sample):
    found = [FakeEntryPoint("example_logger", elg.IMPORTER),
             FakeEntryPoint("broken", ImportError("No module named 'missing_dependency'")),
             FakeEntryPoint("factory", lambda: [_importer(key="made_by_a_function", read=elg.read)])]
    monkeypatch.setattr("importlib.metadata.entry_points", lambda group: found if group == "veltrix.importers" else [])
    monkeypatch.setattr(plugins, "_loaded", False)
    assert sorted(p.key for p in plugins.registered()) == ["example_logger", "made_by_a_function"]
    assert "missing_dependency" in plugins.failed()["broken"]
    assert any("not loaded" in r["remark"] for r in supported_formats() if r["format"] == "broken")
    assert read_file(sample).format == "Example Logger"


def test_plug_ins_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("VELTRIX_NO_PLUGINS", "1")
    monkeypatch.setattr("importlib.metadata.entry_points", lambda group: [FakeEntryPoint("example_logger", elg.IMPORTER)])
    monkeypatch.setattr(plugins, "_loaded", False)
    assert plugins.registered() == []


# ── what registration refuses ─────────────────────────────────────────────────

@pytest.mark.parametrize("changes, reason", [
    ({"key": "csv"}, "built-in"),
    ({"key": "Bad Key"}, "lower-case"),
    ({"extensions": ("TST",)}, "lower case with the dot"),
    ({"extensions": (".csv",)}, "needs a sniff"),
    ({"name": " "}, "no name"),
    ({"read": "not a function"}, "not callable"),
])
def test_registration_refuses_an_unusable_importer(changes, reason):
    with pytest.raises(ValueError, match=reason):
        register(_importer(**changes))


def test_an_importer_for_a_newer_interface_is_refused():
    with pytest.raises(VersionError, match="interface 2"):
        register(_importer(api=2))


def test_a_shared_extension_is_allowed_with_a_sniff():
    register(_importer(extensions=(".csv",), sniff=lambda name, head: head.startswith(b"#TST")))
    assert detect_format("x.csv", b"#TST\n1,2") == "plugin:testfmt"
    assert detect_format("x.csv", b"Time,Speed\n0,1\n") == "csv"


# ── what the engine holds a plug-in to ────────────────────────────────────────

def test_a_table_that_breaks_the_contract_is_refused_with_every_reason(tmp_path):
    bad = RawTable(pd.DataFrame({"Time": [0.0, 0.1], "a": [1, 2]}), units={"nope": "m"}, hints={"wings": "a"})
    register(_importer(read=lambda p: bad))
    (tmp_path / "x.tst").write_bytes(b"anything")
    with pytest.raises(FileFormatError) as e:
        read_file(tmp_path / "x.tst")
    assert "units name columns" in str(e.value) and "'wings' is not a channel id" in str(e.value)


def test_a_plug_in_that_crashes_is_named_in_the_error(tmp_path):
    def read(path):
        return [][1]
    register(_importer(read=read, version="0.3"))
    (tmp_path / "x.tst").write_bytes(b"anything")
    with pytest.raises(FileFormatError, match=r"Test format importer \(a plug-in, 0.3\): IndexError"):
        read_file(tmp_path / "x.tst")


def test_a_sniff_that_raises_counts_as_no(tmp_path):
    def sniff(name, head):
        raise RuntimeError("bug in sniff")
    register(_importer(sniff=sniff))
    assert detect_format("x.csv", b"Time,Speed\n0,1\n") == "csv"


# ── the kit catches what it should ────────────────────────────────────────────

def test_the_kit_catches_a_sniff_that_claims_everything(sample):
    greedy = Importer(**{**elg.IMPORTER.__dict__, "key": "greedy", "sniff": lambda name, head: True})
    result = check_importer(greedy, [sample])
    assert any("claims a file that is not its own" in p for p in result.problems)


def test_a_crash_on_a_damaged_file_still_reaches_the_user_as_a_reason(sample):
    def fragile(path):
        content = Path(path).read_bytes()
        if len(content) < 1000:
            raise IndexError("ran off the end")          # not a refusal: the engine turns it into a FileFormatError,
        return elg.read(path)                            # so the kit's damaged-file check still passes …
    result = check_importer(Importer(**{**elg.IMPORTER.__dict__, "key": "fragile", "read": fragile}), [sample])
    assert result.ok, result.text()                       # … because the user only ever sees a reason


def test_the_kit_catches_a_table_without_laps_or_samples(tmp_path):
    flat = tmp_path / "flat.tst"
    flat.write_bytes(b"x")
    table = lambda p: RawTable(pd.DataFrame({"Time": np.arange(5) / 10.0, "Speed": np.zeros(5)}))
    result = check_importer(_importer(read=table), [flat])
    assert any("no session can be built" in p or "no usable time base" in p for p in result.problems)


def test_the_kit_needs_a_sample():
    assert "no sample files" in check_importer(elg.IMPORTER, []).problems[0]


def test_the_kit_reports_an_unregistrable_importer():
    assert "cannot be registered" in check_importer(_importer(key="csv"), [Path(__file__)]).problems[0]


def test_formats_table_still_lists_every_built_in():
    names = {r["format"] for r in supported_formats()}
    assert {f.name for f in FORMATS} <= names


# ── the command line ──────────────────────────────────────────────────────────

def test_veltrix_importer_check_runs_the_kit(sample, capsys, monkeypatch):
    from veltrix_engine.cli import main
    monkeypatch.chdir(EXAMPLE)
    assert main(["importer", "check", "veltrix_example_logger:IMPORTER", str(sample)]) == 0
    assert "example_logger: passed" in capsys.readouterr().out
    assert main(["importer", "check", "veltrix_example_logger", str(sample.with_name("missing.elg"))]) == 1
    assert "sample files not found" in capsys.readouterr().out


def test_veltrix_importer_list_shows_installed_and_broken(monkeypatch, capsys):
    from veltrix_engine.cli import main
    monkeypatch.setattr("importlib.metadata.entry_points",
                        lambda group: [FakeEntryPoint("example_logger", elg.IMPORTER), FakeEntryPoint("bad", ImportError("x"))])
    monkeypatch.setattr(plugins, "_loaded", False)
    assert main(["importer", "list"]) == 0
    out = capsys.readouterr().out
    assert "example_logger" in out and "Example Logger 1.0.0" in out and "bad" in out and "NOT LOADED" in out
