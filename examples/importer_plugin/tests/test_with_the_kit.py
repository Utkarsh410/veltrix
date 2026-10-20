"""The example importer against the engine's importer test kit, unchanged."""

from pathlib import Path

import pytest

from veltrix_engine.importers.testkit import assert_importer
from veltrix_engine.synth import SAMPLE_RATE_HZ, generate_session

import veltrix_example_logger as elg


@pytest.fixture
def sample(tmp_path) -> Path:
    """Three laps of the engine's synthetic car, written as the logger would."""
    frame = generate_session(3)
    starts = frame.groupby("LapNumber")["Time"].min().to_numpy()
    columns = {"vCar": ("Speed", "km/h", 0.01), "nEngine": ("RPM", "rpm", 1.0), "nGear": ("Gear", "", 1.0),
               "rThrottle": ("ThrottlePos", "%", 0.01), "pBrake": ("BrakePress", "bar", 0.01),
               "aSteer": ("SteeringAngle", "deg", 0.01), "gLat": ("LateralAcc", "g", 0.001),
               "gLong": ("LongAcc", "g", 0.001)}
    return elg.write(tmp_path / "three_laps.elg", frame, columns, SAMPLE_RATE_HZ, beacons=starts[1:],
                     venue="Test circuit", vehicle="GT3", driver="Example")


def test_the_example_importer_passes_the_kit(sample):
    result = assert_importer(elg.IMPORTER, [sample], expect={"three_laps.elg": {
        "rows": 24237, "channels": ["speed", "throttle", "brake", "steering", "acc_lat"]}})
    assert result.checked
