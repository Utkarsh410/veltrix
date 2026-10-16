"""
Every supported file format, read back against one known answer.

The `logger` golden session is written out in each format (tests/format_writers.py)
and imported again. Whatever the format, the engine must find the same
channels, in schema units, and the same laps.
"""

import gzip
import io
import struct

import numpy as np
import pandas as pd
import pytest

import format_writers as fw
from conftest import GOLDEN
from veltrix_engine import ProjectStore, load_session, read_file, supported_formats
from veltrix_engine.cli import file_stem, main as cli_main
from veltrix_engine.importers import can, dbc, detect_format, signals_to_table, Signal

BEACONS = (80.660, 161.430)


@pytest.fixture(scope="module")
def ref(logger):
    return logger.channels


@pytest.fixture(scope="module")
def frames(ref):
    return fw.can_frames(ref)


def trace_error(session, ref, cid):
    """Median and 99th-percentile difference between a channel and the reference."""
    got = np.interp(ref["time"], session.channels["time"], session.channels[cid].astype(float))
    err = np.abs(got - ref[cid].to_numpy(float))[20:-20]
    return float(np.nanmedian(err)), float(np.nanpercentile(err, 99))


def assert_same_laps(session, logger, tol):
    assert len(session.laps) == len(logger.laps)
    assert np.allclose(session.laps["lap_time_s"], logger.laps["lap_time_s"], atol=tol)


# ── the shared time base ──────────────────────────────────────────────────────

def test_signals_at_different_rates_share_one_time_base():
    fast = Signal("Speed", np.arange(0, 10, 0.01), np.linspace(0, 100, 1000), "km/h")
    slow = Signal("Gear", np.arange(2.0, 8.0, 0.5), np.repeat([1, 2, 3], 4).astype(float))
    table = signals_to_table([fast, slow], "test")
    df = table.data
    assert len(df) == 1000 and np.allclose(np.diff(df["Time"]), 0.01)
    assert np.allclose(df["Speed"], fast.y)
    assert df["Gear"][df["Time"] < 1.99].isna().all(), "nothing is invented before a channel starts"
    assert set(df["Gear"].dropna().unique()) == {1.0, 2.0, 3.0}, "a step channel is held, not interpolated"
    assert table.units == {"Time": "s", "Speed": "km/h"}
    assert any("one 100 Hz time base" in m for _, m in table.notes)


# ── DBC ───────────────────────────────────────────────────────────────────────

def test_dbc_bit_layouts():
    db = dbc.parse_dbc('''BU_: A
BO_ 1 M: 8 A
 SG_ intel12 : 4|12@1+ (1,0) [0|0] "" X
 SG_ motorola12 : 3|12@0+ (1,0) [0|0] "" X
 SG_ signed8 : 16|8@1- (0.5,-1) [0|0] "degC" X
 SG_ float32 : 32|32@1- (1,0) [0|0] "" X
BO_ 2 Mux: 8 A
 SG_ page M : 0|8@1+ (1,0) [0|0] "" X
 SG_ first m0 : 8|8@1+ (1,0) [0|0] "" X
 SG_ second m1 : 8|8@1+ (1,0) [0|0] "" X
SIG_VALTYPE_ 1 float32 : 1;
''')
    payload = bytes([0xAB, 0xCD, 0xF6, 0x00]) + struct.pack("<f", 1.5)
    mux = [bytes([0, 11]) + bytes(6), bytes([1, 22]) + bytes(6)] * 2
    frames = can._frames([0.0, 0.1, 0.2, 0.3, 0.4, 0.5], [1, 1, 2, 2, 2, 2], [False] * 6, [payload, payload] + mux)
    signals, _ = dbc.decode(frames, db)
    got = {s.name: s for s in signals}
    assert got["intel12"].y[0] == 0xCDA
    assert got["motorola12"].y[0] == 0xBCD
    assert got["signed8"].y[0] == -10 * 0.5 - 1 and got["signed8"].unit == "degC"
    assert got["float32"].y[0] == 1.5
    assert list(got["first"].y) == [11, 11] and list(got["second"].y) == [22, 22]
    assert list(got["second"].t) == [0.3, 0.5], "a multiplexed signal exists only on its own page"


def test_j1939_identifier_matches_whatever_the_source_address():
    db = dbc.parse_dbc('BU_: A\nBO_ 2364540158 EEC1: 8 A\n SG_ EngineSpeed : 24|16@1+ (0.125,0) [0|0] "rpm" X\n')
    payload = bytes(3) + struct.pack("<H", 8000) + bytes(3)
    frames = can._frames([0.0, 0.1], [0x0CF00400, 0x0CF00400], [True, True], [payload, payload])
    signals, notes = dbc.decode(frames, db)
    assert signals[0].name == "EngineSpeed" and signals[0].y[0] == 1000.0
    assert any("J1939" in m for _, m in notes)


# ── CAN logs ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name, writer", [("bus.log", fw.candump), ("bus.asc", fw.asc), ("bus_v1.trc", fw.trc_v1),
                                           ("bus_v2.trc", fw.trc_v2), ("bus.csv", fw.can_csv)])
