"""
What a person adds to a session beyond what is in the file: maths channels,
event rules, notes, and verdicts on detected events — and the events the
detectors find with those rules.

No Qt here. The desktop app keeps these in its data folder, and `veltrix
report` reads the same folder, so a report made from the command line shows the
same maths channels, events, notes and reviews as the app.

Events are what a detector found — candidates for review, worded as such.
Annotations are what a person wrote. The two are kept apart everywhere: in the
model, on disk and on screen. Both are saved per source file (by its SHA-256),
so they come back when the same file is opened again, whatever it is called by
then.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import events as rule_engine
from . import maths
from .analysis import handling
from .maths import MathSet
from .model import Annotation, events_from_incidents
from .versions import refuse_overwrite, upgrade

RULE_VERSION = "handling 1"
NOTES_VERSION = 1


def data_dir() -> Path:
    """The app's data folder. VELTRIX_DATA_DIR overrides it."""
    override = os.environ.get("VELTRIX_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Veltrix Data"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "veltrix-data"


def detect_events(session, rules=None, fit: bool = True) -> tuple[list, str]:
    """Everything the detectors find in a session, as model events in time
    order, with a few lines saying what was looked for and what could not be.
    Two detectors run: the rule engine (veltrix_engine.events — wheelspin,
    lock-up and the rest, from configurable rules) and the balance-incident
    detector (steering against the car's own norm). Never raises."""
    lines = []
    try:
        found, report = rule_engine.detect(session.model, rules, fit=fit)
    except (ValueError, KeyError) as e:
        found, report = [], {}
        lines.append(f"The event rules could not be run: {e}")
    fitted = report.pop("_calibration", None)
    ran = [f"{k.replace('_', ' ')} {v['events']}" for k, v in report.items() if v["ran"]]
    skipped = [f"{k.replace('_', ' ')} ({v['message'].removeprefix('Not looked for: ')})" for k, v in report.items() if not v["ran"]]
    if ran:
        lines.append("Rules run: " + ", ".join(ran) + ".")
    if skipped:
        lines.append("Not looked for: " + "; ".join(skipped) + ".")
    if fitted and fitted["changes"]:
        lines.append("Thresholds fitted to this session: " + " ".join(fitted["changes"]))
    try:
        result = handling.analyse(session)
        model = result["model"]
        balance = events_from_incidents(result["events"], session.laps, RULE_VERSION,
                                        session.model.get("lap_distance") if session.model.has("lap_distance") else None,
                                        thresholds=steering_limits(model))
        lines.append(f"{len(balance)} possible balance incident(s): steering differing from this car's own norm by at least "
                     f"{model['threshold_deg']:.0f} deg for {handling.MIN_DURATION_S:g} s, over {model['flying_laps']} flying laps.")
    except (ValueError, KeyError) as e:
        balance = []
        lines.append(f"Balance incidents were not looked for: {e}")
    recorded = rule_engine.recorded_incidents(session.model)
    confirmed = rule_engine.confirm_with_records(found + balance, recorded)
    if recorded:
        lines.append(f"{len(recorded)} incident(s) recorded by the simulator; {confirmed} candidate(s) coincide with one "
                     "and are marked confirmed.")
    events = sorted(found + balance + recorded, key=lambda e: e.t_start)
    head = (f"{len(found) + len(balance)} candidate event(s) for review. Thresholds start from the rule set's values and are raised "
            "where this session's own signals show they are too low; they are not validated against a car."
            if fit else f"{len(found) + len(balance)} candidate event(s) for review — thresholds are as configured, not fitted to this session.")
    return events, " ".join([head] + lines)


def steering_limits(model: dict) -> dict:
    """The limits the steering model found its candidates with, for each event."""
    return {"threshold_deg": round(float(model["threshold_deg"]), 1),
            "robust_sigmas": handling.SIGMA_LEVEL, "min_duration_s": handling.MIN_DURATION_S,
            "opposite_lock_deg": handling.COUNTER_STEER_DEG}


def apply_reviews(session, folder) -> int:
    """Put the saved reviews of this file on its events; returns how many earlier
    reviews no longer match an event (kept, with the event as it was detected)."""
    store = rule_engine.ReviewStore(Path(folder) / "reviews")
    left = rule_engine.apply_reviews(session.model.events, store.load(session.raw.sha256))
    session.orphan_reviews = left
    return len(left)


class AnnotationStore:
    """Annotations on disk: one JSON file per source file."""

    def __init__(self, folder: str | Path):
        self.folder = Path(folder)

    def _path(self, sha256: str) -> Path:
        if not sha256 or not sha256.isalnum():
            raise ValueError("Annotations are stored per source file; this session has no file identity.")
        return self.folder / f"{sha256}.json"

    def load(self, sha256: str) -> list:
        try:
            path = self._path(sha256)
        except ValueError:
            return []
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return []                              # an unreadable file is not worth losing the session over
        data = upgrade("set of notes", data, NOTES_VERSION)                 # a newer file raises: never read half
        try:
            return [Annotation(**a) for a in data["annotations"]]
        except (KeyError, TypeError):
            return []

    def save(self, sha256: str, annotations: list) -> None:
        path = self._path(sha256)
        refuse_overwrite(path, "set of notes", NOTES_VERSION)
        self.folder.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": NOTES_VERSION, "annotations": [asdict(a) for a in annotations]}, indent=1), encoding="utf-8")


def annotate(model, text: str, time: float | None = None, category: str = "", author: str = "") -> Annotation:
    """Add a note to a session: at a time (with its lap and position worked
    out), or to the whole session when no time is given."""
    text = text.strip()
    if not text:
        raise ValueError("An annotation needs some text.")
    lap = next((x for x in model.laps if time is not None and x.t_start <= time < x.t_end), None)
    position = None
    if time is not None and model.has("lap_distance"):
        import numpy as np
        d = model.get("lap_distance").at([time])[0][0]
        position = float(d) if np.isfinite(d) else None
    note = Annotation(text=text, scope="session" if time is None else "time", lap=lap.lap if lap else None,
                      time=None if time is None else float(time), position_m=position, category=category.strip(),
                      author=author.strip(), created=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    model.annotations.append(note)
    return note


def load_rules(folder: str | Path | None = None):
    """The user's event rules, or the defaults when none were saved or the file cannot be read."""
    path = Path(folder or data_dir()) / "event_rules.json"
    if path.exists():
        try:
            return rule_engine.RuleSet.load(path), ""
        except (ValueError, KeyError, TypeError, OSError) as e:
            return rule_engine.default_rules(), f"The saved event rules could not be read ({e}); the defaults were used."
    return rule_engine.default_rules(), ""


def apply(session, folder: str | Path | None = None) -> dict:
    """Bring a session up to what the app would show: compute the saved maths
    channels (and make them comparable lap against lap), detect events with
    the saved rules, load the notes for this file. Returns what happened:
    {"maths": {name: Result}, "events": text, "notes": count, "problems": [...]}.
    Nothing here can fail an import: each part reports instead."""
    folder = Path(folder or data_dir())
    problems = []
    results = {}
    maths_file = folder / "maths.json"
    if maths_file.exists():
        try:
            results = maths.compute(session.model, MathSet.load(maths_file))
            maths.on_view(session, results)
            problems += [f"Maths channel {name} was not calculated: {r.message.splitlines()[0]}" for name, r in results.items() if not r.ok]
        except (ValueError, KeyError, TypeError, OSError) as e:
            problems.append(f"The saved maths channels could not be read ({e}).")
    rules, complaint = load_rules(folder)
    if complaint:
        problems.append(complaint)
    session.model.events, summary = detect_events(session, rules)
    orphans = apply_reviews(session, folder)
    if orphans:
        summary += (f" {orphans} earlier review(s) refer to events no longer detected (the rules or thresholds changed); "
                    "they are kept with the events as first detected.")
    session.events_summary = summary
    session.model.annotations = AnnotationStore(folder / "annotations").load(session.raw.sha256)
    return {"maths": results, "events": summary, "notes": len(session.model.annotations), "problems": problems}
