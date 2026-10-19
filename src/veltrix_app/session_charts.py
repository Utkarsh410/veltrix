"""
The session's stacked charts on one time axis: building them (an axis per
unit, the same room for axes on every chart), drawing the curves at the
resolution the screen needs, and the marked range. A mixin of the main
window, kept apart so the window module stays one job long."""

from __future__ import annotations
import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets
from .chart_layout import AXIS_WIDTH, set_key
COLOURS = ("#B9A6FF", "#E0B25C", "#7FD1B9", "#E79A8C", "#8FB8FF", "#D7A6E0")


class SessionCharts:
    """The main window's session charts. Expects the window's `ws`, `graphs`,
    `session` and docks (see MainWindow)."""

    # ── charts ────────────────────────────────────────────────────────────────

    def add_chart(self, channels=()):
        if self.session is None:
            return
        self.active = self.ws.add_chart(channels)
        self._rebuild_charts()

    def remove_chart(self):
        if self.ws.charts:
            self.ws.remove_chart(self.active)
            self.active = max(min(self.active, len(self.ws.charts) - 1), 0)
            self._rebuild_charts()

    def add_channel(self, channel: str):
        if self.session is None:
            return
        if not self.ws.charts:
            self.ws.add_chart()
        if self.ws.add_channel(self.active, channel):
            self._rebuild_charts()

    def select_chart(self, index: int):
        self.active = index
        for k, plot in enumerate(self._plots):
            plot.getAxis("left").setPen("#B9A6FF" if k == index else "#2A2A33")

    def reset_view(self):
        self.ws.reset_view()
        if self._plots:
            self._plots[0].setXRange(*self.ws.window(), padding=0)

    def _rebuild_charts(self):
        for view in self._extra_views:             # second and further axes live in the scene, not in a plot
            if view.scene() is not None:
                view.scene().removeItem(view)
        self.graphs.clear()
        self._plots, self._cursors, self._curves, self._extra_views, self._regions = [], [], [], [], []
        t0, t1 = self.ws.window()
        # every chart keeps the same room for axes on both sides, so the same time is at the same
        # place across the stack whatever each chart shows
        most_extra = max((len(self.ws.units_of(k)) - 1 for k in range(len(self.ws.charts))), default=0)
        for k, chart in enumerate(self.ws.charts):
            plot = self.graphs.addPlot(row=k, col=0)
            plot.getAxis("left").setWidth(AXIS_WIDTH)
            for i in range(1, most_extra + 1):
                plot.layout.setColumnFixedWidth(1 + i, AXIS_WIDTH)
            plot.showGrid(x=True, y=True, alpha=0.15)
            plot.setMouseEnabled(x=True, y=False)
            plot.setMenuEnabled(False)
            if k:
                plot.setXLink(self._plots[0])
            if k == len(self.ws.charts) - 1:
                plot.setLabel("bottom", "Session time", units="s")
            # One y-axis per unit: the first unit on the left, each further one on the right with
            # its own scale, so a temperature and a speed can share a chart without one flattening the other.
            units = self.ws.units_of(k)
            views = {}
            for i, unit in enumerate(units):
                colour = COLOURS[next((n for n, name in enumerate(chart.channels)
                                       if self.ws.model.has(name) and self.ws.model.get(name).unit == unit), 0) % len(COLOURS)]
                if i == 0:
                    plot.setLabel("left", unit if len(units) > 1 else None)
                    plot.getAxis("left").setTextPen(colour if len(units) > 1 else "#9A97A6")
                    views[unit] = None
                    continue
                view = pg.ViewBox()
                view.setMouseEnabled(False, False)
                view.setAcceptedMouseButtons(QtCore.Qt.NoButton)      # clicks and drags go to the chart underneath
                view.wheelEvent = lambda event: event.ignore()
                if i == 1:
                    plot.showAxis("right")
                    axis = plot.getAxis("right")
                else:
                    axis = pg.AxisItem("right")
                    plot.layout.addItem(axis, 2, 1 + i)
                axis.linkToView(view)
                axis.setWidth(AXIS_WIDTH)
                axis.setLabel(unit)
                axis.setTextPen(colour)
                axis.setPen(colour)
                plot.scene().addItem(view)
                view.enableAutoRange(axis=pg.ViewBox.YAxis)

                # The extra view sits exactly over the chart and shows the same stretch of time. It is
                # kept in step by hand: linking the two views lets each resize the other without end.
                def place(_=None, view=view, plot=plot):          # the signal passes the chart's own view first
                    rect = plot.vb.sceneBoundingRect()
                    if view.sceneBoundingRect() != rect:
                        view.setGeometry(rect)

                def follow(_=None, span=None, view=view, plot=plot):
                    view.setXRange(*(span or plot.vb.viewRange()[0]), padding=0)
                plot.vb.sigResized.connect(place)
                plot.vb.sigXRangeChanged.connect(follow)
                place()
                follow()
                views[unit] = view
                self._extra_views.append(view)
            curves, key = [], []
            for n, name in enumerate(chart.channels):
                if self.ws.model is None or not self.ws.model.has(name):
                    curves.append(None)
                    continue
                c = self.ws.model.get(name)
                pen, label = pg.mkPen(COLOURS[n % len(COLOURS)], width=1), f"{c.id} [{c.unit}]"
                if views.get(c.unit) is None:
                    curve = plot.plot([], [], pen=pen, connect="finite")
                else:
                    curve = pg.PlotDataItem([], [], pen=pen, connect="finite")
                    views[c.unit].addItem(curve)
                key.append((label, COLOURS[n % len(COLOURS)], 0))
                curves.append(curve)
            set_key(plot, key)
            cursor = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen("#F4F2F8", width=1))
            cursor.sigPositionChanged.connect(self._cursor_dragged)
            plot.addItem(cursor)
            plot.scene().sigMouseClicked.connect(self._clicked)
            if self.ws.selection:
                region = pg.LinearRegionItem(self.ws.selection, brush=pg.mkBrush(185, 166, 255, 40), pen=pg.mkPen("#B9A6FF"))
                region.setZValue(-5)
                region.sigRegionChanged.connect(self._region_dragged)
                plot.addItem(region)
                self._regions.append(region)
            self._plots.append(plot)
            self._cursors.append(cursor)
            self._curves.append(curves)
        self._even.set_plots(self._plots)
        if self._plots:
            self._plots[0].setXRange(t0, t1, padding=0)
            self._plots[0].sigXRangeChanged.connect(self._range_changed)
            self.select_chart(self.active)
        if self.session is not None:
            # the workspace's saved laps apply when a session or workspace is opened; after that the
            # comparison keeps whatever the user has chosen in it
            self.compare.set_session(self.session.model, self.ws.charts, self.ws.compare if self._apply_compare else None)
            self._apply_compare = False
            self.ws.compare = self.compare.cmp.to_dict() if self.compare.cmp else self.ws.compare
        self.panel.set_drawn({c for chart in self.ws.charts for c in chart.channels})
        missing = self.ws.missing()
        self.unavailable.setText(("Not in this session: " + ", ".join(missing)) if missing else "")
        self._draw_curves()
        self._draw_annotations()
        self._show_range()
        self.set_cursor(self.ws.cursor if self.ws.cursor is not None else self.ws.extent()[0])

    def _range_changed(self, _, span):
        self.ws.zoom(*span)
        self._redraw_timer.start()                 # redraw once the gesture settles, not on every event

    def _draw_curves(self):
        """Fill every curve with what is in view, decimated to the chart's width."""
        width = max(int(self.graphs.width()), 400)
        for chart, curves in zip(self.ws.charts, self._curves):
            for name, curve in zip(chart.channels, curves):
                if curve is None:
                    continue
                s = self.ws.series(name, width)
                if s["discrete"] and len(s["t"]) > 1:
                    # a held signal is drawn as steps: each value runs to the next sample
                    curve.setData(np.repeat(s["t"], 2)[1:], np.repeat(s["v"], 2)[:-1])
                else:
                    curve.setData(s["t"], s["v"])

    # ── a marked range ────────────────────────────────────────────────────────

    def mark_range(self, on: bool = True, t0: float | None = None, t1: float | None = None):
        """Mark a stretch of the session for statistics (default: the middle
        third of what is in view), or clear the mark."""
        if self.session is None:
            self.range_action.setChecked(False)
            return
        if on:
            lo, hi = self.ws.window()
            self.ws.select(lo + (hi - lo) / 3 if t0 is None else t0, hi - (hi - lo) / 3 if t1 is None else t1)
        else:
            self.ws.select(None)
        self.range_action.setChecked(bool(self.ws.selection))
        self._rebuild_charts()
        if self.ws.selection:
            self.range_dock.show()
            self.range_dock.raise_()

    def zoom_to_range(self):
        if self.ws.selection and self._plots:
            self._plots[0].setXRange(*self.ws.zoom(*self.ws.selection), padding=0)

    def _region_dragged(self, region):
        if self._syncing:
            return
        self.ws.select(*region.getRegion())
        self._syncing = True
        for other in self._regions:
            if other is not region and self.ws.selection:
                other.setRegion(self.ws.selection)
        self._syncing = False
        self._range_timer.start()                  # the figures follow once the drag pauses

    def _show_range(self):
        """Fill the range table from full-resolution data."""
        chosen = self.ws.selection
        rows = self.ws.range_stats()
        if not chosen:
            self.range_label.setText("No range marked. Charts ▸ Mark a range (⌘R) puts one on the charts; drag its edges.")
        else:
            laps = sorted({lap.lap for lap in self.ws.model.laps if lap.t_end > chosen[0] and lap.t_start < chosen[1]}, key=str)
            self.range_label.setText(f"{chosen[0]:.3f} s to {chosen[1]:.3f} s  ·  {chosen[1] - chosen[0]:.3f} s"
                                     + (f"  ·  lap {laps[0]}" if len(laps) == 1 else f"  ·  laps {laps[0]}–{laps[-1]}" if laps else ""))
        num = lambda x: "—" if x is None else f"{x:.4g}"
        self.range_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            cells = (row["channel"], row["unit"], num(row["min"]), num(row["max"]), num(row["mean"]), num(row["std"]),
                     num(row["start"]), num(row["end"]), "—" if row["change"] is None else f"{row['change']:+.4g}", f"{row['samples']:,}")
            for k, text in enumerate(cells):
                self.range_table.setItem(r, k, QtWidgets.QTableWidgetItem(text))
        self.range_table.resizeColumnsToContents()
