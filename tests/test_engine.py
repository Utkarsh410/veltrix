"""
Behaviour tests: properties the engine must hold whatever the numbers are, and
the known answers built into the synthetic session.
"""

import numpy as np
import pandas as pd
import pytest

from conftest import GOLDEN
from veltrix_engine import build_session, laps as lap_engine, load_session
from veltrix_engine.analysis import gg, tyres
from veltrix_engine.importers import parse_csv_text, read_csv
from veltrix_engine.mapping import auto_map
from veltrix_engine.models import arb, pacejka
from veltrix_engine.schema import CHANNELS
from veltrix_engine.synth import LLTD_FRONT
from veltrix_engine.track import track_xy
from veltrix_engine.vehicle import VehicleParams


# ── schema and mapping ────────────────────────────────────────────────────────

def test_every_channel_has_a_unit_and_description():
    for cid, cdef in CHANNELS.items():
        assert cdef.unit and cdef.description, cid
        if cdef.kind == "derived":
            assert cdef.requires, f"{cid} must declare what it is computed from"


@pytest.mark.parametrize("column, channel", [
    ("Ground Speed", "speed"), ("vCar", "speed"), ("G Force Lat", "acc_lat"),
    ("LateralAcc", "acc_lat"), ("Steered Angle", "steering"), ("Brake Pres Front", "brake"),
    ("TyreTempFL_inner", "tyre_temp_fl_inner"), ("Tyre Temp RR Centre", "tyre_temp_rr_mid"),
    ("Tyre Temp FL", "tyre_temp_fl"), ("TyrePressFR", "tyre_press_fr"),
    ("SuspTravelRL", "susp_travel_rl"), ("Lap Number", "lap"), ("GPS Latitude", "gps_lat"),
])
def test_known_names_map_to_their_channel(column, channel):
    assert auto_map([column]).get(channel) == column


@pytest.mark.parametrize("column, not_channel", [
    ("Brake Temp FL", "brake"), ("Wheel Speed FL", "speed"), ("Max Speed", "acc_long"),
    ("Lap Time", "lap"), ("Steering Torque", "steering"), ("Tyre Pressure FL", "tyre_temp_fl"),
    ("Engine Speed Limit", "rpm"),
])
def test_lookalike_names_are_not_mistaken(column, not_channel):
    assert auto_map([column]).get(not_channel) is None


def test_each_column_is_used_once():
    mapping = auto_map(["Speed", "GPS Speed", "Time", "Lap", "Lap Distance"])
    assert len(set(mapping.values())) == len(mapping)
    assert mapping["speed"] == "Speed" and mapping["distance"] == "Lap Distance"


# ── importer ──────────────────────────────────────────────────────────────────

def test_semicolon_file_with_decimal_commas():
    text = "Time;Speed;Throttle\n0,00;100,5;50,0\n0,01;101,5;55,0\n0,02;102,5;60,0\n"
    raw = parse_csv_text(text)
    assert raw.sep == ";" and raw.decimal == ","
    assert raw.data["Speed"].tolist() == [100.5, 101.5, 102.5]


def test_logger_layout_header_units_and_beacons():
    raw = read_csv(GOLDEN / "logger_layout_3lap_20hz.csv")
    assert raw.info["Venue"] == "Synthetic Circuit"
    assert raw.units["Ground Speed"] == "m/s"
    assert len(raw.beacons) == 2
    assert raw.data.columns[0] == "Time" and len(raw.data) == 4848


def test_unreadable_cells_are_blanked_and_reported():
    rows = "\n".join(f"{i * 0.01:.2f},{100 + i},{50}" for i in range(60))
    raw = parse_csv_text("Time,Speed,Throttle\n" + rows.replace("0.05,105,50", "0.05,ERR,50"))
    assert raw.bad_cells == {"Speed": 1}
    assert np.isnan(raw.data["Speed"].iloc[5])


def test_missing_required_channel_is_a_clear_error():
    raw = parse_csv_text("Time,Throttle\n" + "\n".join(f"{i * 0.01},{i}" for i in range(50)))
    with pytest.raises(ValueError, match="speed"):
        build_session(raw)


# ── normalisation ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["synthetic", "logger", "monza"])
def test_time_base_is_uniform_and_distance_monotonic(sessions, key):
    s = sessions[key]
    assert np.allclose(np.diff(s.channels["time"]), 1.0 / s.rate_hz, atol=1e-9)
    assert (np.diff(s.channels["distance"]) >= -1e-9).all()
    for lap in s.laps.itertuples():
        d = s.channels.loc[s.channels["lap"] == lap.lap, "lap_distance"].dropna().to_numpy()
        assert (np.diff(d) >= -1e-6).all(), f"lap {lap.lap} distance goes backwards"
        assert d.min() >= -1.0 and d.max() <= s.lap_length * 1.01


