"""
Evaluates a parsed formula over channels.

Everything is a Series: values, the timestamps they sit on, a validity mask, a
unit, and whether the signal is discrete. Every operation returns a Series, so
a result always comes with its mask — a division by zero, a square root of a
negative number, a derivative next to a gap all become invalid samples, never
`inf`, never a silent number.

Rules the functions keep to
  - Timestamps are the channel's own. Derivatives and integrals use the real
    time between samples.
  - Nothing is computed across a gap (a step longer than five median steps), a
    clock reset, or an invalid sample: the signal is cut into gap-free
    segments and each is treated on its own.
  - Two channels on different clocks are combined on the faster clock; the
    slower one is looked up by its own policy (interpolated if continuous,
    held if discrete) and the step is recorded.
  - Nothing is smoothed unless the formula asks for it. A filter states its
    type, cutoff, order and whether it is zero-phase or causal, and all of
    that goes into the result's provenance.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy import signal

from ..model import GAP_FACTOR, Channel
from . import units as U
from .parser import Binary, Call, Convert, FormulaError, Num, Param, Ref, Unary

UNIFORM_TOLERANCE = 0.02      # a filter needs sample intervals within this share of their median


@dataclass
class Series:
    v: np.ndarray | float
    t: np.ndarray | None = None            # None: a constant
    valid: np.ndarray | None = None
    unit: U.Unit = U.NUMBER
    discrete: bool = False
    steps: list = field(default_factory=list)      # what was done on the way, for provenance

    @property
    def scalar(self) -> bool:
        return self.t is None

    def mask(self) -> np.ndarray:
        v = np.asarray(self.v, float)
        finite = np.isfinite(v)
        return finite if self.valid is None else (self.valid & finite)


def _rate(t) -> float:
    dt = np.diff(t)
    dt = dt[dt > 0]
    return float(1.0 / np.median(dt)) if len(dt) else 0.0


def segments(t: np.ndarray, ok: np.ndarray) -> list:
    """[(start, stop)] index ranges of valid samples with no gap, clock reset or invalid sample inside."""
    if len(t) == 0:
        return []
    dt = np.diff(t)
    step = np.median(dt[dt > 0]) if (dt > 0).any() else 0.0
    cut = np.r_[True, (dt > GAP_FACTOR * step) | (dt <= 0) | ~ok[1:] | ~ok[:-1]]
    group = np.cumsum(cut)
    idx = np.flatnonzero(ok)
    if not len(idx):
        return []
    g = group[idx]
    edges = np.flatnonzero(np.r_[True, g[1:] != g[:-1], True])
    return [(int(idx[a]), int(idx[b - 1]) + 1) for a, b in zip(edges[:-1], edges[1:])]


class Evaluator:
    def __init__(self, resolve, parameters: dict | None = None, formula: str = ""):
        """resolve(name) -> Series for a channel name, or raises FormulaError."""
        self.resolve, self.parameters, self.formula = resolve, parameters or {}, formula
        self.used: list = []           # channel names, in the order they were read

    def fail(self, message, node=None):
        return FormulaError(message, self.formula, getattr(node, "pos", None))

    # ── the tree ──────────────────────────────────────────────────────────────

    def eval(self, node) -> Series:
        if isinstance(node, Num):
            return Series(node.value)
        if isinstance(node, Param):
            if node.name not in self.parameters:
                raise self.fail(f"The parameter ${node.name} is not defined. Defined: "
                                f"{', '.join('$' + k for k in self.parameters) or 'none'}.", node)
            return Series(float(self.parameters[node.name]), steps=[f"${node.name} = {self.parameters[node.name]:g}"])
        if isinstance(node, Ref):
            s = self.resolve(node.name)
            self.used.append(node.name)
            return Series(s.v, s.t, s.valid, s.unit, s.discrete, list(s.steps))
        if isinstance(node, Unary):
            x = self.eval(node.x)
            if node.op == "-":
                return Series(-np.asarray(x.v, float) if not x.scalar else -x.v, x.t, x.valid, x.unit, x.discrete, x.steps)
            return self._logic(lambda a: ~(a != 0), node, x)
        if isinstance(node, Convert):
            return self._convert(node)
        if isinstance(node, Binary):
            return self._binary(node)
        if isinstance(node, Call):
            fn = FUNCTIONS.get(node.name)
            if fn is None:
                raise self.fail(f"Unknown function '{node.name}'. Functions: {', '.join(sorted(FUNCTIONS))}.", node)
            return fn(self, node)
        raise self.fail("This formula cannot be evaluated.", node)

    def _convert(self, node: Convert) -> Series:
        x = self.eval(node.x)
        to = U.parse_unit(node.unit)
        if not to.known:
            raise self.fail(f"'{node.unit}' is not a unit this engine knows.", node)
        if node.assert_only:
            if x.unit.known and x.unit is not U.NUMBER and x.unit.dims != to.dims and not x.unit.dimensionless:
                raise self.fail(f"'as {to.name}' would relabel {x.unit.name} as a different kind of quantity. "
                                f"'as' only states a unit; it does not convert.", node)
            return Series(x.v, x.t, x.valid, to, x.discrete, x.steps + [f"unit stated as {to.name} by the formula"])
        try:
            scale, offset = U.convert(x.unit, to)
        except U.UnitError as e:
            raise self.fail(str(e), node) from None
        steps = x.steps + ([f"converted from {x.unit.name} to {to.name}"] if (scale, offset) != (1.0, 0.0) else [])
        return Series(np.asarray(x.v, float) * scale + offset if not x.scalar else x.v * scale + offset,
                      x.t, x.valid, to, x.discrete, steps)

    def pair(self, a: Series, b: Series) -> tuple[Series, Series]:
        """Two series on one clock: the faster of the two."""
        if a.scalar or b.scalar or a.t is b.t or (len(a.t) == len(b.t) and np.array_equal(a.t, b.t)):
            return a, b
        fast, slow, swap = (a, b, False) if _rate(a.t) >= _rate(b.t) else (b, a, True)
        chan = Channel("x", "calculated", slow.t, np.asarray(slow.v, float), valid=slow.valid, discrete=slow.discrete,
                       label="calculated")
        v, _ = chan.at(fast.t)
        how = "held" if slow.discrete else "linearly interpolated"
        moved = Series(v, fast.t, np.isfinite(v), slow.unit, slow.discrete,
                       slow.steps + [f"a {_rate(slow.t):g} Hz input was {how} onto the {_rate(fast.t):g} Hz clock of the other"])
        return (moved, fast) if swap else (fast, moved)

    def together(self, *series) -> list:
        """Several series on one clock: the fastest among them."""
        timed = [s for s in series if not s.scalar]
        if len(timed) < 2:
            return list(series)
        fastest = max(timed, key=lambda s: _rate(s.t))
        return [s if s.scalar or s is fastest else self.pair(fastest, s)[1] for s in series]

    def _combine(self, a: Series, b: Series, values, unit, discrete=False, extra_valid=None) -> Series:
        t = a.t if not a.scalar else b.t
        steps = a.steps + [s for s in b.steps if s not in a.steps]
        if t is None:
            v = float(values)
            return Series(v if np.isfinite(v) else np.nan, None, None, unit, discrete, steps)
        valid = np.isfinite(values)
        for s in (a, b):
            if not s.scalar:
                valid &= s.mask()
            elif not np.isfinite(s.v):
                valid &= False
        if extra_valid is not None:
            valid &= extra_valid
        return Series(np.where(valid, values, np.nan), t, valid, unit, discrete, steps)

    def _logic(self, fn, node, *xs) -> Series:
        xs = list(xs)
        if len(xs) == 2:
            xs[0], xs[1] = self.pair(xs[0], xs[1])
        arrays = [np.asarray(x.v, float) for x in xs]
        with np.errstate(all="ignore"):
            out = fn(*arrays).astype(float)
        a = xs[0]
        b = xs[1] if len(xs) == 2 else Series(0.0)
        return self._combine(a, b, out, U.NO_UNIT, discrete=True)

    def _binary(self, node: Binary) -> Series:
        a, b = self.eval(node.a), self.eval(node.b)
        op = node.op
        if op in ("and", "or"):
            return self._logic((lambda x, y: (x != 0) & (y != 0)) if op == "and" else (lambda x, y: (x != 0) | (y != 0)), node, a, b)
        a, b = self.pair(a, b)
        av, bv = np.asarray(a.v, float), np.asarray(b.v, float)
        with np.errstate(all="ignore"):
            if op in ("+", "-", "<", "<=", ">", ">=", "==", "!="):
                try:
                    unit, scale, offset = U.same_kind(a.unit, b.unit, {"+": "add", "-": "subtract"}.get(op, "compare"))
                except U.UnitError as e:
                    raise self.fail(str(e), node) from None
                if (scale, offset) != (1.0, 0.0):
                    bv = bv * scale + offset
                    b = Series(b.v, b.t, b.valid, b.unit, b.discrete, b.steps + [f"{b.unit.name} converted to {unit.name}"])
                if op == "+":
                    return self._combine(a, b, av + bv, unit)
                if op == "-":
                    return self._combine(a, b, av - bv, unit)
                cmp = {"<": np.less, "<=": np.less_equal, ">": np.greater, ">=": np.greater_equal,
                       "==": np.equal, "!=": np.not_equal}[op]
                return self._combine(a, b, cmp(av, bv).astype(float), U.NO_UNIT, discrete=True)
            if op == "*":
                return self._combine(a, b, av * bv, U.multiply(a.unit, b.unit))
            if op == "/":
                zero = bv == 0
                out = np.where(zero, np.nan, av / np.where(zero, 1.0, bv))
                if b.scalar and zero:
                    raise self.fail("Division by zero.", node)
                return self._combine(a, b, out, U.multiply(a.unit, b.unit, divide=True), extra_valid=None if b.scalar else ~zero)
            if op == "^":
                if not b.scalar:
                    if not (a.unit is U.NUMBER or a.unit.dimensionless) or not (b.unit is U.NUMBER or b.unit.dimensionless):
                        raise self.fail("An exponent that varies in time needs a base and exponent without units.", node)
                    return self._combine(a, b, np.power(av, bv), U.NO_UNIT)
                if not (b.unit is U.NUMBER or b.unit.dimensionless):
                    raise self.fail(f"An exponent cannot have a unit ({b.unit.name}).", node)
                return self._combine(a, b, np.power(av, float(b.v)), U.power(a.unit, float(b.v)))
        raise self.fail(f"Unknown operator '{op}'.", node)

    # ── helpers for functions ─────────────────────────────────────────────────

    def args(self, node: Call, names: tuple, required: int, options: dict | None = None) -> dict:
        """Bind a call's arguments to names. `options` are named-only settings with defaults."""
        options = options or {}
        if len(node.args) > len(names):
            raise self.fail(f"{node.name}() takes at most {len(names)} argument(s), {len(node.args)} given.", node)
        bound = dict(zip(names, node.args))
        for key, value in node.kwargs:
            if key in bound:
                raise self.fail(f"{node.name}(): '{key}' is given twice.", node)
            if key not in names and key not in options:
                raise self.fail(f"{node.name}() has no argument '{key}'. It takes: {', '.join(list(names) + list(options))}.", node)
            bound[key] = value
        missing = [n for n in names[:required] if n not in bound]
        if missing:
            raise self.fail(f"{node.name}() needs {', '.join(missing)}.", node)
        out = {k: self.eval(v) for k, v in bound.items()}
        for key, default in options.items():
            out.setdefault(key, Series(default))
        return out

    def number(self, s: Series, node, what: str) -> float:
        if not s.scalar or not np.isfinite(s.v):
            raise self.fail(f"{node.name}(): {what} must be a number, not a channel.", node)
        return float(s.v)

    def signal(self, s: Series, node, what: str = "its first argument") -> Series:
        if s.scalar:
            raise self.fail(f"{node.name}(): {what} must be a channel.", node)
        return s


