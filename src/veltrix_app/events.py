"""
Events and annotations for the app, without any window.

Events are what a detector found — candidates for review, worded as such.
Annotations are what a person wrote. The two are kept apart everywhere: in the
model, on disk and on screen.

Annotations are saved per source file (by its SHA-256), so they come back when
the same file is opened again, whatever it is called by then.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from veltrix_engine import events as rule_engine
from veltrix_engine.analysis import handling
from veltrix_engine.model import Annotation, events_from_incidents

RULE_VERSION = "handling 1"


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
        balance = events_from_incidents(result["events"], session.laps, RULE_VERSION,
                                        session.model.get("lap_distance") if session.model.has("lap_distance") else None)
        model = result["model"]
        lines.append(f"{len(balance)} possible balance incident(s): steering differing from this car's own norm by at least "
                     f"{model['threshold_deg']:.0f} deg for {handling.MIN_DURATION_S:g} s, over {model['flying_laps']} flying laps.")
    except (ValueError, KeyError) as e:
        balance = []
        lines.append(f"Balance incidents were not looked for: {e}")
    events = sorted(found + balance, key=lambda e: e.t_start)
    head = (f"{len(events)} candidate event(s) for review. Thresholds start from the rule set's values and are raised "
            "where this session's own signals show they are too low; they are not validated against a car."
            if fit else f"{len(events)} candidate event(s) for review — thresholds are as configured, not fitted to this session.")
    return events, " ".join([head] + lines)


def describe(event) -> dict:
    """An event as the row a panel shows."""
    ev = event.evidence
    peak = ev.get("peak")
    detail = ev.get("cause") or (f"peak {peak['value']:.3g} of {peak['of']}" if peak else "")
    what = event.kind if event.kind.lower().startswith(("possible", "braking anomaly")) else f"Possible {event.kind}"
    return {"lap": event.lap, "what": f"{what[0].upper() + what[1:]} — requires review", "severity": event.severity,
            "where": f"{ev.get('corner', '')} {ev.get('phase', '')}".strip(), "position_m": event.position_m,
            "time": float(peak["at_s"]) if peak else 0.5 * (event.t_start + event.t_end), "detail": detail,
            "label": event.label, "rule": event.rule}


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
            return [Annotation(**a) for a in json.loads(path.read_text(encoding="utf-8"))["annotations"]]
        except (ValueError, KeyError, TypeError):
            return []                              # an unreadable file is not worth losing the session over

    def save(self, sha256: str, annotations: list) -> None:
        path = self._path(sha256)
        self.folder.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "annotations": [asdict(a) for a in annotations]}, indent=1), encoding="utf-8")


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
