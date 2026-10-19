"""
Events and annotations for the app's panels. The work — detecting events,
reviews, notes on disk — is in `veltrix_engine.user_data`; this module adds how
an event reads in a panel row, and keeps the old import paths working.
"""

from __future__ import annotations

from veltrix_engine import events as rule_engine
from veltrix_engine.user_data import (RULE_VERSION, AnnotationStore, annotate, apply_reviews,  # noqa: F401
                                      detect_events, steering_limits)


def describe(event) -> dict:
    """An event as the row a panel shows."""
    ev = event.evidence
    peak = ev.get("peak")
    detail = ev.get("cause") or (f"peak {peak['value']:.3g} of {peak['of']}" if peak else "")
    status = getattr(event, "status", "candidate")
    review = getattr(event, "review", {}) or {}
    if status == "recorded":
        what = f"{event.kind[0].upper() + event.kind[1:]} — recorded by the simulator"
        points = ev.get("points", 0)
        detail = f"{points:g}x incident points" if points else "flagged, no incident points"
    else:
        what = event.kind if event.kind.lower().startswith(("possible", "braking anomaly")) else f"Possible {event.kind}"
        what = what[0].upper() + what[1:] + (
            " — rejected on review" if status == "rejected" else
            " — confirmed on review" if status == "confirmed" and review.get("verdict") == "confirmed" else
            " — confirmed by the simulator's record" if status == "confirmed" else " — requires review")
    return {"lap": event.lap, "what": what, "status": status, "severity": event.severity,
            "confidence": event.confidence, "limits": rule_engine.limits_text(getattr(event, "thresholds", {})),
            "review": review,
            "where": f"{ev.get('corner', '')} {ev.get('phase', '')}".strip(), "position_m": event.position_m,
            "time": float(peak["at_s"]) if peak else 0.5 * (event.t_start + event.t_end), "detail": detail,
            "label": event.label, "rule": event.rule}