# ── functions ─────────────────────────────────────────────────────────────────

def _elementwise(name, fn, unit_rule, domain=None):
    def call(ev: Evaluator, node: Call) -> Series:
        x = ev.args(node, ("x",), 1)["x"]
        unit, xv = unit_rule(ev, node, x)
        with np.errstate(all="ignore"):
            ok = domain(xv) if domain else None
            out = fn(np.where(ok, xv, np.nan) if ok is not None else xv)
        if x.scalar:
            return Series(float(out), None, None, unit, False, x.steps)
        valid = x.mask() & np.isfinite(out)
        return Series(np.where(valid, out, np.nan), x.t, valid, unit, x.discrete and name == "abs", x.steps)
    return call


def _same_unit(ev, node, x):
    return x.unit, np.asarray(x.v, float)


def _root_unit(ev, node, x):
    return U.power(x.unit, 0.5), np.asarray(x.v, float)


def _pure(ev, node, x):
    if not (x.unit is U.NUMBER or x.unit.dimensionless):
        raise ev.fail(f"{node.name}() needs a value without a unit; this one is in {x.unit.name}. Divide it by a "
                      "reference value first.", node)
    return U.NO_UNIT, np.asarray(x.v, float)


def _angle(ev, node, x):
    v = np.asarray(x.v, float)
    if x.unit is U.NUMBER or x.unit.dimensionless:
        return U.NO_UNIT, v                              # taken as radians
    if x.unit.known and x.unit.dims == U.parse_unit("rad").dims:
        return U.NO_UNIT, v * x.unit.scale
    raise ev.fail(f"{node.name}() needs an angle; this value is in {x.unit.name}.", node)


