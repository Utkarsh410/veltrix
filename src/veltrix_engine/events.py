"""
The event engine: configurable rules that look through a session's channels —
recorded or maths — for stretches worth a person's attention.

A rule is data, not code:

  on            a maths formula that is true while the condition holds
  off           (optional) a second formula that must become true to end the
                event — with `on`, that is hysteresis: start at one threshold,
                stop at another, so a signal hovering at the limit gives one
                event, not twenty
  min_duration  the condition must hold this long to count
  merge_gap     two events of one rule closer than this are one
  severity      (optional) a formula whose peak during the event grades it
  evidence      channels whose range during the event is recorded with it

Formulas are the maths-channel language (maths/), so a rule can use any
channel, any maths channel, and the rule set's own named parameters — every
threshold is a parameter and none is hard-wired. Helper channels a rule set
needs (a slip estimate, say) are maths channels defined in the set.

What comes out is `model.Event`: start, end, lap, position, severity, the
evidence, a confidence, and the rule and version that found it. Events are
candidates. Their wording says so ("Possible wheelspin"), and nothing here
decides what happened — a detector sees a pattern in channels, not the car.

A rule whose inputs a session lacks is skipped with the reason; the others
still run.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import maths
from .maths import FormulaError, MathSet
from .model import CANDIDATE, CONFIRMED, GAP_FACTOR, RECORDED, REJECTED, Event, SessionModel

RULES_VERSION = 1
_PREFIX = "__event_"


@dataclass
class Rule:
    id: str
    title: str                         # how an event of this rule is called: "Possible wheelspin"
    on: str
    off: str = ""                      # "" = ends when `on` stops holding
    min_duration_s: float = 0.0
    merge_gap_s: float = 0.0
    severity: str = ""                 # formula; its largest absolute value in the event is the event's magnitude
    moderate: float | None = None      # magnitude at or above this is "moderate" ...
    severe: float | None = None        # ... and at or above this "severe"; below both "mild"
    evidence: tuple = ()               # channels summarised with each event
    fine_inputs: tuple = ()            # channels that must be finely recorded: a rule that differentiates a
                                       # signal sees every rounding step of a coarsely recorded one as an event
    confidence: str = "low"            # how far the rule can be trusted, for the reader
    note: str = ""                     # what else could produce the same pattern
    version: int = 1
    enabled: bool = True
    calibrate: dict = field(default_factory=dict)     # how its threshold is fitted to a session; see calibrate()


@dataclass
class RuleSet:
    rules: list = field(default_factory=list)
    parameters: dict = field(default_factory=dict)
    helpers: dict = field(default_factory=dict)      # {maths channel name: formula} the rules build on

    def to_dict(self) -> dict:
        return {"version": RULES_VERSION, "parameters": dict(self.parameters), "helpers": dict(self.helpers),
                "rules": [{**asdict(r), "evidence": list(r.evidence), "fine_inputs": list(r.fine_inputs)} for r in self.rules]}

    @classmethod
    def from_dict(cls, data: dict) -> "RuleSet":
        from .versions import upgrade
        data = upgrade("event rule set", data, RULES_VERSION)
        if data.get("version", 1) > RULES_VERSION:
            raise ValueError("These event rules were saved by a newer version of the engine.")
        rules = [Rule(**{**r, "evidence": tuple(r.get("evidence", ())), "fine_inputs": tuple(r.get("fine_inputs", ()))})
                 for r in data.get("rules", [])]
        out = cls(rules, {str(k): float(v) for k, v in data.get("parameters", {}).items()}, dict(data.get("helpers", {})))
        out.check()
        return out

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "RuleSet":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def check(self) -> None:
        """Every formula must parse, and rule ids must be unique. Raises ValueError."""
        seen = set()
        for r in self.rules:
            if r.id in seen or not r.id.replace("_", "a").isalnum():
                raise ValueError(f"Rule id '{r.id}' is repeated or is not a plain name.")
            seen.add(r.id)
            for what, formula in (("on", r.on), ("off", r.off), ("severity", r.severity)):
                if formula or what == "on":
                    try:
                        maths.parser.parse(formula)
                    except FormulaError as e:
                        raise ValueError(f"Rule '{r.id}', {what}: {e.message}") from None
        for name, formula in self.helpers.items():
            try:
                maths.parser.parse(formula)
            except FormulaError as e:
                raise ValueError(f"Helper channel '{name}': {e.message}") from None


def default_rules() -> RuleSet:
    """A starting rule set. Every number is a named parameter and illustrative:
    tune them to the car and the sensors before reading much into the events."""
    p = {"min_speed": 40.0,            # km/h below which nothing is looked for
         "spin_throttle": 70.0, "spin_on": 8.0, "spin_off": 4.0,           # % throttle, % slip to start, % slip to end
         "lock_brake_g": 0.5, "lock_on": 12.0, "lock_off": 6.0,            # g of braking, % a front wheel is slower
         "loss_lat_g": 0.8,                                                # wheelspin while cornering this hard
         "over_metric": 0.35,                                              # g on the oversteer metric
         "overlap_throttle": 25.0, "overlap_brake_g": 0.4,
         "correction_rate": 400.0,                                         # deg/s at the steering wheel
         "glitch_g": 6.0}                                                  # a change in speed no car can make
    helpers = {
        "FrontWheelSpeed": "(wheel_speed_fl + wheel_speed_fr) / 2",
        "RearWheelSpeed": "(wheel_speed_rl + wheel_speed_rr) / 2",
        "RearSlipEstimate": "if(speed > $min_speed, (RearWheelSpeed - FrontWheelSpeed) / speed * 100, 0)",
        "FrontLockEstimate": "if(speed > $min_speed, (speed - min(wheel_speed_fl, wheel_speed_fr)) / speed * 100, 0)",
        "SpeedChangeG": "abs(derivative(speed in m/s) in g)",
    }
    rules = [
        Rule("wheelspin", "Possible wheelspin", "throttle > $spin_throttle and RearSlipEstimate > $spin_on",
             off="RearSlipEstimate < $spin_off or throttle < $spin_throttle", min_duration_s=0.15, merge_gap_s=0.3,
             severity="RearSlipEstimate", moderate=12.0, severe=20.0, evidence=("throttle", "RearSlipEstimate", "speed"),
             confidence="medium", note="A rear tyre of smaller rolling radius, or a kerb, gives the same reading.",
             calibrate={"parameter": "spin_on", "signal": "RearSlipEstimate", "quiet": "throttle < 20 and speed > $min_speed",
                        "sigmas": 6.0, "off": "spin_off", "off_ratio": 0.5}),
        Rule("lock_up", "Possible front lock-up", "acc_long_calc < -$lock_brake_g and FrontLockEstimate > $lock_on",
             off="FrontLockEstimate < $lock_off", min_duration_s=0.10, merge_gap_s=0.3, severity="FrontLockEstimate",
             moderate=20.0, severe=40.0, evidence=("FrontLockEstimate", "speed", "acc_long_calc"), confidence="medium",
             note="Vehicle speed taken from a wheel that is itself locking hides the lock-up.",
             calibrate={"parameter": "lock_on", "signal": "FrontLockEstimate", "quiet": "acc_long_calc > -0.1 and speed > $min_speed",
                        "sigmas": 6.0, "off": "lock_off", "off_ratio": 0.5}),
        Rule("traction_loss", "Possible traction loss",
             "throttle > $spin_throttle and RearSlipEstimate > $spin_on and abs(acc_lat) > $loss_lat_g",
             off="RearSlipEstimate < $spin_off", min_duration_s=0.15, merge_gap_s=0.3, severity="RearSlipEstimate",
             moderate=12.0, severe=20.0, evidence=("RearSlipEstimate", "acc_lat", "steering", "throttle"), confidence="low",
             note="Wheelspin while cornering; whether the rear stepped out needs the steering trace or the video."),
        Rule("oversteer", "Possible oversteer", "speed > $min_speed and oversteer_metric > $over_metric",
             min_duration_s=0.25, merge_gap_s=0.3, severity="oversteer_metric", moderate=0.5, severe=0.8,
             evidence=("oversteer_metric", "steering", "acc_lat", "speed"), confidence="low",
             note="The metric assumes the configured wheelbase and steering ratio; a wrong value shifts every reading.",
             calibrate={"parameter": "over_metric", "signal": "oversteer_metric", "where": "speed > $min_speed", "percentile": 98.0}),
        Rule("brake_throttle_overlap", "Braking anomaly: throttle held while braking",
             "throttle > $overlap_throttle and acc_long_calc < -$overlap_brake_g and speed > $min_speed",
             min_duration_s=0.30, merge_gap_s=0.2, severity="throttle", evidence=("throttle", "acc_long_calc", "speed"),
             confidence="low", note="Deliberate left-foot braking looks the same; so does lift-off on a steep climb."),
        Rule("steering_correction", "Possible steering correction", "speed > $min_speed and abs(steering_rate) > $correction_rate",
             min_duration_s=0.05, merge_gap_s=0.25, severity="abs(steering_rate)", moderate=600.0, severe=900.0,
             evidence=("steering", "steering_rate", "speed"), confidence="low",
             note="A chicane or a hairpin needs fast hands too; compare with the same place on other laps.",
             calibrate={"parameter": "correction_rate", "signal": "abs(steering_rate)", "where": "speed > $min_speed",
                        "percentile": 99.8}),
        Rule("speed_glitch", "Possible sensor fault: speed jumps", "SpeedChangeG > $glitch_g", min_duration_s=0.0,
             merge_gap_s=0.5, severity="SpeedChangeG", evidence=("speed",), fine_inputs=("speed",), confidence="medium",
             note="A real impact also changes speed this fast."),
    ]
    out = RuleSet(rules, p, helpers)
    out.check()
    return out


def calibrate(model: SessionModel, rules: RuleSet | None = None) -> tuple[RuleSet, list]:
    """Fit a rule set's thresholds to one session, so they are never below
    what this car's sensors read when nothing is happening.

    A rule says how in its `calibrate` field:
      parameter   the threshold to fit (a name in the set's parameters)
      signal      the maths formula the threshold is compared with
      quiet       a condition under which nothing should be detected (coasting,
                  off the brakes); the threshold is raised to the signal's
                  median plus `sigmas` robust standard deviations there
      percentile  instead of `quiet`: the threshold is raised to this
                  percentile of the signal where `where` holds — "unusual for
                  this driver on this day"
      off, off_ratio   a release threshold kept at this share of the fitted one

    A threshold is only ever raised, never lowered below the set's own value:
    the configured number stays the floor. Returns (a new RuleSet, what was
    changed and why, as sentences). A rule whose signal cannot be computed on
    this session is left as it is."""
    rules = rules or default_rules()
    out = RuleSet.from_dict(rules.to_dict())
    probe = MathSet()
    probe.parameters = dict(out.parameters)
    for name, formula in out.helpers.items():
        probe.channels[name] = maths.MathChannel(name, formula)
    changes, fitted = [], {}
    for r in out.rules:
        c = r.calibrate
        key = c.get("parameter")
        if not r.enabled or not key or key not in out.parameters or "signal" not in c:
            continue
        if key in fitted:                                   # two rules sharing a threshold share its fit
            continue
        signal = maths.preview(c["signal"], model, probe)
        scope = maths.preview(c.get("quiet") or c.get("where") or "1 > 0", model, probe) if (c.get("quiet") or c.get("where")) else None
        if not signal.ok or (scope is not None and not scope.ok):
            continue
        ch = signal.channel
        x = np.abs(np.asarray(ch.values, float))
        use = ch.validity.copy()
        if scope is not None:
            mask = scope.channel
            m = np.asarray(mask.values, float) if mask.timestamps is ch.timestamps or np.array_equal(mask.timestamps, ch.timestamps) \
                else mask.at(ch.timestamps)[0]
            use &= np.nan_to_num(m) > 0
        if use.sum() < 200:
            continue
        x = x[use]
        if "quiet" in c:
            centre = float(np.median(x))
            sigma = 1.4826 * float(np.median(np.abs(x - centre)))
            level = centre + float(c.get("sigmas", 6.0)) * sigma
            why = (f"with nothing happening ({c['quiet']}) {c['signal']} already reads {centre:.3g} ± {sigma:.3g}")
        else:
            level = float(np.percentile(x, float(c["percentile"])))
            why = f"{c['signal']} is below {level:.4g} for {float(c['percentile']):g}% of this session"
        fitted[key] = level
        old = out.parameters[key]
        if level > old:
            out.parameters[key] = float(f"{level:.4g}")
            changes.append(f"${key} raised from {old:g} to {out.parameters[key]:g}: {why}.")
            if c.get("off") in out.parameters:
                was = out.parameters[c["off"]]
                out.parameters[c["off"]] = max(was, float(f"{out.parameters[key] * float(c.get('off_ratio', 0.5)):.4g}"))
                if out.parameters[c["off"]] != was:
                    changes.append(f"${c['off']} raised from {was:g} to {out.parameters[c['off']]:g} to keep its distance from ${key}.")
    return out, changes


def _spans(t: np.ndarray, on: np.ndarray, off: np.ndarray | None, valid: np.ndarray) -> list:
    """Index ranges [a, b] where an event is active: it starts on a valid
    sample where `on` holds and runs until `off` holds (or `on` stops, without
    an `off`), an invalid sample, or a gap in the timestamps."""
    n = len(t)
    if n == 0:
        return []
    dt = np.diff(t)
    step = np.median(dt[dt > 0]) if (dt > 0).any() else 0.0
    broken = np.r_[False, (dt > GAP_FACTOR * step) | (dt <= 0)] | ~valid          # an event cannot run through these
    stop = (~on if off is None else off) | broken
    starts, stops = np.flatnonzero(on & ~broken), np.flatnonzero(stop)
    out, i = [], 0
    while i < len(starts):
        a = starts[i]
        k = np.searchsorted(stops, a, side="right")
        b = (stops[k] - 1) if k < len(stops) else n - 1
        out.append((int(a), int(max(b, a))))
        i = np.searchsorted(starts, max(b, a), side="right")
    return out


def detect(model: SessionModel, rules: RuleSet | None = None, fit: bool = False) -> tuple[list, dict]:
    """Run a rule set over a session. Returns (events in time order, report),
    where report[rule id] = {"ran", "events", "message"} says what each rule
    did — including why a rule could not run on this session. With `fit`, the
    thresholds are first fitted to the session (see calibrate) and
    report["_calibration"] lists what was changed."""
    rules = rules or default_rules()
    fitted = []
    original = dict(rules.parameters)
    if fit:
        rules, fitted = calibrate(model, rules)
    limits = {r.id: rule_limits(rules, r, original) for r in rules.rules}
    mset = MathSet()
    mset.parameters = dict(rules.parameters)
    for name, formula in rules.helpers.items():
        mset.channels[name] = maths.MathChannel(name, formula, estimate=True)
    active = [r for r in rules.rules if r.enabled]
    for r in active:
        mset.channels[f"{_PREFIX}{r.id}_on"] = maths.MathChannel(f"{_PREFIX}{r.id}_on", r.on)
        if r.off:
            mset.channels[f"{_PREFIX}{r.id}_off"] = maths.MathChannel(f"{_PREFIX}{r.id}_off", r.off)
        if r.severity:
            mset.channels[f"{_PREFIX}{r.id}_sev"] = maths.MathChannel(f"{_PREFIX}{r.id}_sev", r.severity)
    had = {c.id for c in model.channels("calculated")}
    results = maths.compute(model, mset)
    laps = model.laps
    lap_distance = model.get("lap_distance") if model.has("lap_distance") else None
    events, report = [], {r.id: {"ran": False, "events": 0, "message": "switched off"} for r in rules.rules if not r.enabled}
    validity, found, peaks = {}, {}, []

    def valid_of(ch):                          # a channel's mask is worked out once, not once per event
        if id(ch) not in validity:
            validity[id(ch)] = ch.validity
        return validity[id(ch)]

    def evidence_channel(name):
        if name not in found:
            found[name] = _evidence_channel(model, results, name)
        return found[name]
    try:
        for r in active:
            coarse = [(name, flag) for name in r.fine_inputs for ch in [evidence_channel(name)] if ch is not None
                      for flag in ch.quality if "coarse steps" in flag]
            if coarse:
                report[r.id] = {"ran": False, "events": 0, "message": "Not looked for: " + "; ".join(
                    f"{name} is {flag}" for name, flag in coarse) + " — every rounding step would look like an event."}
                continue
            cond = results[f"{_PREFIX}{r.id}_on"]
            failed = next((results[k] for k in (f"{_PREFIX}{r.id}_on", f"{_PREFIX}{r.id}_off", f"{_PREFIX}{r.id}_sev")
                           if k in results and not results[k].ok), None)
            if failed is not None:
                report[r.id] = {"ran": False, "events": 0, "message": "Not looked for: " + failed.message.splitlines()[0]}
                continue
            c = cond.channel
            t, on = c.timestamps, np.asarray(c.values, float) > 0
            off = None
            if r.off:
                o = results[f"{_PREFIX}{r.id}_off"].channel
                off = (np.asarray(o.values, float) > 0) if o.timestamps is t or np.array_equal(o.timestamps, t) \
                    else o.at(t)[0] > 0
            spans = _spans(t, on & valid_of(c), off, valid_of(c))
            merged = []
            for a, b in spans:                                   # join events closer than the merge gap
                if merged and t[a] - t[merged[-1][1]] <= r.merge_gap_s:
                    merged[-1] = (merged[-1][0], b)
                else:
                    merged.append((a, b))
            sev = results[f"{_PREFIX}{r.id}_sev"].channel if r.severity else None
            kept = 0
            for a, b in merged:
                t0, t1 = float(t[a]), float(t[b])
                if t1 - t0 < r.min_duration_s:
                    continue
                magnitude, t_peak = None, 0.5 * (t0 + t1)
                if sev is not None:
                    i0, i1 = np.searchsorted(sev.timestamps, [t0, t1 + 1e-12])
                    part = np.abs(np.where(valid_of(sev)[i0:i1], np.asarray(sev.values[i0:i1], float), np.nan))
                    if len(part) and np.isfinite(part).any():
                        k = int(np.nanargmax(part))
                        magnitude, t_peak = float(part[k]), float(sev.timestamps[i0 + k])
                level = ""
                if magnitude is not None and (r.moderate is not None or r.severe is not None):
                    level = ("severe" if r.severe is not None and magnitude >= r.severe else
                             "moderate" if r.moderate is not None and magnitude >= r.moderate else "mild")
                evidence = {"duration_s": round(t1 - t0, 3)}
                if magnitude is not None:
                    evidence["peak"] = {"of": r.severity, "value": magnitude, "at_s": t_peak}
                for name in r.evidence:
                    ch = evidence_channel(name)
                    if ch is None:
                        continue
                    i0, i1 = np.searchsorted(ch.timestamps, [t0, t1 + 1e-12])
                    v = np.asarray(ch.values[i0:i1], float)[valid_of(ch)[i0:i1]]
                    if len(v):
                        evidence[name] = {"min": float(v.min()), "max": float(v.max()), "mean": float(v.mean()), "unit": ch.unit}
                lap = next((x for x in laps if x.t_start <= t_peak < x.t_end), None)
                peaks.append(t_peak)
                if r.note:
                    evidence["could_also_be"] = r.note
                events.append(Event(kind=r.title, t_start=t0, t_end=t1, lap=lap.lap if lap else None, position_m=None,
                                    severity=level, channels=tuple(r.evidence), evidence=evidence,
                                    confidence=f"{r.confidence} — requires review", rule=r.id,
                                    rule_version=f"{r.version}", source="rule", thresholds=limits[r.id]))
                kept += 1
            report[r.id] = {"ran": True, "events": kept, "message": f"{kept} found" if kept else "none found"}
    finally:
        # the engine's working channels are not the session's: only helper channels a rule reported on stay
        for name in list(model._channels["calculated"]):
            if name.startswith(_PREFIX) and name not in had:
                del model._channels["calculated"][name]
                model.maths_signatures.pop(name, None)
    if lap_distance is not None and events:                  # where on the lap each one peaked, looked up together
        for e, d in zip(events, lap_distance.at(np.array(peaks))[0]):
            e.position_m = float(d) if np.isfinite(d) else None
    events.sort(key=lambda e: e.t_start)
    if fit:
        report["_calibration"] = {"ran": True, "events": 0, "message": " ".join(fitted) or "No threshold needed raising.",
                                  "changes": fitted, "parameters": dict(rules.parameters)}
    return events, report


def _evidence_channel(model, results, name):
    r = results.get(name)
    if r is not None:
        return r.channel if r.ok else None
    try:
        return maths._lookup(model, name)
    except (KeyError, FormulaError):
        return None


_PARAM = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def rule_limits(rules: RuleSet, rule: Rule, original: dict | None = None) -> dict:
    """The limits a rule finds its events with, as they stand: every threshold
    its formulas use (directly or through a helper channel), which of them were
    fitted to this session and from what, its minimum duration and merge gap, and
    the severity bands. Kept on each event so the classification can be audited
    after the rules have changed."""
    text = " ".join(x for x in (rule.on, rule.off, rule.severity) if x)
    used_helpers = [h for h in rules.helpers if re.search(rf"\b{re.escape(h)}\b", text)]
    text += " " + " ".join(rules.helpers[h] for h in used_helpers)
    names = sorted(set(_PARAM.findall(text)))
    params = {n: rules.parameters[n] for n in names if n in rules.parameters}
    fitted = {n: {"was": original[n], "now": params[n]} for n in names
              if original and n in original and n in params and abs(original[n] - params[n]) > 1e-12}
    return {"rule": rule.id, "version": rule.version, "on": rule.on, "off": rule.off,
            "parameters": params, "fitted": fitted, "min_duration_s": rule.min_duration_s,
            "merge_gap_s": rule.merge_gap_s, "moderate": rule.moderate, "severe": rule.severe,
            "rule_confidence": rule.confidence}


def limits_text(thresholds: dict) -> str:
    """A rule's limits in one line, for a table cell."""
    if not thresholds:
        return ""
    if "parameters" in thresholds:
        parts = [f"{k} {v:g}" + (f" (fitted from {thresholds['fitted'][k]['was']:g})" if k in thresholds.get("fitted", {}) else "")
                 for k, v in thresholds["parameters"].items()]
        if thresholds.get("min_duration_s"):
            parts.append(f"at least {thresholds['min_duration_s']:g} s")
        return ", ".join(parts)
    return ", ".join(f"{k.replace('_', ' ')} {v:g}" if isinstance(v, (int, float)) else f"{k.replace('_', ' ')} {v}"
                     for k, v in thresholds.items())


# ── review: a person's verdict, kept beside what was detected ─────────────────

VERDICTS = ("confirmed", "rejected")
REVIEWS_VERSION = 1
MATCH_S = 1.0                 # a review belongs to an event of the same rule and kind starting this close


def _snapshot(e: Event) -> dict:
    """An event exactly as it was detected — kept, unchanged, with every review."""
    d = asdict(e)
    d["channels"] = list(e.channels)
    d.pop("review", None)
    return json.loads(json.dumps(d, default=lambda o: o.item() if hasattr(o, "item") else str(o)))


def make_review(event: Event, verdict: str, note: str = "", author: str = "", earlier: dict | None = None) -> dict:
    """A review of one event: the verdict ("confirmed", "rejected" or "" for a
    note alone), a note, who and when — and the event as first detected, so the
    detector's own classification can be audited whatever happens later."""
    if verdict not in VERDICTS + ("",):
        raise ValueError(f"A verdict is one of {', '.join(VERDICTS)}, or empty for a note alone.")
    original = (earlier or {}).get("original") or _snapshot(event)
    return {"rule": event.rule, "kind": event.kind, "t_start": float(event.t_start), "t_end": float(event.t_end),
            "lap": event.lap, "verdict": verdict, "note": note.strip(), "author": author.strip(),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "original": original,
            "history": list((earlier or {}).get("history", [])) + (
                [{k: earlier[k] for k in ("verdict", "note", "author", "at")}] if earlier else [])}


def _matches(review: dict, e: Event) -> bool:
    return (review["rule"] == e.rule and review["kind"] == e.kind
            and abs(review["t_start"] - e.t_start) <= MATCH_S)


def find_review(reviews: list, event: Event) -> dict | None:
    return next((r for r in reviews if _matches(r, event)), None)


def apply_reviews(events: list, reviews: list) -> list:
    """Put each review on its event: a confirmed one becomes "confirmed" (by a
    person), a rejected one "rejected"; a note alone leaves the status. Returns
    the reviews no event matches any more — the rules or thresholds changed —
    which are kept, with the event as it was originally detected."""
    left = list(reviews)
    for e in events:
        # start from the event as detected, so a changed or cleared review leaves no trace on it
        e.status = e.evidence.setdefault("detected_status", e.status)
        e.review = {}
        r = find_review(left, e)
        if r is None:
            continue
        left.remove(r)
        e.review = {k: r[k] for k in ("verdict", "note", "author", "at")}
        if r["verdict"] == "confirmed" and e.status != RECORDED:
            e.status = CONFIRMED
        elif r["verdict"] == "rejected" and e.status != RECORDED:
            e.status = REJECTED
    return left


class ReviewStore:
    """Reviews on disk: one JSON file per source file (by its SHA-256)."""

    def __init__(self, folder: str | Path):
        self.folder = Path(folder)

    def _path(self, sha256: str) -> Path:
        if not sha256 or not str(sha256).isalnum():
            raise ValueError("Reviews are stored per source file; this session has no file identity.")
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
            return []
        from .versions import upgrade
        try:
            return list(upgrade("set of event reviews", data, REVIEWS_VERSION)["reviews"])   # a newer file raises
        except (KeyError, TypeError):
            return []

    def save(self, sha256: str, reviews: list) -> None:
        from .versions import refuse_overwrite
        path = self._path(sha256)
        refuse_overwrite(path, "set of event reviews", REVIEWS_VERSION)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": REVIEWS_VERSION, "reviews": reviews}, indent=1), encoding="utf-8")

    def put(self, sha256: str, event: Event, verdict: str, note: str = "", author: str = "") -> dict:
        """Record (or replace) the review of one event; earlier verdicts go into its history."""
        reviews = self.load(sha256)
        earlier = find_review(reviews, event)
        if earlier is not None:
            reviews.remove(earlier)
        new = make_review(event, verdict, note, author, earlier)
        reviews.append(new)
        self.save(sha256, reviews)
        return new

    def clear(self, sha256: str, event: Event) -> bool:
        reviews = self.load(sha256)
        r = find_review(reviews, event)
        if r is None:
            return False
        reviews.remove(r)
        self.save(sha256, reviews)
        return True


# iRacing's irsdk_IncidentFlags, low byte: the kinds named are the ones the SDK documents
SIM_INCIDENT = {0x01: "loss of control", 0x02: "off track"}
CONFIRM_WINDOW_S = 3.0        # a candidate within this of a recorded incident is confirmed by it


def recorded_incidents(model: SessionModel) -> list:
    """Incidents the source itself recorded — a simulator's incident system — as
    events with status "recorded": a rise in the driver's incident count, named
    from the incident flags; and a flagged event with no points."""
    count = model.get("incident_count") if model.has("incident_count") else None
    flags = model.get("incident_flags") if model.has("incident_flags") else None
    if count is None and flags is None:
        return []
    dist = model.get("lap_distance") if model.has("lap_distance") else None

    def code_near(t):
        if flags is None:
            return 0
        a, b = np.searchsorted(flags.timestamps, [t - 0.5, t + 0.5])
        v = np.asarray(flags.values[a:b], float)
        v = v[np.isfinite(v) & (v != 0)]
        return int(v[0]) if len(v) else 0

    def make(t, points, code):
        lap = next((x for x in model.laps if x.t_start <= t < x.t_end), None)
        at = None
        if dist is not None:
            value, _ = dist.at([t])
            at = float(value[0]) if np.isfinite(value[0]) else None
        kind = SIM_INCIDENT.get(code & 0xFF, f"incident code {code & 0xFF}" if code else "incident")
        return Event(kind=kind, t_start=float(t), t_end=float(t), lap=lap.lap if lap else None, position_m=at,
                     severity="", channels=tuple(c for c in ("incident_count", "incident_flags") if model.has(c)),
                     evidence={"points": float(points), "code": int(code)},
                     confidence="recorded by the simulator", rule="simulator incident record", status=RECORDED,
                     source="simulator")

    out, taken = [], []
    if count is not None:
        t, v = count.timestamps, np.asarray(count.values, float)
        for i in np.flatnonzero(np.diff(v) > 0) + 1:
            out.append(make(t[i], v[i] - v[i - 1], code_near(t[i])))
            taken.append(t[i])
    if flags is not None:
        t, v = flags.timestamps, np.asarray(flags.values, float)
        for i in np.flatnonzero((v[1:] != v[:-1]) & (v[1:] != 0) & np.isfinite(v[1:])) + 1:
            if all(abs(t[i] - x) > 1.0 for x in taken):
                out.append(make(t[i], 0.0, int(v[i])))
    return sorted(out, key=lambda e: e.t_start)


def confirm_with_records(candidates: list, recorded: list, window_s: float = CONFIRM_WINDOW_S) -> int:
    """Mark the candidates that a recorded incident with points backs up — the same
    moment, within window_s — as confirmed, naming the record. Returns how many."""
    n = 0
    hits = [r for r in recorded if r.evidence.get("points", 0) > 0 or (r.evidence.get("code", 0) & 0xFF) in SIM_INCIDENT]
    for e in candidates:
        if e.status == RECORDED:
            continue
        for r in hits:
            if r.t_start - window_s <= e.t_end and e.t_start <= r.t_end + window_s:
                e.status = CONFIRMED
                e.evidence["confirmed_by"] = f"{r.kind} recorded by the simulator at {r.t_start:.1f} s"
                n += 1
                break
    return n


def describe(event: Event) -> str:
    """One sentence for a list or a report, worded as a candidate."""
    where = f" on lap {event.lap}" if event.lap is not None else ""
    at = f" at {event.position_m:.0f} m" if event.position_m is not None else ""
    peak = event.evidence.get("peak")
    size = f", peaking at {peak['value']:.3g}" if peak else ""
    grade = f" ({event.severity})" if event.severity else ""
    return (f"{event.kind}{grade}{where}{at}: {event.evidence.get('duration_s', event.t_end - event.t_start):.2f} s from "
            f"{event.t_start:.2f} s{size}. Confidence {event.confidence}.")
