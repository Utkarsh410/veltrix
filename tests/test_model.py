"""The canonical session model: layers that are never written over, channels
on their own timestamps, provenance on everything, and the existing analysis
running on a recorded view of it."""

import json

import numpy as np
import pandas as pd
import pytest

import format_writers as fw
from conftest import GOLDEN
from veltrix_engine import ProjectStore, build_session, load_session
from veltrix_engine.analysis import handling
from veltrix_engine.importers import parse_csv_text
from veltrix_engine.model import (CALCULATED, LABELS, LAYERS, MEASURED, RECONSTRUCTED, Annotation, Channel,
                                  SessionModel, events_from_incidents)
from veltrix_engine.model_store import load_model, save_model

LOGGER = GOLDEN / "logger_layout_3lap_20hz.csv"


# ── the existing workflow runs on the model ───────────────────────────────────

@pytest.mark.parametrize("key", ["synthetic", "logger", "monza"])
def test_every_session_is_a_view_of_its_model(sessions, key):
    s = sessions[key]
    m = s.model
    assert isinstance(m, SessionModel) and m.views["analysis"].frame is s.channels
    assert m.views["analysis"].rate_hz == s.rate_hz
    assert {c.id for c in m.channels("normalised")} == set(s.mapping)
    assert {c.id for c in m.channels("raw")} == set(map(str, s.raw.data.columns))
    assert {c.id for c in m.channels("corrected") + m.channels("calculated")} >= set(s.derived.columns)
    assert [(lap.lap, lap.time_s, lap.status) for lap in m.laps] == \
           [(r.lap, r.lap_time_s, r.status) for r in s.laps.itertuples()]
    assert [st.stint for st in m.stints] == sorted(s.laps["stint"].unique())
    assert m.sources[0].sha256 == s.raw.sha256 and m.sources[0].rows == len(s.raw.data)


def test_the_view_is_what_alignment_of_the_normalised_channels_gives(logger):
    m = logger.model
    aligned = m.align(["speed", "rpm", "gear"], timestamps=logger.channels["time"].to_numpy())
    for cid in ("speed", "rpm", "gear"):
        assert np.allclose(aligned[cid], logger.channels[cid], equal_nan=True), cid


# ── layers and labels ─────────────────────────────────────────────────────────

def test_raw_keeps_the_file_and_normalised_says_what_was_done(logger):
    m = logger.model
    raw, norm = m.get("Ground Speed", "raw"), m.get("speed", "normalised")
    assert raw.unit == "m/s" and norm.unit == "km/h" and raw.label == norm.label == MEASURED
    assert np.shares_memory(raw.values, logger.raw.data["Ground Speed"].to_numpy()), "no copy of the source data"
    assert np.allclose(norm.values, raw.values * 3.6)
    assert norm.provenance.inputs == ("raw:Ground Speed",) and norm.provenance.unit_source == "file"
    assert norm.provenance.parameters["scale"] == pytest.approx(3.6)
    assert m.get("Gear", "raw").unit == "unknown", "a column without a unit says so"
    assert m.get("speed") is norm and m.get("normalised:speed") is norm
    with pytest.raises(ValueError, match="not written over"):
        m.add(Channel("speed", "normalised", norm.timestamps, norm.values))
    with pytest.raises(KeyError, match="No channel 'nope'"):
        m.get("nope")


def test_a_unit_the_engine_had_to_assume_is_flagged_on_the_channel(synthetic):
    m = synthetic.model
    speed, pressure = m.get("speed", "normalised"), m.get("tyre_press_fl", "normalised")
    assert m.get("Speed", "raw").unit == "unknown"
    assert speed.provenance.unit_source == "assumed" and any("unit assumed" in q for q in speed.quality)
    assert pressure.provenance.unit_source == "plausibility" and pressure.unit == "bar"
    assert pressure.provenance.parameters["scale"] == pytest.approx(0.0689476)
    table = m.table("normalised").set_index("id")
    assert table.loc["speed", "unit_source"] == "assumed" and table.loc["rpm", "sample_rate"] == pytest.approx(100)


def test_derived_channels_are_labelled_calculated_with_their_inputs(synthetic):
    m = synthetic.model
    c = m.get("acc_long_calc")
    assert (c.layer, c.label, c.derived) == ("calculated", CALCULATED, True)
    assert c.dependencies == ("normalised:speed",) and "d(speed)/dt" in c.formula
    assert c.provenance.parameters["timebase"] == "the logger's own clock" and c.provenance.parameters["rate_hz"] == 100
    assert m.get("lap_distance").label == CALCULATED
    assert set(c.label for c in m.channels()) <= set(LABELS) and set(c.layer for c in m.channels()) <= set(LAYERS)