def _several(reduce):
    def call(ev: Evaluator, node: Call) -> Series:
        if len(node.args) < 2 or node.kwargs:
            raise ev.fail(f"{node.name}() needs two or more values.", node)
        acc = ev.eval(node.args[0])
        for arg in node.args[1:]:
            a, b = ev.pair(acc, ev.eval(arg))
            try:
                unit, scale, offset = U.same_kind(a.unit, b.unit, "compare")
            except U.UnitError as e:
                raise ev.fail(str(e), node) from None
            acc = ev._combine(a, b, reduce(np.asarray(a.v, float), np.asarray(b.v, float) * scale + offset), unit,
                              discrete=a.discrete and b.discrete)
        return acc
    return call


def _clip(ev: Evaluator, node: Call) -> Series:
    a = ev.args(node, ("x", "low", "high"), 3)
    out = a["x"]
    for bound, fn in ((a["low"], np.maximum), (a["high"], np.minimum)):
        x, b = ev.pair(out, bound)
        try:
            unit, scale, offset = U.same_kind(x.unit, b.unit, "compare")
        except U.UnitError as e:
            raise ev.fail(str(e), node) from None
        out = ev._combine(x, b, fn(np.asarray(x.v, float), np.asarray(b.v, float) * scale + offset), unit, discrete=x.discrete)
    return out


