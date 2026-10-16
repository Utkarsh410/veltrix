"""
Cases found by running a real simulator export (a logger-software CSV of a
50-minute, three-stint session) through the engine. Each test rebuilds the
condition from the golden data, so the behaviour stays covered without
committing a 170 MB file.
"""

import numpy as np
import pandas as pd
import pytest

from conftest import GOLDEN
from veltrix_engine import build_session, laps as lap_engine
from veltrix_engine.analysis import tyres
from veltrix_engine.cli import main as cli_main
from veltrix_engine.importers import parse_csv_text, read_csv
from veltrix_engine.mapping import auto_map
from veltrix_engine.session import _lap_length_check

SYNTH = GOLDEN / "synthetic_gt3_5lap.csv.gz"


def export_layout(df: pd.DataFrame, units: dict, beacons=()) -> str:
    """A DataFrame as a logger-software CSV export: header block, units row, quoted cells."""
    head = ['"Format","Logger CSV File",,,"Workbook",""', '"Venue","monza",,,"Worksheet",""']
    if len(beacons):
        head.append('"Beacon Markers","' + " ".join(f"{b:.3f}" for b in beacons) + ' "')
    head += ["", "", ",".join(f'"{c}"' for c in df.columns),
             ",".join(f'"{units.get(c, "..")}"' for c in df.columns), "", ""]
    body = df.to_csv(index=False, header=False, quoting=1, float_format="%.4f")
    return "\n".join(head) + "\n" + body


@pytest.fixture(scope="module")
def synth_raw():
    return read_csv(SYNTH).data


# ── naming and units ──────────────────────────────────────────────────────────

def test_simulator_channel_names_are_recognised():
    cols = ["Time", "Distance", "LAP_BEACON", "G_LAT", "ROTY", "STEERANGLE", "SPEED", "THROTTLE",
            "BRAKE", "GEAR", "G_LON", "RPMS", "SUS_TRAVEL_LF", "BRAKE_TEMP_RF", "TYRE_PRESS_LR",
            "TYRE_TAIR_RR", "WHEEL_SPEED_LF", "TIME"]
    m = auto_map(cols)
    want = {"time": "Time", "distance": "Distance", "acc_lat": "G_LAT", "yaw_rate": "ROTY",
            "steering": "STEERANGLE", "speed": "SPEED", "brake": "BRAKE", "acc_long": "G_LON",
            "rpm": "RPMS", "susp_travel_fl": "SUS_TRAVEL_LF", "brake_temp_fr": "BRAKE_TEMP_RF",
            "tyre_press_rl": "TYRE_PRESS_LR", "tyre_temp_rr": "TYRE_TAIR_RR",
            "wheel_speed_fl": "WHEEL_SPEED_LF"}
    assert {k: m.get(k) for k in want} == want
    assert "lap" not in m, "a beacon flag column is not a lap counter"


def test_placeholder_units_are_treated_as_no_unit(synth_raw):
    df = synth_raw[["Time", "Speed", "TyrePressFL"]].iloc[:3000]
    raw = parse_csv_text(export_layout(df, {"Time": "s", "Speed": "km/h"}))
    assert "TyrePressFL" not in raw.units
    s = build_session(raw)
    assert any("looks like psi" in w for w in s.warnings())
    assert not any("not recognised" in w for w in s.warnings())


def test_acceleration_labelled_ms2_but_recorded_in_g_is_read_as_g(synth_raw):
    df = synth_raw[["Time", "LapNumber", "Speed", "SteeringAngle", "LateralAcc", "LongAcc"]]
    units = {"Time": "s", "Speed": "km/h", "LateralAcc": "m/s2", "LongAcc": "m/s2"}   # values are in g
    s = build_session(parse_csv_text(export_layout(df, units)))
    assert any("behave like g" in w for w in s.warnings())
    assert s.channels["acc_lat"].abs().max() == pytest.approx(synth_raw["LateralAcc"].abs().max(), rel=1e-3)


def test_correctly_labelled_acceleration_is_left_alone(logger):
    # The logger-layout golden file really is in m/s²
    assert not any("behave like" in w for w in logger.warnings())


def test_a_stated_unit_overrides_the_check(synth_raw):
    df = synth_raw[["Time", "LapNumber", "Speed", "SteeringAngle", "LateralAcc", "LongAcc"]]
    text = export_layout(df, {"Time": "s", "Speed": "km/h", "LateralAcc": "m/s2", "LongAcc": "m/s2"})
    s = build_session(parse_csv_text(text), units={"LongAcc": "m/s2", "LateralAcc": "m/s2"})
    assert not any("behave like" in w for w in s.warnings())
    assert s.channels["acc_lat"].abs().max() < 0.3          # taken at the caller's word


