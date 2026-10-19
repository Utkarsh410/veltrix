"""Vehicle parameters used by the derived channels and the vehicle models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields

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
    # where each value came from: "user" (stated at import), "file" (read from the log, e.g. an
    # iRacing setup), or absent — the default above, which is a placeholder, not this car
    source: dict = field(default_factory=dict, compare=False)

    def set(self, name: str, value: float, source: str) -> None:
        setattr(self, name, float(value))
        self.source[name] = source

    def known(self, name: str) -> bool:
        """True when the value is this car's, not the placeholder default."""
        return self.source.get(name) in ("user", "file", "library")

    def origin(self, name: str) -> str:
        return {"user": "stated at import", "file": "from the file", "library": "from the car library"}.get(
            self.source.get(name), "default, not this car's")

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "source"} | {"source": dict(self.source)}
