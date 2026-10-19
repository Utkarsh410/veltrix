"""
Mapping profiles: how a file's columns become the engine's channels, decided
in a fixed order and recorded column by column.

A profile is a reusable list of rules for one logger set-up (a car, a series, a
team): "a column named like this is that channel, in this unit". It is saved
as JSON and applied to every file from that set-up.

For each source column the decision is the first of these that applies:

  1. explicit metadata   the file format itself identifies the channel
                         (an iRacing variable, a hint from the importer)
  2. verified profile    a rule in the profile that a person has confirmed
  3. plausibility        the name matches a channel AND the values fit what
                         that channel can be; an unconfirmed profile rule also
                         lands here. A name alone never decides
  4. user                an explicit choice passed in for this import
  5. unmapped            nothing above applies; the column stays raw

(An explicit user choice, when given, overrides all of the others.)

Every decision carries its basis, a confidence, whether a person confirmed
it, the unit and where that unit came from, and any validation failures. Two
columns claiming one channel is a conflict: the better-founded one wins and
the other is reported.

  decisions = decide(raw, profile)
  mapping, units = to_import_args(decisions)
  session = build_session(raw, mapping=mapping, units=units)
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .importers import RawTable
from .mapping import auto_map
from .schema import CHANNELS, UNIT_FACTORS

PROFILE_VERSION = 1
BASES = ("metadata", "profile", "plausibility", "user", "unmapped")

# What each quantity's values can plausibly be, in the schema unit. Deliberately
# wide: this catches a wrong channel or unit, not an unusual car. Configurable.
PLAUSIBLE = {
    "speed": (0.0, 450.0), "rpm": (0.0, 25_000.0), "gear": (-1.0, 12.0), "throttle": (-5.0, 105.0),
    "steering": (-1200.0, 1200.0), "acc_lat": (-8.0, 8.0), "acc_long": (-8.0, 8.0), "acc_vert": (-10.0, 10.0),
    "yaw_rate": (-6.0, 6.0), "gps_lat": (-90.0, 90.0), "gps_lon": (-180.0, 180.0), "lap": (0.0, 2000.0),
    "tyre_temp": (-30.0, 250.0), "brake_temp": (-30.0, 1300.0), "tyre_press": (0.3, 5.0), "wheel_speed": (0.0, 500.0),
    "susp_travel": (-300.0, 300.0),
}


def _range_for(channel: str):
    return PLAUSIBLE.get(channel) or next((v for k, v in PLAUSIBLE.items() if channel.startswith(k)), None)


@dataclass
class Rule:
    pattern: str                    # source column name; * and ? are wildcards; case does not matter
    channel: str                    # the engine channel id it maps to
    unit: str = ""                  # the unit the column is in ("" = as the file states)
    valid_min: float | None = None  # values, in the schema unit, the column must stay within
    valid_max: float | None = None
    confidence: float = 0.7
    confirmed: bool = False         # a person has checked this rule


@dataclass
class Profile:
    name: str
    logger: str = ""
    car: str = ""
    series: str = ""
    team: str = ""
    rules: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"version": PROFILE_VERSION, **asdict(self)}

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        from .versions import upgrade
        data = upgrade("mapping profile", data, PROFILE_VERSION)
        if data.get("version", 1) > PROFILE_VERSION:
            raise ValueError("This mapping profile was saved by a newer version of the engine.")
        rules = [Rule(**r) for r in data.get("rules", [])]
        for r in rules:
            if r.channel not in CHANNELS or CHANNELS[r.channel].kind != "measured":
                raise ValueError(f"The profile maps '{r.pattern}' to '{r.channel}', which is not a channel this engine has.")
        return cls(str(data["name"]), data.get("logger", ""), data.get("car", ""), data.get("series", ""),
                   data.get("team", ""), rules)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def rule_for(self, column: str) -> Rule | None:
        low = str(column).lower()
        exact = [r for r in self.rules if r.pattern.lower() == low]
        return (exact or [r for r in self.rules if fnmatch.fnmatchcase(low, r.pattern.lower())] or [None])[0]


@dataclass
class Decision:
    column: str
    channel: str | None             # None: unmapped
    basis: str                      # one of BASES
    confidence: float = 0.0
    confirmed: bool = False
    unit: str = ""                  # the unit the column is taken to be in ("unknown" if nothing says)
    unit_source: str = ""           # file | profile | user | none
    problems: list = field(default_factory=list)      # validation failures and conflicts
    reason: str = ""


def _unit_scale(channel: str, unit: str) -> float | None:
    q = CHANNELS[channel].quantity
    table = UNIT_FACTORS.get(q)
    if not table or not unit:
        return None
    return table.get(unit.strip().lower().replace(" ", "").replace("²", "^2"))


def _validate(values: pd.Series, channel: str, unit: str, lo=None, hi=None) -> list:
    """Reasons the column's values do not fit the channel. Empty: they fit."""
    x = pd.to_numeric(values, errors="coerce").to_numpy(float)
    x = x[np.isfinite(x)]
    if not len(x):
        return ["no numeric values"]
    scale = _unit_scale(channel, unit)
    limits = (lo, hi) if lo is not None or hi is not None else _range_for(channel)
    if limits is None or (scale is None and unit and CHANNELS[channel].quantity in UNIT_FACTORS):
        return [] if limits is None else [f"unit '{unit}' is not one this channel can be converted from"]
    x = x * (scale or 1.0)
    p_lo, p_hi = np.percentile(x, [0.5, 99.5])
    lo_ok = limits[0] is None or p_lo >= limits[0]
    hi_ok = limits[1] is None or p_hi <= limits[1]
    if lo_ok and hi_ok:
        return []
    return [f"values run {p_lo:.4g} to {p_hi:.4g} {CHANNELS[channel].unit}, outside the {limits[0]:g} to {limits[1]:g} "
            f"this channel can be"]