def test_whole_number_export_is_flagged_as_too_coarse(synth_raw):
    df = synth_raw[["Time", "LapNumber", "Speed", "SteeringAngle", "LateralAcc", "LongAcc"]].copy()
    df["Speed"] = (df["Speed"] / 3.6).round()                 # whole m/s
    df[["LateralAcc", "LongAcc"]] = df[["LateralAcc", "LongAcc"]].round()
    text = export_layout(df, {"Time": "s", "Speed": "m/s", "LateralAcc": "g", "LongAcc": "g"})
    s = build_session(parse_csv_text(text))
    coarse = [w for w in s.warnings() if "coarse steps" in w]
    assert len(coarse) == 1 and "Speed (3.6 km/h" in coarse[0] and "LateralAcc (1 g" in coarse[0]


def test_opposite_signs_are_reported_not_fixed(synth_raw):
    df = synth_raw[["Time", "LapNumber", "Speed", "SteeringAngle", "LateralAcc"]].copy()
    df["SteeringAngle"] *= -1
    s = build_session(parse_csv_text(df.to_csv(index=False)))
    assert any("opposite signs" in w for w in s.warnings())
    fixed = build_session(parse_csv_text(df.to_csv(index=False)), flip=("steering",))
    assert not any("opposite signs" in w for w in fixed.warnings())


# ── stints, pit stops, cut laps ───────────────────────────────────────────────

@pytest.fixture(scope="module")
def two_stints(synth_raw):
    """Laps 1-5, a 30 s stop, then laps 1-5 again as laps 6-10 on fresh tyres."""
    first = synth_raw.copy()
    dt = 0.01
    stop = pd.DataFrame({c: first[c].iloc[0] for c in first.columns}, index=range(3000))
    stop["Time"] = first["Time"].iloc[-1] + dt * (1 + np.arange(3000))
    stop["Speed"], stop["LateralAcc"], stop["LongAcc"], stop["LapNumber"] = 0.0, 0.0, 0.0, 6
    second = first.copy()
    second["Time"] += stop["Time"].iloc[-1] + dt
    second["LapNumber"] += 5
    return build_session(parse_csv_text(pd.concat([first, stop, second]).to_csv(index=False)))


def test_pit_stop_splits_the_session_into_stints(two_stints):
    laps = two_stints.laps.set_index("lap")
    assert list(laps["stint"]) == [1] * 5 + [2] * 5
    assert list(laps["stint_lap"]) == [0, 1, 2, 3, 4] * 2
    assert laps.loc[6, "status"] == "pit"
    assert any("1 pit stop(s)" in m for _, m in two_stints.notes)


def test_tyre_change_is_tagged_and_degradation_is_per_stint(two_stints):
    events = tyres.detect_anomalies(two_stints)
    in_pit = events[events["lap_status"] == "pit"]
    assert len(in_pit) >= 1 and (in_pit["delta_C"] < 0).all()      # temperatures drop to the new set
    a, b = tyres.fit_degradation(two_stints, stint=1), tyres.fit_degradation(two_stints, stint=2)
    assert a["slope"] > 0.08 and b["slope"] > 0.08
    assert set(b["laps"]) <= {7, 8, 9, 10}                          # the pit lap is not fitted
    summary = tyres.lap_summary(two_stints)
    assert summary.loc[summary["lap"] == 7, "tyre_age"].iloc[0] == 1


def test_short_lap_is_not_a_flying_lap():
    laps = pd.DataFrame({"lap": range(1, 9), "status": "flying",
                         "dist_m": [5728, 5731, 5726, 5697, 5728, 5724, 5729, 5727],
                         "lap_time_s": [115.0, 115.2, 114.9, 112.3, 115.1, 115.0, 114.8, 115.3]})
    notes = []
    out = _lap_length_check(laps, lambda level, msg: notes.append(msg))
    assert list(out.loc[out["status"] == "short", "lap"]) == [4]
    assert "m less than the typical" in notes[0]


def test_small_distance_scatter_is_not_called_a_cut(sessions):
    for s in sessions.values():
        assert "short" not in set(s.laps["status"])


# ── importer on a large quoted export ─────────────────────────────────────────

