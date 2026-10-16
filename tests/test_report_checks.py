"""
Findings checked against an independent parse of a real iRacing session
(Toyota GR86, Spa, 2026-10-03): each test pins one fault that the report got
wrong on that file, on synthetic data built to show the same thing.
"""

import numpy as np
import pytest

from test_advice import generate_session, staged_session
from veltrix_engine import build_session, corners as C
from veltrix_engine.analysis import engineering
from veltrix_engine.importers import parse_csv_text


def findings(report, sid):
    return next(s for s in report["sections"] if s["id"] == sid)["findings"]


# ── braking point ─────────────────────────────────────────────────────────────

def test_a_short_release_does_not_end_a_braking_phase():
    t = np.arange(20) * 0.05
    on = np.array([0, 1, 1, 1, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0], bool)
    closed = C._close_gaps(on, t, 0.3)
    assert closed[1:8].all(), "a 0.1 s release is trail-braking, one phase"
    assert not closed[8:16].any(), "a 0.4 s release ends it"


def test_the_braking_point_is_where_the_braking_for_the_corner_starts():
    v = np.r_[np.full(10, 200.0), np.linspace(200, 80, 20), np.full(5, 80.0), np.linspace(80, 75, 3), np.full(4, 75.0)]
    braking = np.zeros(len(v), bool)
    braking[10:30] = True                       # the real stop, starting at 10
    braking[35:38] = True                       # a dab just before the apex, worth 5 km/h
    # the zone starts at 15, inside the stop: braking began on the straight shared with the corner before
    assert C._main_braking(braking, v, z0=15, apex=40, floor=0) == (10, 29)
    assert C._main_braking(braking, v, z0=15, apex=40, floor=12) == (12, 29), "never into the corner before"
    assert C._main_braking(np.zeros(len(v), bool), v, 15, 40, 0) is None


def test_braking_points_are_read_from_the_corner_like_marker_boards():
    assert C.before_corner(117.4) == "117 m before the corner"
    assert C.before_corner(-12.0) == "12 m into the corner"
    assert C.before_corner(float("nan")) == "—"
    assert C.before_corner(0.0) == C.before_corner(-0.2) == "at the turn-in"


def test_peak_braking_g_ignores_one_bad_speed_sample():
    raw = generate_session(lap_count=4, seed=7, lockup_lap=0)
    clean = build_session(parse_csv_text(raw.to_csv(index=False)))
    corners = C.find_corners(clean)
    before = C.corner_metrics(clean, corners)
    # one speed sample 3 km/h low in the hardest stop of lap 2: d(speed)/dt turns it into a spike
    m = before[before["lap"] == 2].sort_values("peak_brake_g").iloc[-1]
    ch = clean.channels
    row = ch.index[(ch["lap"] == 2) & (ch["lap_distance"] >= m.brake_m + 30)][0]
    i = int(np.argmin(np.abs(raw["Time"].to_numpy() - ch.loc[row, "time"])))
    raw.loc[i, "Speed"] -= 3.0
    spiked = build_session(parse_csv_text(raw.to_csv(index=False)))
    after = C.corner_metrics(spiked, corners)
    a = after[(after["lap"] == 2) & (after["corner"] == m.corner)].iloc[0]
    assert a.peak_brake_g == pytest.approx(m.peak_brake_g, abs=0.03), "the measured longitudinal g is used"


# ── car parameters and load transfer ──────────────────────────────────────────

def test_the_load_transfer_split_is_withheld_without_this_cars_wheel_rates(synthetic):
    report = engineering.analyse(synthetic)
    titles = [f["conclusion"] for f in findings(report, "vehicle_balance")]
    withheld = next(f for f in findings(report, "vehicle_balance") if "cannot be stated" in f["conclusion"])
    assert not any(t.startswith("The front axle carries") for t in titles)
    assert any("Roll at the front dampers" in e for e in withheld["evidence"])
    assert any("Static front weight not known" in e for e in withheld["evidence"]), "the default is not quoted as the car's"


def test_stated_wheel_rates_and_weight_give_the_split_with_where_they_came_from(synthetic):
    import copy
    s = copy.copy(synthetic)
    s.vehicle = copy.deepcopy(synthetic.vehicle)
    for name, value in (("wheel_rate_front_N_mm", 150.0), ("wheel_rate_rear_N_mm", 120.0), ("front_weight_dist", 0.47)):
        s.vehicle.set(name, value, "user")
    f = next(f for f in findings(engineering.analyse(s), "vehicle_balance") if f["conclusion"].startswith("The front axle carries"))
    assert "Static front weight 47.0% (stated at import)" in f["evidence"]
    assert "150 N/mm (stated at import)" in f["calculation"]


# ── incidents the simulator recorded ──────────────────────────────────────────

