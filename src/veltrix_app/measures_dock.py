"""
The "Measures and laps" panel down the left of the window, laid out the way
race-data programs do it.

  Measures  "Tests loaded" — the open session and the laps loaded from it,
            each with its curve colour and an X to unload it — and under it
            "Measures": one button per channel, pressed in when it is drawn,
            with its value at the cursor beside it (one value per loaded lap).
  Laps      every lap of the session; click a lap to load it, click it again
            to unload it. Loaded laps are drawn over each other.
  Channel details   where each channel came from and how it was mapped.

The panel only shows and asks: the window decides what loading a lap or
pressing a measure does.
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from .compare_view import LAP_COLOURS

LAP_COLUMNS = ("Lap", "Time", "Gap", "Status")
ROW_HEIGHT = 28
ID = QtCore.Qt.UserRole
GREY, NAVY = "#8A8794", "#14147A"
BAR = f"background: {NAVY}; color: white; font-weight: 600; padding: 3px;"
VALUE = "background: #15141B; color: #F4F2F8; padding: 2px 6px; font-family: Menlo, Consolas, monospace;"
MEASURE = ("QPushButton { text-align: left; padding: 5px 8px; border: 1px solid #A9A7B3; border-radius: 2px;"
           " background: #E4E3EA; color: #1B1A22; }"
           "QPushButton:checked { background: #FFFFFF; border: 2px solid #14147A; font-weight: 600; }")
SMALL = "QToolButton { border: 1px solid #A9A7B3; background: #F2F1F6; color: #1B1A22; font-weight: 600; }"


def lap_time(seconds: float) -> str:
    m, s = divmod(float(seconds), 60.0)
    return f"{int(m)}:{s:06.3f}"


def lap_colour(index: int) -> str:
    return LAP_COLOURS[index % len(LAP_COLOURS)]


def _swatch(colour: str | None, size: int = 14) -> QtGui.QIcon:
    """A ticked square in a lap's curve colour, or an empty box for a lap that is not loaded."""
    pix = QtGui.QPixmap(size, size)
    pix.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pix)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.setPen(QtGui.QPen(QtGui.QColor(GREY), 1.2))
    painter.setBrush(QtGui.QColor(colour) if colour else QtCore.Qt.NoBrush)
    painter.drawRoundedRect(QtCore.QRectF(1, 1, size - 2, size - 2), 3, 3)
    if colour:                                 # a tick, dark or light whichever shows on the colour
        dark = QtGui.QColor(colour).lightness() > 140
        painter.setPen(QtGui.QPen(QtGui.QColor("#1B1A22" if dark else "#FFFFFF"), 1.8))
        painter.drawPolyline([QtCore.QPointF(size * 0.25, size * 0.52), QtCore.QPointF(size * 0.43, size * 0.70),
                              QtCore.QPointF(size * 0.76, size * 0.30)])
    painter.end()
    return QtGui.QIcon(pix)


def _bar(text: str) -> QtWidgets.QLabel:
    """A dark heading bar across the panel."""
    label = QtWidgets.QLabel(text)
    label.setAlignment(QtCore.Qt.AlignCenter)
    label.setStyleSheet(BAR)
    return label


def _clear(layout):
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)             # gone now, not when the event loop next runs
            widget.deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())


def _scroll(inner: QtWidgets.QWidget) -> QtWidgets.QScrollArea:
    area = QtWidgets.QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QtWidgets.QFrame.NoFrame)
    area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
    area.setWidget(inner)
    return area


