import json
import sys
import numpy as np
from ibt import IBT

t = IBT(sys.argv[1])
g = t.get
L = float(t.session_info['WeekendInfo']['TrackLength'].split()[0]) * 1000
st, lap, pct = g('SessionTime'), g('Lap'), g('LapDistPct').astype(float)
ch = {k: g(k).astype(float) for k in
      ['Speed', 'Throttle', 'Brake', 'SteeringWheelAngle', 'Gear', 'RPM', 'LatAccel', 'LongAccel',
       'Lat', 'Lon', 'YawRate', 'BrakeABSactive', 'FuelLevel', 'PlayerCarPosition',
       'PlayerCarMyIncidentCount', 'PlayerTrackSurfaceMaterial',
       'LFtempCM', 'RFtempCM', 'LRtempCM', 'RRtempCM', 'LFpressure', 'RFpressure', 'LRpressure', 'RRpressure',
       'LFtempL', 'LFtempM', 'LFtempR', 'RFtempL', 'RFtempM', 'RFtempR',
       'LRtempL', 'LRtempM', 'LRtempR', 'RRtempL', 'RRtempM', 'RRtempR',
       'LFwearM', 'RFwearM', 'LRwearM', 'RRwearM']}
ch['Speed'] *= 3.6
ch['SteeringWheelAngle'] = np.degrees(ch['SteeringWheelAngle'])

# precise lap crossings (interpolate pct wrap)
cross = []
for i in np.flatnonzero(np.diff(lap) == 1) + 1:
    a, b = pct[i - 1], pct[i] + 1.0
    f = (1.0 - a) / (b - a) if b > a else 0
    cross.append((lap[i], st[i - 1] + f * (st[i] - st[i - 1]), i))
SECT = [0.0, 0.313076, 0.522864, 0.723114, 1.0]


def t_at(idx0, idx1, p):
    """time when pct crosses p within [idx0, idx1)"""
    seg = pct[idx0:idx1]
    j = np.flatnonzero((seg[:-1] < p) & (seg[1:] >= p))
    if not len(j):
        return None
    j = j[0]
    f = (p - seg[j]) / (seg[j + 1] - seg[j])
    return st[idx0 + j] + f * (st[idx0 + j + 1] - st[idx0 + j])


GRID = np.arange(0, L, 2.0)
laps = []
for (n0, t0, i0), (n1, t1, i1) in zip(cross[:-1], cross[1:]):
    sl = slice(i0, i1)
    secs = [t0] + [t_at(i0, i1, p) for p in SECT[1:-1]] + [t1]
    d = pct[sl] * L
    order = np.argsort(d)
    res = {}
    for k, v in ch.items():
        res[k] = np.interp(GRID, d[order], v[sl][order])
    res['time'] = np.interp(GRID, d[order], st[sl][order] - t0)
    laps.append(dict(lap=int(n0), time=t1 - t0, sectors=list(np.diff(secs)), data=res,
                     inc=int(ch['PlayerCarMyIncidentCount'][i1 - 1] - ch['PlayerCarMyIncidentCount'][i0]),
                     pos=int(ch['PlayerCarPosition'][i1 - 1]),
                     fuel=float(ch['FuelLevel'][i0] - ch['FuelLevel'][i1 - 1]),
                     vmax=float(ch['Speed'][sl].max()),
                     full_throttle=float((ch['Throttle'][sl] > 0.98).mean()),
                     braking=float((ch['Brake'][sl] > 0.05).mean()),
                     coast=float(((ch['Throttle'][sl] < 0.05) & (ch['Brake'][sl] < 0.05)).mean())))

for l in laps:
    print(l['lap'], round(l['time'], 3), [round(s, 3) for s in l['sectors']], 'inc', l['inc'], 'pos', l['pos'],
          'fuel', round(l['fuel'], 2), 'vmax', round(l['vmax'], 1), 'FT%', round(100 * l['full_throttle'], 1),
          'brk%', round(100 * l['braking'], 1), 'coast%', round(100 * l['coast'], 1))

best = min(laps[1:], key=lambda l: l['time'])  # lap 1 includes rolling start
print('best', best['lap'])
opt = sum(min(l['sectors'][s] for l in laps[1:]) for s in range(4))
print('optimal', opt)

# corners: local speed minima on best lap
v = best['data']['Speed']
from numpy.lib.stride_tricks import sliding_window_view as sw
W = 75  # 150 m window
pad = np.pad(v, W, mode='wrap')
mins = [i for i in range(len(v)) if v[i] == pad[i:i + 2 * W + 1].min()]
mins = [i for i in mins if v[i] < 0.92 * pad[i:i + 2 * W + 1].max()]
print('corner minima (m, kph):', [(int(GRID[i]), round(v[i], 1)) for i in mins])

json.dump(dict(L=L, grid_step=2.0, best=best['lap'], optimal=opt,
               laps=[{k: (vv if k != 'data' else {c: np.round(a, 4).tolist() for c, a in vv.items()})
                      for k, vv in l.items()} for l in laps],
               corners=[int(GRID[i]) for i in mins]),
          open(sys.argv[2], 'w'))
