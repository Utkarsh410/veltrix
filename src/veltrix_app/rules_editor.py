"""
The event rule editor: the rules the detectors run, as a table a person can
read and change — what starts an event, what ends it, how long it must last,
how it is graded — and the named thresholds they share.

Rules are data (`veltrix_engine.events.RuleSet`); this is the form over them.
Every formula is checked as it is saved, "Try on this session" shows how many
events each rule finds with the values on screen, and "Fit thresholds" raises
any threshold that sits inside this session's own sensor noise.
"""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from veltrix_engine import events as rule_engine

COLUMNS = ("On", "Rule", "Starts when", "Ends when", "At least (s)", "Join within (s)", "Graded by", "Moderate", "Severe", "Found")
_FIELDS = {2: "on", 3: "off", 6: "severity"}


class RulesEditor(QtWidgets.QDialog):
    saved = QtCore.Signal()                   # the rule set on disk changed: detect again

    def __init__(self, path: str | Path, model=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Event rules")
        self.resize(1250, 640)
        self.path, self.model = Path(path), model
        self.rules = self._load()
        self._filling = False

        self.table = QtWidgets.QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.parameters = QtWidgets.QTableWidget(0, 2)
        self.parameters.setHorizontalHeaderLabels(("Threshold", "Value"))
        self.parameters.verticalHeader().hide()
        self.parameters.horizontalHeader().setStretchLastSection(True)
        self.parameters.setMaximumWidth(280)
        self.status = QtWidgets.QLabel("Thresholds are used in the formulas as $name. Nothing changes until you press Save.")
        self.status.setWordWrap(True)
        try_it, fit, add, remove, defaults = (QtWidgets.QPushButton(t) for t in (
            "Try on this session", "Fit thresholds to this session", "Add rule", "Remove rule", "Back to the defaults"))
        try_it.clicked.connect(self.try_rules)
        fit.clicked.connect(self.fit)
        add.clicked.connect(self.add_rule)
        remove.clicked.connect(self.remove_rule)
        defaults.clicked.connect(self.use_defaults)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Close)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        row = QtWidgets.QHBoxLayout()
        for b in (try_it, fit, add, remove, defaults):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(buttons)
        split = QtWidgets.QHBoxLayout()
        split.addWidget(self.table, 1)
        split.addWidget(self.parameters)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(split, 1)
        layout.addWidget(self.status)
        layout.addLayout(row)
        self._fill()

    def _load(self):
        if self.path.exists():
            try:
                return rule_engine.RuleSet.load(self.path)
            except (ValueError, KeyError, TypeError, OSError):
                pass
        return rule_engine.default_rules()

    # ── table ⇄ rule set ──────────────────────────────────────────────────────

    def _fill(self, found: dict | None = None):
        self._filling = True
        self.table.setRowCount(len(self.rules.rules))
        for r, rule in enumerate(self.rules.rules):
            tick = QtWidgets.QTableWidgetItem()
            tick.setFlags(QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled)
            tick.setCheckState(QtCore.Qt.Checked if rule.enabled else QtCore.Qt.Unchecked)
            self.table.setItem(r, 0, tick)
            num = lambda x: "" if x is None else f"{x:g}"
            cells = (rule.title, rule.on, rule.off, num(rule.min_duration_s), num(rule.merge_gap_s), rule.severity,
                     num(rule.moderate), num(rule.severe))
            for k, text in enumerate(cells, start=1):
                item = QtWidgets.QTableWidgetItem(text)
                item.setToolTip(rule.note)
                self.table.setItem(r, k, item)
            result = (found or {}).get(rule.id)
            shown = QtWidgets.QTableWidgetItem("" if result is None else str(result["events"]) if result["ran"] else result["message"])
            shown.setFlags(shown.flags() & ~QtCore.Qt.ItemIsEditable)
            if result is not None and not result["ran"]:
                shown.setForeground(QtGui.QColor("#C0392B"))
            self.table.setItem(r, 9, shown)
        self.table.resizeColumnsToContents()
        self.parameters.setRowCount(len(self.rules.parameters))
        for r, (key, value) in enumerate(self.rules.parameters.items()):
            name = QtWidgets.QTableWidgetItem(key)
            name.setFlags(name.flags() & ~QtCore.Qt.ItemIsEditable)
            self.parameters.setItem(r, 0, name)
            self.parameters.setItem(r, 1, QtWidgets.QTableWidgetItem(f"{value:g}"))
        self._filling = False

    def collect(self):
        """The rule set as the tables now read. Raises ValueError naming what is wrong."""
        out = rule_engine.RuleSet.from_dict(self.rules.to_dict())
        number = lambda text, what, row: None if not text.strip() else self._number(text, what, row)
        for r, rule in enumerate(out.rules):
            cell = lambda k: (self.table.item(r, k).text() if self.table.item(r, k) else "").strip()
            rule.enabled = self.table.item(r, 0).checkState() == QtCore.Qt.Checked
            rule.title = cell(1) or rule.title
            edited = (cell(2), cell(3), cell(6))
            if edited != (rule.on, rule.off, rule.severity):
                rule.version += 1              # a changed formula is a new version of the rule
            rule.on, rule.off, rule.severity = edited
            rule.min_duration_s = number(cell(4), "At least", r) or 0.0
            rule.merge_gap_s = number(cell(5), "Join within", r) or 0.0
            rule.moderate, rule.severe = number(cell(7), "Moderate", r), number(cell(8), "Severe", r)
        for r in range(self.parameters.rowCount()):
            key = self.parameters.item(r, 0).text()
            out.parameters[key] = self._number(self.parameters.item(r, 1).text(), f"${key}", r)
        out.check()
        return out

    @staticmethod
    def _number(text: str, what: str, row: int) -> float:
        try:
            return float(text)
        except ValueError:
            raise ValueError(f"{what} in row {row + 1} is not a number: '{text}'.") from None

    def _say(self, text: str, bad: bool = False):
        self.status.setText(text)
        self.status.setStyleSheet("color: #C0392B;" if bad else "")

    # ── actions ───────────────────────────────────────────────────────────────

    def try_rules(self) -> dict | None:
        """Run the rules as shown on the open session and put the counts in the last column."""
        try:
            rules = self.collect()
        except ValueError as e:
            self._say(f"✗ {e}", bad=True)
            return None
        if self.model is None:
            self._say("The formulas are valid. Open a session to see what they find.")
            return None
        found, report = rule_engine.detect(self.model, rules)
        self.rules = rules
        self._fill(report)
        self._say(f"{len(found)} candidate event(s) on this session with the values shown. Nothing is saved yet.")
        return report

    def fit(self) -> list:
        """Raise any threshold that sits inside this session's own noise; shown, not saved."""
        if self.model is None:
            self._say("Open a session first: thresholds are fitted to a session's own signals.")
            return []
        try:
            fitted, changes = rule_engine.calibrate(self.model, self.collect())
        except ValueError as e:
            self._say(f"✗ {e}", bad=True)
            return []
        self.rules = fitted
        self._fill()
        self._say(" ".join(changes) if changes else "No threshold needed raising for this session.")
        return changes

    def add_rule(self):
        n = 1
        while any(r.id == f"rule_{n}" for r in self.rules.rules):
            n += 1
        try:
            self.rules = self.collect()
        except ValueError:
            pass
        self.rules.rules.append(rule_engine.Rule(f"rule_{n}", "Possible new event", "speed > 300", min_duration_s=0.2))
        self._fill()
        self._say("New rule added at the bottom. Give it a name and a condition; channels and maths channels can be used.")

    def remove_rule(self):
        row = self.table.currentRow()
        if 0 <= row < len(self.rules.rules):
            del self.rules.rules[row]
            self._fill()

    def use_defaults(self):
        self.rules = rule_engine.default_rules()
        self._fill()
        self._say("The built-in rules and thresholds are shown. Press Save to keep them.")

    def save(self) -> bool:
        try:
            self.rules = self.collect()
        except ValueError as e:
            self._say(f"✗ {e}", bad=True)
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.rules.save(self.path)
        self._say(f"Saved. {sum(r.enabled for r in self.rules.rules)} rule(s) are switched on.")
        self.saved.emit()
        return True
