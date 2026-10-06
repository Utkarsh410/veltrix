"""
Pacejka Magic Formula, pure slip.

General form:  y(x) = D sin(C atan(B x − E (B x − atan(B x)))) + Sv
with load-dependent B, D, E. Coefficient names follow the MF5.2 / PAC2002
convention (pCy1, pDy1, pKy1 ...).

The default coefficient sets are ILLUSTRATIVE: chosen to give a slick-like grip
level (peak friction about 1.55–1.60 at nominal load) and a wider rear tyre.
They are not fitted to tyre test data. Replace them with fitted sets before
drawing conclusions about a real car.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class TyreCoefficients:
    Fz0: float = 3500.0     # nominal load [N]
    # lateral
    pCy1: float = 1.30
    pDy1: float = 1.55
    pDy2: float = -0.15
    pEy1: float = -1.50
    pEy2: float = -0.80
    pKy1: float = 28.0
    pKy2: float = 1.60
    pKy3: float = 0.22
    pHy1: float = 0.003     # [deg]
    pHy2: float = 0.001
    pVy1: float = 0.0
    pVy2: float = 0.0
    # longitudinal
    pCx1: float = 1.65
    pDx1: float = 1.65
    pDx2: float = -0.12
    pEx1: float = 0.40
    pEx2: float = 0.0
    pEx3: float = 0.0
    pKx1: float = 30.0
    pKx2: float = -0.20
    pKx3: float = 0.10
    pHx1: float = 0.002
    pHx2: float = 0.001
    pVx1: float = 0.0
    pVx2: float = 0.0


FRONT = TyreCoefficients()
REAR = TyreCoefficients(pCy1=1.28, pDy1=1.60, pDy2=-0.14, pEy1=-1.30, pEy2=-0.70,
                        pKy1=38.0, pKy2=1.90, pKy3=0.20, pHy1=0.002,
                        pCx1=1.60, pDx1=1.70, pDx2=-0.11, pEx1=0.38,
                        pKx1=34.0, pKx2=-0.18, pKx3=0.09, pHx1=0.001)


def cornering_stiffness(Fz, c: TyreCoefficients = FRONT, camber_rad: float = 0.0):
    """dFy/dα at zero slip [N/rad] at vertical load Fz [N]. Degressive with load."""
    Fz = np.maximum(np.asarray(Fz, float), 0.0)
    return c.pKy1 * c.Fz0 * np.sin(2 * np.arctan(Fz / (c.pKy2 * c.Fz0))) * (1 - c.pKy3 * abs(camber_rad))


def lateral_force(alpha_deg, Fz, c: TyreCoefficients = FRONT, camber_rad: float = 0.0):
    """Lateral force Fy [N] at slip angle alpha [deg] and vertical load Fz [N]."""
    alpha = np.radians(np.asarray(alpha_deg, float))
    dfz = (Fz - c.Fz0) / c.Fz0
    C = c.pCy1
    D = (c.pDy1 + c.pDy2 * dfz) * Fz
    B = cornering_stiffness(Fz, c, camber_rad) / (C * D + np.finfo(float).eps)
    E = min(c.pEy1 + c.pEy2 * dfz, 1.0)
    x = alpha + np.radians(c.pHy1 + c.pHy2 * dfz)
    Sv = (c.pVy1 + c.pVy2 * dfz) * Fz
    return D * np.sin(C * np.arctan(B * x - E * (B * x - np.arctan(B * x)))) + Sv


def longitudinal_force(kappa, Fz, c: TyreCoefficients = FRONT):
    """Longitudinal force Fx [N] at slip ratio kappa [-] and vertical load Fz [N]."""
    kappa = np.asarray(kappa, float)
    dfz = (Fz - c.Fz0) / c.Fz0
    C = c.pCx1
    D = (c.pDx1 + c.pDx2 * dfz) * Fz
    K = (c.pKx1 + c.pKx2 * dfz) * Fz * np.exp(c.pKx3 * dfz)
    B = K / (C * D + np.finfo(float).eps)
    E = min(c.pEx1 + c.pEx2 * dfz + c.pEx3 * dfz ** 2, 1.0)
    x = kappa + c.pHx1 + c.pHx2 * dfz
    Sv = (c.pVx1 + c.pVx2 * dfz) * Fz
    return D * np.sin(C * np.arctan(B * x - E * (B * x - np.arctan(B * x)))) + Sv


def peak_lateral(Fz, c: TyreCoefficients = FRONT, alpha_max_deg: float = 14.0) -> dict:
    """Peak Fy, the slip angle it occurs at, and the friction coefficient at load Fz."""
    alpha = np.linspace(0.0, alpha_max_deg, 1401)
    fy = lateral_force(alpha, Fz, c)
    i = int(np.argmax(fy))
    return {"Fz": float(Fz), "Fy_peak": float(fy[i]), "alpha_peak_deg": float(alpha[i]),
            "mu": float(fy[i] / Fz),
            "cornering_stiffness_N_per_deg": float(cornering_stiffness(Fz, c) * np.pi / 180)}
