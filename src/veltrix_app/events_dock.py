"""The events and annotations dock: detected events in one list, the user's
notes in another, each row a jump to that moment."""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from .events import describe

EVENT_COLUMNS = ("Lap", "What", "Severity", "Where", "At (m)", "Detail")
NOTE_COLUMNS = ("Lap", "At (s)", "Category", "Note", "Author")


def _table(columns) -> QtWidgets.QTableWidget:
    t = QtWidgets.QTableWidget(0, len(columns))
    t.setHorizontalHeaderLabels(columns)
    t.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
    t.verticalHeader().hide()
    t.horizontalHeader().setStretchLastSection(True)
    return t


class EventsDock(QtWidgets.QDockWidget):
    jump = QtCore.Signal(float)               # go to this session time
    add_note = QtCore.Signal(str, str)        # text, category — at the cursor
    remove_note = QtCore.Signal(int)

    def __init__(self, parent=None):
        super().__init__("Events and notes", parent)
        self.summary = QtWidgets.QLabel("Open a session to look for events.")
        self.summary.setWordWrap(True)
        self.events = _table(EVENT_COLUMNS)
        self.notes = _table(NOTE_COLUMNS)
        self.events.cellClicked.connect(lambda r, _: self._go(self.events, r))
        self.notes.cellClicked.connect(lambda r, _: self._go(self.notes, r))
        self.text = QtWidgets.QLineEdit(placeholderText="Note at the cursor, e.g. driver reported a snap on exit")
        self.category = QtWidgets.QLineEdit(placeholderText="Category")
        self.category.setMaximumWidth(120)
        add, remove = QtWidgets.QPushButton("Add note at cursor"), QtWidgets.QPushButton("Remove selected")
        add.clicked.connect(self._add)
        self.text.returnPressed.connect(self._add)
        remove.clicked.connect(lambda: self.notes.currentRow() >= 0 and self.remove_note.emit(self.notes.currentRow()))
        row = QtWidgets.QHBoxLayout()
        for w in (self.text, self.category, add, remove):
            row.addWidget(w, 1 if w is self.text else 0)
        box = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(QtWidgets.QLabel("Detected events — candidates for review, not findings"))
        layout.addWidget(self.summary)
        layout.addWidget(self.events, 2)
        layout.addWidget(QtWidgets.QLabel("Your notes"))
        layout.addWidget(self.notes, 1)
        layout.addLayout(row)
        self.setWidget(box)

    def _go(self, table, row):
        t = table.item(row, 0).data(QtCore.Qt.UserRole)
        if t is not None:
            self.jump.emit(float(t))

    def _add(self):
        if self.text.text().strip():
            self.add_note.emit(self.text.text(), self.category.text())
            self.text.clear()

    def show_model(self, model, summary: str):
        self.summary.setText(summary)
        rows = [describe(e) for e in model.events]
        self.events.setRowCount(len(rows))
        for r, d in enumerate(rows):
            cells = (str(d["lap"]), d["what"], d["severity"], d["where"],
                     "" if d["position_m"] is None else f"{d['position_m']:.0f}", d["detail"])
            for k, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                item.setData(QtCore.Qt.UserRole, d["time"])
                self.events.setItem(r, k, item)
        self.events.resizeColumnsToContents()
        self.notes.setRowCount(len(model.annotations))
        for r, a in enumerate(model.annotations):
            cells = ("" if a.lap is None else str(a.lap), "session" if a.time is None else f"{a.time:.2f}", a.category, a.text, a.author)
            for k, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                item.setData(QtCore.Qt.UserRole, a.time)
                self.notes.setItem(r, k, item)
        self.notes.resizeColumnsToContents()
