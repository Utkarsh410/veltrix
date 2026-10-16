"""
Keeping a stack of charts evenly laid out.

Every chart in a stack gets the same room for its y-axis, so the same x is at
the same place down the whole stack, and the same height for its plotting
area, whichever chart carries the x-axis numbers at the bottom.
"""

from __future__ import annotations

from PySide6 import QtCore

AXIS_WIDTH = 58           # pixels kept for each y-axis, the same on every chart
X_AXIS_HEIGHT = 40        # the bottom chart's x-axis: numbers and its label
X_AXIS_BARE = 8           # the other charts' x-axis: tick marks only


class EvenRows(QtCore.QObject):
    """Gives the charts of a pyqtgraph GraphicsLayoutWidget equal plotting
    heights, and keeps them equal when the widget is resized."""

    def __init__(self, widget):
        super().__init__(widget)
        self.widget, self.plots, self._rows = widget, [], 0
        widget.installEventFilter(self)

    def set_plots(self, plots: list):
        """The charts now in the widget, top to bottom, one per row."""
        self.plots = list(plots)
        for k, plot in enumerate(self.plots):
            last = k == len(self.plots) - 1
            plot.getAxis("left").setWidth(AXIS_WIDTH)
            plot.getAxis("bottom").setStyle(showValues=last)
            plot.getAxis("bottom").setHeight(X_AXIS_HEIGHT if last else X_AXIS_BARE)
        self.apply()

    def apply(self):
        layout = self.widget.ci.layout
        n = len(self.plots)
        for row in range(n, self._rows):       # rows the stack no longer has take no room
            layout.setRowFixedHeight(row, 0)
        self._rows = n
        if not n:
            return
        left, top, right, bottom = layout.getContentsMargins()
        spacing = layout.verticalSpacing() if layout.verticalSpacing() >= 0 else 0
        room = self.widget.height() - top - bottom - spacing * (n - 1) - X_AXIS_BARE * (n - 1) - X_AXIS_HEIGHT
        each = max(room / n, 40.0)
        for row in range(n):
            layout.setRowFixedHeight(row, each + (X_AXIS_HEIGHT if row == n - 1 else X_AXIS_BARE))

    def eventFilter(self, obj, event):
        if obj is self.widget and event.type() == QtCore.QEvent.Resize:
            QtCore.QTimer.singleShot(0, self.apply)      # after the widget has its new size
        return False