def _if(ev: Evaluator, node: Call) -> Series:
    a = ev.args(node, ("condition", "then", "otherwise"), 3)
    c, x, y = ev.together(a["condition"], a["then"], a["otherwise"])
    try:
        unit, scale, offset = U.same_kind(x.unit, y.unit, "choose between")
    except U.UnitError as e:
        raise ev.fail(str(e), node) from None
    unit = U.NO_UNIT if unit is U.NUMBER else unit
    cv, xv, yv = np.asarray(c.v, float), np.asarray(x.v, float), np.asarray(y.v, float) * scale + offset
    steps = c.steps + [s for s in x.steps + y.steps if s not in c.steps]
    t = next((s.t for s in (c, x, y) if not s.scalar), None)
    if t is None:
        return Series(float(xv) if cv != 0 else float(yv), None, None, unit, False, steps)
    cv, xv, yv = (np.broadcast_to(v, (len(t),)) for v in (cv, xv, yv))
    take = cv != 0
    out = np.where(take, xv, yv)
    valid = np.isfinite(cv) & np.isfinite(out)
    for s, used in ((c, np.ones(len(t), bool)), (x, take), (y, ~take)):      # only the branch taken has to be valid
        if not s.scalar:
            valid &= s.mask() | ~used
    return Series(np.where(valid, out, np.nan), t, valid, unit, all(s.scalar or s.discrete for s in (x, y)), steps)


def _per_segment(x: Series, fn, min_len: int = 2):
    """Apply fn(t, v) -> array to each gap-free segment; the rest is invalid."""
    v = np.asarray(x.v, float)
    out = np.full(len(v), np.nan)
    segs = segments(x.t, x.mask())
    for a, b in segs:
        if b - a >= min_len:
            out[a:b] = fn(x.t[a:b], v[a:b])
    return out, segs


def _derivative(ev: Evaluator, node: Call) -> Series:
    x = ev.signal(ev.args(node, ("x",), 1)["x"], node)
    out, segs = _per_segment(x, lambda t, v: np.gradient(v, t))
    note = "derivative on the logged timestamps (central differences)"
    if len(segs) > 1:
        note += f", restarted at each of {len(segs) - 1} gap(s) or invalid stretches"
    return Series(out, x.t, np.isfinite(out), U.per_second(x.unit), False, x.steps + [note])


