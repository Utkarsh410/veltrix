"""
The lap comparison tab: the session's charts redrawn with one curve per lap,
a delta-time chart underneath, and a readout of every lap at the cursor.

Display only — which laps, how they are aligned and what the numbers are all
come from `veltrix_app.compare.LapComparison`.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from .chart_layout import EvenRows, set_key
from .compare import ALIGN, LapComparison

NO_LAPS = "No laps are loaded. Click laps in the Laps tab on the left to draw them over each other."
LAP_COLOURS = ("#F4F2F8", "#B9A6FF", "#E0B25C", "#7FD1B9", "#E79A8C", "#8FB8FF")
STYLES = (QtCore.Qt.SolidLine, QtCore.Qt.DashLine, QtCore.Qt.DotLine)


def _lap_time(seconds: float) -> str:
    m, s = divmod(float(seconds), 60.0)
    return f"{int(m)}:{s:06.3f}"


CORNER_COLUMNS = ("Corner", "Δ time (s)", "Braking point\n(m before turn-in)", "Brake release\n(m after turn-in)",
                  "Min speed\n(km/h)", "Throttle on\n(m after apex)", "Exit speed\n(km/h)", "What differed")
BETTER, WORSE = "#7FD1B9", "#E79A8C"


def _pair(a: float, b: float, digits: int = 0, flat=(False, False)) -> str:
    """Reference → comparison lap, as one cell."""
    def one(x, is_flat):
        if is_flat:
            return "flat"
        return "—" if not np.isfinite(x) else f"{x:.{digits}f}".replace("-", "−")
    return f"{one(a, flat[0])} → {one(b, flat[1])}"


class CompareView(QtWidgets.QWidget):
    lapsChanged = QtCore.Signal()              # the loaded laps, or which of them is the reference, changed
    cursorMoved = QtCore.Signal(float)         # the cursor's position along the lap
    cornerChosen = QtCore.Signal(str)          # a row of the corner table was clicked: that corner

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

        # corner by corner: the reference lap against one other loaded lap
        self.corners = self.metrics = None
        self.against = QtWidgets.QComboBox()
        self.against.setToolTip("The loaded lap set against the reference, corner by corner")
        self.against.currentIndexChanged.connect(lambda *_: self.fill_corners())
        self.corner_title = QtWidgets.QLabel("")
        self.corner_title.setStyleSheet("font-weight: 600;")
        self.corner_note = QtWidgets.QLabel("")
        self.corner_note.setWordWrap(False)
        self.corner_note.setStyleSheet("color: #8A8794;")
        self.corner_table = QtWidgets.QTableWidget(0, len(CORNER_COLUMNS))
        self.corner_table.setHorizontalHeaderLabels(CORNER_COLUMNS)
        self.corner_table.verticalHeader().hide()
        self.corner_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.corner_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.corner_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.corner_table.horizontalHeader().setStretchLastSection(True)
        self.corner_table.setToolTip("Click a corner to zoom the charts and the map to it")
        self.corner_table.itemSelectionChanged.connect(self._corner_clicked)
        head = QtWidgets.QHBoxLayout()
        head.addWidget(self.corner_title)
        head.addStretch(1)
        head.addWidget(QtWidgets.QLabel("Against"))
        head.addWidget(self.against)
        self.corner_panel = QtWidgets.QWidget()
        cl = QtWidgets.QVBoxLayout(self.corner_panel)
        cl.setContentsMargins(0, 4, 0, 0)
        cl.setSpacing(3)
        cl.addLayout(head)
        cl.addWidget(self.corner_note)
        cl.addWidget(self.corner_table, 1)
        self.split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.split.addWidget(self.graphs)
        self.split.addWidget(self.corner_panel)
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(7)
        self.split.setStretchFactor(0, 4)            # the charts keep most of the height; drag the bar for more table
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([720, 190])
        self.corner_table.setMinimumHeight(70)
        # the table keeps its share of the height as the window changes size; dragging the bar sets the share
        self._table_share = 0.24
        self.split.splitterMoved.connect(self._remember_share)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(self.mode)
        layout.addWidget(self.message)
        layout.addWidget(self.split, 1)

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
        self.fill_corners()
        self.lapsChanged.emit()

    def use_defaults(self):
        if self.cmp:
            self.set_laps(self.cmp.default_laps())

    # ── corner by corner ──────────────────────────────────────────────────────

    def _remember_share(self, *_):
        top, bottom = self.split.sizes()
        if top + bottom > 0 and self.corner_panel.isVisible():
            self._table_share = bottom / (top + bottom)

    def _apply_share(self):
        total = sum(self.split.sizes())
        if total > 0 and self.corner_panel.isVisible():
            bottom = int(round(total * self._table_share))
            self.split.blockSignals(True)
            self.split.setSizes([total - bottom, bottom])
            self.split.blockSignals(False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QtCore.QTimer.singleShot(0, self._apply_share)

    def set_corner_data(self, corners, metrics, reason: str = ""):
        """The session's corners and per-lap corner metrics (corners.analyse), or
        None with the reason they could not be worked out."""
        self.corners, self.metrics, self._corner_reason = corners, metrics, reason
        self.fill_corners()

    def fill_corners(self):
        from veltrix_engine import corners as corner_engine
        table, cmp_ = self.corner_table, self.cmp
        laps = list(cmp_.laps) if cmp_ else []
        keep = self.against.currentData()
        self.against.blockSignals(True)
        self.against.clear()
        for lap in laps[1:]:
            self.against.addItem(f"lap {lap}", lap)
        if keep in laps[1:]:
            self.against.setCurrentIndex(laps[1:].index(keep))
        self.against.blockSignals(False)
        self.against.setVisible(len(laps) > 2)
        table.setRowCount(0)
        self.rows = []
        # corner by corner needs two laps; until then the charts have the whole height
        shown = len(laps) >= 2
        if shown != self.corner_panel.isVisible():
            self.corner_panel.setVisible(shown)
            if shown:
                QtCore.QTimer.singleShot(0, self._apply_share)
        if self.corners is None or self.metrics is None:
            self.corner_title.setText("Corners")
            self.corner_note.setText(getattr(self, "_corner_reason", "") or "Corner analysis is not available for this session.")
            return
        if len(laps) < 2:
            self.corner_title.setText("Corners")
            self.corner_note.setText("Load a second lap to compare the two corner by corner.")
            return
        ref, other = laps[0], self.against.currentData() if self.against.count() else laps[1]
        done = set(self.metrics["lap"])
        if ref not in done or other not in done:
            self.corner_title.setText("Corners")
            self.corner_note.setText(f"Lap {ref if ref not in done else other} has no corner figures (only laps run "
                                     "from line to line are analysed corner by corner).")
            return
        t = corner_engine.pass_comparison(self.metrics, self.corners, ref, other)
        self.rows = list(t["corner"])
        official = (self.corners["basis"] == "circuit file").all() if "basis" in self.corners else False
        source = self.corners.attrs.get("source", "")
        self.corner_title.setText(f"Corners: lap {other} against lap {ref} (reference)")
        self.corner_note.setText(("Official turns" if official else "Corner numbers inferred from the data, not the "
                                  "circuit's own") + " · each cell: reference → this lap · hover for how it is measured")
        self.corner_note.setToolTip(
            (f"Official turns from the {source}." if official else
             f"Corner numbers are inferred from the data ({source}), not the circuit's own: no circuit file fits this "
             "session.") + "\nEach cell is reference → this lap. Braking point and release are measured from the "
            "corner's turn-in, throttle-on from its apex — fixed points, the same for every lap.")
        table.setRowCount(len(t))
        for r, x in enumerate(t.itertuples()):
            name = x.corner + (f"  {x.name}" if isinstance(x.name, str) and x.name else "")
            if not official:
                name += "  · inferred"
            ref_flat = x.ref_text["throttle_after_apex_m"] == "flat out"
            lap_flat = x.lap_text["throttle_after_apex_m"] == "flat out"
            cells = (name, f"{x.d_zone_time_s:+.2f}",
                     _pair(x.ref_brake_before_m, x.lap_brake_before_m),
                     _pair(x.ref_release_after_turn_in_m, x.lap_release_after_turn_in_m),
                     _pair(x.ref_min_speed, x.lap_min_speed),
                     _pair(x.ref_throttle_after_apex_m, x.lap_throttle_after_apex_m, flat=(ref_flat, lap_flat)),
                     _pair(x.ref_exit_speed, x.lap_exit_speed), x.finding)
            for k, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                item.setData(QtCore.Qt.UserRole, x.corner)
                if k == 0:
                    item.setToolTip(f"{x.kind} {('left' if x.direction == 'L' else 'right')}-hander; "
                                    + ("official turn" if x.basis == "circuit file" else "found in the data"))
                if k == 1 and abs(x.d_zone_time_s) >= 0.03:
                    item.setForeground(QtGui.QColor(WORSE if x.d_zone_time_s > 0 else BETTER))
                if k in (2, 3, 5):
                    words = (x.ref_text, x.lap_text)
                    key = ("brake_before_m", "release_after_turn_in_m", None, "throttle_after_apex_m")[k - 2]
                    if key:
                        item.setToolTip(f"Reference: {words[0][key]}\nLap {other}: {words[1][key]}")
                table.setItem(r, k, item)
        table.resizeColumnsToContents()
        table.resizeRowsToContents()

    def _corner_clicked(self):
        rows = self.corner_table.selectionModel().selectedRows()
        if rows:
            corner = self.corner_table.item(rows[0].row(), 0).data(QtCore.Qt.UserRole)
            self.zoom_to_corner(corner)
            self.cornerChosen.emit(corner)

    def zoom_to_corner(self, corner: str):
        """Zoom the charts to a corner's zone and put the cursor on its apex."""
        if self.corners is None or not self._plots or not self.cmp or not self.cmp.laps:
            return
        c = self.corners.set_index("corner").loc[corner]
        d0, d1, apex = float(c.zone_start_m), float(c.zone_end_m), float(c.apex_m)
        if self.cmp.align == "time":
            ref = self.cmp.reference
            t, d = self.cmp._lap_distance(ref)
            start = self.cmp._by_label[ref].t_start
            d0, d1, apex = (float(np.interp(x, d, t) - start) for x in (d0, d1, apex))
        self._plots[0].setXRange(d0, d1, padding=0.02)
        self.set_cursor(apex)

    def set_laps(self, laps: list):
        """Overlay these laps; the first is the reference. None: back to the whole session."""
        if not self.cmp:
            return
        self.cmp.set_laps(laps)
        self.rebuild()
        self.fill_corners()
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
            if k:
                plot.setXLink(self._plots[0])
            if channels is None:
                plot.setLabel("left", "Δt (s), + is slower")
                plot.addLine(y=0, pen=pg.mkPen("#6E6B7A"))
                key = []
                for lap in cmp_.laps[1:]:
                    d = cmp_.delta(lap)
                    if d is not None:
                        plot.plot(d["x"], d["dt"], pen=pg.mkPen(colour[lap], width=1))
                        key.append((f"{self.name(lap)} {d['final_s']:+.3f} s", colour[lap], 0))
                set_key(plot, key, prefix=f"Δt against {self.name(cmp_.reference)}:" if key
                        else "Δt: load a second lap to see time gained and lost")
                plot.setLabel("bottom", cmp_.x_label)
            else:
                key = []
                for n, channel in enumerate(channels):
                    for lap in cmp_.laps:
                        tr = cmp_.trace(channel, lap, width)
                        if tr is None or not len(tr["x"]):
                            continue
                        x, v = tr["x"], tr["v"]
                        if tr["discrete"] and len(x) > 1:
                            x, v = np.repeat(x, 2)[1:], np.repeat(v, 2)[:-1]
                        plot.plot(x, v, pen=pg.mkPen(colour[lap], width=1, style=STYLES[n % len(STYLES)]), connect="finite")
                    unit = self.cmp.model.get(channel).unit if self.cmp.model.has(channel) else ""
                    key.append((f"{channel} [{unit}]" if unit else channel, None, n))
                # the lap colours are in the panel's Tests loaded; the key says which line style is which channel
                set_key(plot, key)
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
