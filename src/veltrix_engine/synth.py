"""
Synthetic GT3 session generator.

A quasi-steady-state point-mass lap simulation, so the channels are physically
consistent with each other and the analysis has known answers to recover:

  - a circuit defined as corners (apex position, radius, direction)
  - corner speed limited by a speed-dependent (aero) lateral grip limit
  - forward / backward passes for traction, power, drag and braking limits
    inside a friction ellipse
  - steering from the bicycle model plus an understeer gradient
  - tyre temperatures from a first-order thermal model carried across laps
  - tyre degradation as a per-lap grip reduction; lap time loss emerges from
    the simulation

One event is injected: a front-left lock-up on `lockup_lap`.

Column names and units deliberately follow a logger-style export (Speed in
km/h, tyre pressure in psi with no units row), not the engine's schema, so the
generated file exercises channel mapping and unit handling.

Sign convention: lateral g and steering +ve in left-hand corners,
longitudinal g +ve under drive.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.signal import lfilter

from .vehicle import VehicleParams

# ── constants ──────────────────────────────────────────────────────────────
SAMPLE_RATE_HZ  = 100
LAP_COUNT       = 10
TRACK_LEN_M     = 3900      # Brands Hatch GP-style layout (proxy, not surveyed)
SEED            = 42
G               = 9.81

# (apex position m, radius m, direction: +1 = left, −1 = right)
CORNERS = [
    ( 260,  75, -1),
    ( 640,  30, -1),   # hairpin
    ( 900,  55, +1),
    (1250,  95, +1),
    (1950, 170, -1),
    (2350, 115, -1),
    (2800, 150, -1),
    (3080,  52, +1),
    (3550,  88, -1),
]
CORNER_SIGMA_M  = 38.0      # curvature bump width → corner length

# Car performance
V_MAX_MS        = 78.0      # gearing-limited top speed
POWER_W         = 390e3     # ~525 hp
CDA_M2          = 0.95      # drag area
TRACTION_G      = 0.80      # RWD traction limit at zero lateral load
BRAKE_FRACTION  = 0.80      # braking limit as a fraction of the lateral limit
DRIVER_USAGE    = 0.97      # fraction of the grip envelope the driver uses

# Balance / degradation
LLTD_FRONT      = 0.53      # true lateral load transfer distribution
KUS_BASE_DEG_G  = 0.08      # understeer gradient at the road wheels, lap 1
KUS_DEG_PER_LAP = 0.012     # front tyres go off → understeer builds
GRIP_LOSS_PER_LAP = 0.004   # 0.4 % grip per lap

# Tyre thermal model
T_START_C       = 70.0      # leaving the pits (blanket heat partly lost)
T_BASE_C        = 55.0      # equilibrium with no work input
HEAT_GAIN_C     = 60.0      # °C per unit of normalised tyre work
TAU_BULK_S      = 45.0
LOCKUP_LAP      = 6

GEAR_TOP_KMH    = np.array([85, 120, 155, 190, 225, 300])


def _lat_limit_g(v: np.ndarray, mu: float) -> np.ndarray:
    """Lateral grip limit in g: mechanical grip plus downforce (∝ v²)."""
    return mu * (1.50 + 0.55 * (v / 70.0) ** 2)


def _curvature(s: np.ndarray) -> np.ndarray:
    """Signed path curvature (1/m) along the lap, periodic in TRACK_LEN_M."""
    kappa = np.zeros_like(s)
    for s_apex, radius, direction in CORNERS:
        d = (s - s_apex + TRACK_LEN_M / 2) % TRACK_LEN_M - TRACK_LEN_M / 2
        kappa += direction / radius * np.exp(-0.5 * (d / CORNER_SIGMA_M) ** 2)
    return kappa


def _solve_speed_profile(mu: float, p: VehicleParams) -> tuple:
    """Quasi-steady-state point-mass lap sim on a 1 m grid. Returns s, v, kappa."""
    ds    = 1.0
    s     = np.arange(0.0, TRACK_LEN_M, ds)
    kappa = _curvature(s)
    n     = len(s)
    mu_d  = mu * DRIVER_USAGE

    # Cornering limit: v²|κ| = g · lat_limit(v), solved in closed form
    aero  = G * mu_d * 0.55 / 70.0 ** 2
    denom = np.abs(kappa) - aero
    with np.errstate(divide="ignore", invalid="ignore"):
        v_lim = np.where(denom > 1e-6, np.sqrt(G * mu_d * 1.50 / denom), V_MAX_MS)
    v_lim = np.minimum(v_lim, V_MAX_MS)

    def lat_fraction(v, k):
        return min(v * v * abs(k) / (G * _lat_limit_g(v, mu_d)), 1.0)

    drag_g = lambda v: 0.5 * 1.2 * CDA_M2 * v * v / (p.mass_kg * G)

    # Forward pass — acceleration (traction ellipse, then power, minus drag)
    start = int(np.argmin(v_lim))
    v = v_lim.copy()
    for step in range(2 * n):
        i, j = (start + step) % n, (start + step + 1) % n
        grip = TRACTION_G * mu_d * np.sqrt(1.0 - lat_fraction(v[i], kappa[i]) ** 2)
        ax   = min(grip, POWER_W / (p.mass_kg * v[i] * G)) - drag_g(v[i])
        v[j] = min(v[j], np.sqrt(max(v[i] ** 2 + 2 * ax * G * ds, 1.0)))

    # Backward pass — braking (braking ellipse, drag helps)
    for step in range(2 * n):
        j, i = (start - step) % n, (start - step - 1) % n
        grip = BRAKE_FRACTION * _lat_limit_g(v[j], mu_d) * np.sqrt(1.0 - lat_fraction(v[j], kappa[j]) ** 2)
        v[i] = min(v[i], np.sqrt(v[j] ** 2 + 2 * (grip + drag_g(v[j])) * G * ds))

    return s, v, kappa


def make_lap(lap_num: int, rng: np.random.Generator, tyre_state: dict,
             p: VehicleParams | None = None, lockup_lap: int = LOCKUP_LAP) -> pd.DataFrame:
    """Build one lap of synthetic telemetry. tyre_state carries bulk tyre
    temperatures from the previous lap and is updated in place."""
    p   = p or VehicleParams()
    fs  = SAMPLE_RATE_HZ
    dt  = 1.0 / fs

    # Tyre degradation: grip falls each lap, with small lap-to-lap scatter
    mu = 1.0 - GRIP_LOSS_PER_LAP * (lap_num - 1) + rng.normal(0, 0.0008)
    s, v_s, kappa_s = _solve_speed_profile(mu, p)

    # Distance domain → uniform time base
    t_s      = np.concatenate([[0.0], np.cumsum(1.0 / v_s)])     # ds = 1 m
    s_ext    = np.append(s, TRACK_LEN_M)
    n        = int(round(t_s[-1] * fs))
    lap_time = n * dt
    t        = np.arange(n) * dt
    dist     = np.interp(t, t_s, s_ext)
    speed_ms = np.interp(dist, s_ext, np.append(v_s, v_s[0]))
    kappa    = np.interp(dist, s_ext, np.append(kappa_s, kappa_s[0]))

    lat_g  = speed_ms ** 2 * kappa / G
    lon_g  = np.gradient(speed_ms, dt) / G
    drag_g = 0.5 * 1.2 * CDA_M2 * speed_ms ** 2 / (p.mass_kg * G)

    # ── driver inputs ──────────────────────────────────────────────────────
    engine_g = POWER_W / (p.mass_kg * np.maximum(speed_ms, 5.0) * G)
    throttle = np.clip((lon_g + drag_g) / np.minimum(engine_g, TRACTION_G), 0, 1) * 100
    brake    = np.clip(-(lon_g + drag_g), 0, None) * 55.0             # bar
    throttle = np.where(brake > 1.0, 0.0, throttle)                   # no overlap

    # Bicycle model + understeer gradient: δ = L·κ + K_us·ay
    kus_rad    = np.radians(KUS_BASE_DEG_G + KUS_DEG_PER_LAP * (lap_num - 1))
    road_wheel = p.wheelbase_m * kappa + kus_rad * lat_g              # rad
    steering   = np.degrees(road_wheel) * p.steer_ratio               # deg at the wheel

    speed_kmh = speed_ms * 3.6
    gear      = np.searchsorted(GEAR_TOP_KMH, speed_kmh) + 1
    rpm       = np.clip(7600 * speed_kmh / GEAR_TOP_KMH[gear - 1], 3200, 7800)

    # ── suspension travel (mm): roll from load transfer + aero heave ───────
    t_mean  = 0.5 * (p.track_front_m + p.track_rear_m)
    llt_tot = p.mass_kg * lat_g * G * p.cg_height_m / t_mean          # N, +ve left turn
    roll_f  = LLTD_FRONT * llt_tot / p.wheel_rate_front_N_mm          # mm
    roll_r  = (1 - LLTD_FRONT) * llt_tot / p.wheel_rate_rear_N_mm
    heave   = 4.0 * (speed_ms / 70.0) ** 2
    susp = {                                   # left turn → right side compresses
        "SuspTravelFL": 45 + heave - roll_f,
        "SuspTravelFR": 45 + heave + roll_f,
        "SuspTravelRL": 42 + 1.3 * heave - roll_r,
        "SuspTravelRR": 42 + 1.3 * heave + roll_r,
    }
    susp = {k: x + rng.normal(0, 0.3, n) for k, x in susp.items()}

    # ── tyre temperatures: first-order thermal model per corner ────────────
    # Work input ∝ sliding speed × force. Outside tyres carry more load;
    # fronts take braking, rears take traction.
    alpha     = dt / TAU_BULK_S
    wear_heat = 1.0 + 0.012 * (lap_num - 1)        # more sliding as grip drops
    temps = {}
    for corner in ("FL", "FR", "RL", "RR"):
        left  = corner[1] == "L"
        front = corner[0] == "F"
        load  = np.clip(1.0 + (-0.30 if left else 0.30) * lat_g, 0.3, None)
        lon_w = np.where(lon_g < 0, 1.0 if front else 0.45, 0.15 if front else 0.9)
        work  = load * (np.abs(lat_g) + lon_w * np.abs(lon_g)) * speed_ms / 50.0
        t_eq  = T_BASE_C + HEAT_GAIN_C * wear_heat * work * (1.0 if front else 0.94)

        t0 = tyre_state[corner]
        bulk, _ = lfilter([alpha], [1.0, -(1.0 - alpha)], t_eq, zi=[(1.0 - alpha) * t0])
        tyre_state[corner] = bulk[-1]

        # Fast surface component on top of the bulk temperature
        a_s = dt / 2.0
        surf, _ = lfilter([a_s], [1.0, -(1.0 - a_s)], work, zi=[(1.0 - a_s) * work[0]])
        mid = bulk + 5.0 * (surf - work.mean())
        temps[corner] = {"inner": mid + 5.0, "mid": mid.copy(), "outer": mid - 5.0}

    if lap_num == lockup_lap:
        # Front-left lock-up in the heaviest braking zone: fast rise, slow decay
        k0    = int(np.argmin(lon_g))
        tt    = t[k0:] - t[k0]
        spike = (1 - np.exp(-tt / 0.12)) * np.exp(-tt / 4.0)
        temps["FL"]["inner"][k0:] += 16.0 * spike
        temps["FL"]["mid"][k0:]   += 10.0 * spike

    temp_cols, pres_cols = {}, {}
    for corner, zones in temps.items():
        for zone, x in zones.items():
            temp_cols[f"TyreTemp{corner}_{zone}"] = x + rng.normal(0, 0.4, n)
        # Pressure follows bulk temperature (ideal gas, 25.5 psi at 70 °C)
        pres_cols[f"TyrePress{corner}"] = (
            25.5 * (zones["mid"] + 273.15) / (70.0 + 273.15) + rng.normal(0, 0.03, n)
        )

    # ── assemble (sensor noise added to the measured channels) ─────────────
    df = pd.DataFrame({
        "Time":           t,
        "LapNumber":      lap_num,
        "Speed":          speed_kmh + rng.normal(0, 0.05, n),
        "RPM":            rpm + rng.normal(0, 40, n),
        "Gear":           gear,
        "ThrottlePos":    throttle,         # 0–100 %
        "BrakePress":     brake,            # bar
        "SteeringAngle":  steering + rng.normal(0, 0.4, n),   # degrees at the wheel
        "LateralAcc":     lat_g + rng.normal(0, 0.03, n),
        "LongAcc":        lon_g + rng.normal(0, 0.03, n),
        "VertAcc":        rng.normal(1.0, 0.08, n),
        **temp_cols,
        **pres_cols,
        **susp,
        "LapTime":        lap_time,
    })

    return df


def generate_session(lap_count: int = LAP_COUNT, seed: int = SEED,
                     lockup_lap: int = LOCKUP_LAP) -> pd.DataFrame:
    """A session of `lap_count` flying laps as one DataFrame."""
    rng        = np.random.default_rng(seed)
    tyre_state = {c: T_START_C for c in ("FL", "FR", "RL", "RR")}
    laps, clock = [], 0.0
    for lap in range(1, lap_count + 1):
        df = make_lap(lap, rng, tyre_state, lockup_lap=lockup_lap)
        df["Time"] += clock
        clock += len(df) / SAMPLE_RATE_HZ
        laps.append(df)
    return pd.concat(laps, ignore_index=True)