def test_a_rebuilt_channel_is_labelled_reconstructed_not_measured():
    raw = pd.read_csv(GOLDEN / "synthetic_gt3_5lap.csv.gz")
    raw["LateralAcc"] = raw["LateralAcc"].round(0)             # exported in whole g: unusable as logged
    s = build_session(parse_csv_text(raw.to_csv(index=False)))
    c = s.model.get("acc_lat_rebuilt")
    assert (c.layer, c.label) == ("corrected", RECONSTRUCTED)
    assert "normalised:acc_lat" in c.dependencies and "normalised:susp_travel_fl" in c.dependencies
    assert any("coarse steps" in q for q in s.model.get("acc_lat", "normalised").quality)
    assert s.model.get("acc_lat", "normalised").label == MEASURED


def test_a_calculated_channel_can_be_replaced_and_its_version_goes_up(logger):
    m = SessionModel()
    t = np.arange(5.0)
    m.add(Channel("x", "calculated", t, t * 2, label=CALCULATED))
    again = m.add(Channel("x", "calculated", t, t * 3, label=CALCULATED), replace=True)
    assert again.version == 2 and m.get("x").values[1] == 3
    with pytest.raises(ValueError):
        Channel("y", "calculated", t, t[:3])
    with pytest.raises(ValueError, match="Unknown label"):
        Channel("y", "calculated", t, t, label="guess")


# ── sampling ──────────────────────────────────────────────────────────────────

def test_channels_keep_their_own_rate_from_a_multi_rate_log(tmp_path, logger):
    ref = logger.channels
    half = ref.iloc[::2]
    (tmp_path / "run.ld").write_bytes(fw.ld([("Ground Speed", "km/h", 20, ref["speed"], ("i2", 2)),
                                             ("Engine RPM", "rpm", 10, half["rpm"], "f4"),
                                             ("Gear", "", 5, ref["gear"].iloc[::4], ("i2", 0))]))
    (tmp_path / "run.ldx").write_text(fw.ldx((80.660, 161.430)))
    s = load_session(tmp_path / "run.ld")
    m = s.model
    assert [m.get(c).sample_rate for c in ("speed", "rpm", "gear")] == pytest.approx([20, 10, 5])
    assert len(m.get("rpm").values) == len(half) and np.allclose(m.get("rpm").values, half["rpm"], atol=1e-3)
    assert len(m.get("Engine RPM", "raw").values) == len(half), "raw is the logger's samples, not the merged table"
    assert m.describe()["rates_hz"] == [5.0, 10.0, 20.0]
    assert len(s.channels) == len(ref) and s.rate_hz == 20, "the analysis view is still one clock"


def test_lookup_policy_interpolates_continuous_holds_discrete_and_marks_which():
    t = np.array([0.0, 1.0, 2.0, 3.0, 10.0, 11.0])
    speed = Channel("speed", "normalised", t, np.array([0.0, 10, 20, 30, 100, 110]))
    gear = Channel("gear", "normalised", t, np.array([1, 1, 2, 2, 5, 5.0]), discrete=True)
    values, exact = speed.at([1.0, 1.5, 6.0, -1.0, 12.0])
    assert list(exact) == [True, False, False, False, False]
    assert values[0] == 10 and values[1] == 15, "linear between samples"
    assert np.isnan(values[2]), "never interpolated across a gap"
    assert np.isnan(values[3]) and np.isnan(values[4]), "never extrapolated"
    held, exact = gear.at([1.5, 2.0, 2.5])
    assert list(held) == [1, 2, 2] and list(exact) == [False, True, False]
    bad = Channel("x", "normalised", t, np.array([0.0, np.nan, 20, 30, 100, 110]))
    assert bad.validity.tolist() == [True, False, True, True, True, True] and bad.meta()["invalid"] == 1
    assert bad.at([1.0])[0][0] == 10, "an invalid sample is skipped, its neighbours still bound the value"


# ── events and annotations ────────────────────────────────────────────────────

def test_detected_incidents_become_events_worded_as_candidates():
    from test_advice import staged_session

    def slide(raw, rows, bump):
        raw.loc[rows, "SteeringAngle"] -= 1.7 * raw.loc[rows, "SteeringAngle"] * bump
        raw.loc[rows, "ThrottlePos"] = 100.0
    s = staged_session({"where": [(lap, 1262, 1300) for lap in (3, 6, 8)], "do": slide})
    found = handling.analyse(s)["events"]
    events = events_from_incidents(found, s.laps, rule_version="1")
    assert len(events) == len(found) > 0
    e = events[0]
    assert e.kind == "possible oversteer" and e.label == "detected event" and e.confidence == "requires review"
    lap = s.lap_row(e.lap)
    assert lap.t_start < e.t_start < e.t_end < lap.t_end and e.evidence["corner"] == "T4"
    s.model.events = events
    s.model.annotations.append(Annotation("Driver reported a snap on exit", scope="lap", lap=3, author="RE"))
    manifest = json.loads(json.dumps(s.model.manifest()))
    assert manifest["events"][0]["label"] == "detected event" and manifest["annotations"][0]["author"] == "RE"


