"""
Units for maths channels: what a unit is, which ones are known, and what
arithmetic does to them.

A unit is a scale to SI and a vector of dimensions. Adding or comparing two
quantities needs the same dimensions (km/h + °C is refused); a different scale
of the same dimensions is converted to the left operand's unit. Multiplying
and dividing combine dimensions. Angle is kept as a dimension of its own so
that degrees and radians convert but neither is silently dropped.

Three special cases:
  a bare number      takes the unit of whatever it is added to or compared with
  "unknown"          a channel whose file gave no unit. It stays unknown: it can
                     be scaled by numbers, but combining it with a known unit is
                     refused until the unit is stated (`x as km/h`)
  "%"                dimensionless, and a label only: 50 % is the number 50
"""

from __future__ import annotations

from dataclasses import dataclass

G0 = 9.80665
#          length time mass temperature angle revolutions
_NONE = (0, 0, 0, 0, 0, 0)


@dataclass(frozen=True)
class Unit:
    name: str
    scale: float = 1.0                 # multiply by this to get SI
    dims: tuple | None = _NONE         # None: unknown
    offset: float = 0.0                # added after scaling (temperature)

    @property
    def known(self) -> bool:
        return self.dims is not None

    @property
    def dimensionless(self) -> bool:
        return self.dims == _NONE


UNKNOWN = Unit("unknown", 1.0, None)
NUMBER = Unit("", 1.0, _NONE)          # a bare number in a formula
NO_UNIT = Unit("-", 1.0, _NONE)

_L, _T, _M, _K, _A, _R = ((1, 0, 0, 0, 0, 0), (0, 1, 0, 0, 0, 0), (0, 0, 1, 0, 0, 0), (0, 0, 0, 1, 0, 0),
                          (0, 0, 0, 0, 1, 0), (0, 0, 0, 0, 0, 1))


def _d(**p) -> tuple:
    base = {"L": _L, "T": _T, "M": _M, "K": _K, "A": _A, "R": _R}
    return tuple(sum(base[k][i] * v for k, v in p.items()) for i in range(6))


_SPEED, _ACC, _PRESS = _d(L=1, T=-1), _d(L=1, T=-2), _d(M=1, L=-1, T=-2)
_TABLE = [
    Unit("-"), Unit("%"),
    Unit("s", 1, _T), Unit("ms", 1e-3, _T), Unit("min", 60, _T), Unit("h", 3600, _T), Unit("Hz", 1, _d(T=-1)),
    Unit("m", 1, _L), Unit("km", 1000, _L), Unit("mm", 1e-3, _L), Unit("cm", 1e-2, _L), Unit("mi", 1609.344, _L),
    Unit("ft", 0.3048, _L), Unit("in", 0.0254, _L),
    Unit("m/s", 1, _SPEED), Unit("km/h", 1 / 3.6, _SPEED), Unit("mph", 0.44704, _SPEED), Unit("mm/s", 1e-3, _SPEED),
    Unit("m/s^2", 1, _ACC), Unit("g", G0, _ACC), Unit("m/s^3", 1, _d(L=1, T=-3)), Unit("g/s", G0, _d(L=1, T=-3)),
    Unit("rad", 1, _A), Unit("deg", 0.017453292519943295, _A),
    Unit("rad/s", 1, _d(A=1, T=-1)), Unit("deg/s", 0.017453292519943295, _d(A=1, T=-1)),
    Unit("rad/s^2", 1, _d(A=1, T=-2)), Unit("deg/s^2", 0.017453292519943295, _d(A=1, T=-2)),
    Unit("rpm", 1 / 60, _d(R=1, T=-1)), Unit("rev", 1, _R),
    Unit("kg", 1, _M), Unit("N", 1, _d(M=1, L=1, T=-2)), Unit("kN", 1000, _d(M=1, L=1, T=-2)),
    Unit("Nm", 1, _d(M=1, L=2, T=-2)), Unit("kW", 1000, _d(M=1, L=2, T=-3)), Unit("W", 1, _d(M=1, L=2, T=-3)),
    Unit("Pa", 1, _PRESS), Unit("kPa", 1e3, _PRESS), Unit("bar", 1e5, _PRESS), Unit("psi", 6894.76, _PRESS),
    Unit("MPa", 1e6, _PRESS),
    Unit("K", 1, _K), Unit("°C", 1, _K, 273.15), Unit("°F", 5 / 9, _K, 255.3722222222222),
    Unit("m^2", 1, _d(L=2)), Unit("m^2/s^2", 1, _d(L=2, T=-2)), Unit("g^2", G0 * G0, _d(L=2, T=-4)),
    Unit("N/mm", 1000, _d(M=1, T=-2)),
]
_ALIASES = {"": "-", "ratio": "-", "1": "-", "sec": "s", "secs": "s", "kph": "km/h", "kmh": "km/h", "km/hr": "km/h",
            "m/s2": "m/s^2", "m/s²": "m/s^2", "m/s/s": "m/s^2", "ms^2": "m/s^2", "g's": "g", "°": "deg", "degrees": "deg",
            "°/s": "deg/s", "c": "°C", "degc": "°C", "celsius": "°C", "f": "°F", "degf": "°F", "kelvin": "K",
            "rad/s2": "rad/s^2", "deg/s2": "deg/s^2", "revs/min": "rpm", "1/min": "rpm", "hz": "Hz", "kpa": "kPa",
            "mpa": "MPa", "pa": "Pa", "nm": "Nm", "n": "N", "kw": "kW", "inch": "in", "mile": "mi", "miles": "mi",
            "percent": "%", "g^2": "g^2", "m2/s2": "m^2/s^2", "k": "K"}