def _integrate(ev: Evaluator, node: Call) -> Series:
    x = ev.signal(ev.args(node, ("x",), 1)["x"], node)
    total = [0.0]

    def area(t, v):
        run = total[0] + np.r_[0.0, np.cumsum(0.5 * (v[1:] + v[:-1]) * np.diff(t))]
        total[0] = float(run[-1])
        return run
    out, segs = _per_segment(x, area, min_len=1)
    note = "integral on the logged timestamps (trapezoid)"
    if len(segs) > 1:
        note += f"; nothing is added across {len(segs) - 1} gap(s) or invalid stretches"
    return Series(out, x.t, np.isfinite(out), U.per_second(x.unit, inverse=True), False, x.steps + [note])


def _window_samples(ev, node, x: Series, seconds: float) -> int:
    if seconds <= 0:
        raise ev.fail(f"{node.name}(): the window must be a positive number of seconds.", node)
    rate = _rate(x.t)
    n = int(round(seconds * rate))
    if n < 2:
        raise ev.fail(f"{node.name}(): a {seconds:g} s window is shorter than two samples at {rate:g} Hz.", node)
    return n


def _rolling(kind: str):
    def call(ev: Evaluator, node: Call) -> Series:
        a = ev.args(node, ("x", "window"), 2)
        x = ev.signal(a["x"], node)
        seconds = ev.number(a["window"], node, "the window (seconds)")
        n = _window_samples(ev, node, x, seconds)

        def roll(t, v):
            r = pd.Series(v).rolling(n, center=True, min_periods=n)
            return getattr(r, kind)().to_numpy()
        out, _ = _per_segment(x, roll, min_len=n)
        unit = x.unit
        note = (f"{node.name} over {n} samples ({seconds:g} s), centred; the first and last {n // 2} samples of each "
                "stretch have no full window and are invalid")
        return Series(out, x.t, np.isfinite(out), unit, False, x.steps + [note])
    return call


CAP_SHARE = 0.4               # cap=1: a cutoff above this share of the sample rate is lowered to it


def _filter(btype: str):
    def call(ev: Evaluator, node: Call) -> Series:
        a = ev.args(node, ("x", "cutoff"), 2, {"order": 2.0, "zero_phase": 1.0, "cap": 0.0, "uniform": 0.0})
        x = ev.signal(a["x"], node)
        cutoff = ev.number(a["cutoff"], node, "the cutoff (Hz)")
        order = int(ev.number(a["order"], node, "the order"))
        zero_phase = ev.number(a["zero_phase"], node, "zero_phase") != 0
        cap = ev.number(a["cap"], node, "cap") != 0
        uniform = ev.number(a["uniform"], node, "uniform") != 0
        if not 1 <= order <= 8:
            raise ev.fail(f"{node.name}(): the order must be between 1 and 8.", node)
        dt = np.diff(x.t)
        dt = dt[dt > 0]
        if not len(dt):
            raise ev.fail(f"{node.name}(): the channel has no usable timestamps.", node)
        step = float(np.median(dt))
        inside = dt[dt <= GAP_FACTOR * step]
        uneven = len(inside) and np.max(np.abs(inside - step)) > UNIFORM_TOLERANCE * step
        if uneven and not uniform:
            raise ev.fail(f"{node.name}(): the channel is not uniformly sampled (its sample interval varies by more "
                          f"than {UNIFORM_TOLERANCE:.0%}), and a filter needs a steady rate. uniform=1 resamples each "
                          "stretch onto an even grid, filters it there and reads the result back.", node)
        fs = 1.0 / step
        capped = False
        if cap and cutoff > CAP_SHARE * fs:
            cutoff, capped = CAP_SHARE * fs, True          # as the engine does for a low-rate log
        if not 0 < cutoff < fs / 2:
            raise ev.fail(f"{node.name}(): the cutoff must be between 0 and {fs / 2:g} Hz (half the {fs:g} Hz sample "
                          f"rate). cap=1 lowers it to {CAP_SHARE:g} × the sample rate instead.", node)
        sos = signal.butter(order, cutoff, btype=btype, fs=fs, output="sos")
        pad = 3 * (2 * len(sos) + 1)

        def plain(v):
            if zero_phase:
                return signal.sosfiltfilt(sos, v)
            zi = signal.sosfilt_zi(sos) * v[0]
            return signal.sosfilt(sos, v, zi=zi)[0]

        def run(t, v):
            if not uneven:
                return plain(v)
            grid = np.arange(t[0], t[-1] + step / 2, step)            # an even grid at the median step
            if len(grid) <= pad + 1:
                return np.full(len(v), np.nan)
            return np.interp(t, grid, plain(np.interp(grid, t, v)))
        out, _ = _per_segment(x, run, min_len=pad + 2)
        phase = "zero-phase (run forwards and backwards, no delay)" if zero_phase else "causal (run forwards only, delays the signal)"
        note = (f"Butterworth {btype}, order {order}, cutoff {cutoff:g} Hz at {fs:g} Hz, {phase}"
                + (f"; cutoff capped at {CAP_SHARE:g} × the sample rate" if capped else "")
                + ("; filtered on an even grid at the median step and read back on the channel's own timestamps"
                   if uneven else ""))
        return Series(out, x.t, np.isfinite(out), x.unit, False, x.steps + [note])
    return call


