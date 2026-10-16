"""
Video synchronisation, without any window: how a moment in a video relates to
a moment in the telemetry, how well that is known, and where it is saved.

Convention
----------
    telemetry_time = rate · video_time + offset          (seconds)

A manual offset is the case rate = 1: an offset of +12.5 means the video
started 12.5 s after the telemetry clock's zero — video second 0 is telemetry
second 12.5. A negative offset means the camera was already running when the
log began.

Anchors
-------
An anchor is one event identified in both: "the brake goes on at 31.20 s in
the video and at 43.72 s in the data". The fit uses the simplest model the
anchors support:

  no anchors      the manual offset; sync is "not validated"
  one anchor      offset only (rate 1)
  two or more     offset only, unless a clock-rate difference (drift) explains
                  the anchors clearly better — then offset and rate
  piecewise       only when asked for (`piecewise = True`) and with three or
                  more anchors: the sync runs straight from anchor to anchor,
                  for a recording whose clock does not drift evenly (a camera
                  that dropped frames, a file joined from clips). It is exact
                  at the anchors by construction, so its uncertainty is taken
                  from leaving each anchor out in turn and seeing how far the
                  others put it

Residuals (how far each anchor is from the fit) are kept and shown. Sync
uncertainty comes from them, never from the frame rate: a 30 fps video has a
frame every 33 ms, which limits how finely an anchor can be placed but says
nothing about whether the offset is right.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

VERSION = 1
DISAGREE_S = 0.10          # anchors further than this from the fit are flagged (configurable per link)
DRIFT_MIN_SPAN_S = 60.0    # anchors must cover at least this much video before drift is considered
DRIFT_GAIN = 0.5           # ... and fitting a rate must cut the residual by at least this share


@dataclass
class Anchor:
    video_time: float
    telemetry_time: float
    label: str = ""


@dataclass
class VideoLink:
    path: str
    camera: str = ""
    offset: float = 0.0                  # seconds; see the convention above
    rate: float = 1.0
    method: str = "manual"               # manual | anchors | piecewise
    piecewise: bool = False              # follow the anchors segment by segment instead of one straight fit
    anchors: list = field(default_factory=list)
    tolerance_s: float = DISAGREE_S
    metadata: dict = field(default_factory=dict)       # duration_s, fps, width, height — whatever the player reports

    # ── the mapping ───────────────────────────────────────────────────────────

    def _knots(self) -> tuple[np.ndarray, np.ndarray]:
        """Anchors as (video, telemetry) in video order, for the piecewise sync."""
        pairs = sorted((a.video_time, a.telemetry_time) for a in self.anchors)
        return np.array([p[0] for p in pairs]), np.array([p[1] for p in pairs])

    @staticmethod
    def _through(x, xs, ys):
        """Straight lines through the points (xs, ys); beyond the first and last, one for one."""
        x = np.asarray(x, float)
        inside = np.interp(x, xs, ys)
        return np.where(x < xs[0], ys[0] + (x - xs[0]), np.where(x > xs[-1], ys[-1] + (x - xs[-1]), inside))

    def to_telemetry(self, video_time):
        if self.method == "piecewise":
            v, t = self._knots()
            return self._through(video_time, v, t)
        return self.rate * np.asarray(video_time, float) + self.offset

    def to_video(self, telemetry_time):
        if self.method == "piecewise":
            v, t = self._knots()
            return self._through(telemetry_time, t, v)
        return (np.asarray(telemetry_time, float) - self.offset) / self.rate

    def set_manual_offset(self, offset: float) -> None:
        """Set the offset by hand. Anchors are kept, but no longer drive the sync."""
        self.offset, self.rate, self.method = float(offset), 1.0, "manual"

    # ── anchors ───────────────────────────────────────────────────────────────

    def add_anchor(self, video_time: float, telemetry_time: float, label: str = "") -> dict:
        self.anchors.append(Anchor(float(video_time), float(telemetry_time), label.strip()))
        return self.fit()

    def remove_anchor(self, index: int) -> dict:
        del self.anchors[index]
        return self.fit()

    def fit(self) -> dict:
        """Fit the sync to the anchors and return its status. With no anchors
        the manual offset stands."""
        if not self.anchors:
            self.method = "manual"
            return self.status()
        v = np.array([a.video_time for a in self.anchors])
        t = np.array([a.telemetry_time for a in self.anchors])
        offset_only = float(np.mean(t - v))
        rate, offset = 1.0, offset_only
        if len(v) >= 3 and np.ptp(v) >= DRIFT_MIN_SPAN_S:
            slope, intercept = np.polyfit(v, t, 1)
            flat = float(np.sqrt(np.mean((t - (v + offset_only)) ** 2)))
            tilted = float(np.sqrt(np.mean((t - (slope * v + intercept)) ** 2)))
            # drift is taken only when the anchors need it: a straight offset misses them by more than the
            # tolerance, and letting the rate vary removes at least half of that
            if flat > self.tolerance_s and tilted <= (1 - DRIFT_GAIN) * flat and 0.9 < slope < 1.1:
                rate, offset = float(slope), float(intercept)
        self.rate, self.offset, self.method = rate, offset, "anchors"
        if self.piecewise and len(v) >= 3 and np.all(np.diff(np.sort(t)) > 0) and len(set(v)) == len(v):
            self.method = "piecewise"
        return self.status()

    def set_piecewise(self, on: bool) -> dict:
        """Follow the anchors segment by segment (on), or fit one straight line through them (off)."""
        self.piecewise = bool(on)
        return self.fit()

    def leave_one_out(self) -> list:
        """For the piecewise sync: how far each anchor is from where the other anchors alone would put it."""
        v, t = self._knots()
        out = []
        for i in range(len(v)):
            keep = np.arange(len(v)) != i
            out.append(float(self._through(v[i], v[keep], t[keep]) - t[i]))
        return out

    def residuals(self) -> list:
        """Per anchor: telemetry time by the fit minus the anchor's telemetry time, seconds."""
        return [float(self.to_telemetry(a.video_time) - a.telemetry_time) for a in self.anchors]

    def status(self) -> dict:
        """How well the sync is known, in words and numbers."""
        res = np.array(self.residuals())
        n = len(res)
        fps = self.metadata.get("fps")
        frame = f" One frame is {1000 / fps:.1f} ms; that is how finely a moment can be picked, not how good the sync is." \
            if fps else ""
        if self.method == "manual" or n == 0:
            return {"validated": False, "uncertainty_s": None, "model": "manual offset", "anchors": n, "disagree": [],
                    "text": f"Not validated: manual offset of {self.offset:+.3f} s, no anchors to check it against." + frame}
        if n == 1:
            return {"validated": False, "uncertainty_s": None, "model": "offset from one anchor", "anchors": 1, "disagree": [],
                    "text": f"Not validated: offset {self.offset:+.3f} s from a single anchor — a second one is needed to "
                            "check it." + frame}
        if self.method == "piecewise":
            loo = np.array(self.leave_one_out())
            spread = float(np.sqrt(np.mean(loo ** 2)))
            return {"validated": spread <= self.tolerance_s, "uncertainty_s": spread, "model": "piecewise", "anchors": n,
                    "disagree": [i for i, r in enumerate(loo) if abs(r) > self.tolerance_s],
                    "text": f"Piecewise through {n} anchors: exact at each anchor, straight between them. Leaving each "
                            f"anchor out in turn, the others place it to within ±{spread:.3f} s — that is how well the "
                            "sync is known between anchors." + (" That is wider than the tolerance: add anchors where "
                            "it matters." if spread > self.tolerance_s else "") + frame}
        rms, worst = float(np.sqrt(np.mean(res ** 2))), float(np.max(np.abs(res)))
        disagree = [i for i, r in enumerate(res) if abs(r) > self.tolerance_s]
        drift = self.rate != 1.0
        model = "offset and clock rate" if drift else "offset"
        text = (f"{model.capitalize()} from {n} anchors: offset {self.offset:+.3f} s"
                + (f", video clock runs {(self.rate - 1) * 1e6:+.0f} ppm against the logger" if drift else "")
                + f". Anchors agree to ±{rms:.3f} s (worst {worst:.3f} s).")
        if disagree:
            text += (f" {len(disagree)} anchor(s) are more than {self.tolerance_s:g} s from the fit — check them: "
                     + ", ".join(self.anchors[i].label or f"#{i + 1}" for i in disagree) + ".")
        return {"validated": not disagree, "uncertainty_s": rms, "model": model, "anchors": n, "disagree": disagree,
                "text": text + frame}

    # ── the file ──────────────────────────────────────────────────────────────

    def exists(self) -> bool:
        return bool(self.path) and Path(self.path).exists()

    def relink(self, path: str | Path) -> None:
        """Point at the same recording in a new place; the sync is kept."""
        self.path = str(path)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "VideoLink":
        anchors = [Anchor(**a) for a in data.get("anchors", [])]
        return cls(**{**{k: v for k, v in data.items() if k in cls.__dataclass_fields__}, "anchors": anchors})


class VideoStore:
    """The videos linked to each session, one JSON file per source file (by SHA-256)."""

    def __init__(self, folder: str | Path):
        self.folder = Path(folder)

    def _path(self, sha256: str) -> Path:
        if not sha256 or not sha256.isalnum():
            raise ValueError("Videos are linked per source file; this session has no file identity.")
        return self.folder / f"{sha256}.json"

    def load(self, sha256: str) -> list:
        try:
            path = self._path(sha256)
            return [VideoLink.from_dict(v) for v in json.loads(path.read_text(encoding="utf-8"))["videos"]] if path.exists() else []
        except (ValueError, KeyError, TypeError):
            return []

    def save(self, sha256: str, links: list) -> None:
        path = self._path(sha256)
        self.folder.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": VERSION, "videos": [v.to_dict() for v in links]}, indent=1), encoding="utf-8")