_BY_NAME = {u.name: u for u in _TABLE}
_BY_LOWER = {u.name.lower(): u for u in _TABLE}


def parse_unit(text) -> Unit:
    """The unit a piece of text names. Text that names no known unit, and
    "unknown" itself, give UNKNOWN (keeping the text as its name)."""
    raw = (text or "").strip()
    if raw.lower() == "unknown":
        return UNKNOWN
    key = raw.replace(" ", "").replace("²", "^2")
    if key in _BY_NAME:
        return _BY_NAME[key]
    low = key.lower()
    if low in _ALIASES:
        return _BY_NAME[_ALIASES[low]]
    if low in _BY_LOWER and low not in ("k",):
        return _BY_LOWER[low]
    return Unit(raw, 1.0, None)


def _named(scale: float, dims: tuple) -> Unit:
    """A unit with this scale and these dimensions, by its usual name if it has one."""
    for u in _TABLE:
        if u.dims == dims and u.offset == 0 and abs(u.scale - scale) <= 1e-9 * max(abs(scale), 1e-300) and u.name != "%":
            return u
    names = ("m", "s", "kg", "K", "rad", "rev")
    top = "·".join(n if p == 1 else f"{n}^{p:g}" for n, p in zip(names, dims) if p > 0)
    bottom = "·".join(n if p == -1 else f"{n}^{-p:g}" for n, p in zip(names, dims) if p < 0)
    si = (top or "1") + (f"/{bottom}" if bottom else "")
    return Unit(si if abs(scale - 1) < 1e-12 else f"{scale:g}·{si}", scale, dims)


class UnitError(ValueError):
    pass


def same_kind(a: Unit, b: Unit, what: str) -> tuple[Unit, float, float]:
    """The unit of `a (+, -, compared with) b`, and the scale and offset that
    bring b's numbers into it."""
    if a is NUMBER or b is NUMBER:
        return (b if a is NUMBER else a), 1.0, 0.0
    if not a.known or not b.known:
        if not a.known and not b.known and a.name == b.name:
            return a, 1.0, 0.0
        which = a if not a.known else b
        raise UnitError(f"Cannot {what} '{a.name or '-'}' and '{b.name or '-'}': the unit '{which.name}' is not known. "
                        f"State it first, for example (x as km/h).")
    if a.dims != b.dims:
        raise UnitError(f"Cannot {what} {a.name} and {b.name}: they measure different things.")
    if a.scale == b.scale and a.offset == b.offset:
        return a, 1.0, 0.0
    return a, b.scale / a.scale, (b.offset - a.offset) / a.scale


def multiply(a: Unit, b: Unit, divide: bool = False) -> Unit:
    """The unit of a × b (or a ÷ b). A bare number leaves the other unit as it is."""
    if b is NUMBER:
        return a
    if a is NUMBER:
        if not divide:
            return b
        a = NO_UNIT
    if not a.known or not b.known:
        return UNKNOWN
    sign = -1 if divide else 1
    dims = tuple(x + sign * y for x, y in zip(a.dims, b.dims))
    return _named(a.scale / b.scale if divide else a.scale * b.scale, dims)


def power(a: Unit, exponent: float) -> Unit:
    if a is NUMBER or a.dimensionless:
        return a
    if not a.known:
        return UNKNOWN
    out = _named(a.scale ** exponent, tuple(x * exponent for x in a.dims))
    if out.name in _BY_NAME or not a.name:
        return out
    base = a.name if a.name.replace("°", "").isalnum() else f"({a.name})"      # (km/h)^2 reads better than 0.0772·m^2/s^2
    return Unit(f"{base}^{exponent:g}", out.scale, out.dims)


def convert(a: Unit, to: Unit) -> tuple[float, float]:
    """Scale and offset taking numbers in `a` to numbers in `to`."""
    if a is NUMBER:
        return 1.0, 0.0
    if not to.known:
        raise UnitError(f"'{to.name}' is not a unit this engine knows.")
    if not a.known:
        raise UnitError(f"Cannot convert to {to.name}: the value's unit ('{a.name}') is not known. "
                        f"Use 'as' to state a unit, 'in' to convert from a known one.")
    if a.dims != to.dims:
        raise UnitError(f"Cannot convert {a.name or '-'} to {to.name}: they measure different things.")
    return a.scale / to.scale, (a.offset - to.offset) / to.scale


def per_second(a: Unit, inverse: bool = False) -> Unit:
    """The unit of a time derivative (or, inverse, a time integral)."""
    if a is NUMBER:
        a = NO_UNIT
    return multiply(a, _BY_NAME["s"], divide=not inverse)
