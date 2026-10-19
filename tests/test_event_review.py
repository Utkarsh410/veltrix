"""
P3: the limits every event was found with, a person's verdict on it, and the
event as first detected kept beside that verdict for audit.
"""

import copy
import json
import os

import numpy as np
import pytest

from veltrix_engine import events as E
from veltrix_engine.model import CANDIDATE, CONFIRMED, RECORDED, REJECTED, Event


def ev(t=100.0, rule="lock_up", kind="Possible front lock-up", status=CANDIDATE):
    return Event(kind=kind, t_start=t, t_end=t + 0.4, lap=2, position_m=500.0, severity="moderate",
                 confidence="low — requires review", rule=rule, rule_version="1", status=status, source="rule",
                 thresholds={"rule": rule, "parameters": {"lock_on": 12.0}, "fitted": {}})


# ── limits on every event ──────────────────────────────────────────────────────

def test_every_rule_event_carries_the_limits_it_was_found_with(synthetic):
    rules = E.default_rules()
    lock = next(r for r in rules.rules if r.id == "lock_up")
    limits = E.rule_limits(rules, lock, dict(rules.parameters))
    assert {"lock_on", "lock_off", "lock_brake_g", "min_speed"} <= set(limits["parameters"])
    assert "min_speed" in limits["parameters"], "found through the FrontLockEstimate helper channel"
    assert limits["on"] == lock.on and limits["min_duration_s"] == lock.min_duration_s and limits["fitted"] == {}
    fitted = E.rule_limits(rules, lock, {**rules.parameters, "lock_on": 8.0})
    assert fitted["fitted"] == {"lock_on": {"was": 8.0, "now": rules.parameters["lock_on"]}}
    assert "lock_on 12 (fitted from 8)" in E.limits_text(fitted)

    found, _ = E.detect(synthetic.model, fit=True)
    assert all(e.thresholds.get("rule") == e.rule and e.thresholds.get("parameters") for e in found)


# ── reviews ────────────────────────────────────────────────────────────────────

def test_a_review_keeps_the_event_as_first_detected_and_its_history(tmp_path):
    store = E.ReviewStore(tmp_path / "reviews")
    e = ev()
    first = store.put("abc123", e, "confirmed", "seen on video", "utkarsh")
    assert first["original"]["status"] == CANDIDATE and first["original"]["thresholds"]["parameters"] == {"lock_on": 12.0}
    # the rules change: the same event is found again with other limits, and the verdict is changed
    again = copy.deepcopy(e)
    again.thresholds = {"rule": "lock_up", "parameters": {"lock_on": 15.0}, "fitted": {}}
    second = store.put("abc123", again, "rejected", "it was a kerb", "utkarsh")
    assert second["original"]["thresholds"]["parameters"] == {"lock_on": 12.0}, "the original detection is never overwritten"
    assert [h["verdict"] for h in second["history"]] == ["confirmed"] and second["note"] == "it was a kerb"
    saved = json.loads((tmp_path / "reviews" / "abc123.json").read_text())
    assert len(saved["reviews"]) == 1
    with pytest.raises(ValueError):
        E.make_review(e, "maybe")


def test_reviews_set_status_and_clearing_returns_to_as_detected(tmp_path):
    a, b, c = ev(100.0), ev(200.0), ev(300.0, rule="simulator incident record", kind="off track", status=RECORDED)
    reviews = [E.make_review(a, "confirmed", "", "x"), E.make_review(b, "rejected", "false alarm", "x"),
               E.make_review(c, "rejected", "", "x")]
    gone = E.make_review(ev(900.0), "confirmed")                 # an event the rules no longer find
    left = E.apply_reviews([a, b, c], reviews + [gone])
    assert (a.status, b.status) == (CONFIRMED, REJECTED)
    assert c.status == RECORDED, "the simulator's own record is not overruled"
    assert b.review["note"] == "false alarm" and left == [gone]
    E.apply_reviews([a, b, c], [])                              # every review cleared
    assert (a.status, b.status) == (CANDIDATE, CANDIDATE) and a.review == {}
    near = ev(100.6)
    assert E.find_review(reviews, near) is reviews[0], "the same event, found a fraction of a second apart"


# ── in the app, and in the report made from the app's data ─────────────────────

