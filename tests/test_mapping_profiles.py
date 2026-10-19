"""Mapping profiles: decisions made in a fixed order, recorded per column, reusable across files."""

import numpy as np
import pandas as pd
import pytest

from conftest import GOLDEN
from test_real_export_cases import export_layout
from veltrix_engine import build_session
from veltrix_engine.importers import parse_csv_text, read_csv
from veltrix_engine.mapping_profiles import (Profile, Rule, decide, diagnostics, profile_from_decisions,
                                             to_import_args)


def table(columns: dict, units: dict | None = None):
    t = np.arange(0, 60, 0.05)
    df = pd.DataFrame({"Time": t, **{k: (v(t) if callable(v) else np.full(len(t), v)) for k, v in columns.items()}})
    text = export_layout(df, {"Time": "s", **(units or {})}).replace('".."', '""')
    return parse_csv_text(text)


def by_column(decisions):
    return {d.column: d for d in decisions}


def test_a_name_alone_never_decides_the_values_have_to_fit():
    raw = table({"Speed": lambda t: 100 + 50 * np.sin(t), "RPM": lambda t: 6000 + 1000 * np.sin(t),
                 "Throttle": lambda t: 5000 + 0 * t,                     # named throttle, but not a pedal position
                 "Chassis_Ref": 3.0}, {"Speed": "km/h", "RPM": "rpm", "Throttle": "%"})
    d = by_column(decide(raw))
    assert (d["Speed"].channel, d["Speed"].basis, d["Speed"].confirmed) == ("speed", "plausibility", False)
    assert d["Speed"].reason == "the name matches and the values fit" and d["Speed"].unit_source == "file"
    assert d["Throttle"].channel is None and "outside the -5 to 105" in d["Throttle"].problems[0]
    assert "the name suggests throttle, but the values do not fit" in d["Throttle"].reason
    assert (d["Chassis_Ref"].channel, d["Chassis_Ref"].basis) == (None, "unmapped")


def test_decision_order_metadata_then_verified_profile_then_plausibility():
    raw = table({"vCar": lambda t: 40 + 10 * np.sin(t), "nEngine": lambda t: 7000 + 0 * t, "Speed": lambda t: 150 + 0 * t,
                 "aSteer": lambda t: 20 * np.sin(t)})
    raw.hints = {"gear": "nEngine"}                                    # pretend the format itself named this column
    profile = Profile("Team A GT3", logger="ADL3", car="GT3", rules=[
        Rule("vCar", "speed", unit="m/s", confirmed=True), Rule("nEngine", "rpm", confirmed=True),
        Rule("asteer*", "steering", unit="deg", confirmed=False)])
    d = by_column(decide(raw, profile))
    assert (d["nEngine"].channel, d["nEngine"].basis) == ("gear", "metadata"), "what the format says comes before a profile"
    assert "outside the -1 to 12" in d["nEngine"].problems[0], "and is still checked against the values"
    assert (d["vCar"].channel, d["vCar"].basis, d["vCar"].confirmed) == ("speed", "profile", True)
    assert (d["vCar"].unit, d["vCar"].unit_source) == ("m/s", "profile"), "the profile supplies the unit the file lacks"
    assert (d["aSteer"].basis, d["aSteer"].confirmed) == ("plausibility", False), "an unconfirmed rule is only a suggestion"
    assert "not yet confirmed" in d["aSteer"].reason
    assert d["Speed"].channel is None and d["Speed"].problems[0].startswith("conflict: vCar is also speed")

    chosen = by_column(decide(raw, profile, user={"Speed": "speed", "aSteer": None}))
    assert (chosen["Speed"].basis, chosen["Speed"].confirmed) == ("user", True), "an explicit choice overrides the profile"
    assert chosen["vCar"].channel is None and chosen["aSteer"].basis == "unmapped"
    with pytest.raises(ValueError, match="not a column"):
        decide(raw, user={"Nope": "speed"})
    with pytest.raises(ValueError, match="not a channel"):
        decide(raw, user={"Speed": "warp_factor"})


def test_a_profile_rule_that_does_not_fit_the_data_is_flagged_not_trusted():
    extra = {"Aux1": 1.0, "Aux2": 2.0}                                # the CSV reader needs three columns to find a table
    raw = table({"vCar": lambda t: 9000 + 0 * t, **extra})
    profile = Profile("p", rules=[Rule("vCar", "speed", unit="km/h", confirmed=True)])
    d = by_column(decide(raw, profile))["vCar"]
    assert d.basis == "plausibility" and not d.confirmed and d.confidence <= 0.3 and "outside the 0 to 450" in d.problems[0]
    own = Profile("p", rules=[Rule("vCar", "speed", unit="km/h", valid_min=8000, valid_max=10000, confirmed=True)])
    assert by_column(decide(raw, own))["vCar"].basis == "profile", "a rule's own limits replace the general ones"
    clash = table({"vCar": lambda t: 50 + 0 * t, **extra}, {"vCar": "mph", "Aux1": "V"})
    d = by_column(decide(clash, Profile("p", rules=[Rule("vCar", "speed", unit="km/h", confirmed=True)])))["vCar"]
    assert d.unit == "mph" and "the file says mph, the profile expects km/h" in d.problems[0] and d.basis == "plausibility"