def test_units_are_converted_from_the_units_row(logger, synthetic):
    # The logger-layout file states m/s and m/s²; the engine must end up in km/h and g
    assert logger.units["speed"] == "km/h" and logger.units["acc_lat"] == "g"
    assert logger.channels["speed"].max() == pytest.approx(synthetic.channels["speed"].max(), rel=5e-3)
    assert logger.channels["acc_lat"].abs().max() == pytest.approx(
        synthetic.channels["acc_lat"].abs().max(), rel=0.03)


def test_unitless_pressure_in_psi_is_flagged_and_converted(synthetic):
    assert any("psi" in w for w in synthetic.warnings())
    assert 1.5 < synthetic.channels["tyre_press_fl"].mean() < 2.2          # bar


def test_normalised_steering_is_flagged(monza):
    assert any("radians or a normalised value" in w for w in monza.warnings())


def test_sign_flip_option(synthetic):
    flipped = load_session(GOLDEN / "synthetic_gt3_5lap.csv.gz", flip=("acc_lat", "steering"))
    assert np.allclose(flipped.channels["acc_lat"], -synthetic.channels["acc_lat"])
    # Mirroring the car left-to-right must not change the balance read
    assert np.allclose(flipped.derived["oversteer_metric"], synthetic.derived["oversteer_metric"], atol=1e-9)


# ── laps ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["synthetic", "logger", "monza"])
def test_lap_times_add_up_to_the_session(sessions, key):
    laps = sessions[key].laps
    assert np.allclose(laps["t_end"].iloc[:-1], laps["t_start"].iloc[1:])
    assert (laps["lap_time_s"] > 0).all()


def test_beacon_laps_agree_with_the_lap_channel(synthetic, logger):
    # Same three laps, once from the lap channel at 100 Hz, once from beacons at 20 Hz
    a = synthetic.laps.set_index("lap")["lap_time_s"]
    b = logger.laps.set_index("lap")["lap_time_s"]
    assert np.allclose(a.loc[[1, 2]], b.loc[[1, 2]], atol=0.011)


def test_lap_timer_that_restarts_is_stitched():
    t = np.r_[np.arange(0, 10, 0.1), np.arange(0, 12, 0.1), np.arange(0, 11, 0.1)]
    raw = parse_csv_text(pd.DataFrame({"Time": t, "Speed": 150.0}).to_csv(index=False))
    s = build_session(raw)
    assert s.lap_source == "lap timer restarts"
    assert len(s.laps) == 3
    assert s.laps["lap_time_s"].iloc[1] == pytest.approx(11.9, abs=0.11)


def test_position_gate_times_laps_without_a_lap_channel(monza):
    # Gate 3 km into the lap. (This layout runs past the start line twice per
    # lap on parallel straights, so the line itself is not a usable gate.)
    raw = read_csv(GOLDEN / "monza_ac_gt3.csv.gz")
    lap, dist = raw.data["Lap"].to_numpy(), raw.data["Distance"].to_numpy()
    gate = int(np.flatnonzero((lap == 1) & (dist == 3000))[0])
    raw.data = raw.data.drop(columns=["Lap", "Distance"])
    s = build_session(raw, start_finish_index=gate)
    assert s.lap_source == "a position gate"
    assert len(s.laps) == 6                      # five crossings split the log into six runs

    # Each crossing must be when that lap reached 3000 m according to the lap channel
    ch = monza.channels
    want = [np.interp(3000.0, g["lap_distance"], g["time"]) for _, g in ch.groupby("lap")]
    got = s.laps["t_end"].iloc[:-1].to_numpy()
    # Position in this file only updates every few samples, so a crossing can
    # read up to ~0.07 s late; the lag is the same every lap and cancels in lap times.
    assert np.allclose(got, want, atol=0.1)
    assert np.allclose(np.diff(got), np.diff(want), atol=0.03)


def test_delta_ends_at_the_lap_time_difference(sessions):
    for s in sessions.values():
        full = s.laps[s.laps["start_crossed"] & s.laps["end_crossed"]]
        ref, other = full.iloc[0], full.iloc[1]
        cmp_ = lap_engine.compare_laps(s, ref.lap, other.lap)
        assert cmp_["delta_t"].iloc[0] == pytest.approx(0.0, abs=0.05)
        assert cmp_["delta_t"].iloc[-1] == pytest.approx(other.lap_time_s - ref.lap_time_s, abs=0.05)


def test_a_lap_compared_with_itself_has_zero_delta(synthetic):
    cmp_ = lap_engine.compare_laps(synthetic, 2, 2)
    assert np.abs(cmp_["delta_t"]).max() < 1e-12