# ── on disk ───────────────────────────────────────────────────────────────────

def test_model_round_trips_without_resampling(tmp_path, synthetic):
    m = synthetic.model
    folder = save_model(m, tmp_path / "model")
    clocks = {np.asarray(c.timestamps, float).tobytes() for c in m.channels() if c.layer != "raw"}
    assert sorted(p.name for p in folder.iterdir()) == [f"group_{i}.parquet" for i in range(len(clocks))] + ["manifest.json"]
    back = load_model(folder)
    assert back.describe() == {**m.describe(), "channels": {**m.describe()["channels"], "raw": 0}}
    for c in m.channels():
        if c.layer == "raw":
            continue
        d = back.get(c.id, c.layer)
        assert np.array_equal(d.timestamps, c.timestamps) and d.meta() == c.meta()
        assert np.array_equal(d.values, c.values, equal_nan=True) if c.id != "lap" else list(d.values) == list(c.values)
    raw_meta = [e for e in back.manifest_channels if e["layer"] == "raw"]
    assert len(raw_meta) == len(m.channels("raw")) and all(e["file"] is None for e in raw_meta)
    only = load_model(folder, channels=["speed"])
    assert [c.ref for c in only.channels()] == ["normalised:speed"]


def test_a_newer_model_version_is_refused_with_a_reason(tmp_path, logger):
    folder = save_model(logger.model, tmp_path / "model")
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["model_version"] = 99
    (folder / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="newer release"):
        load_model(folder)


def test_store_keeps_the_model_and_gives_old_projects_one(tmp_path, logger):
    store = ProjectStore(tmp_path / "project")
    try:
        sid = store.import_file(LOGGER)
        folder = tmp_path / "project" / "sessions" / str(sid) / "model"
        assert (folder / "manifest.json").exists()
        m = store.model(sid, channels=["speed", "acc_long_calc"])
        assert {c.ref for c in m.channels()} == {"normalised:speed", "calculated:acc_long_calc"}
        assert np.array_equal(m.get("speed").values, logger.model.get("speed").values)
        assert [lap.time_s for lap in m.laps] == list(logger.laps["lap_time_s"])
        import shutil
        shutil.rmtree(folder)                               # a project saved before the model existed
        assert len(store.model(sid).channels("normalised")) == len(logger.mapping) and (folder / "manifest.json").exists()
        assert store.db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "3"
    finally:
        store.close()


def test_a_steady_log_is_analysed_on_its_own_clock_whatever_its_rate(monza):
    """60 Hz is not one of the standard rates; it used to be resampled to 100 Hz."""
    t = np.arange(0, 120, 1 / 60)
    df = pd.DataFrame({"Time": t, "Speed": 150 + 40 * np.sin(t / 7), "Steering": 20 * np.sin(t / 3)})
    s = build_session(parse_csv_text(df.to_csv(index=False)))
    assert s.rate_hz == 60 and len(s.channels) == len(t)
    assert np.allclose(s.channels["time"], t) and np.allclose(s.channels["speed"], df["Speed"]), "sample for sample"
    rate = s.model.get("steering_rate")
    assert rate.provenance.parameters["timebase"] == "the logger's own clock"
    assert "nothing was resampled" in s.model.views["analysis"].note
    # a log with no steady clock still goes to a standard rate, and says it was resampled
    assert monza.rate_hz == 100 and monza.model.get("steering_rate").provenance.parameters["timebase"] == "analysis view (resampled)"
    assert "nothing was resampled" not in monza.model.views["analysis"].note


def test_isolated_speed_spikes_are_blanked_at_import_and_real_changes_are_not():
    t = np.arange(0, 60, 0.02)
    speed = 150 + 60 * np.sin(t / 4)
    speed[t > 40] = np.maximum(speed[t > 40] - (t[t > 40] - 40) * 9.81 * 3.6 * 1.5, 30)      # 1.5 g of braking: real
    speed[1000] = 0.0                                                                          # a dropped sample
    speed[2000:2002] = 400.0                                                                   # a two-sample spike
    s = build_session(parse_csv_text(pd.DataFrame({"Time": t, "Speed": speed}).to_csv(index=False)))
    norm = s.model.get("speed", "normalised")
    assert (~norm.validity).sum() == 3 and not norm.validity[[1000, 2000, 2001]].any()
    assert s.model.get("Speed", "raw").values[1000] == 0.0, "the raw layer keeps what the file says"
    assert any("3 sample(s) jump up to" in w for w in s.warnings())
    assert abs(s.channels["speed"].iloc[1000] - (speed[999] + speed[1001]) / 2) < 0.5, "bridged in the analysis view"
    assert s.derived["acc_long_calc"].abs().max() < 3, "and no phantom 100 g in the derived channels"
