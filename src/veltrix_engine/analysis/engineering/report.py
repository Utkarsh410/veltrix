"""The report: every section in order, its summary, and the text and Markdown forms."""

from __future__ import annotations

import json
import re

import numpy as np
import pandas as pd

from ... import circuits
from ... import events as event_rules
from ... import corners as corner_engine
from ... import laps as lap_engine
from ...derived import best_lat
from ...schema import CORNERS as WHEELS
from ...schema import tyre_temp_channel
from .. import advice, balance, handling, plausibility, tyres

from .base import (SIM_INCIDENT, BEST_VS_TYPICAL, BRAKE_M, BRAKE_SCATTER_M, COAST_FLAG_S, COAST_MIN_KMH, COAST_THROTTLE, CONSISTENT_S, DECEL_GAP_G, DECEL_SHARE, DEG_R2, ESTIMATE, HEAVY_BRAKE_KMH, LAPS_HIGH, LAPS_MEDIUM, LAT_SHARE, LEVELS, LOOSE_S, MEASURED, MIN_GAIN_S, NATURE_TEXT, OVERLAP_FLAG_S, PHASE_DEG, PRESSURE_SPLIT_BAR, RATIO_HIGH, RATIO_MEDIUM, SECTIONS, SPEED_KMH, THROTTLE_M, WHEELSPIN_KMH, WHEELSPIN_SHARE, ZONES_CALC, _Ctx, _finding, _join, fmt_time)  # noqa: F401
from .potential_driver import (_driver, _lap_potential)  # noqa: F401
from .braking_cornering import (_braking, _cornering)  # noqa: F401
from .traction_tyres import (_traction, _tyres)  # noqa: F401
from .vehicle_balance import (_balance)  # noqa: F401
from .incidents import (_corner_check, _incidents, _peak_check)  # noqa: F401
from .setup import (_SETUP_CALC, _setup)  # noqa: F401


# ── the report ────────────────────────────────────────────────────────────────

def _plain(value):
    return json.loads(json.dumps(value, default=lambda o: o.item() if hasattr(o, "item") else str(o)))


def analyse(session, corner_result: dict | None = None, handling_result: dict | None = None) -> dict:
    """The engineering report for a session, as plain JSON-ready values.

    Returns {"sections": [{"id", "title", "findings": [...], "not_assessed"}],
    "summary": {...}}. All nine sections are always there, in order (`note`
    says what part of a section the file could not support). A
    finding has: conclusion, where, evidence (list), calculation, confidence
    ({"level", "basis"}), recommendation, gain_s (seconds a lap this finding
    alone is worth, or None) and at_stake_s (time available in the zones it
    concerns, shared with other findings about them, or None).
    `not_assessed` is None, or the reason a section has no findings."""
    c = _Ctx(session, corner_result, handling_result)
    need_corners = "laps cannot be compared corner by corner: " + (c.corner_reason or "")
    sections = []
    for sid, title in SECTIONS:
        findings, reason = [], None
        try:
            if sid == "tyres":
                findings, reason = _tyres(c)
            elif sid == "incidents":
                findings = _incidents(c)
            elif c.metrics is None:
                reason = need_corners
            elif sid == "vehicle_balance":
                findings, reason = _balance(c)
            elif sid == "lap_potential":
                findings = _lap_potential(c)
            elif sid == "driver_performance":
                findings = _driver(c)
            elif sid == "braking":
                findings = _braking(c)
                if not findings:
                    reason = "no braking zone was found: the file has no brake channel and no usable deceleration"
            elif sid == "cornering":
                findings = _cornering(c)
            elif sid == "traction":
                if "throttle" not in c.ch:
                    reason = "it needs a throttle channel"
                else:
                    findings = _traction(c)
            elif sid == "setup_hypotheses":
                findings, reason = _setup(c)
        except (ValueError, KeyError, IndexError) as e:
            findings, reason = [], f"it could not be computed for this file ({type(e).__name__}: {e})"
        if not findings and reason is None:
            reason = "the session has too few flying laps to compare"
        for f in findings:
            # the same chain under the names the reports use: evidence → interpretation → hypothesis →
            # suggested investigation. A hypothesis is only ever stated in the setup section.
            f.setdefault("hypothesis", None)
            f["interpretation"], f["investigation"] = f["conclusion"], f["recommendation"]
            # what kind of statement each finding is (model.LABELS)
            f["label"] = ("hypothesis/recommendation" if sid == "setup_hypotheses" else
                          "recorded event" if sid == "incidents" and "simulator recorded" in f["conclusion"] else
                          "detected event (candidate)" if sid == "incidents" and "candidate" in f["conclusion"] else
                          "detected event" if sid == "incidents" and "incident" in f["conclusion"] else "interpretation")
        sections.append({"id": sid, "title": title, "findings": findings,
                         "not_assessed": reason if not findings else None,      # nothing could be said
                         "note": reason if findings else None})                 # part of it could not

    every = [(sec["title"], f) for sec in sections for f in sec["findings"]]
    priorities = []
    if c.summary is not None:
        for r in c.summary.sort_values("available_s", ascending=False).head(3).itertuples():
            if r.available_s < MIN_GAIN_S:
                continue
            said = [f"{t}: {f['conclusion']}" for t, f in every
                    if t not in ("Lap potential",) and re.search(rf"\b{r.corner}\b", f["where"])]
            priorities.append({"corner": r.corner, "kind": r.kind, "available_s": round(float(r.available_s), 3),
                               "findings": said})
    levels = {lv: sum(f["confidence"]["level"] == lv for _, f in every) for lv in LEVELS}
    summary = {"flying_laps": c.n, "best_lap": c.best_lap, "best_lap_s": c.best_time if c.n else None,
               "ideal_lap_s": getattr(c, "ideal", None), "findings": len(every), "confidence": levels,
               "assessed": [s["title"] for s in sections if s["findings"]],
               "not_assessed": [s["title"] for s in sections if not s["findings"]],
               "gain_s": (round(c.best_time - c.ideal, 3) if c.summary is not None and np.isfinite(c.best_time - c.ideal)
                          else None),
               "gain_basis": "estimate: best lap minus the ideal lap by corner, a composite of several laps",
               "priorities": priorities}
    out = _plain({"sections": sections, "summary": summary})
    for key in ("best_lap_s", "ideal_lap_s"):
        v = out["summary"][key]
        out["summary"][key] = v if isinstance(v, (int, float)) and np.isfinite(v) else None
    return out