def test_sectors_sum_to_the_lap_time(sessions):
    for s in sessions.values():
        sec = lap_engine.sector_times(s, n_sectors=4)
        assert np.allclose(sec[["s1", "s2", "s3", "s4"]].sum(axis=1), sec["lap_time_s"])
        assert lap_engine.theoretical_best(sec) <= sec.loc[sec["status"] == "flying", "lap_time_s"].min() + 1e-9


# ── derived channels: answers built into the synthetic session ────────────────

def cornering(s):
    f = s.frame()
    return f[f["acc_lat"].abs() > 0.3]


def test_calculated_lateral_g_tracks_measured(synthetic):
    c = cornering(synthetic)
    assert np.corrcoef(c["acc_lat"], c["acc_lat_calc"])[0, 1] > 0.98
    assert synthetic.derived["acc_long_calc"].abs().max() < 3.0


def test_load_transfer_split_recovers_the_built_in_value(synthetic):
    assert synthetic.derived["lltd_front"].mean() == pytest.approx(LLTD_FRONT, abs=0.01)


def test_understeer_that_builds_through_the_stint(synthetic):
    per_lap = cornering(synthetic).groupby("lap")["oversteer_metric"].mean()
    assert (per_lap < 0).all() and per_lap.iloc[-1] < per_lap.iloc[0]


def test_grip_utilisation_stays_inside_the_envelope(synthetic):
    assert synthetic.derived["grip_util"].max() < 1.1
    assert 0.3 < gg.gg_summary(synthetic)["mean_util"] < 0.6


def test_derived_channels_are_skipped_not_invented(monza):
    # No lateral acceleration in this file: nothing that needs it may appear
    assert "oversteer_metric" not in monza.derived and "grip_util" not in monza.derived
    assert "acc_long_calc" in monza.derived


# ── tyres ─────────────────────────────────────────────────────────────────────

def test_lockup_is_the_only_anomaly(synthetic):
    events = tyres.detect_anomalies(synthetic)
    assert [(e.lap, e.corner) for e in events.itertuples()] == [(3, "fl")]


def test_left_tyres_run_hotter_on_a_clockwise_lap(synthetic):
    ls = tyres.lap_summary(synthetic).iloc[1:]
    assert ls["temp_fl_mean"].mean() > ls["temp_fr_mean"].mean()
    assert ls["temp_rl_mean"].mean() > ls["temp_rr_mean"].mean()


def test_degradation_rate_is_positive_and_well_fitted(synthetic):
    deg = tyres.fit_degradation(synthetic)
    assert 0.08 < deg["slope"] < 0.20 and deg["r_squared"] > 0.95


# ── track map ─────────────────────────────────────────────────────────────────

def test_logged_position_gives_a_closed_lap(monza):
    xy = track_xy(monza, lap=1)
    assert xy.attrs["source"] == "position"
    gap = np.hypot(xy["x"].iloc[-1] - xy["x"].iloc[0], xy["y"].iloc[-1] - xy["y"].iloc[0])
    assert gap < 5.0
    path = np.hypot(np.diff(xy["x"]), np.diff(xy["y"])).sum()
    assert path == pytest.approx(monza.lap_length, rel=0.01)


def test_reconstructed_map_is_labelled_as_such(synthetic):
    xy = track_xy(synthetic, lap=2)
    assert xy.attrs["source"] == "reconstructed"
    path = np.hypot(np.diff(xy["x"]), np.diff(xy["y"])).sum()
    assert path == pytest.approx(synthetic.lap_length, rel=0.01)


# ── models ────────────────────────────────────────────────────────────────────

def test_tyre_force_is_degressive_with_load():
    peaks = [pacejka.peak_lateral(fz) for fz in (2500, 3500, 4500, 5500)]
    assert all(a["Fy_peak"] < b["Fy_peak"] for a, b in zip(peaks, peaks[1:]))   # more load, more force
    assert all(a["mu"] > b["mu"] for a, b in zip(peaks, peaks[1:]))             # but less per newton
    assert pacejka.lateral_force(0.0, 3500.0) == pytest.approx(0.0, abs=5.0)


def test_cornering_stiffness_matches_the_force_curve_slope():
    slope = (pacejka.lateral_force(0.05, 3500.0) - pacejka.lateral_force(-0.05, 3500.0)) / 0.1
    assert slope == pytest.approx(pacejka.cornering_stiffness(3500.0) * np.pi / 180, rel=1e-3)


def test_stiffer_front_bar_adds_understeer_without_wheel_lift():
    sweep = arb.sweep_front_arb()
    for _, corner in sweep.groupby("ay_g"):
        assert (np.diff(corner["K_us"]) > 0).all()
        assert (np.diff(corner["lltd_front"]) > 0).all()
    assert (sweep["min_inside_Fz"] > 0).all()


def test_baseline_roll_setup_matches_the_telemetry_split():
    assert arb.lltd_front(arb.RollSetup(), VehicleParams()) == pytest.approx(LLTD_FRONT, abs=0.005)
