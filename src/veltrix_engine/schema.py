"""
Canonical telemetry schema.

Every importer maps its source columns onto these channel ids, and every
analysis module reads and writes only these ids. The schema fixes, per channel:
the id, the unit, the sign convention and the names it is known by in logger
exports.

Conventions (ISO 8855 vehicle axes: x forward, y left, z up):
  - acc_long  +ve accelerating, -ve braking
  - acc_lat   +ve in left-hand corners
  - steering  +ve steering left, degrees at the steering wheel
  - susp_travel_*  +ve in compression (bump)
  - pos_x / pos_y  metres in a local, flat track plane; orientation is arbitrary
  - corners are named fl, fr, rl, rr (front/rear, left/right as seen by the driver)

Channel kinds:
  - measured: comes from the logger (after unit conversion and resampling)
  - derived:  computed by the engine from measured channels
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CORNERS = ("fl", "fr", "rl", "rr")
ZONES = ("inner", "mid", "outer")

# Unit tables: {unit as written in a file (lower-case, no spaces): factor to canonical}
UNIT_FACTORS = {
    "time":     {"s": 1, "sec": 1, "secs": 1, "ms": 0.001},
    "distance": {"m": 1, "km": 1000, "mi": 1609.344, "mile": 1609.344, "miles": 1609.344, "ft": 0.3048},
    "speed":    {"km/h": 1, "kph": 1, "kmh": 1, "km/hr": 1, "mph": 1.609344, "m/s": 3.6,
                 "kn": 1.852, "kt": 1.852, "knots": 1.852},
    "accel":    {"g": 1, "m/s^2": 1 / 9.81, "m/s2": 1 / 9.81, "m/s/s": 1 / 9.81, "ms^2": 1 / 9.81},
    "angle":    {"deg": 1, "°": 1, "degrees": 1, "rad": 57.29577951308232},
    "pressure": {"bar": 1, "psi": 0.0689476, "kpa": 0.01, "mpa": 10},
    "length":   {"mm": 1, "cm": 10, "m": 1000, "in": 25.4, "inch": 25.4},
    "position": {"m": 1, "km": 1000, "ft": 0.3048},
}
CANONICAL_UNIT = {"time": "s", "distance": "m", "speed": "km/h", "accel": "g", "angle": "deg",
                  "pressure": "bar", "length": "mm", "position": "m", "temperature": "°C",
                  "percent": "%"}


@dataclass(frozen=True)
class ChannelDef:
    id: str
    unit: str
    description: str
    quantity: str = ""            # key into UNIT_FACTORS / special handling; "" = no conversion
    kind: str = "measured"        # "measured" | "derived"
    sign: str = ""                # sign convention, where one applies
    discrete: bool = False        # step-valued: held, never interpolated
    aliases: tuple = ()           # names as normalised words, best first
    exclude: frozenset = field(default_factory=frozenset)   # words that rule a column out
    requires: tuple = ()          # derived channels: the channel ids they are computed from


def norm_name(name) -> str:
    """Lower-case words: "LateralAcc" -> "lateral acc", "Speed (km/h)" -> "speed km h",
    "TyreTempFL_inner" -> "tyre temp fl inner"."""
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(name))
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", name)
    return " ".join(re.findall(r"[a-z0-9]+", name.lower()))


_CORNER_NAMES = {
    "fl": ("fl", "lf", "front left"),
    "fr": ("fr", "rf", "front right"),
    "rl": ("rl", "lr", "rear left"),
    "rr": ("rr", "rear right"),
}
_ZONE_NAMES = {
    "inner": ("inner", "inside", "in", "i"),
    "mid":   ("mid", "middle", "centre", "center", "m"),
    "outer": ("outer", "outside", "out", "o"),
}
_TYRE_BASES = ("tyre temp", "tire temp", "tyre temperature", "tire temperature", "wheel temp", "ttyre")
_ALL_ZONE_WORDS = frozenset(w for names in _ZONE_NAMES.values() for w in names)
_TYRE_TEMP_EXCLUDE = frozenset({"pressure", "press", "pres", "psi", "bar", "wear", "load", "slip",
                                "speed", "height", "brake", "air"})


def _corner_aliases(bases, corner, suffixes=("",)):
    names = []
    for base in bases:
        for c in _CORNER_NAMES[corner]:
            for s in suffixes:
                names.append(" ".join(p for p in (base, c, s) if p))
                names.append(" ".join(p for p in (c, base, s) if p))
    return tuple(dict.fromkeys(names))


def _build() -> dict:
    defs = [
        ChannelDef("time", "s", "Session clock", "time",
                   aliases=("time", "session time", "elapsed time", "time s", "timestamp", "log time")),
        ChannelDef("distance", "m", "Distance travelled; cumulative over the session", "distance",
                   aliases=("distance", "lap distance", "lap dist", "dist", "distance m", "odometer"),
                   exclude=frozenset({"gap", "to", "brake", "braking"})),
        ChannelDef("lap", "-", "Lap number as logged", discrete=True,
                   aliases=("lap", "lap number", "lap num", "lap no", "lap count", "lap index", "laps"),
                   exclude=frozenset({"time", "distance", "dist", "flag", "valid", "delta", "gain",
                                      "beacon", "marker", "trigger"})),
        ChannelDef("speed", "km/h", "Vehicle ground speed", "speed",
                   aliases=("speed", "ground speed", "vehicle speed", "gps speed", "car speed",
                            "speed over ground", "corr speed", "vcar", "v car"),
                   exclude=frozenset({"wheel", "shaft", "fan", "limit", "target", "engine", "max", "min",
                                      "steering", "steer", "damper", "shock", "wind"})),
        ChannelDef("rpm", "rpm", "Engine speed",
                   aliases=("rpm", "engine rpm", "engine speed", "eng rpm", "revs", "rpms"),
                   exclude=frozenset({"limit", "target", "max", "turbo", "wheel", "fan", "pump"})),
        ChannelDef("gear", "-", "Selected gear", discrete=True,
                   aliases=("gear", "gear pos", "gear position", "gear no"),
                   exclude=frozenset({"ratio", "temp", "oil", "shift", "request", "target"})),
        ChannelDef("throttle", "%", "Throttle pedal position, 0-100", "percent",
                   aliases=("throttle", "throttle pos", "throttle position", "throttle pedal", "tps",
                            "accel pedal", "pedal pos", "aps", "throttle application", "t app"),
                   exclude=frozenset({"temp", "target", "aim", "blip"})),
        ChannelDef("brake", "% or bar", "Brake pedal position (%) or line pressure (bar); "
                   "the session records which", "brake",
                   aliases=("brake", "brake pres front", "brake press front", "brake pressure front",
                            "brake front", "brake pressure", "brake pres", "brake press", "brake pos",
                            "brake pedal", "brake position", "b pres"),
                   exclude=frozenset({"temp", "temperature", "bias", "balance", "bal", "wear", "disc",
                                      "duct", "switch", "light", "fluid"})),
        ChannelDef("steering", "deg", "Steering wheel angle", "angle", sign="+ve steering left",
                   aliases=("steering", "steering angle", "steered angle", "steer angle",
                            "steering wheel angle", "steer", "swa"),
                   exclude=frozenset({"torque", "rate", "speed", "velocity", "trim"})),
        ChannelDef("acc_lat", "g", "Lateral acceleration", "accel", sign="+ve in left-hand corners",
                   aliases=("lat accel", "lateral accel", "lateral acceleration", "lateral g",
                            "g force lat", "gforce lat", "lat g", "lateral acc", "acc lat", "accel lat",
                            "g lat", "ay", "lat acc"),
                   exclude=frozenset({"offset"})),
        ChannelDef("acc_long", "g", "Longitudinal acceleration", "accel", sign="+ve accelerating",
                   aliases=("long accel", "longitudinal accel", "longitudinal acceleration",
                            "longitudinal g", "g force long", "gforce long", "g force lon", "gforce lon",
                            "long g", "inline acc", "inline accel", "acc long", "accel long", "accel lon",
                            "g long", "g lon", "ax", "long acc", "lon acc", "lon g"),
                   exclude=frozenset({"offset"})),
        ChannelDef("acc_vert", "g", "Vertical acceleration", "accel", sign="+ve upward",
                   aliases=("vert accel", "vertical accel", "vertical acceleration", "g force vert",
                            "vert g", "vert acc", "accel vert", "az"),
                   exclude=frozenset({"offset"})),
        ChannelDef("pos_x", "m", "Position in the local track plane, x", "position",
                   aliases=("pos x", "position x", "world position x", "x pos", "gps x")),
        ChannelDef("pos_y", "m", "Position in the local track plane, y", "position",
                   aliases=("pos y", "position y", "y pos", "gps y")),
        ChannelDef("gps_lat", "deg", "GPS latitude",
                   aliases=("gps latitude", "latitude", "gps lat", "lat"),
                   exclude=frozenset({"accel", "acc", "g", "force", "gforce", "slip", "vel", "velocity"})),
        ChannelDef("gps_lon", "deg", "GPS longitude",
                   aliases=("gps longitude", "longitude", "gps lon", "gps long", "lon", "long", "lng"),
                   exclude=frozenset({"accel", "acc", "g", "force", "gforce", "slip", "vel", "velocity"})),
    ]
    for c in CORNERS:
        defs.append(ChannelDef(
            f"tyre_temp_{c}", "°C", f"Tyre temperature {c.upper()}, single sensor", "temperature",
            aliases=_corner_aliases(_TYRE_BASES + ("tyre", "tire"), c),
            exclude=_TYRE_TEMP_EXCLUDE | _ALL_ZONE_WORDS))
        for z in ZONES:
            defs.append(ChannelDef(
                f"tyre_temp_{c}_{z}", "°C", f"Tyre surface temperature {c.upper()}, {z} zone",
                "temperature", aliases=_corner_aliases(_TYRE_BASES, c, _ZONE_NAMES[z]),
                exclude=_TYRE_TEMP_EXCLUDE))
        defs.append(ChannelDef(
            f"tyre_press_{c}", "bar", f"Tyre pressure {c.upper()}", "pressure",
            aliases=_corner_aliases(("tyre press", "tire press", "tyre pressure", "tire pressure",
                                     "tyre pres", "tire pres"), c),
            exclude=frozenset({"temp", "temperature"})))
        defs.append(ChannelDef(
            f"susp_travel_{c}", "mm", f"Suspension travel {c.upper()}", "length",
            sign="+ve in compression",
            aliases=_corner_aliases(("susp travel", "suspension travel", "sus travel", "damper pos",
                                     "damper position", "susp pos", "suspension pos"), c),
            exclude=frozenset({"speed", "velocity", "vel", "rate"})))

    derived = [
        ChannelDef("lap_distance", "m", "Distance from the start/finish line, scaled to the common "
                   "lap length", kind="derived", requires=("time",)),
        ChannelDef("acc_long_calc", "g", "Longitudinal acceleration from d(speed)/dt, 15 Hz low-pass",
                   kind="derived", sign="+ve accelerating", requires=("speed",)),
        ChannelDef("acc_lat_calc", "g", "Lateral acceleration a neutral-steer car would make for the "
                   "steering applied: V² δ / (g L)", kind="derived", sign="+ve in left-hand corners",
                   requires=("speed", "steering")),
        ChannelDef("steering_rate", "deg/s", "d(steering)/dt, 10 Hz low-pass", kind="derived",
                   requires=("steering",)),
        ChannelDef("yaw_rate_est", "rad/s", "Steady-state yaw rate estimate: acc_lat · g / V",
                   kind="derived", sign="+ve turning left", requires=("speed", "acc_lat")),
        ChannelDef("oversteer_metric", "g", "|acc_lat| − |acc_lat_calc| while cornering (>0.3 g)",
                   kind="derived", sign="+ve oversteer, -ve understeer",
                   requires=("speed", "steering", "acc_lat")),
        ChannelDef("llt_front", "N", "Front axle lateral load transfer", kind="derived",
                   sign="+ve in left-hand corners", requires=("acc_lat",)),
        ChannelDef("llt_rear", "N", "Rear axle lateral load transfer", kind="derived",
                   sign="+ve in left-hand corners", requires=("acc_lat",)),
        ChannelDef("lltd_front", "-", "Front share of lateral load transfer, above 0.5 g "
                   "(needs damper travel to carry information)", kind="derived",
                   requires=("acc_lat",)),
        ChannelDef("grip_util", "-", "Combined g as a fraction of the reference envelope",
                   kind="derived", requires=("speed", "acc_lat")),
    ]
    return {d.id: d for d in defs + derived}


CHANNELS: dict[str, ChannelDef] = _build()
MEASURED = [c for c, d in CHANNELS.items() if d.kind == "measured"]
DERIVED = [c for c, d in CHANNELS.items() if d.kind == "derived"]


def tyre_temp_channel(columns, corner: str) -> str | None:
    """The channel that best represents one corner's tyre temperature:
    the mid zone when there are three zones, otherwise the single sensor."""
    for cid in (f"tyre_temp_{corner}_mid", f"tyre_temp_{corner}"):
        if cid in columns:
            return cid
    return None