def decide(raw: RawTable, profile: Profile | None = None, user: dict | None = None) -> list:
    """One Decision per source column, in the file's column order.

    user: {column: channel id or None} — choices made for this import. They
    override everything else; None un-maps a column."""
    user = dict(user or {})
    columns = list(raw.data.columns)
    for col, channel in user.items():
        if col not in columns:
            raise ValueError(f"'{col}' is not a column of this file.")
        if channel is not None and (channel not in CHANNELS or CHANNELS[channel].kind != "measured"):
            raise ValueError(f"'{channel}' is not a channel this engine has.")
    hinted = {col: cid for cid, col in raw.hints.items() if col in columns}
    by_name = {col: cid for cid, col in auto_map(columns).items()}
    out = []
    for col in columns:
        file_unit = raw.units.get(col, "")
        unit, unit_source = (file_unit, "file") if file_unit else ("unknown", "none")
        rule = profile.rule_for(col) if profile else None
        if rule and rule.unit and not file_unit:
            unit, unit_source = rule.unit, "profile"
        check_unit = "" if unit == "unknown" else unit
        if col in user:
            channel = user[col]
            problems = _validate(raw.data[col], channel, check_unit) if channel else []
            out.append(Decision(str(col), channel, "user" if channel else "unmapped", 1.0 if channel else 0.0, bool(channel),
                                unit, unit_source, problems, "chosen for this import" if channel else "un-mapped for this import"))
        elif col in hinted:
            channel = hinted[col]
            out.append(Decision(str(col), channel, "metadata", 0.95, False, unit, unit_source,
                                _validate(raw.data[col], channel, check_unit), f"{raw.format} identifies this channel"))
        elif rule is not None:
            problems = _validate(raw.data[col], rule.channel, check_unit, rule.valid_min, rule.valid_max)
            if rule.unit and file_unit and _unit_scale(rule.channel, rule.unit) != _unit_scale(rule.channel, file_unit):
                problems.append(f"the file says {file_unit}, the profile expects {rule.unit}; the file's unit is used")
            verified = rule.confirmed and not problems
            out.append(Decision(str(col), rule.channel, "profile" if verified else "plausibility",
                                rule.confidence if not problems else min(rule.confidence, 0.3), verified, unit, unit_source,
                                problems, f"profile '{profile.name}' rule '{rule.pattern}'"
                                + ("" if rule.confirmed else " (not yet confirmed by a person)")))
        elif col in by_name:
            channel = by_name[col]
            problems = _validate(raw.data[col], channel, check_unit)
            if problems:
                out.append(Decision(str(col), None, "unmapped", 0.0, False, unit, unit_source, problems,
                                    f"the name suggests {channel}, but the values do not fit it"))
            else:
                checked = _range_for(channel) is not None
                out.append(Decision(str(col), channel, "plausibility", 0.6 if checked and file_unit else 0.4 if checked else 0.3,
                                    False, unit, unit_source, [],
                                    "the name matches and the values fit" if checked else
                                    "the name matches; there is no value check for this channel"))
        else:
            out.append(Decision(str(col), None, "unmapped", 0.0, False, unit, unit_source, [], "no rule and no name match"))

    # one channel, one column: the better-founded decision keeps it
    rank = {"user": 0, "metadata": 1, "profile": 2, "plausibility": 3}
    best = {}
    for d in out:
        if d.channel:
            key = (rank[d.basis], bool(d.problems), -d.confidence)
            if d.channel not in best or key < best[d.channel][0]:
                best[d.channel] = (key, d)
    for d in out:
        if d.channel and best[d.channel][1] is not d:
            d.problems.append(f"conflict: {best[d.channel][1].column} is also {d.channel} and is better founded; "
                              "this column was left unmapped")
            d.reason, d.channel, d.basis, d.confidence, d.confirmed = d.reason + " (lost a conflict)", None, "unmapped", 0.0, False
    return out


