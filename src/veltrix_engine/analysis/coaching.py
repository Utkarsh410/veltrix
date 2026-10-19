"""
Coaching cards: one per corner where the driver has already shown they can be
quicker — what their fastest pass did differently from their usual one, the
reference to use on track (metres before turn-in, after the apex: fixed points
a driver can find), what to try, and the evidence.

The fastest pass never has a recorded incident in it (a corner cut is not a
reference to copy). A card is only made where the usual pass gives away at
least MIN_CARD_S; the differences named use the same limits as the driver tips
(advice.BRAKE_M, SPEED_KMH, THROTTLE_M), so the two never disagree.
"""

from __future__ import annotations

import html

import numpy as np

from .. import corners as corner_engine
from . import advice
from .engineering import _Ctx, fmt_time

MIN_CARD_S = 0.05             # a corner needs this much between the usual and the fastest pass for a card
MAX_CARDS = 6


def _txt(value, words) -> str:
    return words(value) if np.isfinite(value) else "—"


def card(c: _Ctx, corner: str) -> dict | None:
    """One corner's card, or None when there is too little in it."""
    row = c.summary.set_index("corner").loc[corner]
    best, typical, part = c.best_and_typical(corner)
    gap = float(typical["zone_time_s"] - best["zone_time_s"])
    if gap < MIN_CARD_S or len(part) < 3:
        return None
    info = c.corners.set_index("corner").loc[corner]
    lap = int(best["lap"])
    before = lambda v: corner_engine.before_corner(v)
    rel_turn = lambda v: corner_engine.relative(v, "turn-in")
    rel_apex = lambda v: corner_engine.relative(v, "the apex")
    flat = lambda r: np.isfinite(r["min_throttle"]) and r["min_throttle"] >= corner_engine.THROTTLE_FULL
    braked = lambda r: np.isfinite(r["brake_before_m"])
    refs = {
        "Braking point": (_txt(typical["brake_before_m"], before) if braked(typical) else "no braking",
                          _txt(best["brake_before_m"], before) if braked(best) else "no braking"),
        "Brake release": (_txt(typical["release_after_turn_in_m"], rel_turn), _txt(best["release_after_turn_in_m"], rel_turn)),
        "Minimum speed": (_txt(typical["min_speed"], lambda v: f"{v:.0f} km/h"), _txt(best["min_speed"], lambda v: f"{v:.0f} km/h")),
        "Throttle on": ("flat out" if flat(typical) else _txt(typical["throttle_after_apex_m"], rel_apex),
                        "flat out" if flat(best) else _txt(best["throttle_after_apex_m"], rel_apex)),
        "Exit speed": (_txt(typical["exit_speed"], lambda v: f"{v:.0f} km/h"), _txt(best["exit_speed"], lambda v: f"{v:.0f} km/h")),
    }
    tries = []
    d_brake = typical["brake_before_m"] - best["brake_before_m"]          # +: the fastest pass braked later
    d_min = best["min_speed"] - typical["min_speed"]
    if braked(typical) and not braked(best) and len(part["brake_before_m"].dropna()) >= len(part) / 2:
        tries.append(f"Try it without braking: on lap {lap} you lifted instead of braking here — usually you brake "
                     f"{before(typical['brake_before_m'])}.")
    elif np.isfinite(d_brake) and d_brake >= advice.BRAKE_M:
        tries.append(f"Brake later: at {before(best['brake_before_m'])} — you usually brake at "
                     f"{before(typical['brake_before_m'])}; on lap {lap} you braked {d_brake:.0f} m later.")
    elif np.isfinite(d_brake) and d_brake <= -advice.BRAKE_M and np.isfinite(d_min) and d_min >= advice.SPEED_KMH:
        tries.append(f"Brake a little earlier, at {before(best['brake_before_m'])}, and carry {d_min:.0f} km/h more to "
                     f"the apex — that is what lap {lap} did.")
        d_min = 0.0
    d_rel = typical["release_after_turn_in_m"] - best["release_after_turn_in_m"]
    if np.isfinite(d_rel) and d_rel >= advice.BRAKE_M:
        tries.append(f"Come off the brake sooner: by {rel_turn(best['release_after_turn_in_m'])} rather than "
                     f"{rel_turn(typical['release_after_turn_in_m'])}.")
    if np.isfinite(d_min) and d_min >= advice.SPEED_KMH:
        tries.append(f"Carry {d_min:.0f} km/h more through the slowest point: {best['min_speed']:.0f} km/h, as on lap {lap}.")
    d_thr = typical["throttle_after_apex_m"] - best["throttle_after_apex_m"]
    if not flat(best) and np.isfinite(d_thr) and d_thr >= advice.THROTTLE_M:
        tries.append(f"Back on the throttle earlier: {rel_apex(best['throttle_after_apex_m'])} — usually "
                     f"{rel_apex(typical['throttle_after_apex_m'])}.")
    if not tries:
        tries.append("No single input explains the gap: the time is spread through the corner. Watch lap "
                     f"{lap}'s line here on the video or map.")
    conf = c.conf(effect=gap, scatter=float(row["spread_s"]))
    return {"corner": corner, "name": info["name"] if isinstance(info["name"], str) else "",
            "basis": info.get("basis", "inferred"), "kind": info["kind"], "direction": info["direction"],
            "gap_s": round(gap, 3), "best_lap": lap, "best_s": float(best["zone_time_s"]),
            "typical_s": float(typical["zone_time_s"]), "laps": int(len(part)), "references": refs, "tries": tries,
            "evidence": [f"Fastest pass {best['zone_time_s']:.2f} s on lap {lap}, usual {typical['zone_time_s']:.2f} s "
                         f"(median of {len(part)} flying laps), spread ±{row['spread_s']:.2f} s"],
            "confidence": conf}