def _atan2(ev: Evaluator, node: Call) -> Series:
    """atan2(y, x): the angle of the vector (x, y), in rad. y and x must be the same kind of quantity."""
    a = ev.args(node, ("y", "x"), 2)
    y, x = ev.pair(a["y"], a["x"])
    try:
        _, scale, offset = U.same_kind(y.unit, x.unit, "take the angle between")
    except U.UnitError as e:
        raise ev.fail(str(e), node) from None
    out = np.arctan2(np.asarray(y.v, float), np.asarray(x.v, float) * scale + offset)
    return ev._combine(y, x, out, U.parse_unit("rad"))


def _hypot(ev: Evaluator, node: Call) -> Series:
    """hypot(a, b): sqrt(a² + b²), in the unit of a."""
    a = ev.args(node, ("a", "b"), 2)
    x, y = ev.pair(a["a"], a["b"])
    try:
        unit, scale, offset = U.same_kind(x.unit, y.unit, "combine")
    except U.UnitError as e:
        raise ev.fail(str(e), node) from None
    return ev._combine(x, y, np.hypot(np.asarray(x.v, float), np.asarray(y.v, float) * scale + offset), unit)


def _when(ev: Evaluator, node: Call) -> Series:
    """when(condition, x): x where the condition holds, invalid elsewhere — not zero, not held."""
    a = ev.args(node, ("condition", "x"), 2)
    c, x = ev.pair(a["condition"], a["x"])
    if x.scalar and c.scalar:
        return x if c.v else Series(np.nan, None, None, x.unit, False, x.steps)
    t = c.t if not c.scalar else x.t
    cv = np.broadcast_to(np.asarray(c.v, float), (len(t),))
    xv = np.broadcast_to(np.asarray(x.v, float), (len(t),))
    valid = (cv != 0) & np.isfinite(xv) & np.isfinite(cv)
    for s_ in (c, x):
        if not s_.scalar:
            valid &= s_.mask()
    return Series(np.where(valid, xv, np.nan), t, valid, x.unit, x.discrete, c.steps + [s_ for s_ in x.steps if s_ not in c.steps])


def _rolling_median(ev: Evaluator, node: Call) -> Series:
    """rolling_median(x, seconds, edges=false): the median over a centred window — what a signal holds,
    without one-sample jolts. edges=true keeps the ends of each stretch with a shorter window."""
    a = ev.args(node, ("x", "window"), 2, {"edges": 0.0})
    x = ev.signal(a["x"], node)
    seconds = ev.number(a["window"], node, "the window (seconds)")
    n = _window_samples(ev, node, x, seconds)
    edges = ev.number(a["edges"], node, "edges") != 0

    def roll(t, v):
        return pd.Series(v).rolling(n, center=True, min_periods=1 if edges else n).median().to_numpy()
    out, _ = _per_segment(x, roll, min_len=1 if edges else n)
    note = f"rolling median over {n} samples ({seconds:g} s), centred" + ("" if edges else "; the ends of each stretch are invalid")
    return Series(out, x.t, np.isfinite(out), x.unit, False, x.steps + [note])


def _since_change(ev: Evaluator, node: Call) -> Series:
    """since_change(x): seconds since x last took a new value — since_change(lap) is the time into the lap."""
    a = ev.args(node, ("x",), 1)
    x = ev.signal(a["x"], node)
    v, t = np.asarray(x.v, float), np.asarray(x.t, float)
    change = np.r_[True, v[1:] != v[:-1]]
    start = np.maximum.accumulate(np.where(change, np.arange(len(v)), 0))
    out = t - t[start]
    return Series(out, x.t, x.mask().copy(), U.parse_unit("s"), False, x.steps + ["seconds since the value last changed"])


