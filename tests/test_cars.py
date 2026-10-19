"""Phase 5: the car library — a car's parameters entered once, used for every session of it."""

import pytest

from conftest import GOLDEN
from veltrix_engine import load_session
from veltrix_engine.cars import CarLibrary
from veltrix_engine.cli import main as cli_main
from veltrix_engine.errors import VersionError
from veltrix_engine.vehicle import VehicleParams


def test_a_car_in_the_library_supplies_what_the_file_does_not(tmp_path):
    lib = CarLibrary(tmp_path / "cars")
    lib.save("GT3", {"wheelbase_m": 2.71, "steer_ratio": 12.0, "front_weight_dist": 0.47}, aliases=["GT3 Car"])
    s = load_session(GOLDEN / "logger_layout_3lap_20hz.csv", cars=lib)       # its header says Vehicle: GT3
    v = s.vehicle
    assert (v.wheelbase_m, v.steer_ratio) == (2.71, 12.0) and v.origin("wheelbase_m") == "from the car library"
    assert v.known("front_weight_dist") and not v.known("mass_kg")
    assert any("car library (GT3)" in m for _, m in s.notes)
    assert lib.get("gt3 car")["name"] == "GT3", "found by alias, ignoring case and punctuation"


def test_stated_and_file_values_beat_the_library(tmp_path):
    lib = CarLibrary(tmp_path / "cars")
    lib.save("GT3", {"wheelbase_m": 2.71, "mass_kg": 1300.0})
    stated = VehicleParams()
    stated.set("wheelbase_m", 2.60, "user")
    s = load_session(GOLDEN / "logger_layout_3lap_20hz.csv", cars=lib, vehicle=stated)
    assert s.vehicle.wheelbase_m == 2.60 and s.vehicle.mass_kg == 1300.0
    assert stated.source == {"wheelbase_m": "user"}, "the caller's parameters are not changed"


def test_a_car_not_in_the_library_is_named_with_how_to_add_it(tmp_path):
    s = load_session(GOLDEN / "logger_layout_3lap_20hz.csv", cars=CarLibrary(tmp_path / "cars"))
    assert any("not in the car library" in m and "veltrix car set" in m for _, m in s.notes)
    assert not s.vehicle.known("wheelbase_m")


def test_the_library_keeps_entries_and_refuses_bad_ones(tmp_path):
    lib = CarLibrary(tmp_path / "cars")
    lib.save("Toyota GR86", {"wheelbase_m": 2.575}, note="manufacturer")
    lib.save("Toyota GR86", {"steer_ratio": 13.5})
    entry = lib.get("Toyota GR86")
    assert entry["parameters"] == {"wheelbase_m": 2.575, "steer_ratio": 13.5} and entry["note"] == "manufacturer"
    assert lib.names() == ["Toyota GR86"]
    with pytest.raises(ValueError, match="Not car parameters"):
        lib.save("Toyota GR86", {"horsepower": 228})
    path = lib.save("Other", {"mass_kg": 1000.0})
    path.write_text(path.read_text().replace('"version": 1', '"version": 9'))
    with pytest.raises(VersionError):
        lib.save("Other", {"mass_kg": 1100.0})
    assert lib.remove("Toyota GR86") and lib.get("Toyota GR86") is None


def test_the_car_command(tmp_path, capsys):
    folder = str(tmp_path)
    assert cli_main(["car", "set", "Toyota GR86", "--wheelbase", "2.575", "--alias", "GR86", "--app-data", folder]) == 0
    assert cli_main(["car", "show", "gr86", "--app-data", folder]) == 0
    out = capsys.readouterr().out
    assert "Toyota GR86" in out and "wheelbase_m" in out and "2.575" in out
    assert cli_main(["car", "list", "--app-data", folder]) == 0 and "Toyota GR86" in capsys.readouterr().out
    assert cli_main(["car", "set", "Nothing", "--app-data", folder]) == 1


def test_the_spa_setup_beats_the_library_for_what_it_gives(tmp_path):
    from real_sessions import find
    path = find("toyotagr86_spa 2024 up 2026-10-03 01-28-28.ibt")
    if path is None:
        pytest.skip("the Spa .ibt is not on this machine")
    lib = CarLibrary(tmp_path / "cars")
    lib.save("Toyota GR86", {"wheelbase_m": 2.575, "front_weight_dist": 0.60})
    s = load_session(path, cars=lib)
    assert s.vehicle.wheelbase_m == 2.575 and s.vehicle.origin("wheelbase_m") == "from the car library"
    assert s.vehicle.front_weight_dist == pytest.approx(0.532, abs=0.001), "the file's setup beats the library"