def cards(session, corner_result: dict | None = None) -> dict:
    """The session's coaching cards, most time first, with a session summary."""
    c = _Ctx(session, corner_result, None)
    if c.summary is None or not len(c.summary):
        return {"summary": {"reason": c.corner_reason or "no corners"}, "cards": []}
    made = sorted((x for x in (card(c, k) for k in c.summary["corner"]) if x), key=lambda x: -x["gap_s"])
    times = session.laps.loc[session.laps["status"] == "flying", "lap_time_s"].to_numpy(float)
    basis = "official turns" if c.corners is not None and (c.corners.get("basis") == "circuit file").all() else \
            "corner numbers found in the data, not the circuit's own"
    return {"summary": {"best_lap": c.best_lap, "best_time": fmt_time(c.best_time), "flying_laps": c.n,
                        "spread_s": float(np.std(times)) if len(times) > 1 else None, "corners": basis,
                        "on_the_cards_s": round(sum(x["gap_s"] for x in made[:MAX_CARDS]), 2)},
            "cards": made[:MAX_CARDS]}


def to_markdown(result: dict, title: str = "Driver coaching") -> str:
    s = result["summary"]
    out = [f"# {title}", ""]
    if "reason" in s:
        return "\n".join(out + [f"No cards: {s['reason']}.", ""])
    out += [f"Best lap {s['best_time']} (lap {s['best_lap']}), {s['flying_laps']} flying laps"
            + (f", lap times ±{s['spread_s']:.2f} s" if s["spread_s"] is not None else "")
            + f". Corners: {s['corners']}. The usual pass gives away {s['on_the_cards_s']:.2f} s a lap in the corners "
            "below, against your own fastest pass in each — no single lap did them all.", ""]
    for k in result["cards"]:
        out += [f"## {k['corner']}{' ' + k['name'] if k['name'] else ''} — {k['gap_s']:.2f} s on the table", "",
                "| | Usually | Your fastest (lap %d) |" % k["best_lap"], "|---|---|---|"]
        out += [f"| {name} | {usual} | {best} |" for name, (usual, best) in k["references"].items()]
        out += ["", "**Try:**", ""] + [f"- {t}" for t in k["tries"]]
        out += ["", f"*{k['evidence'][0]}. Confidence {k['confidence']['level']}: {k['confidence']['basis']}.*", ""]
    return "\n".join(out)


def to_html(result: dict, title: str = "Driver coaching") -> str:
    """The cards as one self-contained page, laid out to print on A4."""
    e = html.escape
    s = result["summary"]
    head = (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{e(title)}</title><style>"
            "body{font:13px/1.4 -apple-system,Helvetica,Arial,sans-serif;color:#1b1a22;margin:24px;max-width:900px}"
            "h1{font-size:20px;margin:0 0 4px}.sub{color:#555;margin:0 0 16px}"
            ".card{border:1px solid #c9c7d1;border-radius:6px;padding:10px 12px;margin:0 0 12px;break-inside:avoid}"
            ".card h2{font-size:15px;margin:0 0 6px}.gap{float:right;font-weight:600}"
            "table{border-collapse:collapse;width:100%;margin:4px 0 8px}td,th{border-bottom:1px solid #e3e1e8;"
            "padding:3px 6px;text-align:left}th{color:#555;font-weight:600}ul{margin:4px 0 6px 18px;padding:0}"
            ".ev{color:#666;font-size:12px}@page{size:A4;margin:14mm}</style></head><body>")
    if "reason" in s:
        return head + f"<h1>{e(title)}</h1><p>No cards: {e(s['reason'])}.</p></body></html>"
    body = [f"<h1>{e(title)}</h1><p class='sub'>Best lap {e(s['best_time'])} (lap {s['best_lap']}), "
            f"{s['flying_laps']} flying laps. Corners: {e(s['corners'])}. Each card sets your usual pass against your "
            "own fastest one in that corner — no single lap did them all.</p>"]
    for k in result["cards"]:
        rows = "".join(f"<tr><td>{e(n)}</td><td>{e(u)}</td><td>{e(b)}</td></tr>" for n, (u, b) in k["references"].items())
        body.append(f"<div class='card'><h2>{e(k['corner'])} {e(k['name'])}<span class='gap'>{k['gap_s']:.2f} s</span></h2>"
                    f"<table><tr><th></th><th>Usually</th><th>Your fastest (lap {k['best_lap']})</th></tr>{rows}</table>"
                    "<b>Try</b><ul>" + "".join(f"<li>{e(t)}</li>" for t in k["tries"]) + "</ul>"
                    f"<div class='ev'>{e(k['evidence'][0])}. Confidence {e(k['confidence']['level'])}: "
                    f"{e(k['confidence']['basis'])}.</div></div>")
    return head + "".join(body) + "</body></html>"
