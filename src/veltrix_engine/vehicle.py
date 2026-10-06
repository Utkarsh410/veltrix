"""Vehicle parameters used by the derived channels and the vehicle models."""

from __future__ import annotations

from dataclasses import asdict, dataclass

G = 9.81


@dataclass
class VehicleParams:
    """Physical constants for the car under analysis (defaults: GT3 proxy)."""
    wheelbase_m:       float = 2.620
    track_front_m:     float = 1.640
    track_rear_m:      float = 1.620
    mass_kg:           float = 1350.0    # including driver
    cg_height_m:       float = 0.440
    front_weight_dist: float = 0.415     # fraction of mass on the front axle
    steer_ratio:       float = 14.0      # steering wheel angle : road wheel angle
    wheel_rate_front_N_mm: float = 180.0  # roll wheel rate (spring + ARB), per wheel
    wheel_rate_rear_N_mm:  float = 160.0
    grip_limit_g:      float = 2.0       # reference combined-g envelope for utilisation

    def to_dict(self) -> dict:
        return asdict(self)
