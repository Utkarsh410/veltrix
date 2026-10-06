"""
Anti-roll bar sensitivity: steady-state bicycle model with lateral load
transfer and aerodynamic downforce.

  - LLTD_front = front roll stiffness / total roll stiffness
  - Tyre cornering stiffness is degressive with load, so an axle loses
    cornering stiffness as load is transferred across it: more front LLTD →
    less front axle stiffness → more understeer.
  - K_us = W_f / C_f − W_r / C_r   [deg/g]
    W: static axle weight [N] (downforce adds tyre load, not mass)
    C: axle cornering stiffness [N/deg], inner + outer tyre at their loaded Fz

Bar rates, downforce and aero balance are ASSUMED values. Replace them with
measured ones before quoting results.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..vehicle import G, VehicleParams
from .pacejka import FRONT, REAR, TyreCoefficients, cornering_stiffness


@dataclass
class RollSetup:
    k_wheel_front_N_m: float = 85000.0    # wheel rate in roll from the springs
    k_wheel_rear_N_m:  float = 70000.0
    arb_front_Nm_deg:  float = 700.0      # ARB contribution to axle roll stiffness
    arb_rear_Nm_deg:   float = 800.0
    air_density:       float = 1.2        # kg/m³
    ClA_m2:            float = 2.6        # downforce area
    aero_balance:      float = 0.42       # fraction of downforce on the front axle


def roll_stiffness(setup: RollSetup, vehicle: VehicleParams) -> tuple[float, float]:
    """Axle roll stiffness front, rear [N·m/deg]: springs (k·t²/2) plus bar."""
    k_f = setup.k_wheel_front_N_m * vehicle.track_front_m ** 2 / 2 * np.pi / 180 + setup.arb_front_Nm_deg
    k_r = setup.k_wheel_rear_N_m * vehicle.track_rear_m ** 2 / 2 * np.pi / 180 + setup.arb_rear_Nm_deg
    return k_f, k_r


def lltd_front(setup: RollSetup, vehicle: VehicleParams) -> float:
    k_f, k_r = roll_stiffness(setup, vehicle)
    return k_f / (k_f + k_r)


def understeer_gradient(setup: RollSetup, vehicle: VehicleParams, ay_g: float, speed_kmh: float,
                        front: TyreCoefficients = FRONT, rear: TyreCoefficients = REAR) -> dict:
    """K_us [deg/g] at one corner type (lateral g at a speed), with the axle
    cornering stiffnesses and the lowest inside-wheel load (≤ 0 means wheel
    lift: the result is not valid)."""
    w_f = vehicle.mass_kg * G * vehicle.front_weight_dist
    w_r = vehicle.mass_kg * G * (1 - vehicle.front_weight_dist)
    f_aero = 0.5 * setup.air_density * setup.ClA_m2 * (speed_kmh / 3.6) ** 2
    fz_f = (w_f + setup.aero_balance * f_aero) / 2
    fz_r = (w_r + (1 - setup.aero_balance) * f_aero) / 2

    share = lltd_front(setup, vehicle)
    moment = vehicle.mass_kg * ay_g * G * vehicle.cg_height_m
    d_f = share * moment / vehicle.track_front_m
    d_r = (1 - share) * moment / vehicle.track_rear_m

    to_deg = np.pi / 180
    c_f = (cornering_stiffness(fz_f + d_f, front) + cornering_stiffness(fz_f - d_f, front)) * to_deg
    c_r = (cornering_stiffness(fz_r + d_r, rear) + cornering_stiffness(fz_r - d_r, rear)) * to_deg
    return {"K_us": float(w_f / c_f - w_r / c_r), "C_front": float(c_f), "C_rear": float(c_r),
            "lltd_front": float(share), "min_inside_Fz": float(min(fz_f - d_f, fz_r - d_r))}


DEFAULT_CORNERS = ((1.0, 90.0), (1.4, 140.0), (1.7, 190.0), (1.9, 230.0))   # (ay g, speed km/h)


def sweep_front_arb(setup: RollSetup | None = None, vehicle: VehicleParams | None = None,
                    span: float = 0.6, n: int = 50, corners=DEFAULT_CORNERS) -> pd.DataFrame:
    """Sweep the front bar ±`span` around its baseline with the rear fixed.
    One row per (front ARB, corner type)."""
    setup, vehicle = setup or RollSetup(), vehicle or VehicleParams()
    rows = []
    for arb in np.linspace(setup.arb_front_Nm_deg * (1 - span), setup.arb_front_Nm_deg * (1 + span), n):
        s = RollSetup(**{**setup.__dict__, "arb_front_Nm_deg": float(arb)})
        for ay, v in corners:
            rows.append({"arb_front_Nm_deg": float(arb), "ay_g": ay, "speed_kmh": v,
                         **understeer_gradient(s, vehicle, ay, v)})
    return pd.DataFrame(rows)
