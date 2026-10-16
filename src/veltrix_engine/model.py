"""
The canonical session model: the one shape every file format is brought to and
everything else reads from.

Entities: SessionModel, SourceFile, Channel, Lap, Stint, Event, Annotation.

Layers
------
A channel lives in exactly one layer, and a layer is never written over:

  raw          a source column as the file has it: source name, source unit
               ("unknown" when the file gives none), the logger's own timestamps
  normalised   the same measurement under its schema id, in the schema unit and
               sign, still on its own timestamps
  corrected    a measurement repaired or rebuilt from other channels
  calculated   computed from other channels (derived and maths channels)

Every channel carries a `label` saying what kind of statement it is — measured,
corrected/reconstructed, calculated — and a Provenance: what it was made from,
by what formula or algorithm, with which settings, by which engine version.
Events, interpretations and recommendations use the remaining labels.

Sampling
--------
Each channel keeps its own timestamps and therefore its own rate. Nothing in
the model is resampled. When a calculation needs channels on a common clock it
asks for an alignment (`SessionModel.align`), which applies the policy of each
channel — linear interpolation for continuous signals, hold-previous for
discrete ones — never extrapolates past a channel's own samples, and never
interpolates across a gap. The uniform frame the lap, corner and balance
analysis runs on is one such alignment, recorded as `views["analysis"]`.

Arrays are shared, not copied: a CSV's channels all point at one timestamp
array, and raw channels are views of the table that was read.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

MODEL_VERSION = 1
LAYERS = ("raw", "normalised", "corrected", "calculated")
MEASURED, RECONSTRUCTED, CALCULATED = "measured", "corrected/reconstructed", "calculated"
EVENT, INTERPRETATION, HYPOTHESIS = "detected event", "interpretation", "hypothesis/recommendation"
LABELS = (MEASURED, RECONSTRUCTED, CALCULATED, EVENT, INTERPRETATION, HYPOTHESIS)
# how far an event is established: a detector's candidate; recorded by the source itself (a simulator's
# incident system); or a candidate a record (or, later, a person) confirmed
CANDIDATE, RECORDED, CONFIRMED = "candidate", "recorded", "confirmed"
STATUSES = (CANDIDATE, RECORDED, CONFIRMED)
UNKNOWN_UNIT = "unknown"
GAP_FACTOR = 5.0          # a step longer than this many median steps is a gap; alignment does not bridge it


@dataclass
class Provenance:
    """How a channel came to be."""
    inputs: tuple = ()                 # "layer:id" of the channels it was made from, or the source column
    algorithm: str = ""                # the formula, or the algorithm in words
    parameters: dict = field(default_factory=dict)    # filter, resample and threshold settings
    unit_source: str = ""              # file | stated | converted | plausibility | assumed | schema | none
    engine_version: str = ""
    notes: tuple = ()


@dataclass
class Channel:
    id: str
    layer: str
    timestamps: np.ndarray             # seconds on the session clock; shared between channels of one group
    values: np.ndarray
    unit: str = UNKNOWN_UNIT
    label: str = MEASURED
    name: str = ""                     # display name
    source_name: str = ""              # the column or signal in the file
    description: str = ""
    category: str = ""
    quantity: str = ""
    discrete: bool = False             # step-valued: looked up by hold-previous, never interpolated
    valid: np.ndarray | None = None    # False where a sample must not be used; None = wherever finite
    formula: str = ""
    dependencies: tuple = ()
    quality: list = field(default_factory=list)       # flags: "coarse steps", "unit assumed", ...
    provenance: Provenance = field(default_factory=Provenance)
    version: int = 1
    user: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.layer not in LAYERS:
            raise ValueError(f"Unknown layer '{self.layer}'. Layers: {LAYERS}")
        if self.label not in LABELS:
            raise ValueError(f"Unknown label '{self.label}'. Labels: {LABELS}")
        if len(self.timestamps) != len(self.values):
            raise ValueError(f"{self.id}: {len(self.values)} values for {len(self.timestamps)} timestamps.")
        self.name = self.name or self.id

    @property
    def ref(self) -> str:
        return f"{self.layer}:{self.id}"

    @property
    def derived(self) -> bool:
        return self.layer in ("corrected", "calculated")

    @property
    def dtype(self) -> str:
        return str(self.values.dtype)

    @property
    def sample_rate(self) -> float | None:
        """Median rate in Hz, or None with fewer than two samples."""
        if len(self.timestamps) < 2:
            return None
        dt = np.diff(self.timestamps)
        dt = dt[dt > 0]
        return float(1.0 / np.median(dt)) if len(dt) else None

    @property
    def validity(self) -> np.ndarray:
        """The validity mask as an array: the explicit mask, and finite."""
        finite = np.isfinite(self.values) if self.values.dtype.kind == "f" else np.ones(len(self.values), bool)
        return finite if self.valid is None else (self.valid & finite)

    def at(self, t) -> tuple[np.ndarray, np.ndarray]:
        """Values at times `t` by this channel's lookup policy.

        Returns (values, exact). `exact` is True where `t` fell on a sample;
        elsewhere the value is interpolated (continuous) or held (discrete).
        NaN outside the channel's own span, in a gap, and next to invalid
        samples."""
        t = np.atleast_1d(np.asarray(t, float))
        ok = self.validity
        ts, ys = self.timestamps[ok], self.values[ok].astype(float)
        out, exact = np.full(len(t), np.nan), np.zeros(len(t), bool)
        if len(ts) == 0:
            return out, exact
        # a time within rounding of the first or last sample is that sample
        edge = 1e-9 * max(1.0, abs(float(ts[-1])))
        t = np.where(np.abs(t - ts[0]) <= edge, ts[0], np.where(np.abs(t - ts[-1]) <= edge, ts[-1], t))
        hi = np.searchsorted(ts, t, side="left")
        on = (hi < len(ts)) & (ts[np.clip(hi, 0, len(ts) - 1)] == t)
        exact[on] = True
        if self.discrete or len(ts) == 1:
            idx = np.searchsorted(ts, t, side="right") - 1
            inside = (idx >= 0) & (t <= ts[-1])
            out[inside] = ys[idx[inside]]
            return out, exact
        inside = (t >= ts[0]) & (t <= ts[-1])
        out[inside] = np.interp(t[inside], ts, ys)
        # never across a gap: a bracketing pair of samples too far apart gives no value
        step = np.median(np.diff(ts))
        lo = np.clip(hi - 1, 0, len(ts) - 1)
        gap = inside & ~on & ((ts[np.clip(hi, 0, len(ts) - 1)] - ts[lo]) > GAP_FACTOR * step)
        out[gap] = np.nan
        return out, exact

    def meta(self) -> dict:
        """Everything but the arrays, as plain values."""
        rate = self.sample_rate
        return {"id": self.id, "layer": self.layer, "label": self.label, "name": self.name,
                "source_name": self.source_name, "description": self.description, "category": self.category,
                "quantity": self.quantity, "unit": self.unit, "dtype": self.dtype, "discrete": self.discrete,
                "samples": int(len(self.values)), "sample_rate": None if rate is None else round(rate, 6),
                "t_start": float(self.timestamps[0]) if len(self.timestamps) else None,
                "t_end": float(self.timestamps[-1]) if len(self.timestamps) else None,
                "invalid": int((~self.validity).sum()), "formula": self.formula,
                "dependencies": list(self.dependencies), "quality": list(self.quality),
                "provenance": {**asdict(self.provenance), "inputs": list(self.provenance.inputs),
                               "notes": list(self.provenance.notes)},
                "version": self.version, "user": dict(self.user)}


@dataclass
class SourceFile:
    name: str
    sha256: str = ""
    format: str = ""
    info: dict = field(default_factory=dict)       # header metadata as the file gives it
    columns: int = 0
    rows: int = 0


@dataclass
class Lap:
    lap: int | str
    t_start: float
    t_end: float
    time_s: float
    dist_m: float
    status: str
    stint: int
    stint_lap: int
    start_crossed: bool
    end_crossed: bool


@dataclass
class Stint:
    stint: int
    laps: list
    t_start: float
    t_end: float
    flying_laps: int


@dataclass
class Event:
    """Something a detector found, or the source recorded. A detector's event is a
    candidate for review until a record confirms it; never a measurement."""
    kind: str
    t_start: float
    t_end: float
    lap: int | str | None = None
    position_m: float | None = None
    severity: str = ""
    channels: tuple = ()
    evidence: dict = field(default_factory=dict)
    confidence: str = ""
    rule: str = ""
    rule_version: str = ""
    label: str = EVENT
    status: str = CANDIDATE
    source: str = ""                   # what found it: "rule", "steering model", "simulator"


@dataclass
class Annotation:
    """A note a person made. Kept apart from detected events."""
    text: str
    scope: str = "session"             # session | lap | time | position
    lap: int | str | None = None
    time: float | None = None
    position_m: float | None = None
    category: str = ""
    author: str = ""
    created: str = ""


@dataclass
class View:
    """An alignment that was made, and how: a set of channels on one clock."""
    name: str
    rate_hz: float
    policy: dict                       # {channel id: "linear" | "hold"}
    frame: pd.DataFrame
    note: str = ""


class SessionModel:
    def __init__(self, sources: list | None = None):
        self.sources: list = list(sources or [])
        self._channels: dict = {layer: {} for layer in LAYERS}
        self.laps: list = []
        self.stints: list = []
        self.events: list = []
        self.annotations: list = []
        self.views: dict = {}
        self.notes: list = []          # [(level, message)] from the import
        self.maths_signatures: dict = {}       # {maths channel: what it was last computed from}
        self.mapping: list = []                # one dict per mapped column: how it came to be that channel
        self.version = MODEL_VERSION

    # ── channels ──────────────────────────────────────────────────────────────

    def add(self, channel: Channel, replace: bool = False) -> Channel:
        """Add a channel to its layer. Raw and normalised channels are written
        once. A corrected or calculated channel may be replaced when
        `replace` is set; its version number then goes up."""
        layer = self._channels[channel.layer]
        if channel.id in layer:
            if not replace or not channel.derived:
                raise ValueError(f"{channel.ref} already exists; the {channel.layer} layer is not written over.")
            channel.version = layer[channel.id].version + 1
        layer[channel.id] = channel
        return channel

    def get(self, channel_id: str, layer: str | None = None) -> Channel:
        """A channel by id. Without a layer, the most processed one that has
        it: calculated, then corrected, normalised, raw. "layer:id" also works."""
        if layer is None and ":" in channel_id and channel_id.split(":", 1)[0] in LAYERS:
            layer, channel_id = channel_id.split(":", 1)
        for name in ([layer] if layer else reversed(LAYERS)):
            if channel_id in self._channels[name]:
                return self._channels[name][channel_id]
        where = f"the {layer} layer" if layer else "any layer"
        raise KeyError(f"No channel '{channel_id}' in {where}.")

    def has(self, channel_id: str, layer: str | None = None) -> bool:
        try:
            self.get(channel_id, layer)
            return True
        except KeyError:
            return False

    def channels(self, layer: str | None = None) -> list:
        layers = [layer] if layer else LAYERS
        return [c for name in layers for c in self._channels[name].values()]

    def table(self, layer: str | None = None) -> pd.DataFrame:
        """One row per channel, without the arrays — what a channel browser shows."""
        rows = [{k: v for k, v in c.meta().items() if k not in ("provenance", "user", "dependencies")}
                | {"unit_source": c.provenance.unit_source} for c in self.channels(layer)]
        return pd.DataFrame(rows)

    def align(self, ids, rate_hz: float | None = None, timestamps=None) -> pd.DataFrame:
        """The named channels on one clock, each by its own lookup policy.
        Give `timestamps`, or a `rate_hz` for a uniform clock over the span the
        channels cover between them. Nothing is extrapolated or bridged."""
        chans = [self.get(i) for i in ids]
        if timestamps is None:
            if not rate_hz:
                raise ValueError("align() needs timestamps or a rate.")
            t0 = min(c.timestamps[0] for c in chans if len(c.timestamps))
            t1 = max(c.timestamps[-1] for c in chans if len(c.timestamps))
            timestamps = t0 + np.arange(int(np.floor((t1 - t0) * rate_hz + 1e-6)) + 1) / rate_hz
        timestamps = np.asarray(timestamps, float)
        out = {"time": timestamps}
        for c in chans:
            out[c.id] = c.at(timestamps)[0]
        return pd.DataFrame(out)

    # ── summary and plain data ────────────────────────────────────────────────

    def describe(self) -> dict:
        rates = sorted({round(c.sample_rate, 3) for c in self.channels("normalised") if c.sample_rate})
        return {"sources": [s.name for s in self.sources],
                "channels": {layer: len(self._channels[layer]) for layer in LAYERS},
                "rates_hz": rates, "laps": len(self.laps), "stints": len(self.stints),
                "events": len(self.events), "annotations": len(self.annotations)}

    def manifest(self) -> dict:
        """The whole model without its arrays, as JSON-ready values."""
        return {"model_version": self.version, "sources": [asdict(s) for s in self.sources],
                "channels": [c.meta() for c in self.channels()],
                "laps": [asdict(x) for x in self.laps], "stints": [asdict(x) for x in self.stints],
                "events": [{**asdict(e), "channels": list(e.channels)} for e in self.events],
                "annotations": [asdict(a) for a in self.annotations],
                "views": {k: {"rate_hz": v.rate_hz, "policy": v.policy, "note": v.note, "channels": list(v.frame.columns)}
                          for k, v in self.views.items()},
                "mapping": [dict(m) for m in self.mapping], "notes": [list(n) for n in self.notes]}


def laps_from_table(table: pd.DataFrame) -> tuple[list, list]:
    """Lap and Stint entities from the engine's lap table."""
    laps = [Lap(lap=r.lap if isinstance(r.lap, str) else int(r.lap), t_start=float(r.t_start), t_end=float(r.t_end),
                time_s=float(r.lap_time_s), dist_m=float(r.dist_m), status=r.status, stint=int(r.stint),
                stint_lap=int(r.stint_lap), start_crossed=bool(r.start_crossed), end_crossed=bool(r.end_crossed))
            for r in table.itertuples()]
    stints = []
    for k, part in table.groupby("stint", sort=True):
        stints.append(Stint(stint=int(k), laps=[x if isinstance(x, str) else int(x) for x in part["lap"]],
                            t_start=float(part["t_start"].min()), t_end=float(part["t_end"].max()),
                            flying_laps=int((part["status"] == "flying").sum())))
    return laps, stints