@pytest.fixture
def window(tmp_path, monza):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets
    from veltrix_app.window import MainWindow
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = MainWindow(tmp_path / "maths.json")
    w.show_session(monza)
    yield w
    w.close()


def test_confirm_reject_and_note_in_the_events_panel(window, tmp_path, monza):
    w, dock = window, window.events
    events = monza.model.events
    assert events, "the golden session has candidate events to review"
    headers = [dock.events.horizontalHeaderItem(k).text() for k in range(dock.events.columnCount())]
    assert {"Status", "Confidence", "Found with", "Review"} <= set(headers)
    found_with = headers.index("Found with")
    assert dock.events.item(0, found_with).text(), "each event says what it was found with"

    w.review_event(0, "rejected", "a kerb, not a lock-up")
    assert events[0].status == REJECTED and events[0].review["note"] == "a kerb, not a lock-up"
    status_col = headers.index("Status")
    assert dock.events.item(0, status_col).text() == "rejected"
    dock.events.selectRow(0)
    assert "Found with:" in dock.detail.toPlainText() and "As detected: candidate" in dock.detail.toPlainText()
    w.review_event(1, "confirmed", "")
    assert events[1].status == CONFIRMED
    saved = E.ReviewStore(w.data_folder / "reviews").load(monza.raw.sha256)
    assert {r["verdict"] for r in saved} == {"rejected", "confirmed"}
    assert all(r["original"]["status"] == CANDIDATE for r in saved)

    # reopened: the verdicts come back with the session
    w.show_session(monza)
    assert monza.model.events[0].status == REJECTED and monza.model.events[1].status == CONFIRMED
    w.clear_event_review(1)
    assert monza.model.events[1].status == CANDIDATE

    # the report made from the app's data shows the verdicts, the limits, and how confidence is decided
    from veltrix_app import extras
    from veltrix_engine.report import report_data
    import copy as _copy
    s = _copy.copy(monza)
    s.model = _copy.copy(monza.model)
    extras.apply(s, w.data_folder)
    d = report_data(s)["data"]
    rejected = [e for e in d["events"] if e["status"] == "rejected"]
    assert rejected and "a kerb, not a lock-up" in rejected[0]["review"] and rejected[0]["limits"]
    assert any("flying laps" in line for line in d["confidence_rule"])
    assert any(t["name"] == "plausibility.TRANSIENT_G" for t in d["thresholds"])


def test_earlier_reviews_of_events_no_longer_found_are_kept(window, monza):
    w = window
    store = E.ReviewStore(w.data_folder / "reviews")
    ghost = ev(5.0, rule="old_rule", kind="Possible something")
    store.put(monza.raw.sha256, ghost, "confirmed", "from an older rule set", "x")
    w.detect_again()
    assert len(monza.orphan_reviews) == 1 and "no longer detected" in monza.events_summary
    from veltrix_engine.report import report_data
    d = report_data(monza)["data"]
    assert d["orphan_reviews"][0]["kind"] == "Possible something" and d["orphan_reviews"][0]["detected_as"] == "candidate"


def test_a_rejected_balance_candidate_is_marked_in_the_engineering_report(synthetic):
    from test_engineering import findings
    from test_advice import staged_session
    from veltrix_app.events import detect_events
    from veltrix_engine.analysis import engineering
    def slide(raw, rows, bump):
        raw.loc[rows, "SteeringAngle"] -= 1.7 * raw.loc[rows, "SteeringAngle"] * bump
        raw.loc[rows, "ThrottlePos"] = 100.0
        raw.loc[rows, "BrakePress"] = 0.0
    s = staged_session({"where": [(lap, 1262, 1300) for lap in (3, 6, 8)], "do": slide})
    s.model.events, _ = detect_events(s)
    cand = next(e for e in s.model.events if e.source == "steering model")
    E.apply_reviews(s.model.events, [E.make_review(cand, "rejected", "driver says it was a test", "x")])
    f = next(f for f in findings(engineering.analyse(s), "incidents") if "balance-incident candidates" in f["conclusion"])
    assert "1 rejected on review" in f["conclusion"]
    assert any("rejected on review by x" in e and "driver says it was a test" in e for e in f["evidence"])
