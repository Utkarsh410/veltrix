"""The events and annotations dock: detected events in one list, the user's
notes in another, each row a jump to that moment."""

from __future__ import annotations

import json

from PySide6 import QtCore, QtGui, QtWidgets

from .events import describe

EVENT_COLUMNS = ("Lap", "What", "Status", "Severity", "Confidence", "Where", "At (m)", "Found with", "Review", "Detail")
STATUS_TEXT = {"candidate": "candidate", "recorded": "recorded by the simulator", "confirmed": "confirmed",
               "rejected": "rejected"}
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
    review = QtCore.Signal(int, str, str)     # event row, verdict ("confirmed" / "rejected" / "" for a note), note
    clear_review = QtCore.Signal(int)         # event row
    def __init__(self, parent=None):
        super().__init__("Events and notes", parent)
        self.summary = QtWidgets.QLabel("Open a session to look for events.")
        self.summary.setWordWrap(True)
        self.events = _table(EVENT_COLUMNS)
        self.notes = _table(NOTE_COLUMNS)
        self.events.cellClicked.connect(lambda r, _: self._go(self.events, r))
        self.events.itemSelectionChanged.connect(self._show_detail)
        self.model = None
        confirm, reject = QtWidgets.QPushButton("Confirm"), QtWidgets.QPushButton("Reject")
        note, clear = QtWidgets.QPushButton("Note…"), QtWidgets.QPushButton("Clear review")
        confirm.setToolTip("You have checked this event and it happened (video, driver, data)")
        reject.setToolTip("You have checked this event and it did not happen: a false detection")
        note.setToolTip("Add a note to this event, with or without a verdict")
        clear.setToolTip("Take your verdict and note off this event; it goes back to how it was detected")
        confirm.clicked.connect(lambda: self._review("confirmed"))
        reject.clicked.connect(lambda: self._review("rejected"))
        note.clicked.connect(lambda: self._review(None))
        clear.clicked.connect(lambda: self.events.currentRow() >= 0 and self.clear_review.emit(self.events.currentRow()))
        self.review_buttons = (confirm, reject, note, clear)
        reviewing = QtWidgets.QHBoxLayout()
        reviewing.addWidget(QtWidgets.QLabel("Selected event:"))
        for b in self.review_buttons:
            reviewing.addWidget(b)
        reviewing.addStretch(1)
        self.detail = QtWidgets.QPlainTextEdit(readOnly=True)
        self.detail.setPlaceholderText("Select an event to see what it was found with, as detected, and its review history.")
        self.detail.setMaximumHeight(130)
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
        layout.addLayout(reviewing)
        layout.addWidget(self.detail)
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

    def _review(self, verdict):
        row = self.events.currentRow()
        if row < 0 or self.model is None:
            return
        e = self.model.events[row]
        kept = (e.review or {}).get("note", "")
        if verdict is None:                    # a note, keeping whatever verdict there is
            text, ok = QtWidgets.QInputDialog.getText(self, "Note on this event", "Note:", text=kept)
            if not ok:
                return
            self.review.emit(row, (e.review or {}).get("verdict", ""), text)
        else:
            self.review.emit(row, verdict, kept)

    def _show_detail(self):
        row = self.events.currentRow()
        if self.model is None or not 0 <= row < len(self.model.events):
            self.detail.clear()
            return
        e = self.model.events[row]
        lines = [f"{e.kind} — {STATUS_TEXT.get(e.status, e.status)}; found by {e.source or e.rule}"
                 + (f" (rule {e.rule} v{e.rule_version})" if e.rule_version else ""),
                 f"Confidence: {e.confidence}"]
        if e.thresholds:
            lines.append("Found with: " + json.dumps(e.thresholds, default=str))
        if e.evidence:
            lines.append("Evidence: " + json.dumps({k: v for k, v in e.evidence.items() if k != "detected_status"}, default=str))
        if e.review:
            lines.append(f"Review: {e.review.get('verdict') or 'note only'} by {e.review.get('author') or 'you'} "
                         f"at {e.review.get('at', '')}" + (f" — {e.review['note']}" if e.review.get("note") else ""))
        if e.evidence.get("detected_status"):
            lines.append(f"As detected: {STATUS_TEXT.get(e.evidence['detected_status'], e.evidence['detected_status'])}")
        self.detail.setPlainText("\n".join(lines))

    def show_model(self, model, summary: str):
        self.model = model
        self.summary.setText(summary)
        rows = [describe(e) for e in model.events]
        keep = self.events.currentRow()
        self.events.setRowCount(len(rows))
        for r, d in enumerate(rows):
            review = d["review"]
            said = (review.get("verdict") or "note") + (f": {review['note']}" if review.get("note") else "") if review else ""
            cells = (str(d["lap"]), d["what"], STATUS_TEXT.get(d["status"], d["status"]), d["severity"], d["confidence"],
                     d["where"], "" if d["position_m"] is None else f"{d['position_m']:.0f}", d["limits"], said, d["detail"])
            for k, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                item.setData(QtCore.Qt.UserRole, d["time"])
                if d["status"] == "rejected":
                    item.setForeground(QtGui.QColor("#8A8794"))
                    f = item.font()
                    f.setStrikeOut(k == 1)
                    item.setFont(f)
                self.events.setItem(r, k, item)
        self.events.resizeColumnsToContents()
        if 0 <= keep < len(rows):
            self.events.selectRow(keep)
        self._show_detail()
        self.notes.setRowCount(len(model.annotations))
        for r, a in enumerate(model.annotations):
            cells = ("" if a.lap is None else str(a.lap), "session" if a.time is None else f"{a.time:.2f}", a.category, a.text, a.author)
            for k, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                item.setData(QtCore.Qt.UserRole, a.time)
                self.notes.setItem(r, k, item)
        self.notes.resizeColumnsToContents()