def events_from_incidents(events: pd.DataFrame, laps: pd.DataFrame, rule_version: str = "",
                          lap_distance: Channel | None = None) -> list:
    """Event entities from the understeer / oversteer detector's table
    (analysis/handling.py). The detector works in distance; each event's time
    is read from the lap-distance channel when one is given, and otherwise
    estimated from how far round the lap the incident sits."""
    start = laps.set_index("lap")
    out = []
    for e in events.itertuples():
        lap = start.loc[e.lap] if e.lap in start.index else None
        t_mid = np.nan
        if lap is not None and lap_distance is not None:
            a, b = np.searchsorted(lap_distance.timestamps, [lap.t_start, lap.t_end])
            d = np.asarray(lap_distance.values[a:b], float)
            ok = np.isfinite(d)
            if ok.sum() >= 2:
                t_mid = float(np.interp(e.peak_m, np.maximum.accumulate(d[ok]), lap_distance.timestamps[a:b][ok]))
        if not np.isfinite(t_mid) and lap is not None and lap.dist_m:
            t_mid = float(lap.t_start + e.peak_m / lap.dist_m * lap.lap_time_s)
        out.append(Event(
            kind=f"possible {e.kind}", t_start=t_mid - e.duration_s / 2, t_end=t_mid + e.duration_s / 2,
            lap=e.lap if isinstance(e.lap, str) else int(e.lap), position_m=float(e.peak_m), severity=e.severity,
            channels=("steering", "acc_lat", "speed"),
            evidence={"corner": e.corner, "phase": e.phase, "cause": e.cause, "peak_deg": float(e.peak_deg),
                      "speed_kmh": float(e.speed), "lat_g": float(e.lat_g)},
            confidence="requires review", rule="steering residual against the session's own norm",
            rule_version=rule_version, source="steering model"))
    return out