def test_quoted_export_with_blank_lines_and_crlf(synth_raw):
    df = synth_raw[["Time", "Speed", "ThrottlePos"]].iloc[:5000]
    lf = export_layout(df, {"Time": "s", "Speed": "km/h", "ThrottlePos": "%"}, beacons=[20.0, 40.0])
    for text in (lf, lf.replace("\n", "\r\n")):
        raw = parse_csv_text(text)
        assert list(raw.data.columns) == ["Time", "Speed", "ThrottlePos"] and len(raw.data) == 5000
        assert raw.units == {"Time": "s", "Speed": "km/h", "ThrottlePos": "%"}
        assert list(raw.beacons) == [20.0, 40.0]
        assert raw.data["Speed"].iloc[-1] == pytest.approx(df["Speed"].iloc[-1], abs=1e-3)


def test_duplicate_column_names_are_kept_apart():
    rows = "\n".join(f"{i * 0.01:.2f},{100 + i},{i}" for i in range(50))
    raw = parse_csv_text("Time,Speed,Time\n" + rows)
    assert list(raw.data.columns) == ["Time", "Speed", "Time.1"]


# ── command line ──────────────────────────────────────────────────────────────

def test_inspect_command(capsys, tmp_path):
    code = cli_main(["inspect", str(GOLDEN / "monza_ac_gt3.csv.gz"), "--export", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert code == 0
    for text in ("Laps", "← best", "Theoretical best", "Corners (", "Ideal lap", "Corner by corner",
                 "against reference lap", "Track map"):
        assert text in out
    assert {p.name for p in (tmp_path / "out").iterdir()} == \
           {"laps.csv", "sectors.csv", "lap_comparison.csv", "track_map.csv", "import_notes.csv",
            "corners.csv", "corner_metrics.csv", "corner_summary.csv", "suggestions.csv"}


def test_inspect_reports_a_missing_file(capsys):
    assert cli_main(["inspect", "nope.csv"]) == 1
    assert "Error" in capsys.readouterr().err


# ── rebuilding signals a coarse export has destroyed ──────────────────────────

def _degrade(raw: pd.DataFrame, dampers: bool = True) -> pd.DataFrame:
    """The synthetic session as the whole-number export would have written it:
    speed in whole m/s and accelerations in whole g at 20 Hz, 1 deg steering,
    1 mm dampers."""
    hold = lambda x: np.repeat(np.asarray(x)[::5], 5)[:len(x)]
    out = raw.copy()
    out["Speed"] = np.round(hold(raw["Speed"] / 3.6)) * 3.6
    out["LateralAcc"], out["LongAcc"] = np.round(hold(raw["LateralAcc"])), np.round(hold(raw["LongAcc"]))
    out["SteeringAngle"] = raw["SteeringAngle"].round()
    susp = [c for c in raw.columns if c.startswith("SuspTravel")]
    out[susp] = raw[susp].round()
    return out if dampers else out.drop(columns=susp)


@pytest.fixture(scope="module")
def coarse(synth_raw):
    return build_session(parse_csv_text(_degrade(synth_raw).to_csv(index=False)))


def rms(a, b):
    return float(np.sqrt(np.nanmean((np.asarray(a, float) - np.asarray(b, float)) ** 2)))


def test_coarse_channels_are_identified(coarse):
    assert set(coarse.coarse) == {"speed", "acc_lat", "acc_long"}
    assert coarse.coarse["acc_lat"] == pytest.approx(1.0)
    assert set(coarse.rebuilt) == {"acc_lat", "acc_long_calc"}


def test_lateral_g_is_rebuilt_from_damper_roll(coarse, synthetic):
    truth = synthetic.channels["acc_lat"].to_numpy()
    logged = rms(truth, coarse.channels["acc_lat"])
    rebuilt = rms(truth, coarse.derived["acc_lat_rebuilt"])
    assert logged > 0.2 and rebuilt < 0.06                 # an order of magnitude better
    info = coarse.rebuilt["acc_lat"]
    assert info["r"] > 0.9
    assert info["residual_g"] == pytest.approx(info["rounding_g"], rel=0.15)   # only rounding is left


def test_longitudinal_g_survives_whole_number_speed(coarse, synthetic):
    truth = synthetic.derived["acc_long_calc"].to_numpy()
    got = coarse.derived["acc_long_calc"].to_numpy()
    assert rms(truth, got) < 0.2
    assert -got.min() == pytest.approx(-truth.min(), rel=0.15)        # peak braking, not a spike


def test_grip_and_balance_use_the_rebuilt_signal(coarse, synthetic):
    from veltrix_engine.analysis import balance, gg
    a, b = gg.gg_summary(coarse), gg.gg_summary(synthetic)
    assert a["peak_lat_g"] == pytest.approx(b["peak_lat_g"], abs=0.1)
    assert a["mean_util"] == pytest.approx(b["mean_util"], abs=0.03)
    got, want = balance.balance_by_lap(coarse), balance.balance_by_lap(synthetic)
    assert np.allclose(got.values, want.values, atol=0.05)
    assert (got < 0).all() and got.iloc[-1] < got.iloc[0]             # still reads building understeer
    assert coarse.derived["lltd_front"].mean() == pytest.approx(0.53, abs=0.015)


def test_without_dampers_coarse_lateral_g_is_not_used(synth_raw):
    s = build_session(parse_csv_text(_degrade(synth_raw, dampers=False).to_csv(index=False)))
    assert "acc_lat_rebuilt" not in s.derived and "oversteer_metric" not in s.derived
    assert "grip_util" not in s.derived
    assert any("cannot be rebuilt" in w for w in s.warnings())
    assert "acc_long_calc" in s.derived                                # speed-based channels still work


def test_fine_data_is_never_rebuilt(sessions):
    for s in sessions.values():
        assert s.coarse == {} and s.rebuilt == {}
        assert "acc_lat_rebuilt" not in s.derived


# ── steering ratio and understeer gradient ────────────────────────────────────

def test_steering_ratio_and_understeer_gradient_are_recovered(synthetic):
    from veltrix_engine.analysis import balance
    from veltrix_engine.synth import KUS_BASE_DEG_G, KUS_DEG_PER_LAP
    sc = balance.steering_characteristic(synthetic)
    assert sc["ratio"] == pytest.approx(synthetic.vehicle.steer_ratio, rel=0.05)     # built in: 14
    built_in = KUS_BASE_DEG_G + KUS_DEG_PER_LAP * 2.5          # mean over flying laps 2-5
    assert sc["understeer_deg_per_g"] == pytest.approx(built_in, abs=0.03)
    by_lap = [balance.steering_characteristic(synthetic, laps=[lap], ratio=14.0)["understeer_deg_per_g"]
              for lap in (2, 3, 4, 5)]
    assert np.all(np.diff(by_lap) > 0)                         # understeer builds each lap
    assert np.diff(by_lap).mean() == pytest.approx(KUS_DEG_PER_LAP, abs=0.006)


def test_understeer_gradient_survives_the_coarse_export(coarse, synthetic):
    from veltrix_engine.analysis import balance
    a, b = balance.steering_characteristic(coarse), balance.steering_characteristic(synthetic)
    assert a["ratio"] == pytest.approx(b["ratio"], rel=0.08)
    assert a["understeer_deg_per_g"] == pytest.approx(b["understeer_deg_per_g"], abs=0.05)


# ── a few garbage samples must not defeat the checks ──────────────────────────

def test_glitch_samples_do_not_block_the_unit_fix_the_rebuild_or_the_map(synth_raw):
    """A second real export had a dozen samples reading over 1,000 in its g
    channels (a crash or a reset to the pits). They hid the g / m/s² mislabel
    and made damper roll look unrelated to lateral g, so no track map was drawn."""
    from veltrix_engine.track import track_map
    df = _degrade(synth_raw)[["Time", "LapNumber", "Speed", "SteeringAngle", "LateralAcc", "LongAcc",
                              "SuspTravelFL", "SuspTravelFR", "SuspTravelRL", "SuspTravelRR"]].copy()
    df.loc[20000:20010, "LateralAcc"] = -1448.0
    df.loc[20011:20021, "LateralAcc"] = 1592.0
    df.loc[20000:20010, "LongAcc"] = -1571.0
    units = {"Time": "s", "Speed": "km/h", "LateralAcc": "m/s2", "LongAcc": "m/s2"}     # values are in g
    s = build_session(parse_csv_text(export_layout(df, units)))

    glitch = [w for w in s.warnings() if "Treated as glitches" in w]
    assert len(glitch) == 2 and "read up to 1,592 in the file" in glitch[0]
    assert any("behave like g" in w for w in s.warnings())            # the mislabel is still caught
    assert "acc_lat" in s.rebuilt and s.rebuilt["acc_lat"]["r"] > 0.9   # and lateral g is still rebuilt
    assert s.derived["acc_lat_rebuilt"].abs().max() < 2.5
    assert track_map(s).attrs["source"] == "reconstructed"


def test_real_spikes_are_kept(synthetic):
    # nothing in a clean session is far enough outside the rest to be called a glitch
    assert not any("Treated as glitches" in w for w in synthetic.warnings())