def test_recorded_off_tracks_are_reported_and_never_the_fastest_pass(synthetic):
    import copy
    s = copy.copy(synthetic)
    s.channels = synthetic.channels.copy()
    ch = s.channels
    corners = C.find_corners(s)
    metrics = C.corner_metrics(s, corners)
    # the fastest pass through the slowest-to-drive corner, then an off track recorded right there
    corner = metrics.groupby("corner")["zone_time_s"].std().idxmax()
    fastest = metrics[metrics["corner"] == corner].sort_values("zone_time_s").iloc[0]
    c = corners.set_index("corner").loc[corner]
    at = ch.index[(ch["lap"] == fastest.lap) & (ch["lap_distance"] >= (c.start_m + c.end_m) / 2)][0]
    ch["incident_count"] = 0.0
    ch.loc[at:, "incident_count"] = 1.0
    ch["incident_flags"] = 0.0
    ch.loc[at:at + 20, "incident_flags"] = float(0x0202)
    report = engineering.analyse(s)
    rec = next(f for f in findings(report, "incidents") if "simulator recorded" in f["conclusion"])
    assert rec["conclusion"] == f"The simulator recorded 1 incident — on lap {int(fastest.lap)}"
    assert any(e.startswith(f"Lap {int(fastest.lap)}, {corner} at") and e.endswith("off track, 1x") for e in rec["evidence"])
    for f in findings(report, "braking") + findings(report, "cornering") + findings(report, "traction"):
        for e in f["evidence"]:
            if e.startswith(f"{corner}:") and "fastest pass" in e:
                assert f"(lap {int(fastest.lap)})" not in e, "an off-track pass is not the reference to copy"


# ── P0: original and cleaned sample counts ────────────────────────────────────

def test_original_and_cleaned_sample_counts_are_kept_apart():
    raw = generate_session(lap_count=3, seed=7, lockup_lap=0)
    raw = raw.iloc[:-1].copy()
    raw.loc[100, "Time"] = raw.loc[99, "Time"]                 # one repeated timestamp
    raw.loc[200, "Time"] = np.nan                               # one row with no time
    raw.loc[300, "Speed"] = raw.loc[300, "Speed"] + 120.0       # one speed glitch
    s = build_session(parse_csv_text(raw.to_csv(index=False)))
    k = s.counts
    assert k["rows_in_file"] == len(raw)
    assert k["rows_without_time"] == 1 and k["rows_repeated_time"] == 1
    assert k["rows_used"] == len(raw) - 2 and k["blanked"].get("speed") == 1
    assert k["samples_analysed"] == len(s.channels)
    lines = s.count_lines()
    assert lines[0] == f"{len(raw):,} rows in the file"
    assert "dropped 1 without a time and 1 repeating the previous timestamp" in lines[1]
    assert any(l.startswith("Blanked as glitches") and "speed 1" in l for l in lines)
    report = engineering.analyse(s)
    f = next(f for f in findings(report, "incidents") if "logged rows were used" in f["conclusion"])
    assert f["conclusion"].startswith(f"{len(raw) - 2:,} of {len(raw):,} logged rows were used")


# ── P0: corner zones against the circuit's definition ─────────────────────────

def test_inferred_corners_are_checked_against_a_circuit_definition(synthetic):
    from veltrix_engine import circuits
    inferred = C.find_corners(synthetic, track=None)
    assert (inferred["basis"] == "inferred").all() and inferred.attrs["validation"] is None
    track = circuits.draft_from_session(synthetic, inferred, name="Test circuit")
    for i, t in enumerate(track["corners"]):
        t["name"] = f"Turn {i + 1}"
    used = C.find_corners(synthetic, track=track)
    v = used.attrs["validation"]
    assert (used["basis"] == "circuit file").all()
    assert v["found"] == v["total"] == len(track["corners"]) and not v["extra"]

    # a turn the data does not show, and a corner the file does not have
    bogus = dict(track, corners=[dict(c) for c in track["corners"]])
    bogus["corners"].append({"number": 99, "name": "Phantom", "direction": "L",
                             "start_m": track["lap_length_m"] * 0.999 - 5, "end_m": track["lap_length_m"] * 0.999})
    v = circuits.validate(bogus, synthetic.lap_length, inferred)
    assert v["found"] == len(track["corners"]) and not v["turns"][-1]["found"]
    shorter = dict(track, corners=track["corners"][1:])
    v = circuits.validate(shorter, synthetic.lap_length, inferred)
    assert v["extra"] == [inferred["corner"].iloc[0]]

    f = next(f for f in findings(engineering.analyse(synthetic), "incidents") if f["conclusion"].startswith("Corner zones"))
    assert f["conclusion"] == "Corner zones are inferred from the data, not checked against a circuit definition"