class MeasuresPanel(QtWidgets.QWidget):
    lapToggled = QtCore.Signal(object)         # a lap was clicked in the Laps tab, or its X in "Tests loaded"
    lapsRequested = QtCore.Signal(list)        # load exactly these laps (the first is the reference)
    referenceRequested = QtCore.Signal(object)
    measureToggled = QtCore.Signal(str, bool)  # a channel's button was pressed in or let out

    def __init__(self, details: QtWidgets.QWidget | None = None, parent=None):
        super().__init__(parent)
        self.laps: list = []                   # the session's laps
        self.loaded_laps: list = []            # labels of the loaded laps; the first is the reference
        self.source = ""
        self.measures: dict = {}               # channel -> its button
        self.values: dict = {}                 # channel -> the label with its value(s) at the cursor
        self.loaded: list = []                 # one entry per loaded lap: {"lap", "text", "remove", "reference"}
        self._units: dict = {}
        self._drawn: set = set()
        self.drawn_first = False

        # ── Measures tab ─────────────────────────────────────────────────────
        self.sort = QtWidgets.QPushButton("Sort channels")
        self.sort.setCheckable(True)
        self.sort.setToolTip("Pressed in: the channels being drawn come first. Out: in the session's order.")
        self.sort.toggled.connect(self._sort_toggled)
        self.channel_search = QtWidgets.QLineEdit(placeholderText="Search channels…")
        self.channel_search.setClearButtonEnabled(True)
        self.channel_search.textChanged.connect(self._filter_channels)
        tools = QtWidgets.QHBoxLayout()
        tools.addWidget(self.channel_search, 1)
        tools.addWidget(self.sort)

        loaded_inner = QtWidgets.QWidget()
        self.loaded_box = QtWidgets.QVBoxLayout(loaded_inner)
        self.loaded_box.setContentsMargins(0, 0, 0, 0)
        self.loaded_box.setSpacing(0)
        tests = QtWidgets.QWidget()
        tl = QtWidgets.QVBoxLayout(tests)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(0)
        tl.addWidget(_bar("Tests loaded:"))
        tl.addWidget(_scroll(loaded_inner), 1)

        measures_inner = QtWidgets.QWidget()
        self.grid = QtWidgets.QGridLayout(measures_inner)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(1)
        self.grid.setColumnStretch(0, 3)
        self.grid.setColumnStretch(1, 2)
        measures = QtWidgets.QWidget()
        ml = QtWidgets.QVBoxLayout(measures)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(0)
        ml.addWidget(_bar("Measures:"))
        ml.addWidget(_scroll(measures_inner), 1)

        self.split = QtWidgets.QSplitter(QtCore.Qt.Vertical)     # drag to give the loaded laps or the channels more room
        self.split.addWidget(tests)
        self.split.addWidget(measures)
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(7)
        self.split.setSizes([170, 560])
        measures_tab = QtWidgets.QWidget()
        mt = QtWidgets.QVBoxLayout(measures_tab)
        mt.setContentsMargins(4, 6, 4, 4)
        mt.setSpacing(4)
        mt.addLayout(tools)
        mt.addWidget(self.split, 1)

        # ── Laps tab ─────────────────────────────────────────────────────────
        self.hint = QtWidgets.QLabel("Click a lap to load it. Click it again to unload it. Loaded laps are drawn over each other.")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color: {GREY};")
        self.session_bar = _bar("No session open")
        self.lap_list = QtWidgets.QTreeWidget()
        self.lap_list.setHeaderLabels(LAP_COLUMNS)
        self.lap_list.setRootIsDecorated(False)
        self.lap_list.setUniformRowHeights(True)
        self.lap_list.setAlternatingRowColors(True)
        self.lap_list.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)   # loading is the click itself
        self.lap_list.setIconSize(QtCore.QSize(16, 16))
        self.lap_list.setStyleSheet(f"QTreeWidget::item {{ height: {ROW_HEIGHT}px; }}")
        self.lap_list.header().setStretchLastSection(True)       # the lap's name is never cut short
        for k in range(len(LAP_COLUMNS) - 1):
            self.lap_list.header().setSectionResizeMode(k, QtWidgets.QHeaderView.ResizeToContents)
        self.lap_list.itemClicked.connect(self._lap_clicked)
        self.best_two = QtWidgets.QPushButton("Best two laps")
        self.best_two.setToolTip("Load the fastest flying lap as the reference, and the next fastest against it")
        self.best_two.clicked.connect(self._load_best_two)
        self.unload = QtWidgets.QPushButton("Unload all")
        self.unload.setToolTip("Unload every lap and show the whole session again")
        self.unload.clicked.connect(lambda: self.lapsRequested.emit([]))
        quick = QtWidgets.QHBoxLayout()
        quick.addWidget(self.best_two)
        quick.addWidget(self.unload)
        quick.addStretch(1)
        laps_tab = QtWidgets.QWidget()
        lt = QtWidgets.QVBoxLayout(laps_tab)
        lt.setContentsMargins(4, 6, 4, 4)
        lt.setSpacing(4)
        lt.addWidget(self.hint)
        lt.addWidget(self.session_bar)
        lt.addWidget(self.lap_list, 1)
        lt.addLayout(quick)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(measures_tab, "Measures")
        self.tabs.addTab(laps_tab, "Laps")
        if details is not None:
            self.tabs.addTab(details, "Channel details")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs)
        self._show_loaded()

    # ── the session ───────────────────────────────────────────────────────────

    def set_session(self, source: str, laps: list, channels: list):
        """A session was opened. laps: the model's laps; channels: [(id, unit), ...] in listing order."""
        self.source, self.laps = source, list(laps)
        self._units = dict(channels)
        self.session_bar.setText(source or "Session")
        self.session_bar.setToolTip(source)
        self._fill_laps()
        self._fill_measures()
        self.set_loaded([x for x in self.loaded_laps if any(x == lap.lap for lap in self.laps)])

    def set_channels(self, channels: list):
        """The channels changed (a maths channel was added or removed)."""
        self._units = dict(channels)
        self._fill_measures()

    @staticmethod
    def _usable(lap) -> bool:
        return bool(lap.start_crossed and lap.end_crossed)

    def _best(self):
        flying = [x for x in self.laps if x.status == "flying" and self._usable(x)]
        return min(flying, key=lambda x: x.time_s) if flying else None

    def _fill_laps(self):
        best = self._best()
        self.lap_list.clear()
        bold = QtGui.QFont(self.lap_list.font())
        bold.setBold(True)
        for lap in self.laps:
            row = QtWidgets.QTreeWidgetItem(self.lap_list)
            row.setData(0, ID, lap.lap)
            row.setText(0, f"Lap {lap.lap}")
            row.setText(1, lap_time(lap.time_s))
            if best is not None and self._usable(lap):
                row.setText(2, f"+{lap.time_s - best.time_s:.3f}")
            row.setText(3, lap.status)
            row.setFlags(QtCore.Qt.ItemIsEnabled)
            tip = f"Lap {lap.lap} · {lap_time(lap.time_s)} · {lap.status}"
            if self._usable(lap):
                row.setIcon(0, _swatch(None))
                if lap is best:
                    row.setFont(1, bold)
                    tip += "\nFastest lap of the session"
            else:
                tip += "\nThis lap did not run from line to line, so it cannot be overlaid."
                for k in range(len(LAP_COLUMNS)):
                    row.setForeground(k, QtGui.QColor(GREY))
            for k in range(len(LAP_COLUMNS)):
                row.setToolTip(k, tip)

    def lap_row(self, label):
        for i in range(self.lap_list.topLevelItemCount()):
            if self.lap_list.topLevelItem(i).data(0, ID) == label:
                return self.lap_list.topLevelItem(i)
        return None

    def _lap_clicked(self, row, _column=0):
        lap = next((x for x in self.laps if x.lap == row.data(0, ID)), None)
        if lap is not None and self._usable(lap):
            self.lapToggled.emit(lap.lap)

    def _load_best_two(self):
        best = self._best()
        if best is None:
            return
        others = sorted((x for x in self.laps if x.status == "flying" and self._usable(x) and x is not best), key=lambda x: x.time_s)
        self.lapsRequested.emit([best.lap] + [x.lap for x in others[:1]])

    # ── loaded laps ───────────────────────────────────────────────────────────

    def set_loaded(self, labels: list):
        """These laps are loaded now, the first being the reference."""
        self.loaded_laps = list(labels)
        colour = {x: lap_colour(i) for i, x in enumerate(self.loaded_laps)}
        bold, plain = QtGui.QFont(self.lap_list.font()), QtGui.QFont(self.lap_list.font())
        bold.setBold(True)
        tint = QtGui.QColor(127, 120, 200, 46)
        for lap in self.laps:
            row = self.lap_row(lap.lap)
            if row is None or not self._usable(lap):
                continue
            on = lap.lap in colour
            row.setIcon(0, _swatch(colour.get(lap.lap)))
            row.setText(0, f"Lap {lap.lap}" + ("  — reference" if on and lap.lap == self.loaded_laps[0] else ""))
            row.setFont(0, bold if on else plain)
            for k in range(len(LAP_COLUMNS)):
                row.setBackground(k, QtGui.QBrush(tint) if on else QtGui.QBrush())
        self.tabs.setTabText(1, f"Laps ({len(self.loaded_laps)} loaded)" if self.loaded_laps else "Laps")
        self._show_loaded()

    def _show_loaded(self):
        """The "Tests loaded" list: the session, and its loaded laps under it."""
        _clear(self.loaded_box)
        self.loaded = []

        def x_button(tip, slot):
            b = QtWidgets.QToolButton()
            b.setText("X")
            b.setFixedSize(24, 24)
            b.setStyleSheet(SMALL)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            return b

        def line(*widgets):
            row = QtWidgets.QWidget()
            h = QtWidgets.QHBoxLayout(row)
            h.setContentsMargins(2, 1, 2, 1)
            h.setSpacing(4)
            for w in widgets:
                h.addWidget(w, 1 if isinstance(w, (QtWidgets.QLabel, QtWidgets.QPushButton)) else 0)
            self.loaded_box.addWidget(row)

        if self.source:
            session = QtWidgets.QLabel(self.source)
            session.setStyleSheet(f"background: {NAVY}; color: white; padding: 3px 6px;")
            session.setToolTip(self.source)
            line(x_button("Unload every lap and show the whole session", lambda: self.lapsRequested.emit([])), session)
        by_label = {lap.lap: lap for lap in self.laps}
        ref = by_label.get(self.loaded_laps[0]) if self.loaded_laps else None
        for i, label in enumerate(self.loaded_laps):
            lap = by_label[label]
            box = QtWidgets.QLabel()
            box.setFixedSize(22, 22)
            box.setStyleSheet(f"background: {lap_colour(i)}; border: 1px solid #55535F;")
            box.setToolTip("The colour of this lap's curves")
            text = f"{lap.lap}   {lap_time(lap.time_s)}   " + ("reference" if i == 0 else f"{lap.time_s - ref.time_s:+.3f}")
            name = QtWidgets.QPushButton(text)
            name.setFlat(True)
            name.setStyleSheet("QPushButton { text-align: left; padding: 3px 4px; border: none;"
                               + (" font-weight: 700; }" if i == 0 else " }"))
            name.setToolTip("The reference lap: the others are measured against it." if i == 0
                            else "Click to make this the reference lap.")
            name.clicked.connect(lambda _=False, x=label: self.referenceRequested.emit(x))
            remove = x_button("Unload this lap", lambda _=False, x=label: self.lapToggled.emit(x))
            line(remove, box, name)
            self.loaded.append({"lap": label, "text": text, "remove": remove, "reference": name})
        if not self.loaded_laps:
            empty = QtWidgets.QLabel("No laps loaded — the whole session is drawn.\nOpen the Laps tab and click laps to overlay them."
                                     if self.source else "Open a telemetry file to begin.")
            empty.setAlignment(QtCore.Qt.AlignCenter)
            empty.setWordWrap(True)
            empty.setStyleSheet(f"color: {GREY}; padding: 10px;")
            self.loaded_box.addWidget(empty)
        self.loaded_box.addStretch(1)

    # ── measures ──────────────────────────────────────────────────────────────

    def _fill_measures(self):
        names = list(self._units)
        if self.drawn_first:
            names = [c for c in names if c in self._drawn] + [c for c in names if c not in self._drawn]
        _clear(self.grid)
        for r in range(self.grid.rowCount()):
            self.grid.setRowStretch(r, 0)
        self.measures, self.values = {}, {}
        for r, name in enumerate(names):
            unit = self._units[name]
            button = QtWidgets.QPushButton(f"{name}   [{unit}]" if unit else name)
            button.setCheckable(True)
            button.setChecked(name in self._drawn)
            button.setStyleSheet(MEASURE)
            button.setMinimumHeight(ROW_HEIGHT + 4)
            button.setToolTip(f"Click to draw {name} on a chart of its own, or to stop drawing it.")
            button.clicked.connect(lambda on, name=name: self.measureToggled.emit(name, bool(on)))
            value = QtWidgets.QLabel("")
            value.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            value.setTextFormat(QtCore.Qt.RichText)
            value.setStyleSheet(VALUE)
            self.grid.addWidget(button, r, 0)
            self.grid.addWidget(value, r, 1)
            self.measures[name], self.values[name] = button, value
        self.grid.setRowStretch(len(names), 1)
        self._filter_channels()

    def set_drawn(self, channels):
        """These channels are on a chart now: their buttons are pressed in."""
        self._drawn = set(channels)
        if self.drawn_first:
            self._fill_measures()
            return
        for name, button in self.measures.items():
            button.setChecked(name in self._drawn)

    def _sort_toggled(self, on: bool):
        self.drawn_first = bool(on)
        shown = {name: label.text() for name, label in self.values.items()}
        self._fill_measures()
        for name, text in shown.items():
            if name in self.values:
                self.values[name].setText(text)

    def _filter_channels(self, *_):
        words = self.channel_search.text().lower().split()
        for name, button in self.measures.items():
            show = all(w in name.lower() for w in words)
            button.setVisible(show)
            self.values[name].setVisible(show)

    def channel_names(self, shown_only: bool = False) -> list:
        """The channels in the order they are listed."""
        return [n for n, b in self.measures.items() if not shown_only or not b.isHidden()]

    def show_values(self, values: dict):
        """values: channel -> [(text, colour), ...], one entry per loaded lap
        (or a single one for the whole session). Channels left out show nothing."""
        for name, label in self.values.items():
            rows = values.get(name)
            label.setText("<br>".join(f'<span style="color:{colour}">{text}</span>' for text, colour in rows) if rows else "")
