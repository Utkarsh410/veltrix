"""
Maths channels: channels defined by a formula, computed on the canonical model.

  from veltrix_engine import load_session, maths

  s = load_session("run.csv")
  channels = maths.MathSet()
  channels.add("CombinedG", "sqrt(acc_lat^2 + acc_long^2)")
  channels.add("HighThrottle", "if(throttle > $limit, 1, 0)")
  channels.parameters["limit"] = 90
  results = maths.compute(s.model, channels)
  results["CombinedG"].channel        # a calculated Channel: values, validity mask, unit, provenance

A MathSet is the list of definitions and the named parameters they share; it
saves to and loads from JSON. `compute` works out the order the channels
depend on each other in, refuses cycles, reports missing inputs per channel,
and adds each result to the model's calculated layer with its formula, inputs,
settings and version as provenance. A channel whose formula and inputs have
not changed is not computed again.

The language is in parser.py, the unit rules in units.py, the functions in
evaluate.py. A formula is never executed as code.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from ..model import CALCULATED, Channel, Provenance, SessionModel
from ..schema import norm_name
from . import units as U
from .evaluate import FUNCTIONS, REFERENCE, Evaluator, Series
from .parser import FormulaError, parse, references

__all__ = ["FUNCTIONS", "REFERENCE", "FormulaError", "MathChannel", "MathSet", "Result", "check", "compute",
           "dependencies", "order", "on_view", "preview"]

SET_VERSION = 1
ENGINE = "maths 1"


@dataclass
class MathChannel:
    name: str
    formula: str
    unit: str = ""                 # the unit the result must come out in; "" = whatever the formula gives
    description: str = ""
    category: str = "maths"
    estimate: bool = False         # an estimate of something not measured: flagged on the channel
    version: int = 1               # goes up each time the formula or unit is changed


@dataclass
class Result:
    name: str
    ok: bool
    message: str = ""
    channel: Channel | None = None
    cached: bool = False


class MathSet:
    def __init__(self):
        self.channels: dict = {}
        self.parameters: dict = {}

    def add(self, name: str, formula: str, unit: str = "", description: str = "", category: str = "maths",
            estimate: bool = False) -> MathChannel:
        """Add a channel, or change one. The formula must parse; whether its
        inputs exist is a matter for the session it is computed on."""
        if not name or not name.replace("_", "a").replace(" ", "a").isalnum() or name[0].isdigit():
            raise FormulaError(f"'{name}' is not a usable channel name: use letters, digits, spaces and underscores, "
                               "starting with a letter.")
        parse(formula)
        old = self.channels.get(name)
        changed = old is not None and (old.formula, old.unit) != (formula, unit)
        self.channels[name] = MathChannel(name, formula, unit, description, category, estimate,
                                          (old.version + 1) if changed else (old.version if old else 1))
        return self.channels[name]

    def remove(self, name: str) -> None:
        self.channels.pop(name, None)

    def to_dict(self) -> dict:
        return {"version": SET_VERSION, "parameters": dict(self.parameters),
                "channels": [asdict(c) for c in self.channels.values()]}

    @classmethod
    def from_dict(cls, data: dict) -> "MathSet":
        if data.get("version", 1) > SET_VERSION:
            raise ValueError("These maths channels were saved by a newer version of the engine.")
        out = cls()
        out.parameters = {str(k): float(v) for k, v in data.get("parameters", {}).items()}
        for c in data.get("channels", []):
            out.channels[c["name"]] = MathChannel(**c)
        return out

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "MathSet":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# ── names ─────────────────────────────────────────────────────────────────────

def _lookup(model: SessionModel, name: str) -> Channel:
    """The session channel a name in a formula means.

    In order: "layer:id" exactly; the id of a calculated, corrected or
    normalised channel; the normalised channel that came from a source column
    of that name; the same two ignoring case and punctuation; a raw column.
    So `speed` and `Ground Speed` both mean the normalised speed, in the schema
    unit, and `[raw:Ground Speed]` means the column as the file has it. A name
    that fits more than one channel is refused rather than guessed."""
    if ":" in name and model.has(name):
        return model.get(name)
    key = norm_name(name)
    processed = model.channels("calculated") + model.channels("corrected") + model.channels("normalised")
    raw = model.channels("raw")
    for chans, test in ((processed, lambda c: c.id == name), (processed, lambda c: c.source_name == name),
                        (processed, lambda c: norm_name(c.id) == key),
                        (processed, lambda c: bool(c.source_name) and norm_name(c.source_name) == key),
                        (raw, lambda c: c.id == name), (raw, lambda c: norm_name(c.id) == key)):
        hits = [c for c in chans if test(c)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise FormulaError(f"'{name}' fits more than one channel: {', '.join(c.ref for c in hits)}. "
                               "Name the one meant, for example [normalised:speed].")
    raise KeyError(name)


def _series(c: Channel) -> Series:
    values = c.values
    if values.dtype.kind not in "biuf":
        raise FormulaError(f"The channel '{c.id}' is not numeric.")
    return Series(values, c.timestamps, c.validity, U.parse_unit(c.unit), c.discrete)


# ── order ─────────────────────────────────────────────────────────────────────

def order(mset: MathSet) -> tuple[list, dict]:
    """The order to compute the channels in, each after the ones it uses, and
    {name: message} for those that cannot be computed at all: a formula that
    does not parse, or a channel that depends on itself."""
    needs, problems = {}, {}
    for name, c in mset.channels.items():
        try:
            refs, params = references(parse(c.formula))
        except FormulaError as e:
            problems[name] = str(e)
            continue
        needs[name] = {r for r in refs if r in mset.channels}
    done, out = set(), []
    state: dict = {}

    def visit(name, path):
        if name in problems or state.get(name) == "done":
            return
        if state.get(name) == "open":
            loop = path[path.index(name):] + [name]
            for n in loop[:-1]:
                problems[n] = "This channel depends on itself: " + " → ".join(loop) + "."
            return
        state[name] = "open"
        for dep in sorted(needs[name]):
            visit(dep, path + [name])
        state[name] = "done"
        if name not in problems:
            out.append(name)
            done.add(name)
    for name in needs:
        visit(name, [])
    return [n for n in out if n not in problems], problems


def _signature(c: MathChannel, inputs: list, parameters: dict) -> str:
    h = hashlib.sha1(f"{ENGINE}|{c.formula}|{c.unit}|{c.version}".encode())
    for ch in inputs:
        v = np.asarray(ch.values, float) if ch.values.dtype.kind in "biuf" else np.zeros(1)
        h.update(f"|{ch.ref}|{ch.version}|{len(v)}|{float(np.nansum(v)) if len(v) else 0.0!r}|"
                 f"{float(ch.timestamps[0]) if len(v) else 0.0!r}|{float(ch.timestamps[-1]) if len(v) else 0.0!r}".encode())
    for k in sorted(parameters):
        h.update(f"|${k}={parameters[k]!r}".encode())
    return h.hexdigest()


# ── compute ───────────────────────────────────────────────────────────────────

def compute(model: SessionModel, mset: MathSet, names: list | None = None) -> dict:
    """Compute maths channels on a session and add them to its calculated
    layer. Returns {name: Result}; a channel that could not be computed has
    ok=False and a message saying why, and does not stop the others."""
    sequence, problems = order(mset)
    results = {name: Result(name, False, msg) for name, msg in problems.items()}
    wanted = None
    if names is not None:                      # the channels asked for, and everything they are built on
        wanted, todo = set(), list(names)
        while todo:
            n = todo.pop()
            if n in wanted or n not in mset.channels:
                continue
            wanted.add(n)
            if n not in problems:
                todo += list(references(parse(mset.channels[n].formula))[0])
    cache = model.maths_signatures
    for name in sequence:
        if wanted is not None and name not in wanted:
            continue
        c = mset.channels[name]
        tree = parse(c.formula)
        refs, params = references(tree)
        if any(ch.id == name for layer in ("raw", "normalised", "corrected") for ch in model.channels(layer)):
            results[name] = Result(name, False, f"The session already has a channel called '{name}'. Give the maths "
                                                "channel another name.")
            continue
        failed = sorted(r for r in refs if r in results and not results[r].ok)
        if failed:
            why = results[failed[0]].message.splitlines()[0]       # the reason at the bottom of the chain
            results[name] = Result(name, False, f"It uses {', '.join(failed)}, which could not be calculated: {why}")
            continue
        inputs, missing = {}, []
        try:
            for r in sorted(refs):
                try:
                    inputs[r] = results[r].channel if r in results else _lookup(model, r)
                except KeyError:
                    missing.append(r)
            if missing:
                results[name] = Result(name, False, "This session has no channel named " + ", ".join(missing) + ".")
                continue
            used = {k: mset.parameters[k] for k in params if k in mset.parameters}
            signature = _signature(c, list(inputs.values()), used)
            if cache.get(name) == signature and model.has(name, "calculated"):
                results[name] = Result(name, True, "", model.get(name, "calculated"), cached=True)
                continue
            ev = Evaluator(lambda r: _series(inputs[r]), mset.parameters, c.formula)
            out = ev.eval(tree)
            channel = _to_channel(model, c, out, inputs, used)
        except FormulaError as e:
            results[name] = Result(name, False, str(e))
            continue
        model.add(channel, replace=True)
        cache[name] = signature
        results[name] = Result(name, True, "", channel)
    return results


def _to_channel(model: SessionModel, c: MathChannel, out: Series, inputs: dict, used: dict) -> Channel:
    if out.scalar:
        raise FormulaError(f"The formula gives a single number ({out.v:g}), not a channel: it uses no channel.", c.formula)
    unit, steps = out.unit, list(out.steps)
    if c.unit:
        declared = U.parse_unit(c.unit)
        if not declared.known:
            raise FormulaError(f"The channel's unit '{c.unit}' is not one this engine knows.")
        if unit is U.NUMBER or not unit.known or (unit.dimensionless and declared.dimensionless):
            if not unit.known:
                steps.append(f"unit stated as {declared.name} by the channel definition (the formula's own unit is unknown)")
            unit = declared
        elif unit.dims != declared.dims:
            raise FormulaError(f"The formula gives {unit.name}, but the channel is declared in {declared.name}: "
                               "they measure different things.")
        elif (unit.scale, unit.offset) != (declared.scale, declared.offset):
            raise FormulaError(f"The formula gives {unit.name}, but the channel is declared in {declared.name}. Write "
                               f"(...) in {declared.name} to convert, or (...) as {declared.name} if the numbers are "
                               f"already in {declared.name}.")
        else:
            unit = declared
    name = "-" if unit is U.NUMBER else unit.name
    refs = tuple(ch.ref for ch in inputs.values())
    valid = out.mask()
    return Channel(
        id=c.name, layer="calculated", label=CALCULATED, timestamps=out.t, values=np.asarray(out.v, float), valid=valid,
        unit=name, name=c.name, description=c.description, category=c.category, discrete=out.discrete,
        formula=c.formula, dependencies=refs,
        quality=(["estimate — not a measurement"] if c.estimate else [])
                + ([f"{int((~valid).sum())} of {len(valid)} samples invalid"] if (~valid).any() else []),
        provenance=Provenance(inputs=refs, algorithm=c.formula,
                              parameters={"formula_version": c.version, **{f"${k}": v for k, v in used.items()}},
                              unit_source="schema", engine_version=ENGINE, notes=tuple(steps)),
        version=c.version, user={"maths": True})


def preview(formula: str, model: SessionModel, mset: MathSet | None = None, unit: str = "", name: str = "") -> Result:
    """Compute a formula on a session without keeping the result: what an
    editor needs to show a channel before it is saved. `mset` supplies the
    other maths channels and the parameters the formula may use; `name` is the
    channel being edited, which is left out so it cannot be its own input.
    The session's channels are not changed."""
    probe = MathSet()
    if mset is not None:
        probe.channels = {k: v for k, v in mset.channels.items() if k != name}
        probe.parameters = dict(mset.parameters)
    key = "__preview__"
    try:
        refs, _ = references(parse(formula))
    except FormulaError as e:
        return Result(key, False, e.message)
    if name and name in refs:
        return Result(key, False, f"'{name}' cannot be an input to itself.")
    probe.channels[key] = MathChannel(key, formula, unit)
    before = {c.id: c for c in model.channels("calculated")}
    signatures = dict(model.maths_signatures)
    try:
        if name:                               # the saved version of the channel being edited is not an input either
            model._channels["calculated"] = {k: v for k, v in before.items() if k != name}
        result = compute(model, probe, names=[key])[key]
    finally:
        # put the calculated layer back exactly as it was: nothing a preview computed stays
        model._channels["calculated"] = before
        model.maths_signatures = signatures
    return result


