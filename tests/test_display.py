"""Display decimation: fewer points, same picture — extremes kept, gaps kept, real samples on zoom."""

import numpy as np

from veltrix_engine.display import decimate


def signal(n=200_000):
    t = np.arange(n) / 200.0
    v = np.sin(t) + 0.01 * np.random.default_rng(0).standard_normal(n)
    return t, v


def test_extremes_survive_at_any_zoom():
    t, v = signal()
    v[123_456] = 50.0                                   # a spike one sample wide
    v[40_000] = -50.0
    for width in (200, 1000, 3000):
        td, vd, info = decimate(t, v, width_px=width)
        assert info["decimated"] and info["points"] <= 2 * width and len(td) == len(vd)
        assert vd.max() == 50.0 and vd.min() == -50.0
        assert td[np.argmax(vd)] == t[123_456], "the spike is drawn where it happened"
        assert np.all(np.diff(td) > 0), "points stay in time order"


def test_every_column_keeps_its_min_and_max():
    t, v = signal(50_000)
    td, vd, _ = decimate(t, v, width_px=100)
    edges = np.linspace(t[0], t[-1], 101)
    for k in (0, 37, 99):
        inside = (t >= edges[k]) & (t < edges[k + 1])
        got = vd[(td >= edges[k]) & (td < edges[k + 1])]
        assert got.min() == v[inside].min() and got.max() == v[inside].max()


def test_zooming_in_ends_at_the_real_samples():
    t, v = signal()
    td, vd, info = decimate(t, v, t0=100.0, t1=102.0, width_px=1000)
    assert not info["decimated"]
    inside = (td >= 100.0) & (td <= 102.0)
    assert np.array_equal(vd[inside], v[(t >= 100.0) & (t <= 102.0)])
    assert td[0] < 100.0 and td[-1] > 102.0, "one sample either side, so the line reaches the edges"


def test_a_gap_stays_a_gap():
    t, v = signal()
    v[60_000:80_000] = np.nan
    td, vd, _ = decimate(t, v, width_px=500)
    hole = (td > t[61_000]) & (td < t[79_000])
    assert hole.any() and np.isnan(vd[hole]).all(), "invalid stretches are not bridged"
    assert np.isfinite(vd[~hole & (td < t[59_000])]).all()


def test_small_and_empty_inputs():
    assert decimate(np.array([]), np.array([]))[2] == {"samples": 0, "points": 0, "decimated": False}
    t, v = np.arange(10.0), np.arange(10.0)
    td, vd, info = decimate(t, v, width_px=1000)
    assert np.array_equal(td, t) and not info["decimated"]