def test_decisions_drive_the_import_and_the_unit_source_is_recorded():
    raw = table({"vCar": lambda t: 40 + 10 * np.sin(t / 5), "nEngine": lambda t: 7000 + 500 * np.sin(t)})
    profile = Profile("Team A", rules=[Rule("vCar", "speed", unit="m/s", confirmed=True), Rule("nEngine", "rpm", confirmed=True)])
    decisions = decide(raw, profile)
    mapping, units = to_import_args(decisions)
    assert mapping == {"time": "Time", "speed": "vCar", "rpm": "nEngine"} and units == {"vCar": "m/s"}
    s = build_session(raw, mapping=mapping, units=units)
    assert s.channels["speed"].max() == pytest.approx(50 * 3.6, rel=1e-3), "m/s from the profile, converted to km/h"
    assert s.model.get("speed", "normalised").provenance.unit_source == "stated"
    report = diagnostics(raw, decisions)
    assert report["by_basis"] == {"metadata": 0, "profile": 2, "plausibility": 1, "user": 0, "unmapped": 0}
    assert report["needs_confirmation"] == ["Time"] and report["missing_required"] == [] and report["unknown_units"] == []


def test_diagnostics_list_what_a_person_still_has_to_look_at():
    raw = table({"Speed": lambda t: 30 + 5 * np.sin(t), "Throttle": lambda t: 5000 + 0 * t, "Odd": 1.0},
                {"Throttle": "%", "Odd": "V"})
    report = diagnostics(raw, decide(raw))
    assert report["unknown_units"] == ["Speed"], "plausible as a speed, but nothing says in what unit"
    assert report["failed_validation"] == ["Throttle"] and report["mapped"] == 2 and report["confirmed"] == 0
    blind = table({"Odd": 1.0, "Aux1": 1.0, "Aux2": 2.0}, {"Odd": "V", "Aux1": "V"})
    assert diagnostics(blind, decide(blind))["missing_required"] == ["speed"]


def test_a_profile_saves_reloads_and_reproduces_a_confirmed_mapping(tmp_path):
    raw = read_csv(GOLDEN / "logger_layout_3lap_20hz.csv")
    first = decide(raw)
    assert all(d.basis == "plausibility" for d in first if d.channel)
    profile = profile_from_decisions("Logger layout", first, confirmed=True, logger="Synthetic", car="GT3", colour="red")
    back = Profile.load(profile.save(tmp_path / "profiles" / "logger.json"))
    assert back.to_dict() == profile.to_dict() and (back.logger, back.car) == ("Synthetic", "GT3")
    second = decide(raw, back)
    assert all(d.basis == "profile" and d.confirmed for d in second if d.channel)
    assert to_import_args(second)[0] == to_import_args(first)[0]
    same = build_session(raw, mapping=to_import_args(second)[0])
    assert list(same.laps["lap_time_s"]) == list(build_session(read_csv(GOLDEN / "logger_layout_3lap_20hz.csv")).laps["lap_time_s"])
    bad = back.to_dict()
    bad["rules"][0]["channel"] = "warp_factor"
    with pytest.raises(ValueError, match="not a channel this engine has"):
        Profile.from_dict(bad)
    with pytest.raises(ValueError, match="newer release"):
        Profile.from_dict({**back.to_dict(), "version": 9})


