"""
The maths channels dock: the list of formula channels and whether each one
computes on the open session, with the way into the editor
(`veltrix_app.maths_editor`), where they are written, checked and previewed.
"""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from .maths_editor import MathsEditor


class MathsDock(QtWidgets.QDockWidget):
    changed = QtCore.Signal()                 # definitions were saved or deleted: recompute and refresh
    preview = QtCore.Signal(str)              # plot this channel on the active chart

    def __init__(self, path: str | Path, parent=None):
        super().__init__("Maths channels", parent)
        self.editor = MathsEditor(path, parent)
        self.editor.changed.connect(self._editor_changed)
        self.editor.plot_requested.connect(self.preview)

        self.list = QtWidgets.QTableWidget(0, 3)
        self.list.setHorizontalHeaderLabels(("Channel", "Unit", "Formula"))
        self.list.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.list.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.list.verticalHeader().hide()
        self.list.horizontalHeader().setStretchLastSection(True)
        self.list.itemDoubleClicked.connect(lambda item: self.open_editor(self.list.item(item.row(), 0).text()))
        new, edit, delete, plot = (QtWidgets.QPushButton(t) for t in ("New…", "Edit…", "Delete", "Plot on selected chart"))
        new.clicked.connect(lambda: self.open_editor(None))
        edit.clicked.connect(lambda: self.open_editor(self.selected()))
        delete.clicked.connect(self.delete_channel)
        plot.clicked.connect(lambda: self.selected() and self.preview.emit(self.selected()))
        self.status = QtWidgets.QLabel(self.editor.load_error or "No maths channels yet. Press New… to write one.")
        self.status.setWordWrap(True)
        row = QtWidgets.QHBoxLayout()
        for b in (new, edit, delete, plot):
            row.addWidget(b)
        row.addStretch(1)
        box = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.status)
        layout.addLayout(row)
        self.setWidget(box)
        self._refresh()

    @property
    def mset(self):
        return self.editor.mset

    @property
    def results(self) -> dict:
        return self.editor.results

    def compute(self, model) -> dict:
        """Compute every saved channel on a session. Returns {name: Result}."""
        results = self.editor.compute(model)
        self._refresh()
        return results

    def selected(self) -> str | None:
        row = self.list.currentRow()
        return self.list.item(row, 0).text() if row >= 0 else None

    def open_editor(self, name: str | None = None):
        """Show the editor, on a saved channel or on a blank one."""
        if name and name in self.mset.channels:
            self.editor.load(name)
        else:
            self.editor.new_channel()
        self.editor.show()
        self.editor.raise_()
        self.editor.activateWindow()

    def delete_channel(self) -> bool:
        name = self.selected()
        if not name:
            return False
        done = self.editor.delete_channel(name)
        if not done:
            self.status.setText(self.editor.status.text())
        return done

    def _editor_changed(self):
        self._refresh()
        self.changed.emit()

    def _refresh(self):
        keep = self.selected()
        self.list.setRowCount(len(self.mset.channels))
        failed = []
        for r, (name, c) in enumerate(self.mset.channels.items()):
            result = self.results.get(name)
            ok = result is None or result.ok
            unit = result.channel.unit if result is not None and result.ok else c.unit
            for k, text in enumerate((name, unit, c.formula)):
                item = QtWidgets.QTableWidgetItem(text)
                item.setToolTip(c.description or c.formula if ok else result.message)
                if not ok:
                    item.setForeground(QtGui.QColor("#C0392B"))
                self.list.setItem(r, k, item)
            if not ok:
                failed.append(name)
            if name == keep:
                self.list.selectRow(r)
        self.list.resizeColumnToContents(0)
        if not self.mset.channels:
            self.status.setText(self.editor.load_error or "No maths channels yet. Press New… to write one.")
        elif failed:
            self.status.setText(f"Not calculated for this session: {', '.join(failed)}. Hover a row for the reason.")
        else:
            self.status.setText(f"{len(self.mset.channels)} maths channel(s). Double-click one to edit it.")
