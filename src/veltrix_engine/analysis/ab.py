"""
Two sessions, one change: did it help?

`compare_sessions(a, b)` sets session B against session A — normally the same
car on the same circuit with one setup change between them — and says, for the
lap and for every corner, whether B is faster, slower, or not detectably
different, using each session's own lap-to-lap scatter to decide.

The rule: the difference of the mean times over the flying laps, set against
its standard error (the two sessions' scatters combined). At least
REAL_RATIO standard errors is a real difference; LIKELY_RATIO to REAL_RATIO is
"probably"; below that it is "no detectable change" — however big it looks.
Confidence follows the same laps and effect-against-scatter rule as the
engineering report.

What changed is read from the files where they record it (an iRacing setup
sheet); values that are results of running (last hot pressures, tread left)
are not settings and are left out. A change can also be stated.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .. import corners as corner_engine

REAL_RATIO = 2.0              # a difference at least this many standard errors is real
LIKELY_RATIO = 1.0            # ... this many, probably real
SAME_TRACK = 0.02             # lap lengths this close are the same circuit
MIN_LAPS = 3                  # flying laps each session needs for a verdict
_NOT_SETTINGS = ("LastHotPressure", "LastTemps", "TreadRemaining", "UpdateCount")


def setup_changes(a: dict, b: dict) -> list:
    """[(item, value in A, value in B)] for every setting that differs."""
    keys = sorted(set(a) | set(b))
    return [(k, a.get(k, "—"), b.get(k, "—")) for k in keys
            if not any(x in k for x in _NOT_SETTINGS) and a.get(k) != b.get(k)]


def _flying(session) -> pd.DataFrame:
    return session.laps[session.laps["status"] == "flying"]


def verdict(times_a, times_b) -> dict:
    """B against A on a set of times (s, lower is better)."""
    a, b = np.asarray(times_a, float), np.asarray(times_b, float)
    a, b = a[np.isfinite(a)], b[np.isfinite(b)]
    out = {"n_a": int(len(a)), "n_b": int(len(b)), "mean_a": float(a.mean()) if len(a) else np.nan,
           "mean_b": float(b.mean()) if len(b) else np.nan}
    out["delta_s"] = out["mean_b"] - out["mean_a"]
    if len(a) < MIN_LAPS or len(b) < MIN_LAPS:
        out.update(se_s=np.nan, ratio=np.nan, call="not enough laps",
                   words=f"too few flying laps to judge ({len(a)} and {len(b)}; {MIN_LAPS} each needed)")
        return out
    se = float(np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b)))
    ratio = abs(out["delta_s"]) / se if se > 0 else np.inf
    faster = out["delta_s"] < 0
    call = ("faster" if faster else "slower") if ratio >= REAL_RATIO else \
           ("probably faster" if faster else "probably slower") if ratio >= LIKELY_RATIO else "no detectable change"
    out.update(se_s=se, ratio=float(ratio), call=call,
               words=(f"{abs(out['delta_s']):.3f} s {'quicker' if faster else 'slower'}, "
                      f"{ratio:.1f} times its uncertainty (±{se:.3f} s)"))
    return out


def _confidence(v: dict) -> str:
    n = min(v["n_a"], v["n_b"])
    if not np.isfinite(v.get("ratio", np.nan)):
        return "low"
    laps = 2 if n >= 8 else 1 if n >= 5 else 0
    effect = 2 if v["ratio"] >= REAL_RATIO else 1 if v["ratio"] >= LIKELY_RATIO else 0
    return ("low", "medium", "high")[min(laps, effect) if v["call"] != "no detectable change" else min(laps, 1)]


def compare_sessions(a, b, change: str = "") -> dict:
    """Session B against session A. Returns {comparable, problems, changes,
    stated_change, lap, corners, summary}. `corners` has one row per corner
    of A: the zone-time verdict and how minimum speed, braking point and exit
    speed moved."""
    problems = []
    va, vb = str(a.raw.info.get("Venue", "")), str(b.raw.info.get("Venue", ""))
    ca, cb = str(a.raw.info.get("Vehicle", "")), str(b.raw.info.get("Vehicle", ""))
    if va and vb and va != vb:
        problems.append(f"different circuits ({va} / {vb})")
    if ca and cb and ca != cb:
        problems.append(f"different cars ({ca} / {cb})")
    if a.lap_length and b.lap_length and abs(a.lap_length / b.lap_length - 1) > SAME_TRACK:
        problems.append(f"lap lengths {a.lap_length:.0f} m and {b.lap_length:.0f} m — another layout or start line")
    changes = setup_changes(getattr(a.raw, "setup", {}) or {}, getattr(b.raw, "setup", {}) or {})
    lap = verdict(_flying(a)["lap_time_s"], _flying(b)["lap_time_s"])
    lap["confidence"] = _confidence(lap)

    rows = []
    try:
        corners = corner_engine.find_corners(a)
        ma = corner_engine.corner_metrics(a, corners)
        mb = corner_engine.corner_metrics(b, corners)          # B measured on A's corners: the same zones
    except (ValueError, KeyError) as e:
        problems.append(f"corners could not be compared: {e}")
        ma = mb = None
    if ma is not None:
        fa, fb = set(_flying(a)["lap"]), set(_flying(b)["lap"])
        ma, mb = ma[ma["lap"].isin(fa)], mb[mb["lap"].isin(fb)]
        for c in corners.itertuples():
            pa, pb = ma[ma["corner"] == c.corner], mb[mb["corner"] == c.corner]
            v = verdict(pa["zone_time_s"], pb["zone_time_s"])
            v["confidence"] = _confidence(v)
            med = lambda part, col: float(part[col].median()) if col in part and part[col].notna().any() else np.nan
            rows.append({"corner": c.corner, "name": c.name, "kind": c.kind, **v,
                         "d_min_speed": med(pb, "min_speed") - med(pa, "min_speed"),
                         "d_brake_before_m": med(pb, "brake_before_m") - med(pa, "brake_before_m"),
                         "d_exit_speed": med(pb, "exit_speed") - med(pa, "exit_speed")})
    table = pd.DataFrame(rows)
    real = table[table["call"].isin(("faster", "slower"))] if len(table) else table
    summary = (f"Lap: {lap['call']}" + (f" — {lap['words']}" if lap["call"] != "not enough laps" else f" ({lap['words']})")
               + (f". Corners that moved for real: " + ", ".join(f"{r.corner} {r.call} ({r.delta_s:+.3f} s)"
                                                              for r in real.itertuples()) if len(real) else
                  ". No corner moved by more than its own scatter."))
    return {"comparable": not problems, "problems": problems, "changes": changes, "stated_change": change.strip(),
            "lap": lap, "corners": table, "summary": summary,
            "a": {"file": a.raw.source, "laps": int(len(_flying(a)))}, "b": {"file": b.raw.source, "laps": int(len(_flying(b)))}}


def to_markdown(result: dict, title: str = "Setup change: session against session") -> str:
    a, b, lap = result["a"], result["b"], result["lap"]
    out = [f"# {title}", "", f"**A** {a['file']} ({a['laps']} flying laps) · **B** {b['file']} ({b['laps']} flying laps)", ""]
    if result["problems"]:
        out += ["**Not comparable as one change:** " + "; ".join(result["problems"]) + ".", ""]
    out += ["## What changed", ""]
    if result["stated_change"]:
        out.append(f"- Stated: {result['stated_change']}")
    out += [f"- {k}: {x} → {y}" for k, x, y in result["changes"]] or (["- Nothing recorded in the files"]
                                                                       if not result["stated_change"] else [])
    if len(result["changes"]) > 1:
        out.append(f"- {len(result['changes'])} settings differ: the result cannot be put down to any one of them.")
    out += ["", "## Verdict", "", f"**{result['summary']}**", "",
            f"Confidence in the lap verdict: {lap['confidence']}. A difference counts as real at {REAL_RATIO:g} times "
            "its uncertainty (the two sessions' lap-to-lap scatter combined), as probable from "
            f"{LIKELY_RATIO:g} times; below that it is no detectable change, however big it looks.", "",
            "## Corner by corner", "", "| Corner | Verdict | Δ time (s) | ± (s) | Δ min speed (km/h) | "
            "Δ braking point (m, + earlier) | Δ exit speed (km/h) | Confidence |", "|---|---|---|---|---|---|---|---|"]
    fmt = lambda x, d=0: "—" if not np.isfinite(x) else f"{x:+.{d}f}"
    for r in result["corners"].itertuples():
        out.append(f"| {r.corner} | {r.call} | {fmt(r.delta_s, 3)} | {r.se_s:.3f} | {fmt(r.d_min_speed)} | "
                   f"{fmt(r.d_brake_before_m)} | {fmt(r.d_exit_speed)} | {r.confidence} |"
                   if np.isfinite(r.se_s) else f"| {r.corner} | {r.call} | {fmt(r.delta_s, 3)} | — | — | — | — | low |")
    return "\n".join(out) + "\n"
