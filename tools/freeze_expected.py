"""
Freeze the engine's outputs on the golden datasets into datasets/expected/.

The JSON files are the numerical contract of the engine: tests compare the
Python engine against them on every run, and any future implementation (the
Swift/Rust core) must reproduce them to count as numerically equal.

Re-run ONLY when a calculation is changed on purpose, and review the diff:

  python tools/freeze_expected.py
"""

import json
from pathlib import Path

import numpy as np

from veltrix_engine import load_session
from veltrix_engine import laps as lap_engine
from veltrix_engine.analysis import gg, tyres
from veltrix_engine.models import arb, pacejka

ROOT = Path(__file__).resolve().parents[1]
GOLDEN, EXPECTED = ROOT / "datasets" / "golden", ROOT / "datasets" / "expected"
CHECKPOINTS = 200          # evenly spaced samples stored per channel


def _plain(o):
    """numpy scalars -> plain Python for JSON."""
    return o.item() if hasattr(o, "item") else str(o)


def _round(x, nd=9):
    return None if x is None or not np.isfinite(x) else round(float(x), nd)


def channel_fingerprint(x) -> dict:
    """Summary statistics plus evenly spaced samples of one channel."""
    x = np.asarray(x, float)
    ok = np.isfinite(x)
    idx = np.linspace(0, len(x) - 1, CHECKPOINTS).round().astype(int)
    return {"n": int(len(x)), "n_valid": int(ok.sum()),
            "mean": _round(x[ok].mean()), "std": _round(x[ok].std()),
            "min": _round(x[ok].min()), "max": _round(x[ok].max()),
            "samples": [_round(v) for v in x[idx]]}


def session_expected(name: str) -> dict:
    s = load_session(GOLDEN / name)
    frame = s.frame()
    out = {
        "dataset": name, "source_sha256": s.raw.sha256,
        "rate_hz": s.rate_hz, "samples": len(frame), "lap_length": _round(s.lap_length, 6),
        "lap_source": s.lap_source, "crossing_method": s.crossing_method,
        "mapping": s.mapping, "units": s.units,
        "warnings": s.warnings(),
        "laps": [{"lap": r.lap, "t_start": _round(r.t_start), "t_end": _round(r.t_end),
                  "lap_time_s": _round(r.lap_time_s), "dist_m": _round(r.dist_m, 6),
                  "status": r.status} for r in s.laps.itertuples()],
        "channels": {c: channel_fingerprint(frame[c]) for c in frame.columns if c != "lap"},
    }
    if s.lap_length and len(s.laps) > 2:
        best = lap_engine.best_lap(s)
        out["best_lap"] = best
        sectors = lap_engine.sector_times(s)
        out["sectors"] = [{k: (_round(v) if isinstance(v, float) else v) for k, v in row.items()}
                          for row in sectors.to_dict("records")]
        out["theoretical_best"] = _round(lap_engine.theoretical_best(sectors))
        other = next(l for l in sectors["lap"] if l != best)
        cmp_ = lap_engine.compare_laps(s, best, other, channels=("speed",), step_m=1.0)
        loss, gain = lap_engine.biggest_swing(cmp_["delta_t"].to_numpy(), cmp_.index.to_numpy())
        out["delta"] = {"ref": best, "lap": other,
                        "delta_t": channel_fingerprint(cmp_["delta_t"]),
                        "biggest_loss": [_round(v) for v in loss],
                        "biggest_gain": [_round(v) for v in gain]}
    if "acc_lat" in frame:
        out["gg"] = {k: _round(v) if isinstance(v, float) else v for k, v in gg.gg_summary(s).items()}
        out["gg_phases"] = {k: _round(v) for k, v in gg.phase_split(s).items()}
    summary = tyres.lap_summary(s)
    if any(c.startswith("temp_") for c in summary.columns):
        out["tyre_lap_summary"] = [{k: (_round(v) if isinstance(v, float) else v) for k, v in row.items()}
                                   for row in summary.to_dict("records")]
        out["tyre_anomalies"] = [{"lap": int(e.lap), "corner": e.corner, "delta_C": _round(e.delta_C, 6)}
                                 for e in tyres.detect_anomalies(s).itertuples()]
    deg = tyres.fit_degradation(s)
    out["degradation"] = {"slope": _round(deg["slope"]), "intercept": _round(deg["intercept"]),
                          "r_squared": _round(deg["r_squared"]), "threshold_age": deg["threshold_age"]}
    return out


def models_expected() -> dict:
    loads = (2500.0, 3500.0, 4500.0, 5500.0)
    alpha = np.linspace(-14, 14, 57)
    kappa = np.linspace(-0.25, 0.25, 51)
    sweep = arb.sweep_front_arb(n=13)
    return {
        "pacejka": {
            axle: {"peaks": [{k: _round(v) for k, v in pacejka.peak_lateral(fz, c).items()} for fz in loads],
                   "Fy_3500": [_round(v) for v in pacejka.lateral_force(alpha, 3500.0, c)],
                   "Fx_3500": [_round(v) for v in pacejka.longitudinal_force(kappa, 3500.0, c)]}
            for axle, c in (("front", pacejka.FRONT), ("rear", pacejka.REAR))},
        "arb_sweep": [{k: _round(v) for k, v in row.items()} for row in sweep.to_dict("records")],
    }


if __name__ == "__main__":
    EXPECTED.mkdir(parents=True, exist_ok=True)
    for name in sorted(p.name for p in GOLDEN.iterdir() if ".csv" in p.name):
        stem = name.split(".csv")[0]
        (EXPECTED / f"{stem}.json").write_text(json.dumps(session_expected(name), indent=1, default=_plain) + "\n")
        print(f"  {stem}.json")
    (EXPECTED / "models.json").write_text(json.dumps(models_expected(), indent=1) + "\n")
    print("  models.json")
