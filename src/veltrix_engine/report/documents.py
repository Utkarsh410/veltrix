"""
Engineering documents to print or send: static pages (no scripts), laid out
for A4, built from the same analysis as the interactive report.

  debrief       the engineering report: every section, every finding with its
                evidence, calculation, confidence and recommendation
  driver-card   the coaching cards (analysis/coaching.py)
  comparison    session B against session A — a setup change, or one driver
                against another (analysis/ab.py)

A document is repeatable: built twice from the same files and the same spec,
the page is byte-for-byte the same (no clock time in it). A spec is a small
JSON file naming the template, the files and the options; `build(spec)` makes
the document from it, `save_spec` keeps it with the outputs.
"""

from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path

from .. import __version__

TEMPLATES = ("debrief", "driver-card", "comparison")
SPEC_VERSION = 1

_CSS = ("body{font:12.5px/1.45 -apple-system,Helvetica,Arial,sans-serif;color:#1b1a22;margin:24px;max-width:920px}"
        "h1{font-size:21px;margin:0 0 4px}h2{font-size:16px;margin:22px 0 6px;border-bottom:2px solid #1b1a22}"
        "h3{font-size:13.5px;margin:14px 0 4px}.sub{color:#555;margin:0 0 12px}.tag{display:inline-block;font-size:11px;"
        "padding:1px 6px;border-radius:3px;background:#eceaf2;margin-right:6px}.tag.estimate{background:#fbe9d0}"
        ".lvl{font-weight:600}.finding{break-inside:avoid;margin:0 0 10px}dl{margin:2px 0 0}dt{font-weight:600;"
        "color:#555;font-size:11.5px;margin-top:3px}dd{margin:0 0 0 0}ul{margin:2px 0 2px 18px;padding:0}"
        "table{border-collapse:collapse;width:100%;margin:6px 0}td,th{border-bottom:1px solid #e3e1e8;padding:3px 6px;"
        "text-align:left;vertical-align:top}th{color:#555}.foot{color:#777;font-size:11px;margin-top:24px}"
        "@page{size:A4;margin:14mm}")


def _page(title: str, body: str, fingerprint: str) -> str:
    e = html.escape
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{e(title)}</title><style>{_CSS}</style>"
            f"</head><body>{body}<p class='foot'>Veltrix Data {__version__} · built from {e(fingerprint)}</p></body></html>")


def _fingerprint(sessions) -> str:
    """What the document was built from: each source file's SHA-256 (short) — no clock time."""
    return ", ".join(f"{s.raw.source} ({s.raw.sha256[:12]})" for s in sessions)


def debrief_html(session, title: str | None = None) -> str:
    from ..analysis import engineering
    e = html.escape
    report = engineering.analyse(session)
    info = session.raw.info
    title = title or f"Debrief — {session.raw.source}"
    head = [f"<h1>{e(title)}</h1>",
            "<p class='sub'>" + " · ".join(e(str(x)) for x in (info.get("Venue", ""), info.get("Vehicle", ""),
                                                               info.get("Driver", ""), info.get("Log Date", "")) if x)
            + "</p>"]
    summ = report["summary"]
    if summ.get("best_lap_s"):
        head.append(f"<p>Best lap <b>{engineering.fmt_time(summ['best_lap_s'])}</b> (lap {summ['best_lap']}) from "
                    f"{summ['flying_laps']} flying laps. {summ['findings']} findings — "
                    + ", ".join(f"{n} {lv}" for lv, n in summ["confidence"].items()) + " confidence.</p>")
    if summ.get("priorities"):
        head.append("<p><b>Corners to work on first:</b> " + ", ".join(
            f"{e(p['corner'])} ({p['available_s']:.2f} s on the table)" for p in summ["priorities"]) + "</p>")
    body = []
    for sec in report["sections"]:
        body.append(f"<h2>{e(sec['title'])}</h2>")
        if sec["not_assessed"]:
            body.append(f"<p class='sub'>Not assessed: {e(sec['not_assessed'])}.</p>")
        for f in sec["findings"]:
            tags = (f"<span class='tag {e(f['nature'])}'>{'Estimate — built from several laps, not driven' if f['nature'] == 'estimate' else 'Measured'}</span>"
                    if f.get("nature") else "") + (f"<span class='tag'>{e(f['where'])}</span>" if f.get("where") else "")
            body.append(
                f"<div class='finding'><h3>{e(f['conclusion'])}</h3>{tags}<dl>"
                f"<dt>Evidence</dt><dd><ul>{''.join(f'<li>{e(x)}</li>' for x in f['evidence'])}</ul></dd>"
                f"<dt>Calculation</dt><dd>{e(f['calculation'])}</dd>"
                f"<dt>Confidence</dt><dd><span class='lvl'>{e(f['confidence']['level'])}</span> — {e(f['confidence']['basis'])}</dd>"
                + (f"<dt>Hypothesis</dt><dd>{e(f['hypothesis'])}</dd>" if f.get("hypothesis") else "")
                + f"<dt>{'Suggested investigation' if f.get('hypothesis') else 'Recommendation'}</dt><dd>{e(f['recommendation'])}</dd>"
                "</dl></div>")
    return _page(title, "".join(head + body), _fingerprint([session]))