def unavailable(reason: str) -> dict:
    """A report with all nine sections present and none assessed, for a
    session the analysis could not run on at all."""
    sections = [{"id": sid, "title": title, "findings": [], "not_assessed": reason, "note": None}
                for sid, title in SECTIONS]
    return {"sections": sections,
            "summary": {"flying_laps": 0, "best_lap": None, "best_lap_s": None, "ideal_lap_s": None, "findings": 0,
                        "confidence": {lv: 0 for lv in LEVELS}, "assessed": [],
                        "not_assessed": [t for _, t in SECTIONS], "gain_s": None, "priorities": []}}


def _worth(f: dict, before: str, after: str) -> str:
    if f["gain_s"]:
        return f"{before}{f['gain_s']:.2f} s a lap{after}"
    if f["at_stake_s"]:
        return f"{before}the zones concerned hold {f['at_stake_s']:.2f} s a lap{after}"
    return ""


def to_text(report: dict) -> str:
    """The report as plain text, each finding as its chain of evidence."""
    lines = []
    for sec in report["sections"]:
        lines += ["", sec["title"].upper(), "─" * len(sec["title"])]
        if sec["not_assessed"]:
            lines.append(f"  Not assessed: {sec['not_assessed']}.")
        if sec["note"]:
            lines.append(f"  Partly assessed: {sec['note']}.")
        for n, f in enumerate(sec["findings"], 1):
            lines.append(f"  {n}. {f['conclusion']}{_worth(f, '  (', ')')}")
            if f.get("nature"):
                lines.append(f"     {NATURE_TEXT[f['nature']]}")
            lines += [f"     Evidence        {e}" if i == 0 else f"                     {e}" for i, e in enumerate(f["evidence"])]
            lines.append(f"     Calculation     {f['calculation']}")
            lines.append(f"     Confidence      {f['confidence']['level']} — {f['confidence']['basis']}")
            if f.get("hypothesis"):
                lines.append(f"     Hypothesis      {f['hypothesis']}")
            lines.append(f"     Recommendation  {f['recommendation']}")
    return "\n".join(lines).lstrip("\n")


def to_markdown(report: dict, title: str = "Performance engineering report") -> str:
    lines = [f"# {title}", ""]
    for sec in report["sections"]:
        lines += [f"## {sec['title']}", ""]
        if sec["not_assessed"] or sec["note"]:
            lines += [f"*{'Not' if sec['not_assessed'] else 'Partly'} assessed: {sec['not_assessed'] or sec['note']}.*", ""]
        for f in sec["findings"]:
            lines += [f"### {f['conclusion']}{_worth(f, ' — ', '')}", ""]
            if f.get("nature"):
                lines += [f"*{NATURE_TEXT[f['nature']]}*", ""]
            lines += ["**Evidence**", ""]
            lines += [f"- {e}" for e in f["evidence"]]
            lines += ["", f"**Calculation.** {f['calculation']}", "",
                      f"**Confidence: {f['confidence']['level']}.** {f['confidence']['basis'][0].upper() + f['confidence']['basis'][1:]}.", "",
                      *([f"**Hypothesis.** {f['hypothesis']}", ""] if f.get("hypothesis") else []),
                      f"**Recommendation.** {f['recommendation']}", ""]
    return "\n".join(lines)
