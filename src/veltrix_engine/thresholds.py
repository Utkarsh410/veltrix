"""
Thresholds: every tunable number of the analysis, listed, overridable and
saved — none of them is fixed in code as far as a user is concerned.

The analysis modules keep their thresholds as named constants with a comment
saying what each means. This module is the way in:

  thresholds.snapshot()                       {"corners.SLOW_KMH": 110.0, "handling.SIGMA_LEVEL": 3.5, ...}
  with thresholds.use({"corners.SLOW_KMH": 90}):
      session = load_session(path)            # analysed with the override
  thresholds.save(path, overrides) / thresholds.load(path)

`use` checks every name and type, applies the overrides, and puts the
originals back on the way out, whatever happens inside. A project keeps its
overrides in `thresholds.json` (`ProjectStore.thresholds()`), and they are
applied whenever the project imports or rebuilds a session.

(Maths channels and event rules carry their own named parameters and do not
need this.)

Limit: an override applies to code that reads the constant when it runs. The
few places that take a constant as a function's default argument keep the
value from import time; pass the argument explicitly there.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from . import corners, derived, laps, session
from .analysis import advice, balance, engineering, gg, handling, plausibility, tyres

MODULES = {"session": session, "laps": laps, "derived": derived, "corners": corners, "handling": handling,
           "balance": balance, "gg": gg, "tyres": tyres, "advice": advice, "engineering": engineering,
           "plausibility": plausibility}


def _tunable(value) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, tuple) and bool(value) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)


def snapshot() -> dict:
    """Every threshold and its current value: {"module.NAME": number or list of numbers}."""
    out = {}
    for mod_name, mod in MODULES.items():
        for name, value in vars(mod).items():
            if name.isupper() and not name.startswith("_") and _tunable(value):
                out[f"{mod_name}.{name}"] = list(value) if isinstance(value, tuple) else value
    return out


def _check(overrides: dict) -> dict:
    known, clean = snapshot(), {}
    for key, value in overrides.items():
        if key not in known:
            near = [k for k in known if k.split(".")[-1] == str(key).split(".")[-1]]
            raise ValueError(f"'{key}' is not a threshold of the engine." + (f" Did you mean {near[0]}?" if near else
                             " thresholds.snapshot() lists them."))
        current = known[key]
        if isinstance(current, list):
            if not isinstance(value, (list, tuple)) or len(value) != len(current) or not all(_tunable(v) for v in value):
                raise ValueError(f"{key} takes {len(current)} numbers, like {current}.")
            clean[key] = tuple(float(v) if isinstance(c, float) else v for v, c in zip(value, current))
        else:
            if not _tunable(value) or isinstance(value, tuple):
                raise ValueError(f"{key} takes a number, like {current}.")
            clean[key] = float(value) if isinstance(current, float) else value
    return clean


@contextmanager
def use(overrides: dict | None):
    """Run the code inside with these thresholds in place of the defaults."""
    clean = _check(overrides or {})
    undo = []
    try:
        for key, value in clean.items():
            mod_name, name = key.split(".", 1)
            old = getattr(MODULES[mod_name], name)
            # a module that took its copy of this threshold from another follows it
            for other in MODULES.values():
                if hasattr(other, name) and getattr(other, name) == old and type(getattr(other, name)) is type(old):
                    undo.append((other, name, getattr(other, name)))
                    setattr(other, name, value)
        yield clean
    finally:
        for mod, name, old in reversed(undo):
            setattr(mod, name, old)


def save(path: str | Path, overrides: dict) -> Path:
    clean = _check(overrides)
    path = Path(path)
    path.write_text(json.dumps({"version": 1, "thresholds": {k: list(v) if isinstance(v, tuple) else v for k, v in clean.items()}},
                               indent=1), encoding="utf-8")
    return path


def load(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return _check(data.get("thresholds", {}))
