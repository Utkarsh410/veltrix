"""
What the app adds to a session beyond what is in the file: the user's maths
channels, the events the rules find, and the notes written against that file.

No Qt here. The window uses these functions, and so does `veltrix report`, so
a report made from the command line shows the same maths channels, events and
notes as the app does.
"""

from __future__ import annotations

from pathlib import Path

from veltrix_engine import events as rule_engine
from veltrix_engine import maths
from veltrix_engine.maths import MathSet

from .events import AnnotationStore, detect_events
from .paths import data_dir


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
    session.events_summary = summary
    session.model.annotations = AnnotationStore(folder / "annotations").load(session.raw.sha256)
    return {"maths": results, "events": summary, "notes": len(session.model.annotations), "problems": problems}
