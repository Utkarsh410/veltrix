"""
The track map, without any window: the lap's shape, where the cursor is on
it, what a channel reads along it, and which point of the lap a click means.

The shape comes from `veltrix_engine.track.track_map`. When the file has no
position, the engine rebuilds the shape from speed and lateral g: that gives
the circuit's outline and corner order, not surveyed coordinates, and `source`
says so. With neither position nor usable lateral g there is no map at all —
nothing is invented.
"""

from __future__ import annotations

import numpy as np

from veltrix_engine.track import track_map


class TrackMap:
    def __init__(self, session):
        """Raises ValueError (with the reason) when the session cannot give a map."""
        self.session, self.model = session, session.model
        frame = track_map(session)
        self.dist = frame["dist"].to_numpy(float)
        self.x, self.y = frame["x"].to_numpy(float), frame["y"].to_numpy(float)
        self.measured = frame.attrs["source"] == "position"
        self.closed = bool(frame.attrs.get("closed", True))
        if self.measured:
            self.source = "Logged position."
        else:
            self.source = ("Rebuilt from speed and lateral g: the circuit's shape and corner order, not surveyed "
                           "positions" + ("." if self.closed else "; the two ends did not meet, so it is drawn open."))
        self._dist_channel = self.model.get("lap_distance")
        self._laps = {lap.lap: lap for lap in self.model.laps}

    def lap_of(self, t: float):
        return next((lap for lap in self.model.laps if lap.t_start <= t < lap.t_end), None)

    def distance_at(self, t: float) -> float | None:
        """Metres from the line at session time t, or None outside the laps."""
        d = self._dist_channel.at([t])[0][0]
        return float(d) if np.isfinite(d) else None

    def position(self, t: float) -> tuple[float, float] | None:
        """Where on the map the car was at session time t."""
        d = self.distance_at(t)
        if d is None:
            return None
        d = float(np.clip(d, self.dist[0], self.dist[-1]))
        return float(np.interp(d, self.dist, self.x)), float(np.interp(d, self.dist, self.y))

    def nearest(self, x: float, y: float) -> float:
        """Metres from the line of the map point closest to (x, y)."""
        return float(self.dist[int(np.argmin(np.hypot(self.x - x, self.y - y)))])

    def time_at(self, lap, distance: float) -> float | None:
        """Session time at which a lap was at this distance from the line."""
        row = self._laps.get(lap)
        if row is None:
            return None
        t, d = self._dist_channel.timestamps, np.asarray(self._dist_channel.values, float)
        a, b = np.searchsorted(t, [row.t_start, row.t_end])
        ok = np.isfinite(d[a:b])
        tt, dd = t[a:b][ok], np.maximum.accumulate(d[a:b][ok])
        if len(dd) < 2 or not dd[0] <= distance <= dd[-1]:
            return None
        return float(np.interp(distance, dd, tt))

    def colour_values(self, channel: str, lap) -> dict | None:
        """A channel along the map for one lap: its value at each map point,
        read from the channel's own samples. None if the session lacks it."""
        if not self.model.has(channel) or lap not in self._laps:
            return None
        c = self.model.get(channel)
        times = np.array([self.time_at(lap, d) or np.nan for d in self.dist])
        values = np.full(len(times), np.nan)
        ok = np.isfinite(times)
        values[ok] = c.at(times[ok])[0]
        return {"values": values, "unit": c.unit, "channel": c.id, "lap": lap, "label": c.label}