def test_can_log_decoded_with_dbc(tmp_path, frames, ref, logger, name, writer):
    (tmp_path / name).write_text(writer(frames))
    (tmp_path / "car.dbc").write_text(fw.DBC_TEXT)
    s = load_session(tmp_path / name)                       # the DBC is found beside the log
    assert "CAN" in s.raw.format and s.raw.info["DBC"] == "car.dbc"
    want = {"speed": "Speed", "rpm": "RPM", "steering": "Steered_Angle", "gear": "Gear", "lap": "Lap_Number",
            "throttle": "Throttle_Pos", "brake": "Brake_Pres_Front", "acc_lat": "G_Force_Lat", "acc_long": "G_Force_Long"}
    assert {k: s.mapping.get(k) for k in want} == want
    assert s.units["brake"] == "bar" and s.rate_hz == 20
    for cid, tol in (("speed", 0.02), ("rpm", 0.5), ("steering", 0.11), ("acc_lat", 0.002)):
        median, worst = trace_error(s, ref, cid)
        assert median <= tol and worst <= 4 * tol, (cid, median, worst)
    assert trace_error(s, ref, "throttle")[0] <= 0.6        # sent at half rate, in 0.5 % steps
    assert_same_laps(s, logger, 0.06)
    assert any("1 identifier(s) are not in the DBC" in m and "7AA" in m for _, m in s.notes)
    assert any("Decoded with the DBC beside the log" in m for _, m in s.notes)


def test_can_log_formats_agree_frame_for_frame(frames):
    parsed = {name: can.read_frames(writer(frames).encode(), kind)[0]
              for name, writer, kind in (("candump", fw.candump, "candump"), ("asc", fw.asc, "asc"),
                                         ("trc1", fw.trc_v1, "trc"), ("trc2", fw.trc_v2, "trc"), ("csv", fw.can_csv, "can_csv"))}
    first = parsed["asc"]
    assert len(first.t) == len(frames)
    for name, got in parsed.items():
        assert np.array_equal(got.id, first.id) and np.array_equal(got.extended, first.extended), name
        assert np.array_equal(got.data, first.data) and np.array_equal(got.dlc, first.dlc), name
        assert np.allclose(got.t - got.t[0], first.t - first.t[0], atol=1e-4), name


@pytest.mark.parametrize("suffix, kind", [(".asc", "asc"), (".log", "candump"), (".trc", "trc")])
def test_can_logs_written_by_python_can(tmp_path, frames, suffix, kind):
    """The same frames written by an independent implementation of each format."""
    pycan = pytest.importorskip("can")
    use = frames[:3000]
    path = tmp_path / f"bus{suffix}"
    with pycan.Logger(str(path)) as writer:
        for t, i, x, p in use:
            writer.on_message_received(pycan.Message(timestamp=1_700_000_000 + t, arbitration_id=i, is_extended_id=x,
                                                     data=p, channel=0))
    assert detect_format(path.name, path.read_bytes()[:65536]) == kind
    got, _ = can.read_frames(path.read_bytes(), kind)
    assert len(got.t) == len(use)
    assert list(got.id) == [f[1] for f in use] and list(got.extended) == [f[2] for f in use]
    assert [bytes(row[:n]) for row, n in zip(got.data, got.dlc)] == [f[3] for f in use]
    assert np.allclose(got.t - got.t[0], np.array([f[0] for f in use]) - use[0][0], atol=2e-3)


def test_blf_log(tmp_path, frames, logger):
    pycan = pytest.importorskip("can")
    path = tmp_path / "bus.blf"
    with pycan.BLFWriter(str(path)) as writer:
        for t, i, x, p in frames[:6000]:
            writer.on_message_received(pycan.Message(timestamp=1_700_000_000 + t, arbitration_id=i, is_extended_id=x, data=p))
    s = load_session(path, dbc=_dbc_file(tmp_path))
    assert s.raw.format == "Vector BLF CAN log" and {"speed", "steering", "acc_lat"} <= set(s.mapping)
    assert trace_error(s, logger.channels[logger.channels["time"] < s.channels["time"].iloc[-1]], "speed")[0] <= 0.02


def _dbc_file(folder):
    path = folder / "db" / "car.dbc"
    path.parent.mkdir(exist_ok=True)
    path.write_text(fw.DBC_TEXT)
    return path


def test_can_log_without_a_dbc_says_what_is_missing(tmp_path, frames):
    (tmp_path / "bus.asc").write_text(fw.asc(frames[:200]))
    with pytest.raises(ValueError, match="needs a DBC"):
        read_file(tmp_path / "bus.asc")
    wrong = tmp_path / "other.dbc"
    wrong.write_text('BU_: A\nBO_ 999 Other: 8 A\n SG_ x : 0|8@1+ (1,0) [0|0] "" X\n')
    with pytest.raises(ValueError, match="right DBC"):
        read_file(tmp_path / "bus.asc", dbc=wrong)


def test_a_dbc_or_ldx_alone_is_not_a_session(tmp_path):
    path = _dbc_file(tmp_path)
    with pytest.raises(ValueError, match="3 messages, 9 signals"):
        read_file(path)
    (tmp_path / "run.ldx").write_text(fw.ldx(BEACONS))
    with pytest.raises(ValueError, match="Import the .ld file"):
        read_file(tmp_path / "run.ldx")


# ── VBOX ──────────────────────────────────────────────────────────────────────

def test_vbox(tmp_path, ref, logger):
    path = tmp_path / "run.vbo"
    path.write_text(fw.vbo(ref, logger.lap_length, BEACONS))
    s = load_session(path)
    assert s.raw.format == "VBOX .vbo"
    assert {"time": "time", "speed": "velocity kmh", "gps_lat": "latitude", "gps_lon": "longitude",
            "rpm": "Engine_Speed", "steering": "Steering_Angle"}.items() <= s.mapping.items()
    lat, lon = s.raw.data["latitude"], s.raw.data["longitude"]
    assert 44.9 < lat.mean() < 45.1 and 8.9 < lon.mean() < 9.1, "minutes, west positive → degrees, east positive"
    assert s.raw.data["time"].iloc[0] == 0 and np.allclose(np.diff(s.raw.data["time"]), 0.05, atol=1e-6), \
        "the clock runs through the hour without a jump"
    assert np.allclose(s.raw.beacons, BEACONS, atol=0.01), "crossings of the stored start/finish line"
    assert s.lap_source == "beacon markers"
    assert_same_laps(s, logger, 0.01)
    assert trace_error(s, ref, "speed")[1] <= 0.002
    assert s.raw.info["Log Date"] == "08/10/2026" and s.raw.info["Logger"] == "VBOX 3i"
    assert {"pos_x", "pos_y"} <= set(s.channels.columns)