def _peak_detect(ev: Evaluator, node: Call) -> Series:
    a = ev.args(node, ("x",), 1, {"min_height": np.nan, "min_distance": 0.0, "prominence": np.nan})
    x = ev.signal(a["x"], node)
    height, prominence = a["min_height"].v, a["prominence"].v
    spacing = ev.number(a["min_distance"], node, "min_distance (seconds)")
    distance = max(int(round(spacing * _rate(x.t))), 1)
    kwargs = {"distance": distance}
    if np.isfinite(height):
        kwargs["height"] = float(height)
    if np.isfinite(prominence):
        kwargs["prominence"] = float(prominence)

    def find(t, v):
        marks = np.zeros(len(v))
        marks[signal.find_peaks(v, **kwargs)[0]] = 1.0
        return marks
    out, _ = _per_segment(x, find, min_len=3)
    settings = ", ".join(f"{k} {v:g}" for k, v in (("min height", height), ("prominence", prominence)) if np.isfinite(v))
    note = f"local maxima at least {spacing:g} s apart" + (f", {settings}" if settings else "")
    return Series(out, x.t, np.isfinite(out), U.NO_UNIT, True, x.steps + [note])


FUNCTIONS = {
    "abs": _elementwise("abs", np.abs, _same_unit),
    "sqrt": _elementwise("sqrt", np.sqrt, _root_unit, lambda v: v >= 0),
    "log": _elementwise("log", np.log, _pure, lambda v: v > 0),
    "exp": _elementwise("exp", np.exp, _pure),
    "sin": _elementwise("sin", np.sin, _angle),
    "cos": _elementwise("cos", np.cos, _angle),
    "tan": _elementwise("tan", np.tan, _angle),
    "min": _several(np.minimum),
    "max": _several(np.maximum),
    "clip": _clip,
    "if": _if,
    "derivative": _derivative,
    "integrate": _integrate,
    "moving_average": _rolling("mean"),
    "rolling_min": _rolling("min"),
    "rolling_max": _rolling("max"),
    "rolling_std": _rolling("std"),
    "lowpass": _filter("lowpass"),
    "highpass": _filter("highpass"),
    "peak_detect": _peak_detect,
    "atan2": _atan2,
    "hypot": _hypot,
    "when": _when,
    "rolling_median": _rolling_median,
    "since_change": _since_change,
}

REFERENCE = {
    "abs(x)": "absolute value",
    "sqrt(x)": "square root; negative values become invalid",
    "log(x)": "natural logarithm of a value without a unit; zero and negative values become invalid",
    "exp(x)": "e to the power x, for a value without a unit",
    "sin(x) cos(x) tan(x)": "of an angle in deg or rad (a bare number is taken as radians)",
    "min(a, b, ...) max(a, b, ...)": "smallest / largest of the values, sample by sample",
    "clip(x, low, high)": "x held between low and high",
    "if(condition, a, b)": "a where the condition holds, b elsewhere",
    "derivative(x)": "rate of change per second on the logged timestamps; never across a gap or reset",
    "integrate(x)": "running integral over time; nothing is added across a gap",
    "moving_average(x, seconds)": "centred mean over a window; samples without a full window are invalid",
    "rolling_min / rolling_max / rolling_std (x, seconds)": "the same for minimum, maximum and standard deviation",
    "lowpass(x, cutoff, order=2, zero_phase=true, cap=false, uniform=false)": "Butterworth low-pass, cutoff in Hz; "
        "zero_phase=false is causal; cap=1 lowers a cutoff too high for the sample rate to 0.4 × the rate; uniform=1 "
        "filters an unevenly sampled channel on an even grid",
    "highpass(x, cutoff, order=2, zero_phase=true)": "Butterworth high-pass",
    "peak_detect(x, min_height=, min_distance=, prominence=)": "1 at each local maximum, 0 elsewhere; min_distance in seconds",
    "atan2(y, x)": "the angle of the vector (x, y) in rad; y and x the same kind of quantity",
    "hypot(a, b)": "sqrt(a² + b²), in the unit of a",
    "when(condition, x)": "x where the condition holds, invalid elsewhere (not zero)",
    "rolling_median(x, seconds, edges=false)": "centred median over a window: what a signal holds, without one-sample jolts",
    "since_change(x)": "seconds since x last changed value — since_change(lap) is the time into the lap",
}
