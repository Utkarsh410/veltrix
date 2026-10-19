"""
Regression check on real sessions: each release must give the same laps,
corners, findings and confidence on them as when they were frozen. Run with

    .venv/bin/python -m pytest -m real

A change that is meant (a corrected finding) is accepted by refreezing:
tools/freeze_real_sessions.py — then read the diff it prints.
"""

import json

import pytest

from real_sessions import CORPUS, EXPECTED, find, summary

pytestmark = pytest.mark.real


@pytest.mark.parametrize("key", sorted(CORPUS))
def test_a_real_session_gives_what_it_gave_when_frozen(key):
    path = find(CORPUS[key])
    if path is None:
        pytest.skip(f"{CORPUS[key]} is not on this machine")
    frozen = EXPECTED / f"{key}.json"
    if not frozen.exists():
        pytest.skip(f"not frozen yet: run tools/freeze_real_sessions.py {key}")
    want, got = json.loads(frozen.read_text()), json.loads(json.dumps(summary(path), default=str))
    for part in want:
        assert got[part] == want[part], f"{key}: {part} changed (refreeze only if the change is meant)"
