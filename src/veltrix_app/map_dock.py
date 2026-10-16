"""The track map dock: the lap's outline coloured by a channel, a marker at the
cursor, and a click on the map moves the cursor to that place on the current lap."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from .trackmap import TrackMap


class MapDock(QtWidgets.QDockWidget):
    moved = QtCore.Signal(float)              # the user clicked the map: session time to move the cursor to

    def __init__(self, parent=None):
        super().__init__("Track map", parent)
        self.map: TrackMap | None = None
        self.lap = None
        self.colour = QtWidgets.QComboBox()
        self.colour.currentIndexChanged.connect(lambda *_: self.redraw())
        self.note = QtWidgets.QLabel("Open a session to see its map.")
        self.note.setWordWrap(True)
        self.plot = pg.PlotWidget()
        self.plot.setAspectLocked(True)
        self.plot.hideAxis("left")
        self.plot.hideAxis("bottom")
        self.plot.setMenuEnabled(False)
        self.line = self.plot.plot([], [], pen=pg.mkPen("#6E6B7A", width=2))
        self.dots = pg.ScatterPlotItem(size=5, pen=None)
        self.marker = pg.ScatterPlotItem(size=14, pen=pg.mkPen("#0A0A0D", width=2), brush=pg.mkBrush("#F4F2F8"))
        self.notes = pg.ScatterPlotItem(size=13, symbol="d", pen=pg.mkPen("#0A0A0D", width=1), brush=pg.mkBrush("#E0B25C"),
                                        hoverable=True, tip=lambda x, y, data: str(data))
        self.plot.addItem(self.dots)
        self.plot.addItem(self.notes)
        self.plot.addItem(self.marker)
        self.plot.scene().sigMouseClicked.connect(self._clicked)
        self.scale = QtWidgets.QLabel("")
        box = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(6, 6, 6, 6)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Colour by"))
        row.addWidget(self.colour, 1)
        layout.addLayout(row)
        layout.addWidget(self.plot, 1)
        layout.addWidget(self.scale)
        layout.addWidget(self.note)
        self.setWidget(box)

    def set_session(self, session, cursor: float):
        self.colour.blockSignals(True)
        self.colour.clear()
        try:
            self.map = TrackMap(session)
        except (ValueError, KeyError) as e:
            self.map = None
            self.line.setData([], [])
            self.dots.setData([], [])
            self.marker.setData([], [])
            self.scale.setText("")
            self.note.setText(f"No map for this session: {e}" if str(e) else
                              "No map for this session: it has no position, and no speed with usable lateral g to rebuild one.")
            self.colour.blockSignals(False)
            return
        self.colour.addItem("Nothing", None)
        for c in session.model.channels("normalised") + session.model.channels("corrected") + session.model.channels("calculated"):
            if c.id not in ("time", "lap", "distance", "lap_distance", "pos_x", "pos_y", "gps_lat", "gps_lon") and not c.discrete:
                self.colour.addItem(f"{c.id} [{c.unit}]", c.id)
        index = self.colour.findData("speed")
        self.colour.setCurrentIndex(max(index, 0))
        self.colour.blockSignals(False)
        self.note.setText(self.map.source)
        x, y = self.map.x, self.map.y
        self.line.setData(np.r_[x, x[0]] if self.map.closed else x, np.r_[y, y[0]] if self.map.closed else y)
        self.plot.autoRange()
        self.set_cursor(cursor)

    def set_notes(self, annotations: list) -> int:
        """Mark each timed note where on the lap it was made. Returns how many could be placed."""
        spots = []
        if self.map:
            for a in annotations:
                at = self.map.position(a.time) if a.time is not None else None
                if at:
                    spots.append({"pos": at, "data": f"lap {a.lap}: {a.text}" if a.lap is not None else a.text})
        self.notes.setData(spots)
        return len(spots)

    def set_cursor(self, t: float):
        """Move the marker; recolour when the cursor has entered another lap."""
        if not self.map:
            return
        lap = self.map.lap_of(t)
        label = lap.lap if lap else None
        if label != self.lap:
            self.lap = label
            self.redraw()
        at = self.map.position(t)
        self.marker.setData([at[0]], [at[1]]) if at else self.marker.setData([], [])

    def redraw(self):
        if not self.map:
            return
        channel = self.colour.currentData()
        data = self.map.colour_values(channel, self.lap) if channel and self.lap is not None else None
        if data is None or not np.isfinite(data["values"]).any():
            self.dots.setData([], [])
            self.scale.setText("")
            return
        v = data["values"]
        ok = np.isfinite(v)
        lo, hi = float(np.nanmin(v)), float(np.nanmax(v))
        share = np.clip((v[ok] - lo) / (hi - lo), 0, 1) if hi > lo else np.zeros(ok.sum())
        cmap = pg.colormap.get("viridis")
        self.dots.setData(self.map.x[ok], self.map.y[ok], brush=[pg.mkBrush(c) for c in cmap.map(share, mode="qcolor")])
        self.scale.setText(f"{data['channel']} on lap {data['lap']}: dark {lo:.4g} → bright {hi:.4g} {data['unit']}"
                           f"   ({data['label']})")

    def _clicked(self, event):
        if not self.map or self.lap is None or not self.plot.sceneBoundingRect().contains(event.scenePos()):
            return
        p = self.plot.plotItem.vb.mapSceneToView(event.scenePos())
        t = self.map.time_at(self.lap, self.map.nearest(p.x(), p.y()))
        if t is not None:
            self.moved.emit(t)