# ── MoTeC / Assetto Corsa ─────────────────────────────────────────────────────

def _ld_bytes(ref):
    half = ref.iloc[::2]
    return fw.ld([("Ground Speed", "km/h", 20, ref["speed"], ("i2", 2)),
                  ("Engine RPM", "rpm", 10, half["rpm"], "f4"),
                  ("Steered Angle", "deg", 20, ref["steering"], ("i2", 1)),
                  ("G Force Lat", "G", 20, ref["acc_lat"], "f4"),
                  ("Throttle Pos", "%", 10, half["throttle"], "f4"),
                  ("Brake Pres Front", "bar", 20, ref["brake"], ("i4", 3)),
                  ("Gear", "", 20, ref["gear"], ("i2", 0))])


def test_motec_ld_with_ldx(tmp_path, ref, logger):
    (tmp_path / "run.ld").write_bytes(_ld_bytes(ref))
    (tmp_path / "run.ldx").write_text(fw.ldx(BEACONS))
    s = load_session(tmp_path / "run.ld")
    assert s.raw.format == "MoTeC .ld"
    assert s.raw.info["Venue"] == "Synthetic Circuit" and s.raw.info["Driver"] == "Test Driver"
    assert s.raw.info["Log Date"] == "08/10/2026" and s.raw.info["Total Laps"] == "3"
    assert {"speed": "Ground Speed", "rpm": "Engine RPM", "steering": "Steered Angle", "acc_lat": "G Force Lat",
            "throttle": "Throttle Pos", "brake": "Brake Pres Front", "gear": "Gear"}.items() <= s.mapping.items()
    assert s.lap_source == "beacon markers"
    assert_same_laps(s, logger, 1e-3)
    assert trace_error(s, ref, "speed")[1] <= 0.006          # stored to two decimals
    assert trace_error(s, ref, "steering")[1] <= 0.06
    assert trace_error(s, ref, "acc_lat")[1] <= 1e-5
    assert trace_error(s, ref, "rpm")[0] <= 15               # logged at half the rate
    assert any("10 to 20 Hz" in m for m in dict(map(reversed, s.notes)))


def test_motec_ld_without_ldx_has_no_laps_and_says_why(tmp_path, ref):
    (tmp_path / "run.ld").write_bytes(_ld_bytes(ref))
    s = load_session(tmp_path / "run.ld")
    assert len(s.laps) == 1
    assert any(".ldx" in m for _, m in s.notes)


def test_ld_with_unknown_encoding_keeps_the_rest(tmp_path, ref):
    data = bytearray(_ld_bytes(ref))
    first = fw.motec._HEAD.size
    struct.pack_into("<H", data, first + 18, 0x55)           # the first channel's encoding
    (tmp_path / "run.ld").write_bytes(bytes(data))
    raw = read_file(tmp_path / "run.ld")
    assert "Ground Speed" not in raw.data and "Engine RPM" in raw.data
    assert any("Ground Speed" in m and level == "warning" for level, m in raw.notes)


# ── iRacing ───────────────────────────────────────────────────────────────────