def driver_card_html(session, title: str | None = None) -> str:
    from ..analysis import coaching
    title = title or f"Driver coaching — {session.raw.source}"
    page = coaching.to_html(coaching.cards(session), title)
    # the same foot as the other documents: what it was built from, no clock time
    return page.replace("</body></html>", f"<p class='ev'>Veltrix Data {__version__} · built from "
                                         f"{html.escape(_fingerprint([session]))}</p></body></html>")


def comparison_html(a, b, change: str = "", title: str | None = None) -> str:
    from ..analysis import ab
    e = html.escape
    r = ab.compare_sessions(a, b, change)
    title = title or f"{'Setup change' if change or r['changes'] else 'Comparison'} — {a.raw.source} against {b.raw.source}"
    body = [f"<h1>{e(title)}</h1>",
            f"<p class='sub'>A: {e(r['a']['file'])} ({r['a']['laps']} flying laps) · B: {e(r['b']['file'])} "
            f"({r['b']['laps']} flying laps)</p>"]
    if r["problems"]:
        body.append("<p><b>Not comparable as one change:</b> " + e("; ".join(r["problems"])) + ".</p>")
    changed = ([f"Stated: {r['stated_change']}"] if r["stated_change"] else []) + [f"{k}: {x} → {y}" for k, x, y in r["changes"]]
    body.append("<h2>What changed</h2><ul>" + "".join(f"<li>{e(x)}</li>" for x in changed or ["Nothing recorded in the files"])
                + "</ul>")
    if len(r["changes"]) > 1:
        body.append(f"<p class='sub'>{len(r['changes'])} settings differ: the result cannot be put down to any one of them.</p>")
    lap = r["lap"]
    body.append(f"<h2>Verdict</h2><p><b>{e(r['summary'])}</b></p><p class='sub'>Confidence in the lap verdict: "
                f"{e(lap['confidence'])}. A difference counts as real at {ab.REAL_RATIO:g} times its uncertainty (the two "
                f"sessions' lap-to-lap scatter combined), as probable from {ab.LIKELY_RATIO:g}; below that it is no "
                "detectable change, however big it looks.</p>")
    rows = "".join(
        f"<tr><td>{e(x.corner)}</td><td>{e(x.call)}</td><td>{x.delta_s:+.3f}</td><td>{x.se_s:.3f}</td>"
        f"<td>{x.d_min_speed:+.0f}</td><td>{x.d_brake_before_m:+.0f}</td><td>{x.d_exit_speed:+.0f}</td><td>{e(x.confidence)}</td></tr>"
        .replace("nan", "—") for x in r["corners"].itertuples()) if len(r["corners"]) else ""
    body.append("<h2>Corner by corner</h2><table><tr><th>Corner</th><th>Verdict</th><th>Δ time s</th><th>± s</th>"
                "<th>Δ min speed km/h</th><th>Δ braking point m (+ earlier)</th><th>Δ exit km/h</th><th>Confidence</th></tr>"
                + rows + "</table>")
    return _page(title, "".join(body), _fingerprint([a, b]))


# ── specs: the same document again ────────────────────────────────────────────

def make_spec(template: str, files: list, title: str = "", change: str = "", app_data: bool = True) -> dict:
    if template not in TEMPLATES:
        raise ValueError(f"A document is one of {', '.join(TEMPLATES)}, not '{template}'.")
    needs = 2 if template == "comparison" else 1
    if len(files) != needs:
        raise ValueError(f"A {template} is built from {needs} file{'s' if needs > 1 else ''}, not {len(files)}.")
    return {"version": SPEC_VERSION, "template": template, "files": [str(f) for f in files], "title": title,
            "change": change, "app_data": bool(app_data)}


def save_spec(spec: dict, path: str | Path) -> Path:
    from ..versions import refuse_overwrite
    path = Path(path)
    refuse_overwrite(path, "document spec", SPEC_VERSION)
    path.write_text(json.dumps(spec, indent=1), encoding="utf-8")
    return path


def load_spec(path: str | Path) -> dict:
    from ..versions import upgrade
    spec = upgrade("document spec", json.loads(Path(path).read_text(encoding="utf-8")), SPEC_VERSION)
    return make_spec(spec["template"], spec["files"], spec.get("title", ""), spec.get("change", ""), spec.get("app_data", True))


def build(spec: dict, load=None) -> str:
    """The document's HTML from a spec. `load(path)` opens a session (default:
    load_session, with the app's car library when spec["app_data"])."""
    if load is None:
        from .. import load_session
        cars = None
        if spec.get("app_data", True):
            from ..cars import CarLibrary
            from ..user_data import data_dir
            cars = CarLibrary(Path(data_dir()) / "cars")
        load = lambda p: load_session(p, cars=cars)
    sessions = [load(f) for f in spec["files"]]
    title = spec.get("title") or None
    if spec["template"] == "debrief":
        return debrief_html(sessions[0], title)
    if spec["template"] == "driver-card":
        return driver_card_html(sessions[0], title)
    return comparison_html(sessions[0], sessions[1], spec.get("change", ""), title)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
