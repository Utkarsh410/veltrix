"""Section 9: setup hypotheses."""

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


# ── 9. setup hypotheses ───────────────────────────────────────────────────────

_SETUP_CALC = (
    ("Rear traction on corner exit",
     f"Oversteer incidents while on throttle, rated moderate or severe, are counted across the session. At least "
     f"{advice.PATTERN_MIN_EVENTS} of them in two or more corners is treated as the car, not one corner.",
     "fewer power-oversteer incidents and an earlier throttle pick-up in the same corners"),
    ("Rear stability on corner entry",
     f"Oversteer incidents under braking or on a lift, rated moderate or severe; {advice.PATTERN_MIN_EVENTS} or more is a pattern.",
     "fewer entry-oversteer incidents and steadier braking points in the same corners"),
    ("Front grip at corner",
     f"Understeer incidents rated moderate or severe; {advice.PATTERN_MIN_EVENTS} or more in two or more corners is a pattern. "
     "The phase is the one most of them fall in.",
     "fewer understeer incidents and a higher minimum speed in the same corners"),
    ("Oversteer is the car's norm",
     "The car's habitual steering residual at corner exit (see Vehicle balance) reads oversteer in two or more corners.",
     "the exit residual in those corners moving toward zero"),
    ("Balance moves toward",
     "The understeer gradient is fitted per stint with the steering ratio held fixed; a one-way change of 15% or more is raised.",
     "a smaller change in the gradient from the first stint to the last"),
    ("camber", f"Mean inner-edge minus outer-edge temperature on flying laps, against a target of "
               f"{advice.CAMBER_SPREAD_C[0]:.0f}–{advice.CAMBER_SPREAD_C[1]:.0f} °C.",
     "the spread on that tyre moving into the target range"),
    ("tyres running hotter", f"Mean tyre temperature per axle on flying laps; {advice.TEMP_SPLIT_C:.0f} °C or more apart is raised.",
     "the front and rear means closing up"),
    ("brake temperatures", f"Peak of each brake disc temperature channel; {advice.BRAKE_TEMP_WATCH_C:.0f} °C or more is raised.",
     "a lower peak on that axle at the same braking performance"),
    ("ABS working", "Share of samples with the brake on in which the ABS channel is active; 30% or more is raised.",
     "a lower ABS share with the same or shorter stopping distances"),
)


def _setup(c: _Ctx) -> tuple[list, str | None]:
    out = []
    for item in advice._setup_items(c.session, c.corners, c.handling):
        calc, confirm = next(((a, b) for key, a, b in _SETUP_CALC if key.lower() in item["title"].lower()),
                             ("Pattern across corners in this session's measurements.", "the same measure improving"))
        conf = c.conf(uses=("acc_lat",),
                      caps=[(item["confidence"], "how often the pattern appears"),
                            ("medium", "the directions are standard rules of thumb; the engine does not know this car's "
                                       "current setup, adjustment range or regulations")])
        if conf["level"] == "low":
            # no setup-change advice on thin evidence: say what would have to be seen first
            advice_text = (f"The evidence is too thin to suggest a setup change. Investigate first: run at least "
                           f"{LAPS_MEDIUM} flying laps on one setup and see whether the pattern repeats in the same "
                           f"corners; check it against the driver's comments and, if there is one, the video. A change "
                           f"would be worth testing once it does, and would be judged by {confirm}.")
        else:
            advice_text = (item["action"] + f" Change one thing, run the same number of laps, and call it confirmed "
                           f"by {confirm}.")
        finding = _finding(
            f"Hypothesis: {item['title'][0].lower() + item['title'][1:]} is limiting the car", item["evidence"], calc,
            conf, advice_text, item["where"])
        finding["hypothesis"] = f"{item['title']} is what limits the car in {item['where']}."
        out.append(finding)
    if not out:
        evidence = []
        if c.handling is not None:
            ev = c.handling["events"]
            evidence.append(f"{int((ev['severity'] != 'mild').sum()) if len(ev) else 0} balance incidents rated moderate or "
                            f"severe (a pattern needs {advice.PATTERN_MIN_EVENTS} across two corners)")
            reads = c.handling["tendency"]["read"].value_counts()
            evidence.append("Habitual balance by corner: " + ", ".join(f"{n} {k}" for k, n in reads.items() if k != "—"))
        else:
            evidence.append("Balance could not be read: " + (c.handling_reason or "missing channels"))
        have = [n for n, ok in (("tyre temperatures", any(tyre_temp_channel(c.ch.columns, w) for w in WHEELS)),
                                ("brake temperatures", all(f"brake_temp_{w}" in c.ch for w in WHEELS)),
                                ("ABS", "abs_active" in c.ch), ("traction control", "tc_active" in c.ch)) if ok]
        if c.handling is None and not have:
            return [], ("the file has nothing a setup pattern can be read from: balance needs steering and a usable "
                        "lateral acceleration channel, and there are no tyre, brake, ABS or traction-control channels")
        evidence.append("Also checked: " + (_join(have) if have else "nothing else — no tyre, brake, ABS or TC channels in the file"))
        out.append(_finding(
            "No setup change is indicated by " + ("this session" if c.handling is not None else "the channels available"),
            evidence,
            "Every setup pattern the engine knows was tested against the session: repeated power oversteer, entry "
            "oversteer, understeer, a habitual exit slide, balance drift between stints, camber spread, axle "
            "temperature split, brake temperature and ABS use. None reached its threshold.",
            c.conf(caps=[("medium", "absence of a pattern in the channels logged is not proof the setup is optimal")]),
            "Leave the car alone and work on the driver findings above. To look for setup gains, log what is missing "
            "and run longer stints.", "whole car"))
    return out, None
