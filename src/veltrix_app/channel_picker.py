"""Choosing which channels of a large file to load. Formats that are read
lazily (MDF) can leave the rest on disk, which is the difference between
seconds and minutes on a long log with hundreds of channels."""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

COLUMNS = ("Load", "Channel", "Unit", "Hz", "Samples", "Group")


class ChannelPicker(QtWidgets.QDialog):
    def __init__(self, info: dict, parent=None):
        """info: what `veltrix_engine.importers.inspect_file` returned."""
        super().__init__(parent)
        self.setWindowTitle(f"Choose channels — {info.get('file', 'file')}")
        self.resize(760, 620)
        self.info = info
        duration = info.get("duration_s")
        rates = ", ".join(f"{r:g}" for r in info.get("rates_hz", [])) or "unknown"
        lead = (f"{info.get('format', '')} {info.get('version', '')}: {info['channel_count']} channels in {info['group_count']} "
                f"group(s), rates {rates} Hz" + (f", {duration / 60:.1f} minutes" if duration else "") + ".")
        if info.get("missing_units"):
            lead += f" {len(info['missing_units'])} channel(s) have no unit in the file."
        if info.get("raw_can_frames"):
            lead += f" {info['raw_can_frames']:,} raw CAN frames need a DBC to be decoded."
        for line in info.get("unsupported", []):
            lead += f" Not read: {line}."
        self.summary = QtWidgets.QLabel(lead)
        self.summary.setWordWrap(True)
        self.search = QtWidgets.QLineEdit(placeholderText="Search channels…")
        self.search.textChanged.connect(self._filter)
        self.table = QtWidgets.QTableWidget(len(info["channels"]), len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        for r, c in enumerate(info["channels"]):
            tick = QtWidgets.QTableWidgetItem()
            tick.setFlags(QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled)
            tick.setCheckState(QtCore.Qt.Checked)
            self.table.setItem(r, 0, tick)
            rate = c.get("rate_hz")
            for k, text in enumerate((c["name"], c.get("unit") or "—", f"{rate:g}" if rate else "", f"{c.get('samples', 0):,}",
                                      str(c.get("group", ""))), start=1):
                self.table.setItem(r, k, QtWidgets.QTableWidgetItem(text))
        self.table.resizeColumnsToContents()
        self.table.itemChanged.connect(lambda _: self._count())
        self.count = QtWidgets.QLabel()
        every, none = QtWidgets.QPushButton("Tick all shown"), QtWidgets.QPushButton("Untick all shown")
        every.clicked.connect(lambda: self.set_shown(True))
        none.clicked.connect(lambda: self.set_shown(False))
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("Load these")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        row = QtWidgets.QHBoxLayout()
        for w in (every, none, self.count):
            row.addWidget(w)
        row.addStretch(1)
        row.addWidget(buttons)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addWidget(self.search)
        layout.addWidget(self.table, 1)
        layout.addLayout(row)
        self._count()

    def _filter(self, text: str):
        words = text.lower().split()
        for r in range(self.table.rowCount()):
            row = " ".join(self.table.item(r, k).text().lower() for k in range(1, len(COLUMNS)))
            self.table.setRowHidden(r, not all(w in row for w in words))

    def set_shown(self, on: bool):
        """Tick or untick every channel the search currently shows."""
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            if not self.table.isRowHidden(r):
                self.table.item(r, 0).setCheckState(QtCore.Qt.Checked if on else QtCore.Qt.Unchecked)
        self.table.blockSignals(False)
        self._count()

    def chosen(self) -> list:
        return [self.table.item(r, 1).text() for r in range(self.table.rowCount())
                if self.table.item(r, 0).checkState() == QtCore.Qt.Checked]

    def _count(self):
        n = len(self.chosen())
        self.count.setText(f"{n} of {self.table.rowCount()} channels will be loaded; the rest stay on disk.")