# ── P0: implausible acceleration peaks ────────────────────────────────────────

def test_a_kerb_jolt_is_set_aside_and_a_peak_no_other_measurement_supports_is_left_out(synthetic):
    import copy
    from veltrix_engine.analysis import plausibility
    from veltrix_engine.vehicle import G
    s = copy.copy(synthetic)
    s.channels, s.derived = synthetic.channels.copy(), synthetic.derived.copy()
    ch = s.channels
    corners = C.find_corners(s)
    # an independent measurement that agrees with lateral g everywhere: yaw rate = ay / V
    v = np.maximum(ch["speed"].to_numpy() / 3.6, 5.0)
    ch["yaw_rate"] = ch["acc_lat"].to_numpy() * G / v
    s.units = dict(s.units, yaw_rate="rad/s")
    flying = list(s.laps.loc[s.laps["status"] == "flying", "lap"])
    c = corners.iloc[2]
    in_corner = lambda lap: ch.index[(ch["lap"] == lap) & ch["lap_distance"].between(c.start_m, c.end_m)]
    # lap A: one sample jolted 1.2 g up (a kerb strike, as a jolt); lap B: the corner reads 0.6 g more, held
    rows_a = in_corner(flying[0])
    peak_row = ch.loc[rows_a, "acc_lat"].abs().idxmax()
    ch.loc[peak_row, "acc_lat"] += 1.2 * np.sign(ch.loc[peak_row, "acc_lat"])
    rows_b = in_corner(flying[1])
    ch.loc[rows_b, "acc_lat"] += 0.6 * np.sign(ch.loc[rows_b, "acc_lat"])
    lat = ch["acc_lat"]
    s.derived["acc_lat_sustained"] = lat.rolling(max(int(round(0.25 * s.rate_hz)), 1), center=True, min_periods=1).median()
    p = plausibility.check_peaks(s, corners).set_index(["lap", "corner", "kind"])
    assert p.loc[(flying[0], c.corner, "lateral"), "verdict"] in ("spike", "jolt")
    assert p.loc[(flying[1], c.corner, "lateral"), "verdict"] == "unsupported"
    assert p.loc[(flying[2], c.corner, "lateral"), "verdict"] == "ok"

    report = engineering.analyse(s)
    f = next(f for f in findings(report, "incidents") if "acceleration peaks" in f["conclusion"])
    assert "left out of the peak figures" in f["conclusion"]
    ctx = engineering._Ctx(s, None, None)
    m = ctx.metrics.set_index(["lap", "corner"])
    assert np.isnan(m.loc[(flying[1], c.corner), "peak_lat_g"]), "an unsupported peak is not used"
    assert np.isfinite(m.loc[(flying[2], c.corner), "peak_lat_g"])


# ── P0: recorded incidents against inferred candidates ────────────────────────

def test_recorded_incidents_confirm_candidates_and_are_kept_apart_from_them(synthetic):
    from veltrix_engine import events as E
    from veltrix_engine.model import CANDIDATE, CONFIRMED, RECORDED, Channel, Event
    model = synthetic.model
    t = model.get("speed").timestamps
    lap = next(l for l in model.laps if l.lap == 2)
    at = 0.5 * (lap.t_start + lap.t_end)
    count = np.where(t >= at, 1.0, 0.0)
    flags = np.where((t >= at) & (t < at + 0.3), float(0x0202), 0.0)
    import copy
    m = copy.copy(model)
    m._channels = {layer: dict(chans) for layer, chans in model._channels.items()}   # not the shared fixture's
    added = [Channel(id="incident_count", layer="normalised", timestamps=t, values=count, unit="-", discrete=True),
             Channel(id="incident_flags", layer="normalised", timestamps=t, values=flags, unit="-", discrete=True)]
    for c in added:
        m.add(c)
    recorded = E.recorded_incidents(m)
    assert len(recorded) == 1 and recorded[0].status == RECORDED and recorded[0].kind == "off track"
    assert recorded[0].lap == 2 and recorded[0].evidence["points"] == 1
    near = Event(kind="possible oversteer", t_start=at - 1.0, t_end=at - 0.5, lap=2)
    far = Event(kind="possible oversteer", t_start=at + 60, t_end=at + 61, lap=2)
    assert E.confirm_with_records([near, far], recorded) == 1
    assert near.status == CONFIRMED and "off track recorded by the simulator" in near.evidence["confirmed_by"]
    assert far.status == CANDIDATE

    from veltrix_app.events import describe
    assert describe(recorded[0])["what"] == "Off track — recorded by the simulator"
    assert describe(near)["what"].endswith("confirmed by the simulator's record")
    assert describe(far)["what"].endswith("requires review")