MIN_PROFILE_MATCHES = 3        # a profile is offered for a file only if at least this many of its rules find a column
MIN_PROFILE_SHARE = 0.5        # ... and at least this share of its rules do


def best_profile(raw: RawTable, profiles) -> tuple[Profile | None, str]:
    """The profile that fits a file best, and a sentence saying why it was
    chosen (or why none was). `profiles` is a folder of profile JSON files or
    a list of Profile objects. A profile fits by how many of its rules find a
    column in the file; one whose logger or car is named in the file's own
    header wins a tie. A profile that barely matches is not offered."""
    if isinstance(profiles, (str, Path)):
        loaded = []
        for path in sorted(Path(profiles).glob("*.json")) if Path(profiles).is_dir() else []:
            try:
                loaded.append(Profile.load(path))
            except (ValueError, KeyError, TypeError, OSError):
                continue                        # an unreadable profile is skipped, not fatal
        profiles = loaded
    header = " ".join(str(v).lower() for v in raw.info.values())
    columns = [str(c) for c in raw.data.columns]
    best, best_key, best_hits = None, None, 0
    for p in profiles:
        if not p.rules:
            continue
        hits = sum(any(fnmatch.fnmatchcase(c.lower(), r.pattern.lower()) for c in columns) for r in p.rules)
        named = sum(bool(x) and x.lower() in header for x in (p.logger, p.car, p.series, p.team))
        key = (hits / len(p.rules) >= MIN_PROFILE_SHARE and hits >= MIN_PROFILE_MATCHES, named, hits, hits / len(p.rules))
        if best_key is None or key > best_key:
            best, best_key, best_hits = p, key, hits
    if best is None:
        return None, "No mapping profile is saved yet."
    if not best_key[0]:
        return None, (f"No saved profile fits this file: the closest, '{best.name}', matches {best_hits} of its "
                      f"{len(best.rules)} columns.")
    return best, (f"Profile '{best.name}' chosen automatically: {best_hits} of its {len(best.rules)} columns are in this file"
                  + (", and the file's header names its " + " and ".join(
                      n for n, x in (("logger", best.logger), ("car", best.car), ("series", best.series), ("team", best.team))
                      if x and x.lower() in header) if best_key[1] else "") + ".")


def to_import_args(decisions: list) -> tuple[dict, dict]:
    """(mapping, units) for build_session from a list of decisions. Units are
    passed only where a profile or the user supplied one the file lacks."""
    mapping = {d.channel: d.column for d in decisions if d.channel}
    units = {d.column: d.unit for d in decisions if d.channel and d.unit_source in ("profile", "user")}
    return mapping, units


def diagnostics(raw: RawTable, decisions: list) -> dict:
    """What an import dialog shows before the user commits."""
    mapped = [d for d in decisions if d.channel]
    return {"format": raw.format, "columns": len(decisions), "mapped": len(mapped),
            "by_basis": {b: sum(d.basis == b for d in decisions) for b in BASES},
            "confirmed": sum(d.confirmed for d in mapped),
            "needs_confirmation": [d.column for d in mapped if not d.confirmed],
            "unknown_units": [d.column for d in mapped if d.unit == "unknown" and CHANNELS[d.channel].quantity],
            "conflicts": [d.column for d in decisions if any(p.startswith("conflict") for p in d.problems)],
            "failed_validation": [d.column for d in decisions if d.problems and not any(p.startswith("conflict") for p in d.problems)],
            "missing_required": [c for c in ("time", "speed") if c not in {d.channel for d in mapped}]}


def profile_from_decisions(name: str, decisions: list, confirmed: bool = True, **scope) -> Profile:
    """A profile that reproduces these decisions on the next file from the
    same set-up. With `confirmed`, every rule is marked as checked by a person
    — call it that way only when someone has looked at the mapping."""
    rules = [Rule(d.column, d.channel, "" if d.unit == "unknown" else d.unit, confidence=0.9 if confirmed else 0.6,
                  confirmed=confirmed) for d in decisions if d.channel]
    return Profile(name, rules=rules, **{k: v for k, v in scope.items() if k in ("logger", "car", "series", "team")})
