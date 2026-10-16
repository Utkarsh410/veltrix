"""
The maths channel editor: a window for writing, checking, previewing and
keeping formula channels.

  left      the channels defined so far, with whether each computes on the
            open session
  middle    name, unit, category, description; the formula, coloured as it
            is typed, with the place of any error marked; what the formula
            gives; a preview plot of the result before it is saved
  right     the session's channels to pick from, the function reference, the
            named parameters, and what the channel depends on and how it was
            calculated

Everything the editor shows comes from `veltrix_engine.maths` — `preview`
computes without keeping, `dependencies` gives the graph, `compute` does the
saving run. A formula is never executed as code. Definitions live in one JSON
file and are back when the app is next started.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from veltrix_engine import maths
from veltrix_engine.display import decimate
from veltrix_engine.maths import FormulaError, MathChannel, MathSet

_WORD = re.compile(r"\$?[A-Za-z_][A-Za-z0-9_]*|\[[^\[\]]*\]|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_KEYWORDS = {"and", "or", "not", "in", "as", "true", "false"}
COLOURS = {"function": "#8F79E8", "parameter": "#B98A3E", "number": "#3F8F7B", "keyword": "#8F79E8",
           "channel": None, "unknown": "#C0392B"}


def as_reference(name: str) -> str:
    """A channel name as it is written in a formula: bare if it can be, in [ ] otherwise."""
    plain = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) and name.lower() not in _KEYWORDS and name.lower() not in maths.FUNCTIONS
    return name if plain else f"[{name}]"


class FormulaHighlighter(QtGui.QSyntaxHighlighter):
    """Colours a formula by what each word is: a function, a parameter, a
    number, a channel the session has, or a name nothing answers to."""

    def __init__(self, document, known):
        super().__init__(document)
        self.known = known                     # name -> bool: is this a channel or maths channel?

    def highlightBlock(self, text: str):
        for m in _WORD.finditer(text):
            word, kind = m.group(), None
            after = text[m.end():].lstrip()
            if word.startswith("$"):
                kind = "parameter"
            elif word[0].isdigit() or word[0] == ".":
                kind = "number"
            elif word.lower() in _KEYWORDS:
                before = text[:m.start()].rstrip().lower()
                kind = "keyword"
                if before.endswith((" in", " as")) or before in ("in", "as"):
                    kind = None                # a unit after in / as
            elif word.lower() in maths.FUNCTIONS and after.startswith("("):
                kind = "function"
            elif re.search(r"\b(in|as)\s*$", text[:m.start()]):
                kind = None                    # a unit name, not a channel
            else:
                kind = "channel" if self.known(word.strip("[]").strip()) else "unknown"
            colour = COLOURS.get(kind)
            if colour:
                fmt = QtGui.QTextCharFormat()
                fmt.setForeground(QtGui.QColor(colour))
                if kind == "unknown":
                    fmt.setUnderlineStyle(QtGui.QTextCharFormat.WaveUnderline)
                    fmt.setUnderlineColor(QtGui.QColor(colour))
                self.setFormat(m.start(), m.end() - m.start(), fmt)


class MathsEditor(QtWidgets.QWidget):
    changed = QtCore.Signal()                 # definitions were saved or deleted: recompute and refresh
    plot_requested = QtCore.Signal(str)       # put this channel on the selected chart

    def __init__(self, path: str | Path, parent=None):
        super().__init__(parent, QtCore.Qt.Window)
        self.setWindowTitle("Maths channels")
        self.resize(1250, 780)
        self.path = Path(path)
        self.model = None
        self.results: dict = {}
        self.view_range = None                # callable giving (t0, t1) to preview over; None = the whole session
        self._editing: str | None = None      # the saved channel loaded in the form, if any
        self._undo: list = []                 # the definitions before each saved change, newest last
        self._redo: list = []
        self._loading = False
        self.last_preview = None
        try:
            self.mset = MathSet.load(self.path) if self.path.exists() else MathSet()
            self.load_error = ""
        except (ValueError, KeyError, OSError) as e:
            self.mset, self.load_error = MathSet(), f"The saved maths channels could not be read ({e})."

        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        # ── left: the channels ────────────────────────────────────────────────
        self.list = QtWidgets.QListWidget()
        self.list.currentTextChanged.connect(self._list_selected)
        new, duplicate, delete = (QtWidgets.QPushButton(t) for t in ("New", "Duplicate", "Delete"))
        new.clicked.connect(self.new_channel)
        duplicate.clicked.connect(self.duplicate_channel)
        delete.clicked.connect(self.delete_channel)
        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(QtWidgets.QLabel("Maths channels"))
        lv.addWidget(self.list, 1)
        row = QtWidgets.QHBoxLayout()
        for b in (new, duplicate, delete):
            row.addWidget(b)
        lv.addLayout(row)

        # ── middle: the channel being edited ──────────────────────────────────
        self.name = QtWidgets.QLineEdit(placeholderText="Name, e.g. CombinedG")
        self.unit = QtWidgets.QLineEdit(placeholderText="Optional. If given, the formula must come out in it")
        self.category = QtWidgets.QLineEdit("maths")
        self.description = QtWidgets.QLineEdit(placeholderText="What this channel is")
        self.estimate = QtWidgets.QCheckBox("An estimate, not a measurement (flagged wherever the channel appears)")
        self.formula = QtWidgets.QPlainTextEdit()
        self.formula.setFont(mono)
        self.formula.setPlaceholderText("sqrt(acc_lat^2 + acc_long^2)")
        self.formula.setMinimumHeight(90)
        self.highlighter = FormulaHighlighter(self.formula.document(), self._is_known)
        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        self.status.setTextFormat(QtCore.Qt.PlainText)
        self.status.setFont(mono)
        self.plot = pg.PlotWidget()
        self.plot.setMinimumHeight(170)
        self.plot.setMenuEnabled(False)
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.curve = self.plot.plot([], [], pen=pg.mkPen("#B9A6FF", width=1), connect="finite")
        self.stats = QtWidgets.QLabel("Preview: open a session and type a formula.")
        self.stats.setWordWrap(True)
        self.save_button, revert, plot = (QtWidgets.QPushButton(t) for t in ("Save channel", "Revert", "Plot on selected chart"))
        self.undo_button, self.redo_button = QtWidgets.QPushButton("Undo last change"), QtWidgets.QPushButton("Redo")
        self.undo_button.setToolTip("Take back the last save, delete or parameter change (typing has its own undo, ⌘Z, in the formula box)")
        self.undo_button.clicked.connect(self.undo)
        self.redo_button.clicked.connect(self.redo)
        self.undo_button.setEnabled(False)
        self.redo_button.setEnabled(False)
        self.save_button.setShortcut(QtGui.QKeySequence.Save)
        self.save_button.clicked.connect(self.save_channel)
        revert.clicked.connect(self.revert)
        plot.clicked.connect(lambda: self.plot_requested.emit(self.name.text().strip()))
        form = QtWidgets.QFormLayout()
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)      # macOS keeps fields narrow otherwise
        form.addRow("Name", self.name)
        form.addRow("Unit", self.unit)
        form.addRow("Category", self.category)
        form.addRow("Description", self.description)
        middle = QtWidgets.QWidget()
        mv = QtWidgets.QVBoxLayout(middle)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.addLayout(form)
        mv.addWidget(self.estimate)
        mv.addWidget(QtWidgets.QLabel("Formula"))
        mv.addWidget(self.formula, 1)
        mv.addWidget(self.status)
        mv.addWidget(QtWidgets.QLabel("Preview (not saved until you press Save)"))
        mv.addWidget(self.plot, 2)
        mv.addWidget(self.stats)
        row = QtWidgets.QHBoxLayout()
        for b in (self.save_button, revert, plot):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.undo_button)
        row.addWidget(self.redo_button)
        mv.addLayout(row)

        # ── right: what to build with, and what was built ─────────────────────
        self.search = QtWidgets.QLineEdit(placeholderText="Search channels…")
        self.search.textChanged.connect(self._filter_channels)
        self.channels = QtWidgets.QTableWidget(0, 4)
        self.channels.setHorizontalHeaderLabels(("Channel", "Unit", "Hz", "Kind"))
        self.channels.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.channels.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.channels.verticalHeader().hide()
        self.channels.horizontalHeader().setStretchLastSection(True)
        self.channels.itemDoubleClicked.connect(lambda item: self.insert(as_reference(self.channels.item(item.row(), 0).text())))
        browse = QtWidgets.QWidget()
        bv = QtWidgets.QVBoxLayout(browse)
        bv.addWidget(self.search)
        bv.addWidget(self.channels, 1)
        bv.addWidget(QtWidgets.QLabel("Double-click to put a channel in the formula."))

        self.functions = QtWidgets.QListWidget()
        self.functions.setWordWrap(True)
        for signature, text in maths.REFERENCE.items():
            item = QtWidgets.QListWidgetItem(f"{signature}\n    {text}")
            item.setData(QtCore.Qt.UserRole, signature.split("(")[0].split()[0] + "(")
            self.functions.addItem(item)
        for syntax, text in (("x in m/s", "convert to a unit (dimensions must match)"), ("x as g", "state a unit without converting"),
                             ("$name", "a named parameter"), ("[Name With Spaces]", "a channel whose name has spaces"),
                             ("and  or  not  <  <=  >  >=  ==  !=", "logic and comparison, giving 1 or 0")):
            item = QtWidgets.QListWidgetItem(f"{syntax}\n    {text}")
            item.setData(QtCore.Qt.UserRole, "")
            self.functions.addItem(item)
        self.functions.itemDoubleClicked.connect(lambda item: self.insert(item.data(QtCore.Qt.UserRole)))

        self.parameters = QtWidgets.QTableWidget(0, 2)
        self.parameters.setHorizontalHeaderLabels(("Parameter", "Value"))
        self.parameters.verticalHeader().hide()
        self.parameters.horizontalHeader().setStretchLastSection(True)
        self.parameters.itemChanged.connect(self._parameters_edited)
        add_p, del_p = QtWidgets.QPushButton("Add parameter"), QtWidgets.QPushButton("Remove")
        add_p.clicked.connect(lambda: self.set_parameter(self._free_parameter_name(), 0.0))
        del_p.clicked.connect(self._remove_parameter)
        params = QtWidgets.QWidget()
        pv = QtWidgets.QVBoxLayout(params)
        pv.addWidget(QtWidgets.QLabel("Thresholds and constants shared by every maths channel. Use one in a formula as $name."))
        pv.addWidget(self.parameters, 1)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(add_p)
        row.addWidget(del_p)
        row.addStretch(1)
        pv.addLayout(row)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(("Dependency", "Detail"))
        self.provenance = QtWidgets.QPlainTextEdit(readOnly=True)
        self.provenance.setFont(mono)
        self.provenance.setMaximumHeight(170)
        deps = QtWidgets.QWidget()
        dv = QtWidgets.QVBoxLayout(deps)
        dv.addWidget(self.tree, 1)
        dv.addWidget(QtWidgets.QLabel("How the saved channel was calculated"))
        dv.addWidget(self.provenance)

        self.tabs = QtWidgets.QTabWidget()
        for widget, title in ((browse, "Channels"), (self.functions, "Functions"), (params, "Parameters"), (deps, "Dependencies")):
            self.tabs.addTab(widget, title)

        split = QtWidgets.QSplitter()
        for widget, stretch in ((left, 2), (middle, 6), (self.tabs, 4)):
            split.addWidget(widget)
            split.setStretchFactor(split.count() - 1, stretch)
        outer = QtWidgets.QVBoxLayout(self)
        outer.addWidget(split)

        self._timer = QtCore.QTimer(self, singleShot=True, interval=250, timeout=self.validate)
        for widget in (self.formula, ):
            widget.textChanged.connect(self._timer.start)
        for widget in (self.name, self.unit):
            widget.textChanged.connect(self._timer.start)
        self._fill_parameters()
        self._refresh_list()
        if self.load_error:
            self.status.setText(self.load_error)

    # ── session ───────────────────────────────────────────────────────────────

    def compute(self, model) -> dict:
        """Compute every saved channel on a session. Returns {name: Result}."""
        self.model = model
        self.results = maths.compute(model, self.mset) if model is not None else {}
        self._refresh_list()
        self._fill_channels()
        self.highlighter.rehighlight()
        if self.isVisible():
            self.validate()
        return self.results

    def _is_known(self, name: str) -> bool:
        if name in self.mset.channels:
            return True
        if self.model is None:
            return True                        # nothing to check against: do not cry wolf
        try:
            maths._lookup(self.model, name)
            return True
        except (KeyError, FormulaError):
            return False

    # ── lists ─────────────────────────────────────────────────────────────────

    def _refresh_list(self):
        current = self._editing or ""
        self.list.blockSignals(True)
        self.list.clear()
        for name, c in self.mset.channels.items():
            r = self.results.get(name)
            item = QtWidgets.QListWidgetItem(name)
            if r is not None and not r.ok:
                item.setForeground(QtGui.QColor("#C0392B"))
                item.setToolTip(r.message)
            else:
                item.setToolTip((f"[{r.channel.unit}]  " if r is not None else "") + c.formula)
            self.list.addItem(item)
        match = self.list.findItems(current, QtCore.Qt.MatchExactly)
        if match:
            self.list.setCurrentItem(match[0])
        self.list.blockSignals(False)

    def _fill_channels(self):
        rows = []
        if self.model is not None:
            for layer in ("normalised", "corrected", "calculated"):
                for c in self.model.channels(layer):
                    if c.id != "time" and c.values.dtype.kind in "biuf" and not c.user.get("maths"):
                        rows.append((c.id, c.unit, c.sample_rate, c.label + (f" — from {c.source_name}" if c.source_name and c.source_name != c.id else "")))
            mapped = {c.source_name for c in self.model.channels("normalised")}
            rows += [(c.id, c.unit, c.sample_rate, "raw column, not mapped") for c in self.model.channels("raw")
                     if c.id not in mapped and c.values.dtype.kind in "biuf"]
        for name, c in self.mset.channels.items():
            r = self.results.get(name)
            rows.append((name, r.channel.unit if r is not None and r.ok else c.unit, r.channel.sample_rate if r is not None and r.ok else None,
                         "maths channel" if r is None or r.ok else "maths channel — not calculated for this session"))
        self.channels.setRowCount(len(rows))
        for r, (name, unit, rate, kind) in enumerate(rows):
            for k, text in enumerate((name, unit, f"{rate:g}" if rate else "", kind)):
                self.channels.setItem(r, k, QtWidgets.QTableWidgetItem(text))
        self.channels.resizeColumnsToContents()
        self._filter_channels(self.search.text())

    def _filter_channels(self, text: str):
        words = text.lower().split()
        for r in range(self.channels.rowCount()):
            row = " ".join(self.channels.item(r, k).text().lower() for k in range(4))
            self.channels.setRowHidden(r, not all(w in row for w in words))

    def insert(self, text: str):
        """Put text into the formula at the cursor."""
        if text:
            self.formula.insertPlainText(text)
            self.formula.setFocus()

    # ── parameters ────────────────────────────────────────────────────────────

    def _fill_parameters(self):
        self._loading = True
        self.parameters.setRowCount(len(self.mset.parameters))
        for r, (key, value) in enumerate(self.mset.parameters.items()):
            self.parameters.setItem(r, 0, QtWidgets.QTableWidgetItem(key))
            self.parameters.setItem(r, 1, QtWidgets.QTableWidgetItem(f"{value:g}"))
        self._loading = False

    def _free_parameter_name(self) -> str:
        n = 1
        while f"parameter{n}" in self.mset.parameters:
            n += 1
        return f"parameter{n}"

    def set_parameter(self, name: str, value: float) -> None:
        """Add or change a named parameter; saved channels that use it are recomputed."""
        name = name.strip().lstrip("$")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise FormulaError(f"'{name}' is not a usable parameter name: letters, digits and underscores, starting with a letter.")
        self._remember()
        self.mset.parameters[name] = float(value)
        self._parameters_changed()

    def _remove_parameter(self):
        row = self.parameters.currentRow()
        if row >= 0:
            self._remember()
            self.mset.parameters.pop(self.parameters.item(row, 0).text(), None)
            self._parameters_changed()

    def _parameters_edited(self, item):
        if self._loading:
            return
        collected, problem = {}, ""
        for r in range(self.parameters.rowCount()):
            key = (self.parameters.item(r, 0).text() if self.parameters.item(r, 0) else "").strip().lstrip("$")
            raw = (self.parameters.item(r, 1).text() if self.parameters.item(r, 1) else "").strip()
            try:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                    raise ValueError
                collected[key] = float(raw)
            except ValueError:
                problem = f"Parameter row {r + 1} needs a plain name and a number."
        if problem:
            self.status.setText(f"✗ {problem}")
            return
        if collected != self.mset.parameters:
            self._remember()
        self.mset.parameters = collected
        self._parameters_changed(refill=False)

    def _parameters_changed(self, refill: bool = True):
        if refill:
            self._fill_parameters()
        self._write()
        if self.model is not None:
            self.results = maths.compute(self.model, self.mset)
            self._refresh_list()
        self.changed.emit()
        self.validate()

    # ── the form ──────────────────────────────────────────────────────────────

    def _list_selected(self, name: str):
        if name in self.mset.channels:
            self.load(name)

    def load(self, name: str):
        """Put a saved channel in the form."""
        c = self.mset.channels[name]
        self._loading = True
        self._editing = name
        self.name.setText(c.name)
        self.unit.setText(c.unit)
        self.category.setText(c.category)
        self.description.setText(c.description)
        self.estimate.setChecked(c.estimate)
        self.formula.setPlainText(c.formula)
        self._loading = False
        match = self.list.findItems(name, QtCore.Qt.MatchExactly)
        if match and self.list.currentItem() is not match[0]:
            self.list.blockSignals(True)
            self.list.setCurrentItem(match[0])
            self.list.blockSignals(False)
        self.validate()

    def new_channel(self):
        self._editing = None
        self._loading = True
        self.list.blockSignals(True)
        self.list.setCurrentRow(-1)
        self.list.blockSignals(False)
        for w in (self.name, self.unit, self.description):
            w.clear()
        self.category.setText("maths")
        self.estimate.setChecked(False)
        self.formula.clear()
        self._loading = False
        self.validate()
        self.name.setFocus()

    clear_form = new_channel

    def duplicate_channel(self):
        if self._editing is None:
            return
        base, n = self._editing, 2
        while f"{base}{n}" in self.mset.channels:
            n += 1
        self._editing = None
        self.name.setText(f"{base}{n}")
        self.validate()

    def revert(self):
        self.load(self._editing) if self._editing in self.mset.channels else self.new_channel()

    def is_modified(self) -> bool:
        """Whether the form differs from the saved channel it was loaded from."""
        typed = (self.name.text().strip(), self.formula.toPlainText().strip(), self.unit.text().strip(),
                 self.description.text().strip(), self.category.text().strip() or "maths", self.estimate.isChecked())
        c = self.mset.channels.get(self._editing) if self._editing else None
        if c is None:
            return bool(typed[0] or typed[1])
        return typed != (c.name, c.formula, c.unit, c.description, c.category, c.estimate)

    # ── checking and preview ──────────────────────────────────────────────────

    def validate(self) -> dict:
        """Check the formula as typed against the open session, mark any
        error where it is, and draw what the formula gives."""
        if self._loading:
            return {"ok": False, "message": "", "unit": None}
        text = self.formula.toPlainText()
        own = self._editing or self.name.text().strip()
        self.formula.setExtraSelections([])
        self.last_preview = None
        self.setWindowTitle("Maths channels" + (" — unsaved changes" if self.is_modified() else ""))
        if not text.strip():
            self.status.setText("")
            self._show_preview(None)
            self._show_dependencies()
            return {"ok": False, "message": "", "unit": None}
        info = maths.check(text, self.model, self.mset, self.unit.text().strip(), own)
        if info["ok"]:
            if self.model is None:
                gives = "the syntax is valid (open a session to check its channels and unit)"
            else:
                gives = f"gives {info['unit']}"
            uses = ", ".join(info["inputs"] + [f"${p}" for p in info["parameters"]])
            self.status.setText(f"✓ {gives}" + (f"   ·   uses {uses}" if uses else ""))
            self.status.setStyleSheet("")
        else:
            self.status.setText(f"✗ {info['message']}")
            self.status.setStyleSheet("color: #C0392B;")
            self._mark(info.get("position"))
        result = maths.preview(text, self.model, self.mset, self.unit.text().strip(), own) if self.model is not None and info["ok"] else None
        self._show_preview(result)
        self._show_dependencies()
        return info

    def _mark(self, position):
        """Underline the formula from where the error was noticed."""
        if position is None or "\n" in self.formula.toPlainText():
            return
        cursor = self.formula.textCursor()
        cursor.setPosition(min(position, len(self.formula.toPlainText())))
        cursor.movePosition(QtGui.QTextCursor.EndOfWord if position < len(self.formula.toPlainText()) else QtGui.QTextCursor.End,
                            QtGui.QTextCursor.KeepAnchor)
        if not cursor.hasSelection():
            cursor.movePosition(QtGui.QTextCursor.PreviousCharacter, QtGui.QTextCursor.KeepAnchor)
        selection = QtWidgets.QTextEdit.ExtraSelection()
        selection.cursor = cursor
        selection.format.setUnderlineStyle(QtGui.QTextCharFormat.WaveUnderline)
        selection.format.setUnderlineColor(QtGui.QColor("#C0392B"))
        selection.format.setBackground(QtGui.QColor(192, 57, 43, 40))
        self.formula.setExtraSelections([selection])

    def _show_preview(self, result):
        if result is None or not result.ok:
            self.curve.setData([], [])
            self.stats.setText("Preview: " + ("nothing to show yet." if result is None else result.message.splitlines()[0]))
            return
        c = self.last_preview = result.channel
        values = np.where(c.validity, np.asarray(c.values, float), np.nan)
        t0, t1 = self.view_range() if self.view_range else (float(c.timestamps[0]), float(c.timestamps[-1]))
        t, v, info = decimate(c.timestamps, values, t0, t1, max(self.plot.width(), 400))
        if c.discrete and len(t) > 1:
            t, v = np.repeat(t, 2)[1:], np.repeat(v, 2)[:-1]
        self.curve.setData(t, v)
        self.plot.setXRange(t0, t1, padding=0)
        self.plot.enableAutoRange(axis="y")
        self.plot.setLabel("left", c.unit)
        a, b = np.searchsorted(c.timestamps, [t0, t1])
        part = values[a:b]
        seen = part[np.isfinite(part)]
        invalid = int((~c.validity).sum())
        rate = c.sample_rate
        self.stats.setText(
            f"[{c.unit}]  {len(c.values):,} samples at {rate:g} Hz" + (", held between samples" if c.discrete else "")
            + f"  ·  {invalid:,} invalid ({invalid / max(len(c.values), 1):.1%})"
            + (f"  ·  in view: min {seen.min():.4g}, max {seen.max():.4g}, mean {seen.mean():.4g}" if len(seen) else "  ·  no valid samples in view")
            + ("" if not info["decimated"] else "  ·  drawn thinned for the screen; the numbers are from every sample"))

    def _show_dependencies(self):
        """The tree of what the channel in the form uses and what uses it."""
        self.tree.clear()
        name = self.name.text().strip() or "(unnamed)"
        probe = MathSet()
        probe.channels, probe.parameters = dict(self.mset.channels), dict(self.mset.parameters)
        text = self.formula.toPlainText().strip()
        if text:
            if self._editing and self._editing != name:
                probe.channels.pop(self._editing, None)
            probe.channels[name] = MathChannel(name, text)
        graph = maths.dependencies(probe, self.model)
        entry = graph.get(name)
        saved = self.results.get(self._editing) if self._editing else None
        if entry is None:
            self.provenance.setPlainText("")
            return
        root = QtWidgets.QTreeWidgetItem(self.tree, [name, entry["problem"] or (f"computed {entry['order']} of {len([g for g in graph.values() if g['order']])}"
                                                                               if entry["order"] else "")])

        def add_uses(parent, channel, seen):
            e = graph.get(channel)
            if e is None:
                return
            for s in e["uses_session"]:
                QtWidgets.QTreeWidgetItem(parent, [s["name"], f"{s['channel']}  [{s['unit']}]" if s["channel"] else "session channel"])
            for m in e["uses_maths"]:
                node = QtWidgets.QTreeWidgetItem(parent, [m, "maths channel: " + probe.channels[m].formula])
                if m not in seen:
                    add_uses(node, m, seen | {m})
            for m in e["missing"]:
                item = QtWidgets.QTreeWidgetItem(parent, [m, "not in this session"])
                item.setForeground(0, QtGui.QColor("#C0392B"))
            for p in e["parameters"]:
                defined = p in probe.parameters
                item = QtWidgets.QTreeWidgetItem(parent, [f"${p}", f"= {probe.parameters[p]:g}" if defined else "not defined"])
                if not defined:
                    item.setForeground(0, QtGui.QColor("#C0392B"))
        uses = QtWidgets.QTreeWidgetItem(root, ["Uses", ""])
        add_uses(uses, name, {name})
        used_by = QtWidgets.QTreeWidgetItem(root, ["Used by", "" if entry["used_by"] else "nothing"])
        for m in entry["used_by"]:
            QtWidgets.QTreeWidgetItem(used_by, [m, probe.channels[m].formula])
        self.tree.expandAll()
        self.tree.resizeColumnToContents(0)

        if saved is None or not saved.ok:
            self.provenance.setPlainText("" if saved is None else f"Not calculated: {saved.message}")
            return
        c = saved.channel
        version = c.provenance.parameters.get("formula_version", 1)
        lines = [f"{c.id} [{c.unit}] — {c.label}, formula version {version}, {int((~c.validity).sum())} of {len(c.values)} samples invalid",
                 "inputs: " + (", ".join(c.dependencies) or "none")]
        lines += [f"${k[1:]} = {v:g}" for k, v in c.provenance.parameters.items() if k.startswith("$")]
        lines += [f"· {n}" for n in c.provenance.notes if not n.startswith("$")]
        self.provenance.setPlainText("\n".join(lines))

    # ── saving ────────────────────────────────────────────────────────────────

    def save_channel(self) -> bool:
        """Add or change the channel in the form, write the file, recompute.
        A formula has to parse to be saved; it does not have to compute on the
        session that happens to be open."""
        name, old = self.name.text().strip(), self._editing
        renamed = old is not None and old != name and old in self.mset.channels
        try:
            if renamed:
                users = maths.dependencies(self.mset)[old]["used_by"]
                if users:
                    raise FormulaError(f"'{old}' is used by {', '.join(users)}. Change those formulas before renaming it.")
                if name in self.mset.channels:
                    raise FormulaError(f"There is already a maths channel called '{name}'.")
            before = self.mset.to_dict()
            self.mset.add(name, self.formula.toPlainText().strip(), self.unit.text().strip(), self.description.text().strip(),
                          self.category.text().strip() or "maths", self.estimate.isChecked())
        except FormulaError as e:
            self.status.setText(f"✗ {e.message}")
            self.status.setStyleSheet("color: #C0392B;")
            return False
        if renamed:
            self._forget(old)
        if self.mset.to_dict() != before:
            self._undo.append(before)
            del self._undo[:-50]
            self._redo.clear()
            self.undo_button.setEnabled(True)
            self.redo_button.setEnabled(False)
        self._editing = name
        self._write()
        if self.model is not None:
            self.results = maths.compute(self.model, self.mset)
        self._refresh_list()
        self._fill_channels()
        self.changed.emit()
        self.validate()
        return True

    def _forget(self, name: str):
        self.mset.remove(name)
        self.results.pop(name, None)
        if self.model is not None and self.model.has(name, "calculated"):
            del self.model._channels["calculated"][name]
            self.model.maths_signatures.pop(name, None)

    def delete_channel(self, name: str | None = None) -> bool:
        """Delete a saved channel (default: the one in the form). Refused while
        another channel is built on it."""
        name = name or self._editing
        if name not in self.mset.channels:
            return False
        users = maths.dependencies(self.mset)[name]["used_by"]
        if users:
            self.status.setText(f"✗ '{name}' is used by {', '.join(users)}. Delete or change those first.")
            self.status.setStyleSheet("color: #C0392B;")
            return False
        self._remember()
        self._forget(name)
        self._write()
        if self._editing == name:
            self.new_channel()
        self._refresh_list()
        self._fill_channels()
        self.changed.emit()
        return True

    # ── undo ──────────────────────────────────────────────────────────────────

    def _remember(self):
        """Keep the definitions as they are now, before a change is made to them."""
        self._undo.append(self.mset.to_dict())
        del self._undo[:-50]
        self._redo.clear()
        self.undo_button.setEnabled(True)
        self.redo_button.setEnabled(False)

    def _restore(self, snapshot: dict):
        gone = set(self.mset.channels)
        self.mset = MathSet.from_dict(snapshot)
        for name in gone - set(self.mset.channels):          # a channel the older state did not have leaves the session too
            self.results.pop(name, None)
            if self.model is not None and self.model.has(name, "calculated"):
                del self.model._channels["calculated"][name]
                self.model.maths_signatures.pop(name, None)
        self._write()
        if self.model is not None:
            self.results = maths.compute(self.model, self.mset)
        self._fill_parameters()
        self._refresh_list()
        self._fill_channels()
        self.undo_button.setEnabled(bool(self._undo))
        self.redo_button.setEnabled(bool(self._redo))
        self.changed.emit()
        self.load(self._editing) if self._editing in self.mset.channels else self.new_channel()

    def undo(self) -> bool:
        """Take back the last saved change: a save, a delete, a rename or a parameter change."""
        if not self._undo:
            return False
        self._redo.append(self.mset.to_dict())
        self._restore(self._undo.pop())
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self.mset.to_dict())
        self._restore(self._redo.pop())
        return True

    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.mset.save(self.path)
