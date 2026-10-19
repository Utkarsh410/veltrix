"""
Automated performance engineering: the report every session produces.

Nine sections, always in this order, always present:

  lap potential · driver performance · braking · cornering · traction ·
  tyres · vehicle balance · incidents · setup hypotheses

Each section holds findings. A finding is one conclusion and the chain that
leads to it:

  evidence        the numbers from this session, as sentences
  calculation     how those numbers were worked out, so they can be checked
  confidence      high / medium / low, with what it rests on
  recommendation  what to do about it

The same chain is also exposed as evidence → interpretation → hypothesis →
suggested investigation (`interpretation`, `hypothesis`, `investigation`). A
hypothesis is stated only in the setup section, and a setup change is only
suggested when the confidence is better than low: on thin evidence the
recommendation says what to look for first instead.

A section the data cannot support says so, and says which channels would make
it possible — it is never left out and never filled with guesses.

Confidence is a rule, not a judgement. It is the lowest of:
  laps          8 or more flying laps → high, 5 to 7 → medium, fewer → low
  size          the effect against its own lap-to-lap scatter: 2× → high, 1× → medium
  data          a channel recorded too coarsely caps it; a rebuilt channel caps it at medium
  knowledge     anything resting on a generic figure (a tyre window, a rule of
                thumb about setup) is capped, and the cap is named

Everything here is computed from what the corner, handling, tyre and balance
modules already measure; this module adds no new signal processing, it puts
their results in engineering order and makes each one answer for itself.

  from veltrix_engine.analysis import engineering
  report = engineering.analyse(session)
  print(engineering.to_text(report))
"""

from __future__ import annotations

from .base import (SIM_INCIDENT, BEST_VS_TYPICAL, BRAKE_M, BRAKE_SCATTER_M, COAST_FLAG_S, COAST_MIN_KMH, COAST_THROTTLE, CONSISTENT_S, DECEL_GAP_G, DECEL_SHARE, DEG_R2, ESTIMATE, HEAVY_BRAKE_KMH, LAPS_HIGH, LAPS_MEDIUM, LAT_SHARE, LEVELS, LOOSE_S, MEASURED, MIN_GAIN_S, NATURE_TEXT, OVERLAP_FLAG_S, PHASE_DEG, PRESSURE_SPLIT_BAR, RATIO_HIGH, RATIO_MEDIUM, SECTIONS, SPEED_KMH, THROTTLE_M, WHEELSPIN_KMH, WHEELSPIN_SHARE, ZONES_CALC, _Ctx, _finding, _join, fmt_time)  # noqa: F401
from .potential_driver import (_driver, _lap_potential)  # noqa: F401
from .braking_cornering import (_braking, _cornering)  # noqa: F401
from .traction_tyres import (_traction, _tyres)  # noqa: F401
from .vehicle_balance import (_balance)  # noqa: F401
from .incidents import (_corner_check, _incidents, _peak_check)  # noqa: F401
from .setup import (_SETUP_CALC, _setup)  # noqa: F401
from .report import (_plain, _worth, analyse, to_markdown, to_text, unavailable)  # noqa: F401
from .base import (WHEELS, advice, balance, best_lat, circuits, corner_engine, event_rules, handling, json, lap_engine, np, pd, plausibility, re, tyre_temp_channel, tyres)  # noqa: F401
