"""
The lap comparison tab: the session's charts redrawn with one curve per lap,
a delta-time chart underneath, and a readout of every lap at the cursor.

Display only — which laps, how they are aligned and what the numbers are all
come from `veltrix_app.compare.LapComparison`.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from .chart_layout import EvenRows
from .compare import ALIGN, LapComparison

NO_LAPS = "No laps are loaded. Click laps in the Laps tab on the left to draw them over each other."
LAP_COLOURS = ("#F4F2F8", "#B9A6FF", "#E0B25C", "#7FD1B9", "#E79A8C", "#8FB8FF")
STYLES = (QtCore.Qt.SolidLine, QtCore.Qt.DashLine, QtCore.Qt.DotLine)


def _lap_time(seconds: float) -> str:
    m, s = divmod(float(seconds), 60.0)
    return f"{int(m)}:{s:06.3f}"


class CompareView(QtWidgets.QWidget):
    lapsChanged = QtCore.Signal()              # the loaded laps, or which of them is the reference, changed
    cursorMoved = QtCore.Signal(float)         # the cursor's position along the lap

    def __init__(self):
        super().__init__()
        self.cmp: LapComparison | None = None
        self.charts: list = []
        self.cursor_x = 0.0
        self._plots, self._cursors, self._syncing = [], [], False

        self.align = QtWidgets.QComboBox()
        self.align.addItems([a.capitalize() for a in ALIGN])
        self.align.currentIndexChanged.connect(self._align_changed)
        self.mode = QtWidgets.QLabel("")
        self.mode.setStyleSheet("font-weight: 600;")
        self.message = QtWidgets.QLabel(NO_LAPS)
        self.message.setWordWrap(True)

        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(QtWidgets.QLabel("Aligned by"))
        bar.addWidget(self.align)
        bar.addStretch(1)
        self.graphs = pg.GraphicsLayoutWidget()
        self._even = EvenRows(self.graphs)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(self.mode)
        layout.addWidget(self.message)
        layout.addWidget(self.graphs, 1)

    # ── what to compare ───────────────────────────────────────────────────────

    def name(self, lap) -> str:
        """What a lap is called in legends and tables."""
        return f"lap {lap}"

    def set_session(self, model, charts: list, settings: dict | None = None):
        """Take a session's laps and charts. No lap is loaded until one is
        asked for (or the settings name some); a session without laps says why."""
        self.charts = [list(c.channels) for c in charts]
        settings = settings or (self.cmp.to_dict() if self.cmp else {})
        try:
            self.cmp = LapComparison(model, settings.get("laps"), settings.get("align", "distance"))
        except ValueError as e:
            self.cmp = None
            self.message.setText(str(e))
            self.message.show()
            self.graphs.clear()
            self._plots, self._cursors = [], []
            self.mode.setText("")
            self.lapsChanged.emit()
            return
        if not settings.get("laps"):
            self.cmp.laps = []                 # nothing loaded yet, rather than the model's own suggestion
        self._syncing = True
        self.align.setCurrentIndex(ALIGN.index(self.cmp.align))
        self._syncing = False
        self.rebuild()
        self.lapsChanged.emit()

    def use_defaults(self):
        if self.cmp:
            self.set_laps(self.cmp.default_laps())

    def set_laps(self, laps: list):
        """Overlay these laps; the first is the reference. None: back to the whole session."""
        if not self.cmp:
            return
        self.cmp.set_laps(laps)
        self.rebuild()
        self.lapsChanged.emit()

    def set_align(self, align: str):
        self.align.setCurrentIndex(ALIGN.index(align))

    def _align_changed(self, index):
        if self.cmp and not self._syncing:
            self.cmp.set_align(ALIGN[index])
            self.rebuild()

    # ── drawing ───────────────────────────────────────────────────────────────

    def rebuild(self):
        self.graphs.clear()
        self._plots, self._cursors = [], []
        self._even.set_plots([])
        if not self.cmp:
            return
        if not self.cmp.laps:
            self.message.setText(NO_LAPS)
            self.message.show()
            self.mode.setText("")
            return
        self.message.hide()
        cmp_ = self.cmp
        by = "distance — the same x is the same place on the track" if cmp_.align == "distance" else \
            "time — the same x is the same moment of each lap, not the same place"
        self.mode.setText(f"Laps aligned by {by}.   Reference: {self.name(cmp_.reference)}.")
        width = max(int(self.graphs.width()), 400)
        colour = {lap: LAP_COLOURS[i % len(LAP_COLOURS)] for i, lap in enumerate(cmp_.laps)}
        rows = [c for c in self.charts if c] + [None]                  # None: the delta chart
        for k, channels in enumerate(rows):
            plot = self.graphs.addPlot(row=k, col=0)
            plot.showGrid(x=True, y=True, alpha=0.15)
            plot.setMouseEnabled(x=True, y=False)
            plot.setMenuEnabled(False)
            plot.addLegend(offset=(8, 4), labelTextSize="9pt")
            if k:
                plot.setXLink(self._plots[0])
            if channels is None:
                plot.setLabel("left", "Δt (s), + is slower")
                plot.addLine(y=0, pen=pg.mkPen("#6E6B7A"))
                for lap in cmp_.laps[1:]:
                    d = cmp_.delta(lap)
                    if d is not None:
                        plot.plot(d["x"], d["dt"], pen=pg.mkPen(colour[lap], width=1),
                                  name=f"{self.name(lap)} − {self.name(cmp_.reference)}: {d['final_s']:+.3f} s")
                plot.setLabel("bottom", cmp_.x_label)
            else:
                for n, channel in enumerate(channels):
                    for lap in cmp_.laps:
                        tr = cmp_.trace(channel, lap, width)
                        if tr is None or not len(tr["x"]):
                            continue
                        x, v = tr["x"], tr["v"]
                        if tr["discrete"] and len(x) > 1:
                            x, v = np.repeat(x, 2)[1:], np.repeat(v, 2)[:-1]
                        plot.plot(x, v, pen=pg.mkPen(colour[lap], width=1, style=STYLES[n % len(STYLES)]),
                                  name=f"{channel} [{tr['unit']}] · {self.name(lap)}", connect="finite")
            cursor = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen("#F4F2F8", width=1))
            cursor.sigPositionChanged.connect(self._dragged)
            plot.addItem(cursor)
            self._plots.append(plot)
            self._cursors.append(cursor)
        self._even.set_plots(self._plots)
        self.graphs.scene().sigMouseClicked.connect(self._clicked)
        lo, hi = cmp_.extent()
        self._plots[0].setXRange(lo, hi, padding=0)
        self.set_cursor(float(np.clip(self.cursor_x, lo, hi)))

    def _clicked(self, event):
        for plot in self._plots:
            if plot.sceneBoundingRect().contains(event.scenePos()):
                self.set_cursor(plot.vb.mapSceneToView(event.scenePos()).x())
                return

    def _dragged(self, line):
        if not self._syncing:
            self.set_cursor(line.value())

    def set_cursor(self, x: float):
        if not self.cmp or not self.cmp.laps:
            return
        self.cursor_x = float(x)
        self._syncing = True
        for line in self._cursors:
            line.setValue(self.cursor_x)
        self._syncing = False
        self.cursorMoved.emit(self.cursor_x)