def test_iracing_ibt(tmp_path, ref, logger):
    n = len(ref)
    # iRacing writes a flat pedal as the 32-bit float just above 1, as real files show (1.00000012)
    pedal = (ref["throttle"] / 100).to_numpy(np.float64, copy=True)
    pedal[pedal >= 1.0] = float(np.nextafter(np.float32(1), np.float32(2)))
    abs_on = ((ref["brake"] > 0.5 * ref["brake"].max()) & (np.arange(n) % 7 == 0)).to_numpy()
    count, flags = np.zeros(n), np.zeros(n)
    count[n // 2:] = 1                       # one off-track half way through: 1x
    flags[n // 2:n // 2 + 40] = 0x0202
    variables = [("SessionTime", "double", "s", ref["time"]), ("Speed", "float", "m/s", ref["speed"] / 3.6),
                 ("Lap", "int", "", ref["lap"]), ("LapDist", "float", "m", ref["lap_distance"]),
                 ("LapDistPct", "float", "%", ref["lap_distance"] / logger.lap_length),
                 ("Throttle", "float", "%", pedal), ("Brake", "float", "%", ref["brake"] / ref["brake"].max()),
                 ("SteeringWheelAngle", "float", "rad", np.radians(ref["steering"])),
                 ("LatAccel", "float", "m/s^2", ref["acc_lat"] * 9.81), ("LongAccel", "float", "m/s^2", ref["acc_long"] * 9.81),
                 ("RPM", "float", "revs/min", ref["rpm"]), ("Gear", "int", "", ref["gear"]),
                 ("IsOnTrack", "bool", "", np.ones(n, bool)), ("LFtempCL", "float", "C", np.full(n, 80.0)),
                 ("RFtempCL", "float", "C", np.full(n, 81.0)), ("CarIdxLap", "int", "", np.zeros((n, 64))),
                 # dials and other channels that look like measurements by name but are not
                 ("dcABS", "float", "", np.full(n, 2.0)), ("dcTractionControl", "float", "", np.full(n, 1.0)),
                 ("BrakeABSactive", "bool", "", abs_on), ("TireLF_RumblePitch", "float", "Hz", np.zeros(n)),
                 ("dpLFTireChange", "float", "", np.ones(n)),
                 ("PlayerCarMyIncidentCount", "int", "", count), ("PlayerIncidents", "int", "irsdk_IncidentFlags", flags),
                 ("VelocityX", "float", "m/s", ref["speed"] / 3.6), ("VelocityY", "float", "m/s", np.zeros(n))]
    path = tmp_path / "gt3_synthetic 2026-10-08 10-59-30.ibt"
    path.write_bytes(fw.ibt(variables, 20))
    s = load_session(path)
    assert s.raw.format == "iRacing .ibt"
    assert s.raw.info["Venue"] == "Synthetic Circuit Grand Prix" and s.raw.info["Driver"] == "Test Driver"
    assert s.raw.info["Vehicle"] == "GT3 Car" and s.raw.info["Session"] == "Practice"
    assert "CarIdxLap" not in s.raw.data
    want = {"time": "SessionTime", "speed": "Speed", "lap": "Lap", "distance": "LapDist", "throttle": "Throttle",
            "brake": "Brake", "steering": "SteeringWheelAngle", "acc_lat": "LatAccel", "acc_long": "LongAccel",
            "rpm": "RPM", "gear": "Gear", "tyre_temp_fl_outer": "LFtempCL", "tyre_temp_fr_inner": "RFtempCL"}
    assert {k: s.mapping.get(k) for k in want} == want
    for cid, tol in (("speed", 1e-3), ("steering", 1e-3), ("acc_lat", 1e-5), ("throttle", 1e-3)):
        assert trace_error(s, ref, cid)[1] <= tol, cid       # m/s, rad, m/s² and 0–1 all converted
    assert s.channels["throttle"].max() == pytest.approx(100, abs=1e-3) and s.units["throttle"] == "%"
    flat = (ref["throttle"] >= 99.9).to_numpy()
    assert flat.any() and (s.channels["throttle"].to_numpy()[flat] >= 99.9).all(), "full throttle reads 100 %, not 1 %"
    assert s.channels["brake"].max() == pytest.approx(100, abs=1e-3) and s.units["brake"] == "%"
    assert s.raw.data["LapDistPct"].max() > 90, "every variable iRacing labels % is scaled, not only the pedals"
    assert any("scaled to 0–100" in m for _, m in s.raw.notes)
    # the flag, not the driver's dial; no traction-control flag in the file, so none; no rumble pitch as a tyre temperature
    assert s.mapping.get("abs_active") == "BrakeABSactive" and s.mapping.get("tc_active") is None
    assert s.mapping.get("tyre_temp_fl") is None
    assert s.mapping.get("incident_count") == "PlayerCarMyIncidentCount" and s.mapping.get("incident_flags") == "PlayerIncidents"
    # the car setup in the session info gives the static front weight and the mass
    assert s.vehicle.front_weight_dist == pytest.approx((3632 + 3462) / (3632 + 3462 + 3206 + 3029), abs=1e-4)
    assert s.vehicle.mass_kg == pytest.approx(13329 / 9.80665, abs=0.1) and s.vehicle.known("front_weight_dist")
    assert not s.vehicle.known("wheel_rate_front_N_mm"), "spring rates alone are not wheel rates"
    assert np.nanmax(np.abs(s.derived["body_slip"])) < 0.01
    assert_same_laps(s, logger, 0.06)


# ── MDF ───────────────────────────────────────────────────────────────────────

def test_mf4_channels_in_groups_of_different_rates(tmp_path, ref, logger):
    asammdf = pytest.importorskip("asammdf")
    t, half = ref["time"].to_numpy(), ref.iloc[::2]
    mdf = asammdf.MDF(version="4.10")
    mdf.append([asammdf.Signal(ref["speed"].to_numpy(), t, name="Ground Speed", unit="km/h"),
                asammdf.Signal(ref["steering"].to_numpy(), t, name="Steered Angle", unit="deg"),
                asammdf.Signal(ref["lap"].to_numpy(np.int32), t, name="Lap Number")])
    mdf.append([asammdf.Signal(half["rpm"].to_numpy(), half["time"].to_numpy(), name="Engine RPM", unit="rpm")])
    path = tmp_path / "run.mf4"
    mdf.save(path, overwrite=True)
    mdf.close()
    s = load_session(path)
    assert s.raw.format.startswith("MDF 4")
    assert {"speed": "Ground Speed", "steering": "Steered Angle", "lap": "Lap Number", "rpm": "Engine RPM"}.items() \
        <= s.mapping.items()
    assert trace_error(s, ref, "speed")[1] <= 1e-6 and trace_error(s, ref, "rpm")[0] <= 15
    assert_same_laps(s, logger, 0.06)


def test_mf4_raw_can_bus_logging(tmp_path, frames, ref):
    asammdf = pytest.importorskip("asammdf")
    use = [f for f in frames if len(f[3]) == 8][:8000]
    dtype = np.dtype([("CAN_DataFrame.BusChannel", "u1"), ("CAN_DataFrame.ID", "<u4"), ("CAN_DataFrame.IDE", "u1"),
                      ("CAN_DataFrame.DLC", "u1"), ("CAN_DataFrame.DataLength", "u1"), ("CAN_DataFrame.DataBytes", "u1", (8,))])
    rec = np.zeros(len(use), dtype)
    rec["CAN_DataFrame.BusChannel"] = 1
    rec["CAN_DataFrame.ID"] = [f[1] for f in use]
    rec["CAN_DataFrame.IDE"] = [f[2] for f in use]
    rec["CAN_DataFrame.DLC"] = rec["CAN_DataFrame.DataLength"] = 8
    rec["CAN_DataFrame.DataBytes"] = np.frombuffer(b"".join(f[3] for f in use), np.uint8).reshape(-1, 8)
    mdf = asammdf.MDF(version="4.10")
    mdf.append(asammdf.Signal(rec, np.array([f[0] for f in use]), name="CAN_DataFrame"), comment="CAN bus")
    path = tmp_path / "bus.mf4"
    mdf.save(path, overwrite=True)
    mdf.close()

    with pytest.raises(ValueError, match="raw CAN frames"):
        read_file(path)
    s = load_session(path, dbc=_dbc_file(tmp_path))
    assert {"speed": "Speed", "steering": "Steered_Angle", "acc_lat": "G_Force_Lat"}.items() <= s.mapping.items()
    part = ref[ref["time"] < s.channels["time"].iloc[-1]]
    assert trace_error(s, part, "speed")[0] <= 0.02


# ── MAT ───────────────────────────────────────────────────────────────────────

def test_mat_one_struct_per_channel(tmp_path, ref, logger):
    from scipy.io import savemat
    half = ref.iloc[::2]
    t = ref["time"].to_numpy()
    savemat(tmp_path / "run.mat", {
        "Ground_Speed": {"Time": t, "Value": ref["speed"].to_numpy(), "Units": "km/h"},
        "Engine_RPM": {"Time": half["time"].to_numpy(), "Value": half["rpm"].to_numpy(), "Units": "rpm"},
        "Steered_Angle": {"Time": t, "Value": np.radians(ref["steering"].to_numpy()), "Units": "rad"},
        "Lap_Number": {"Time": t, "Value": ref["lap"].to_numpy(float), "Units": ""}})
    s = load_session(tmp_path / "run.mat")
    assert s.raw.format == "MATLAB .mat"
    assert {"speed": "Ground_Speed", "rpm": "Engine_RPM", "steering": "Steered_Angle", "lap": "Lap_Number"}.items() \
        <= s.mapping.items()
    assert trace_error(s, ref, "speed")[1] <= 1e-6 and trace_error(s, ref, "steering")[1] <= 1e-6
    assert_same_laps(s, logger, 0.06)


@pytest.mark.parametrize("layout", ["vectors", "struct", "matrix", "simulink"])
def test_mat_table_layouts(tmp_path, ref, layout):
    from scipy.io import savemat
    cols = {"t": ref["time"].to_numpy(), "Speed": ref["speed"].to_numpy(), "LapNumber": ref["lap"].to_numpy(float),
            "SteeringAngle": ref["steering"].to_numpy()}
    if layout == "vectors":
        content = {**cols, "sample_rate": 20.0, "note": "test"}
    elif layout == "struct":
        content = {"log": cols, "meta": {"driver": "Test Driver"}}
    elif layout == "matrix":
        content = {"data": np.column_stack(list(cols.values())), "names": np.array(["time", *list(cols)[1:]], dtype=object)}
    else:
        content = {"logsout": {"time": cols["t"], "signals": {"values": np.column_stack([cols["Speed"], cols["SteeringAngle"]]),
                                                             "label": "Speed"}}}
    savemat(tmp_path / "run.mat", content)
    if layout == "simulink":
        raw = read_file(tmp_path / "run.mat")
        assert list(raw.data.columns) == ["Time", "Speed_1", "Speed_2"] and np.allclose(raw.data["Speed_1"], cols["Speed"])
        return
    s = load_session(tmp_path / "run.mat")
    assert {"speed": "Speed", "steering": "SteeringAngle", "lap": "LapNumber"}.items() <= s.mapping.items()
    assert s.mapping["time"] in ("t", "time") and len(s.laps) == 3
    assert trace_error(s, ref, "speed")[1] <= 1e-6


def test_mat_v73(tmp_path, ref):
    h5py = pytest.importorskip("h5py")
    path = tmp_path / "run.mat"
    with h5py.File(path, "w", userblock_size=512) as f:
        f["Time"] = ref["time"].to_numpy().reshape(1, -1)
        f["Speed"] = ref["speed"].to_numpy().reshape(1, -1)
        f["LapNumber"] = ref["lap"].to_numpy(float).reshape(1, -1)
    with open(path, "r+b") as f:
        f.write(b"MATLAB 7.3 MAT-file, Platform: MACI64".ljust(512, b" "))
    s = load_session(path)
    assert {"time": "Time", "speed": "Speed", "lap": "LapNumber"}.items() <= s.mapping.items()
    assert trace_error(s, ref, "speed")[1] <= 1e-6


def test_mat_without_telemetry_says_what_it_found(tmp_path):
    from scipy.io import savemat
    savemat(tmp_path / "x.mat", {"gain": 2.0, "label": "nothing here"})
    with pytest.raises(ValueError, match="No telemetry was found.*gain"):
        read_file(tmp_path / "x.mat")


# ── AiM ───────────────────────────────────────────────────────────────────────

def test_aim_xrk_through_libxrk(tmp_path, ref, logger, monkeypatch):
    libxrk = pytest.importorskip("libxrk")
    import pyarrow as pa

    def channel(name, values, t, units, interpolate=True):
        meta = libxrk.ChannelMetadata(units=units, interpolate=interpolate).to_field_metadata()
        schema = pa.schema([pa.field("timecodes", pa.int64()), pa.field(name, pa.float64(), metadata=meta)])
        return pa.table([pa.array(np.round(t * 1000).astype(np.int64)), pa.array(np.asarray(values, float))], schema=schema)

    t, half = ref["time"].to_numpy() + 12.5, ref.iloc[::2]          # the logger's clock does not start at zero
    channels = {"GPS Speed": channel("GPS Speed", ref["speed"] / 3.6, t, "m/s"),
                "RPM": channel("RPM", half["rpm"], half["time"].to_numpy() + 12.5, "rpm"),
                "GPS_LateralAcc": channel("GPS_LateralAcc", ref["acc_lat"], t, "g"),
                "Gear": channel("Gear", ref["gear"], t, "", interpolate=False)}
    starts = np.array([0.0, *BEACONS]) + 12.5
    laps = pa.table({"num": [0, 1, 2], "start_time": (starts * 1000).astype(np.int64),
                     "end_time": (np.r_[starts[1:], t[-1]] * 1000).astype(np.int64)})
    log = libxrk.LogFile(channels, laps, {"Venue": "Synthetic Circuit", "Racer": "Test Driver"}, "run.xrk")
    monkeypatch.setattr(libxrk, "aim_xrk", lambda data: log)

    path = tmp_path / "run.xrk"
    path.write_bytes(b"<hCNF" + bytes(64))
    s = load_session(path)
    assert s.raw.format == "AiM .xrk"
    assert s.raw.info["Venue"] == "Synthetic Circuit" and s.raw.info["Driver"] == "Test Driver"
    assert {"speed": "GPS Speed", "rpm": "RPM", "acc_lat": "GPS_LateralAcc", "gear": "Gear"}.items() <= s.mapping.items()
    assert np.allclose(s.raw.beacons, BEACONS), "lap starts, on the clock of the table"
    assert_same_laps(s, logger, 1e-3)
    assert trace_error(s, ref, "speed")[1] <= 1e-6


def test_aim_file_that_cannot_be_read(tmp_path):
    pytest.importorskip("libxrk")
    (tmp_path / "broken.xrk").write_bytes(b"<hCNF" + bytes(200))
    with pytest.raises(ValueError):
        read_file(tmp_path / "broken.xrk")


def test_aim_csv_export(tmp_path, ref, logger):
    cols = ref[["time", "speed", "rpm"]].iloc[::1]
    head = ['"Format","AIM CSV File"', '"Venue","Synthetic Circuit"', '"Vehicle","GT3"', '"Racer","Test Driver"',
            '"Date","Thursday, October 8, 2026"', '"Sample Rate","20"',
            '"Beacon Markers",' + ",".join(f'"{b:.3f}"' for b in BEACONS), "",
            '"Time","GPS Speed","RPM"', '"Time","GPS Speed","RPM"', '"s","km/h","rpm"', '"1","2","3"', ""]
    body = "\n".join(f'"{r.time:.3f}","{r.speed:.3f}","{r.rpm:.1f}"' for r in cols.itertuples())
    (tmp_path / "aim.csv").write_text("\n".join(head) + "\n" + body + "\n")
    s = load_session(tmp_path / "aim.csv")
    assert s.raw.format == "AiM CSV" and s.raw.info["Driver"] == "Test Driver"
    assert len(s.raw.data) == len(ref), "the row of channel numbers is not a sample"
    assert s.mapping["speed"] == "GPS Speed"
    assert_same_laps(s, logger, 1e-3)


# ── telling formats apart ─────────────────────────────────────────────────────

def test_format_is_recognised_from_content_not_only_the_name(frames, ref):
    assert detect_format("x.bin", fw.asc(frames[:50]).encode()) == "asc"
    assert detect_format("x.txt", fw.candump(frames[:50]).encode()) == "candump"
    assert detect_format("x.txt", fw.trc_v2(frames[:50]).encode()) == "trc"
    assert detect_format("x.csv", fw.can_csv(frames[:50]).encode()) == "can_csv"
    assert detect_format("x.txt", fw.DBC_TEXT.encode()) == "dbc"
    assert detect_format("x.txt", fw.vbo(ref.iloc[:50], 4000.0).encode()) == "vbo"
    assert detect_format("x.dat", b"MDF     4.10    ") == "mdf"
    assert detect_format("x.bin", b"MATLAB 5.0 MAT-file, Platform") == "mat"
    assert detect_format("x.bin", fw.ibt([("Speed", "float", "m/s", np.zeros(4))], 60)[:4096]) == "ibt"
    assert detect_format("run.LD", b"\x40\0\0\0" + bytes(4000)) == "ld"
    assert detect_format("x.csv", b"Time,Speed\n0,1\n0.1,2\n") == "csv"
    with pytest.raises(ValueError, match="not a format this engine reads"):
        detect_format("x.bin", bytes(range(256)) * 4)


def test_gzip_is_read_through_for_any_format(tmp_path, ref, logger):
    (tmp_path / "run.vbo.gz").write_bytes(gzip.compress(fw.vbo(ref, logger.lap_length, BEACONS).encode()))
    s = load_session(tmp_path / "run.vbo.gz")
    assert s.raw.format == "VBOX .vbo" and len(s.laps) == 3
    assert file_stem(tmp_path / "run.vbo.gz") == "run" and file_stem("a/b/session.csv") == "session"


def test_every_requested_format_is_listed():
    names = {row["format"] for row in supported_formats()}
    assert {"CSV", "MDF / MF4", "MAT", "AiM", "MoTeC", "VBOX", "iRacing", "Assetto Corsa", "CAN", "DBC"} <= names


# ── store and command line ────────────────────────────────────────────────────

def test_store_keeps_the_dbc_and_the_ldx_with_the_source(tmp_path, frames, ref):
    (tmp_path / "bus.asc").write_text(fw.asc(frames))
    (tmp_path / "run.ld").write_bytes(_ld_bytes(ref))
    (tmp_path / "run.ldx").write_text(fw.ldx(BEACONS))
    store = ProjectStore(tmp_path / "project")
    try:
        can_id = store.import_file(tmp_path / "bus.asc", dbc=_dbc_file(tmp_path))
        ld_id = store.import_file(tmp_path / "run.ld")
        listing = store.sessions().set_index("id")
        assert listing.loc[can_id, "format"] == "Vector ASCII CAN log" and listing.loc[ld_id, "format"] == "MoTeC .ld"
        assert [p.name for p in store.dbc_files(can_id)] == ["car.dbc"]
        assert store.source_file(ld_id).name == "run.ld"
        (tmp_path / "run.ldx").unlink()                        # the stored copies are enough to rebuild
        (tmp_path / "db" / "car.dbc").unlink()
        assert len(store.reload_session(ld_id).laps) == 3
        again = store.reload_session(can_id)
        pd.testing.assert_frame_equal(again.channels, store.channels(can_id)[again.channels.columns], check_dtype=False)
    finally:
        store.close()


def test_cli_reads_any_format(tmp_path, frames, ref, logger, capsys):
    (tmp_path / "run.vbo").write_text(fw.vbo(ref, logger.lap_length, BEACONS))
    (tmp_path / "bus.log").write_text(fw.candump(frames))
    assert cli_main(["formats"]) == 0
    assert "MoTeC" in capsys.readouterr().out
    assert cli_main(["report", str(tmp_path / "run.vbo"), "-o", str(tmp_path / "out.html")]) == 0
    assert (tmp_path / "out.html").stat().st_size > 10_000
    assert cli_main(["inspect", str(tmp_path / "bus.log")]) == 1
    assert "needs a DBC" in capsys.readouterr().err
    assert cli_main(["inspect", str(tmp_path / "bus.log"), "--dbc", str(_dbc_file(tmp_path))]) == 0
    assert "candump CAN log" in capsys.readouterr().out


def test_lap_timing_displays_are_not_the_lap_counter():
    from veltrix_engine.mapping import auto_map
    cols = ["Time", "GPS Speed", "Prev Lap Diff", "Best Run Diff", "Ref Lap Diff", "Predictive Time", "LapDistPct"]
    assert "lap" not in auto_map(cols)
    assert auto_map(cols + ["Lap"])["lap"] == "Lap"


# ── MDF: metadata first, lazy, cancellable ────────────────────────────────────

@pytest.fixture()
def mf4(tmp_path, ref):
    asammdf = pytest.importorskip("asammdf")
    t, slow = ref["time"].to_numpy(), ref.iloc[::4]
    mdf = asammdf.MDF(version="4.10")
    mdf.append([asammdf.Signal(ref["speed"].to_numpy(), t, name="Ground Speed", unit="km/h"),
                asammdf.Signal(ref["steering"].to_numpy(), t, name="Steered Angle", unit="deg"),
                asammdf.Signal(ref["lap"].to_numpy(np.int32), t, name="Lap Number")], comment="chassis 20 Hz")
    mdf.append([asammdf.Signal(slow["rpm"].to_numpy(), slow["time"].to_numpy(), name="Engine RPM", unit="rpm"),
                asammdf.Signal(np.full(len(slow), 90.0), slow["time"].to_numpy(), name="Oil Temp")], comment="engine 5 Hz")
    path = tmp_path / "run.mf4"
    mdf.save(path, overwrite=True)
    mdf.close()
    return path


def test_mdf_is_described_without_reading_its_samples(mf4, ref):
    from veltrix_engine.importers import inspect_file
    info = inspect_file(mf4)
    assert info["format"] == "MDF" and info["version"].startswith("4") and info["file"] == "run.mf4"
    assert (info["group_count"], info["channel_count"]) == (2, 5) and info["rates_hz"] == [5.0, 20.0]
    assert info["duration_s"] == pytest.approx(ref["time"].iloc[-1], abs=0.2)
    assert info["missing_units"] == ["Lap Number", "Oil Temp"] and info["unsupported"] == [] and info["raw_can_frames"] == 0
    rpm = next(c for c in info["channels"] if c["name"] == "Engine RPM")
    assert (rpm["unit"], rpm["group"], rpm["rate_hz"], rpm["samples"]) == ("rpm", 1, 5.0, len(ref.iloc[::4]))
    assert [g["comment"] for g in info["groups"]] == ["chassis 20 Hz", "engine 5 Hz"]
    other = inspect_file(GOLDEN / "logger_layout_3lap_20hz.csv")           # other formats are read, then described
    assert other["format"] == "CSV" and other["channel_count"] == 13 and other["rates_hz"] == [20.0]
    assert other["missing_units"] == ["Gear"]


def test_mdf_loads_only_the_channels_asked_for_and_reports_progress(mf4):
    ticks = []
    raw = read_file(mf4, channels=["Ground Speed", "Engine RPM", "Not There"], progress=lambda done, total: ticks.append((done, total)))
    assert set(raw.data.columns) == {"Time", "Ground Speed", "Engine RPM"} and ticks == [(1, 2), (2, 2)]
    assert any("Not in the file: Not There" in m for _, m in raw.notes)
    assert any("2 channel(s) were read; 3 were left on disk" in m for _, m in raw.notes)
    assert raw.signals["Engine RPM"].t[1] - raw.signals["Engine RPM"].t[0] == pytest.approx(0.2), "its own 5 Hz clock"
    s = load_session(mf4, channels=["Ground Speed", "Lap Number"])
    assert set(s.mapping) == {"time", "speed", "lap"} and len(s.laps) == 3
    assert len(read_file(mf4).data.columns) == 6


def test_mdf_import_can_be_cancelled_and_leaves_the_file_free(mf4):
    from veltrix_engine.importers.mdf import ImportCancelled
    seen = []
    with pytest.raises(ImportCancelled, match="cancelled"):
        read_file(mf4, progress=lambda done, total: seen.append(done), cancel=lambda: len(seen) >= 2)
    assert seen == [1, 2], "it stopped between channels"
    mf4.rename(mf4.with_name("moved.mf4"))                                 # the handle was released
    assert read_file(mf4.with_name("moved.mf4")).format.startswith("MDF 4")


def test_damaged_and_unsupported_mdf_files_say_what_is_wrong(mf4, tmp_path):
    from veltrix_engine.importers import inspect_file
    cut = tmp_path / "cut.mf4"
    cut.write_bytes(mf4.read_bytes()[:1500])
    for reader in (read_file, inspect_file):
        with pytest.raises(ValueError, match=r"cut\.mf4 could not be read as MDF.*cut short or damaged"):
            reader(cut)
    header_only = tmp_path / "empty.mf4"
    header_only.write_bytes(mf4.read_bytes()[:64])
    with pytest.raises(ValueError, match="could not be read as MDF|no numeric channels"):
        read_file(header_only)


# ── laps from a lap timer channel (found on a real ACC .ld without its .ldx) ──

def _lap_timer(ref):
    """A lap timer as loggers write it: seconds since the last line crossing."""
    t = ref["time"].to_numpy()
    starts = np.array([0.0, *BEACONS])
    return t - starts[np.searchsorted(starts, t, side="right") - 1]


def test_ld_without_ldx_takes_laps_from_its_lap_timer_channel(tmp_path, ref, logger):
    half = ref.iloc[::2]
    channels = [("SPEED", "km/h", 20, ref["speed"], "f4"), ("RPMS", "1/min", 20, ref["rpm"], "f4"),
                ("LAP_BEACON", "..", 20, np.zeros(len(ref)), "f4"),                 # present but never set, as in the real file
                ("TYRE_PRESS_LF", "..", 10, np.full(len(half), 28.5), "f4"),
                ("TIME", "..", 10, _lap_timer(half), "f4")]
    (tmp_path / "run.ld").write_bytes(fw.ld(channels))
    s = load_session(tmp_path / "run.ld")
    assert s.lap_source == "beacon markers" and np.allclose(s.raw.beacons, BEACONS, atol=1e-3)
    assert_same_laps(s, logger, 1e-3)
    assert any("line crossing(s) taken from the lap timer channel 'TIME'" in m for _, m in s.notes)
    assert "TIME" not in s.mapping.values(), "the lap timer is not the session clock"
    assert s.raw.units.get("TYRE_PRESS_LF", "") == "" and s.raw.units.get("LAP_BEACON", "") == "", "'..' is not a unit"
    assert any("TYRE_PRESS_LF has no unit in the file and looks like psi" in w for w in s.warnings())
    assert s.channels["tyre_press_fl"].mean() == pytest.approx(28.5 * 0.0689476, rel=1e-3)


def test_a_channel_is_only_a_lap_timer_if_it_behaves_like_one():
    from veltrix_engine.importers.table import lap_timer_crossings
    t = np.arange(0, 300, 0.02)
    timer = t % 100.0
    found, name = lap_timer_crossings([Signal("TIME", t, timer)])
    assert name == "TIME" and np.allclose(found, [100.0, 200.0], atol=1e-6), "placed where the timer read zero"
    late = Signal("TIME", t, np.where(timer < 0.013, timer, timer))                  # first sample after the line reads 0.0
    assert np.allclose(lap_timer_crossings([late])[0], [100.0, 200.0], atol=0.02)
    assert lap_timer_crossings([Signal("Speed", t, timer)]) is None, "the name must say timer"
    assert lap_timer_crossings([Signal("TIME", t, t)]) is None, "a clock that never restarts is not a lap timer"
    assert lap_timer_crossings([Signal("TIME", t, (t % 100.0) * 3)]) is None, "nor one that does not count seconds"
    assert lap_timer_crossings([Signal("TIME", t, np.where(t < 150, t, t - 150))]) is None, "one restart is not enough"
    given = signals_to_table([Signal("TIME", t, timer), Signal("Speed", t, timer)], "test", beacons=np.array([50.0]))
    assert list(given.beacons) == [50.0], "markers the file gives outright are not replaced"


def test_a_pedal_logged_as_a_float32_fraction_is_still_read_as_a_fraction(tmp_path):
    """Any logger that writes a 0–1 pedal through a 32-bit float can read 1.00000012
    when the pedal is flat. That is still a fraction, so it becomes 0–100 %."""
    t = np.arange(0, 60, 0.05)
    flat = float(np.nextafter(np.float32(1), np.float32(2)))
    throttle = np.clip(np.sin(t / 3) * 1.4, 0, None)
    throttle = np.where(throttle >= 1, flat, throttle)
    df = pd.DataFrame({"Time": t, "Speed": 150 + 20 * np.sin(t / 5), "Throttle": throttle, "Brake": np.clip(-np.sin(t / 3), 0, 1)})
    path = tmp_path / "pedals.csv"
    df.to_csv(path, index=False, float_format="%.9g")
    s = load_session(path)
    assert s.channels["throttle"].max() == pytest.approx(100, abs=1e-3) and s.units["throttle"] == "%"
    assert (s.channels["throttle"] >= 99.9).mean() > 0.2
    assert s.channels["brake"].max() == pytest.approx(100, abs=1e-3)