def test_a_mapped_import_reopens_reproducibly_from_the_store(tmp_path):
    """Phase 4's "done when": a file opens, maps through a profile, analyses,
    and comes back the same from the project — stated units, sign flip and
    channel selection included."""
    asammdf = pytest.importorskip("asammdf")
    from veltrix_engine import ProjectStore
    from veltrix_engine.importers import inspect_file, read_file
    ref = build_session(read_csv(GOLDEN / "logger_layout_3lap_20hz.csv")).channels
    t, slow = ref["time"].to_numpy(), ref.iloc[::2]
    mdf = asammdf.MDF(version="4.10")
    mdf.append([asammdf.Signal(ref["speed"].to_numpy() / 3.6, t, name="vCar"),          # m/s, and the file does not say so
                asammdf.Signal(-ref["steering"].to_numpy(), t, name="aSteer", unit="deg"),
                asammdf.Signal(ref["lap"].to_numpy(np.int32), t, name="NLap"),
                asammdf.Signal(ref["acc_lat"].to_numpy(), t, name="gLat", unit="g")])
    mdf.append([asammdf.Signal(slow["rpm"].to_numpy(), slow["time"].to_numpy(), name="nEngine", unit="rpm"),
                asammdf.Signal(np.full(len(slow), 1.0), slow["time"].to_numpy(), name="Spare")])
    path = tmp_path / "team.mf4"
    mdf.save(path, overwrite=True)
    mdf.close()

    info = inspect_file(path)
    assert info["missing_units"] == ["vCar", "NLap", "Spare"] and info["rates_hz"] == [10.0, 20.0]
    profile = Profile("Team A", logger="ADL3", rules=[
        Rule("vCar", "speed", unit="m/s", confirmed=True), Rule("aSteer", "steering", confirmed=True),
        Rule("NLap", "lap", confirmed=True), Rule("gLat", "acc_lat", confirmed=True), Rule("nEngine", "rpm", confirmed=True)])
    wanted = [r.pattern for r in profile.rules]
    raw = read_file(path, channels=wanted)
    decisions = decide(raw, profile)
    assert diagnostics(raw, decisions)["needs_confirmation"] == ["Time"]
    mapping, units = to_import_args(decisions)

    store = ProjectStore(tmp_path / "project")
    try:
        sid = store.import_file(path, mapping=mapping, units=units, flip=("steering",), channels=wanted, profile="Team A")
        first = store.channels(sid)
        assert np.allclose(first["speed"], ref["speed"], atol=1e-6), "m/s from the profile → km/h"
        assert np.allclose(first["steering"], ref["steering"], atol=1e-6), "the flipped sign is back to the schema's"
        assert len(store.laps(sid)) == 3 and "Spare" not in store.model(sid).manifest_channels[0]["id"]
        assert store.import_options(sid) == {"units": {"vCar": "m/s"}, "flip": ["steering"], "channels": wanted, "profile": "Team A"}
        again = store.reload_session(sid)
        pd.testing.assert_frame_equal(again.frame(), first[again.frame().columns], check_dtype=False)
        assert again.model.get("rpm").sample_rate == pytest.approx(10), "native rates survive the round trip"
        assert again.model.get("speed", "normalised").provenance.unit_source == "stated"
    finally:
        store.close()


def test_the_profile_that_fits_a_file_is_chosen_and_one_that_barely_fits_is_not(tmp_path):
    from veltrix_engine.mapping_profiles import best_profile
    raw = table({"vCar": lambda t: 40 + 0 * t, "nEngine": lambda t: 7000 + 0 * t, "aSteer": lambda t: 5 * np.sin(t),
                 "rThrottle": lambda t: 50 + 0 * t}, {"nEngine": "rpm"})
    raw.info = {"Vehicle": "AMG GT3", "Device": "ADL3"}
    team = Profile("Team A GT3", logger="ADL3", car="GT3", rules=[Rule("vCar", "speed", "m/s", confirmed=True),
                   Rule("nEngine", "rpm", confirmed=True), Rule("aSteer", "steering", confirmed=True),
                   Rule("rThrottle", "throttle", confirmed=True)])
    same_columns = Profile("Other team", rules=list(team.rules))
    other = Profile("Kart", rules=[Rule("GPS_Speed", "speed"), Rule("RPM", "rpm"), Rule("vCar", "speed"), Rule("Lambda", "rpm")])
    for p in (team, same_columns, other):
        p.save(tmp_path / f"{p.name}.json")
    (tmp_path / "broken.json").write_text("{not json")
    chosen, why = best_profile(raw, tmp_path)
    assert chosen.name == "Team A GT3" and "4 of its 4 columns" in why and "header names its logger and car" in why
    assert best_profile(raw, [other]) == (None, "No saved profile fits this file: the closest, 'Kart', matches 1 of its 4 columns.")
    assert best_profile(raw, tmp_path / "nowhere") == (None, "No mapping profile is saved yet.")
    assert all(d.basis == "profile" for d in decide(raw, chosen) if d.channel and d.column != "Time")


def test_how_each_column_was_mapped_is_kept_with_the_session(tmp_path):
    from veltrix_engine.model_store import load_model, save_model
    raw = table({"vCar": lambda t: 40 + 10 * np.sin(t / 5), "nEngine": lambda t: 7000 + 0 * t, "Throttle": lambda t: 50 + 0 * t},
                {"nEngine": "rpm", "Throttle": "%"})
    s = build_session(raw, mapping={"speed": "vCar"}, units={"vCar": "m/s"})
    by = {m["channel"]: m for m in s.model.mapping}
    assert (by["speed"]["column"], by["speed"]["basis"], by["speed"]["unit_source"]) == ("vCar", "chosen at import", "stated")
    assert (by["throttle"]["basis"], by["throttle"]["unit"], by["throttle"]["unit_source"]) == ("name match", "%", "file")
    assert "rpm" not in by, "nEngine is not a name the engine knows; it stays unmapped until someone says so"
    back = load_model(save_model(s.model, tmp_path / "model"))
    assert back.mapping == s.model.mapping
