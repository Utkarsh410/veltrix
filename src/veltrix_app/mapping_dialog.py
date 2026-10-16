"""
The import mapping dialog: every source column with what the engine takes it
to be and why, for a person to check, change and confirm before the file is
analysed. The confirmed mapping can be saved as a profile for the next file
from the same logger.

The decisions are `veltrix_engine.mapping_profiles.decide`'s; the dialog only
shows them and collects the user's changes.
"""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from veltrix_engine.mapping_profiles import (Profile, best_profile, decide, diagnostics, profile_from_decisions,
                                             to_import_args)
from veltrix_engine.schema import CHANNELS, MEASURED

COLUMNS = ("Column in the file", "Channel", "Unit", "Decided by", "Confidence", "Confirmed", "Why / problems")
NOT_MAPPED = "— not mapped —"
BASIS_TEXT = {"metadata": "the file format", "profile": "verified profile", "plausibility": "name + values (suggestion)",
              "user": "you", "unmapped": ""}


class MappingDialog(QtWidgets.QDialog):
    def __init__(self, raw, profile: Profile | None = None, profiles_dir: str | Path | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Map channels — {raw.source or 'file'}")
        self.resize(1100, 640)
        self.raw, self.profile, self.profiles_dir = raw, profile, Path(profiles_dir) if profiles_dir else None
        self.profile_note = ""
        if self.profile is None and self.profiles_dir is not None:       # a saved profile that fits this file is applied
            self.profile, self.profile_note = best_profile(raw, self.profiles_dir)
        self.user: dict = {}                  # {column: channel or None} chosen here
        self.units: dict = {}                 # {column: unit} typed here
        self.confirmed: set = set()           # columns the user ticked
        self.decisions: list = []
        self._filling = False

        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        self.table = QtWidgets.QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemChanged.connect(self._item_changed)
        load, save = QtWidgets.QPushButton("Load profile…"), QtWidgets.QPushButton("Save as profile…")
        confirm_all = QtWidgets.QPushButton("Confirm all without problems")
        load.clicked.connect(self._choose_profile)
        save.clicked.connect(self._choose_save)
        confirm_all.clicked.connect(self.confirm_all)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("Import")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        row = QtWidgets.QHBoxLayout()
        for w in (load, save, confirm_all):
            row.addWidget(w)
        row.addStretch(1)
        row.addWidget(buttons)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addWidget(self.table, 1)
        layout.addLayout(row)
        self.refresh()

    # ── decisions ─────────────────────────────────────────────────────────────

    def refresh(self):
        raw = self.raw
        if self.units:                         # a unit typed here is the file's unit as far as the decision goes
            from copy import copy
            raw = copy(self.raw)
            raw.units = {**self.raw.units, **self.units}
        self.decisions = decide(raw, self.profile, self.user)
        for d in self.decisions:
            if d.column in self.units:
                d.unit_source = "user"
            if d.column in self.confirmed and d.channel:
                d.confirmed = True
        self._fill()

    def _fill(self):
        self._filling = True
        self.table.setRowCount(len(self.decisions))
        for r, d in enumerate(self.decisions):
            name = QtWidgets.QTableWidgetItem(d.column)
            name.setFlags(name.flags() & ~QtCore.Qt.ItemIsEditable)
            self.table.setItem(r, 0, name)
            combo = QtWidgets.QComboBox()
            combo.addItem(NOT_MAPPED, None)
            for cid in MEASURED:
                combo.addItem(cid, cid)
                combo.setItemData(combo.count() - 1, f"{CHANNELS[cid].description} [{CHANNELS[cid].unit}]", QtCore.Qt.ToolTipRole)
            combo.setCurrentIndex(max(combo.findData(d.channel), 0))
            combo.currentIndexChanged.connect(lambda _, col=d.column, box=combo: self.set_channel(col, box.currentData()))
            self.table.setCellWidget(r, 1, combo)
            unit = QtWidgets.QTableWidgetItem(d.unit)
            unit.setToolTip({"file": "from the file", "profile": "from the profile", "user": "typed here",
                             "none": "the file gives no unit — type one if you know it"}.get(d.unit_source, ""))
            if d.unit == "unknown":
                unit.setForeground(QtGui.QColor("#B0701E"))
            self.table.setItem(r, 2, unit)
            for k, text in ((3, BASIS_TEXT[d.basis]), (4, f"{d.confidence:.0%}" if d.channel else "")):
                item = QtWidgets.QTableWidgetItem(text)
                item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEditable)
                self.table.setItem(r, k, item)
            tick = QtWidgets.QTableWidgetItem()
            tick.setFlags((tick.flags() | QtCore.Qt.ItemIsUserCheckable) & ~QtCore.Qt.ItemIsEditable)
            tick.setCheckState(QtCore.Qt.Checked if d.confirmed else QtCore.Qt.Unchecked)
            self.table.setItem(r, 5, tick)
            why = QtWidgets.QTableWidgetItem("; ".join(d.problems) or d.reason)
            why.setFlags(why.flags() & ~QtCore.Qt.ItemIsEditable)
            if d.problems:
                why.setForeground(QtGui.QColor("#A33A28"))
            self.table.setItem(r, 6, why)
        self.table.resizeColumnsToContents()
        self._filling = False
        rep = diagnostics(self.raw, self.decisions)
        parts = [f"{rep['format']}: {rep['mapped']} of {rep['columns']} columns mapped, {rep['confirmed']} confirmed."]
        if rep["missing_required"]:
            parts.append("Still needed before the file can be analysed: " + ", ".join(rep["missing_required"]) + ".")
        if rep["unknown_units"]:
            parts.append("No unit in the file for: " + ", ".join(rep["unknown_units"]) + " — type it in, or it will be assumed.")
        if rep["failed_validation"]:
            parts.append("Values do not fit the channel for: " + ", ".join(rep["failed_validation"]) + ".")
        if rep["conflicts"]:
            parts.append("Two columns claimed one channel: " + ", ".join(rep["conflicts"]) + ".")
        if self.profile_note:
            parts.append(self.profile_note)
        elif self.profile:
            parts.append(f"Profile: {self.profile.name}.")
        self.summary.setText("  ".join(parts))

    def _item_changed(self, item):
        if self._filling:
            return
        d = self.decisions[item.row()]
        if item.column() == 2:
            self.set_unit(d.column, item.text())
        elif item.column() == 5:
            self.set_confirmed(d.column, item.checkState() == QtCore.Qt.Checked)

    # ── what the user can do (also the dialog's API for tests) ────────────────

    def set_channel(self, column: str, channel: str | None):
        if self._filling:
            return
        self.user[column] = channel
        self.confirmed.discard(column)
        QtCore.QTimer.singleShot(0, self.refresh) if self.isVisible() else self.refresh()

    def set_unit(self, column: str, unit: str):
        unit = unit.strip()
        if unit and unit != "unknown":
            self.units[column] = unit
        else:
            self.units.pop(column, None)
        self.refresh()

    def set_confirmed(self, column: str, on: bool):
        (self.confirmed.add if on else self.confirmed.discard)(column)
        self.refresh()

    def confirm_all(self):
        self.confirmed |= {d.column for d in self.decisions if d.channel and not d.problems}
        self.refresh()

    def load_profile(self, path: str | Path):
        self.profile, self.profile_note = Profile.load(path), ""
        self.refresh()

    def save_profile(self, path: str | Path, name: str | None = None) -> Profile:
        """Save the confirmed rows as a profile; unconfirmed rows are left out."""
        kept = [d for d in self.decisions if d.channel and d.confirmed]
        profile = profile_from_decisions(name or Path(path).stem, kept, confirmed=True)
        profile.save(path)
        return profile

    def import_args(self) -> tuple[dict, dict]:
        """(mapping, units) to import the file with."""
        mapping, units = to_import_args(self.decisions)
        return mapping, {**units, **{c: u for c, u in self.units.items() if c in mapping.values()}}

    def _choose_profile(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load mapping profile", str(self.profiles_dir or ""), "Profile (*.json)")
        if path:
            try:
                self.load_profile(path)
            except (ValueError, KeyError, OSError) as e:
                QtWidgets.QMessageBox.warning(self, "Could not load the profile", str(e))

    def _choose_save(self):
        if self.profiles_dir:
            self.profiles_dir.mkdir(parents=True, exist_ok=True)
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save mapping profile", str(self.profiles_dir or ""), "Profile (*.json)")
        if path:
            self.save_profile(path)
