"""
The channels the engine derives, written as maths channels — so an engineer can
read exactly how each is made, and change it.

`standard_set(vehicle)` is the set, with the car's parameters as $parameters.
The engine computes the same channels in `derived.py` (faster, and with two
things a formula does not do: a speed channel recorded in coarse steps is
smoothed harder first, and jumps in speed beyond MAX_LONG_G are bridged). The
two agree on every golden session (tests/test_maths_builtin.py).

To change one, copy it into your own maths channels under the same name and
edit it there: a maths channel replaces the engine's channel of that name
everywhere the analysis reads it.
"""

from __future__ import annotations

from ..vehicle import VehicleParams
from . import MathChannel, MathSet

F_LAT = "lowpass(acc_lat, 15, order=4, cap=1, uniform=1)"

CHANNELS = {
    "acc_long_calc": ("lowpass(derivative(speed in m/s) in g, 15, order=4, cap=1, uniform=1)",
                      "Longitudinal acceleration from d(speed)/dt, 15 Hz low-pass. With speed recorded in coarse "
                      "steps the engine smooths speed at 2 Hz first; speed jumps beyond 6 g are bridged."),
    "steering_rate": ("lowpass(derivative(steering), 10, order=4, cap=1, uniform=1)", "Steering speed, 10 Hz low-pass"),
    "acc_lat_calc": ("clip(lowpass(((speed in m/s)^2 * ((steering in rad) / (1 as rad)) / $steer_ratio / ($wheelbase_m as m)) in g, "
                     "15, order=4, cap=1, uniform=1), -4, 4)",
                     "Lateral acceleration a neutral-steer car would make for the steering applied: V² δ / (g L)"),
    "yaw_rate_est": ("lowpass(if(speed > 18, (acc_lat in m/s^2) / (speed in m/s), 0) * (1 as rad), 15, order=4, cap=1, uniform=1)",
                     "Steady-state yaw rate a·g / V (0 below 18 km/h, where it is undefined)"),
    "oversteer_metric": (f"lowpass(if(abs({F_LAT}) > 0.3, abs({F_LAT}) - abs(acc_lat_calc), 0), 5, order=4, cap=1, uniform=1)",
                         "Lateral g beyond what the steering asks for: + oversteer, − understeer (above 0.3 g)"),
    "acc_lat_sustained": ("rolling_median(acc_lat, 0.25, edges=1)",
                          "Lateral g held for a quarter of a second: what the tyres sustain, without kerb jolts"),
    "body_slip": ("when(vel_x > 5, atan2(vel_y, vel_x) in deg)",
                  "Body slip angle from the car-frame velocities, above 5 m/s"),
    "llt_front": ("(($wheel_rate_front_N_mm as N/mm) * (lowpass(susp_travel_fr, 10, order=4, cap=1, uniform=1) - "
                  "lowpass(susp_travel_fl, 10, order=4, cap=1, uniform=1)) / 2) in N",
                  "Front lateral load transfer from damper roll: wheel rate × (right − left travel) / 2"),
    "llt_rear": ("(($wheel_rate_rear_N_mm as N/mm) * (lowpass(susp_travel_rr, 10, order=4, cap=1, uniform=1) - "
                 "lowpass(susp_travel_rl, 10, order=4, cap=1, uniform=1)) / 2) in N", "Rear lateral load transfer, as the front"),
    "lltd_front": ("when(abs(acc_lat) > 0.5 and abs(llt_front) + abs(llt_rear) > 1, "
                   "abs(llt_front) / (abs(llt_front) + abs(llt_rear)))",
                   "Front share of lateral load transfer, above 0.5 g"),
    "grip_util": (f"hypot({F_LAT}, acc_long_calc) / ($grip_limit_g as g)",
                  "Combined g as a share of the car's reference grip envelope"),
}

# derived channels no formula can express, and why
NOT_A_FORMULA = {
    "acc_lat_rebuilt": "rebuilt from damper roll by a fit against the logged channel, only when lateral g is "
                       "recorded too coarsely — a calibration, not a formula",
}

PARAMETERS = ("steer_ratio", "wheelbase_m", "wheel_rate_front_N_mm", "wheel_rate_rear_N_mm", "grip_limit_g")


def standard_set(vehicle: VehicleParams | None = None) -> MathSet:
    """The engine's derived channels as a maths set, with this car's parameters."""
    vehicle = vehicle or VehicleParams()
    ms = MathSet()
    ms.parameters = {name: float(getattr(vehicle, name)) for name in PARAMETERS}
    for name, (formula, description) in CHANNELS.items():
        ms.channels[name] = MathChannel(name, formula, description=description, category="built-in")
    return ms