def check(formula: str, model: SessionModel | None = None, mset: MathSet | None = None, unit: str = "",
          name: str = "") -> dict:
    """Validate a formula as it is typed: {"ok", "message", "position",
    "inputs", "parameters", "unit"}. With a model, inputs are checked against
    the session and the unit the formula gives is worked out."""
    try:
        tree = parse(formula)
        refs, params = references(tree)
    except FormulaError as e:
        return {"ok": False, "message": e.message, "position": e.position, "inputs": [], "parameters": [], "unit": None}
    info = {"ok": True, "message": "", "position": None, "inputs": sorted(refs), "parameters": sorted(params), "unit": None}
    if model is None:
        return info
    result = preview(formula, model, mset, unit, name)
    if not result.ok:
        head, _, tail = result.message.partition("\n")
        marker = tail.rsplit("\n", 1)[-1] if tail else ""
        return {**info, "ok": False, "message": head, "position": marker.index("^") - 2 if "^" in marker else None}
    return {**info, "unit": result.channel.unit}


def dependencies(mset: MathSet, model: SessionModel | None = None) -> dict:
    """What every maths channel is built from and what is built on it:
    {name: {"uses_maths", "uses_session", "missing", "parameters",
    "undefined_parameters", "used_by", "order", "problem"}}. `uses_session`
    gives the session channel each name resolves to ("layer:id", unit);
    without a model nothing is resolved and nothing is reported missing."""
    sequence, problems = order(mset)
    out = {}
    for name, c in mset.channels.items():
        entry = {"uses_maths": [], "uses_session": [], "missing": [], "parameters": [], "undefined_parameters": [],
                 "used_by": [], "order": sequence.index(name) + 1 if name in sequence else None,
                 "problem": problems.get(name, "")}
        try:
            refs, params = references(parse(c.formula))
        except FormulaError:
            out[name] = entry
            continue
        entry["parameters"] = sorted(params)
        entry["undefined_parameters"] = sorted(p for p in params if p not in mset.parameters)
        for r in sorted(refs):
            if r in mset.channels:
                entry["uses_maths"].append(r)
            elif model is not None:
                try:
                    ch = _lookup(model, r)
                    entry["uses_session"].append({"name": r, "channel": ch.ref, "unit": ch.unit})
                except (KeyError, FormulaError):
                    entry["missing"].append(r)
            else:
                entry["uses_session"].append({"name": r, "channel": "", "unit": ""})
        out[name] = entry
    for name, entry in out.items():
        for dep in entry["uses_maths"]:
            out[dep]["used_by"].append(name)
    return out


def on_view(session, results: dict) -> list:
    """Put computed maths channels on the session's analysis view, so lap
    comparison, corner analysis and reports can use them like any other
    channel. Each is looked up on the view's clock by its own policy.
    Returns the names added."""
    t = session.channels["time"].to_numpy(float)
    added = {}
    for name, r in results.items():
        if r.ok and not name.startswith("__"):
            added[name] = r.channel.at(t)[0]
    if added:
        import pandas as pd
        session.derived = pd.concat([session.derived.drop(columns=[c for c in added if c in session.derived]),
                                     pd.DataFrame(added, index=session.derived.index)], axis=1)
    return list(added)
