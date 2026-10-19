"""
The desktop workspace window (PySide6 + pyqtgraph).

A thin display over `veltrix_app.workspace.Workspace`: a channel browser, a
stack of charts sharing one time axis and one cursor, and a readout of every
charted channel at the cursor. Files are loaded on a background thread, so the
window stays responsive while a large session is read.

  veltrix app                 open the window
  veltrix app session.csv     open it on a file
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from veltrix_engine import build_session, corners as corner_engine, read_file, supported_formats
from veltrix_engine.importers import inspect_file
from veltrix_engine.events import ReviewStore
from veltrix_engine.report import write_report

from .compare_view import CompareView
from .channel_picker import ChannelPicker
from .chart_layout import EvenRows
from .events import AnnotationStore, annotate, apply_reviews, detect_events
from .extras import load_rules
from .paths import data_dir
from .rules_editor import RulesEditor
from .events_dock import EventsDock
from .map_dock import MapDock
from .mapping_dialog import MappingDialog
from .maths_dock import MathsDock
from .session_charts import SessionCharts
from .measures_dock import MeasuresPanel, lap_colour
from .video_dock import VideoDock
from .video_sync import VideoStore
from .workspace import Workspace, default_layout

COLOURS = ("#B9A6FF", "#E0B25C", "#7FD1B9", "#E79A8C", "#8FB8FF", "#D7A6E0")
BROWSER_COLUMNS = ("Channel", "Unit", "Hz", "Layer", "Kind", "From", "Mapped by", "Notes")
READOUT_COLUMNS = ("Channel", "Value", "Unit", "", "Kind")
RANGE_COLUMNS = ("Channel", "Unit", "Min", "Max", "Mean", "Std dev", "At start", "At end", "Change", "Samples")
HOW_MARK = {"sample": "", "interpolated": "≈ interpolated", "held": "↦ held", "none": "no data here"}


class _Loader(QtCore.QThread):
    """Reads a session off the UI thread. With `raw_only` it stops after the
    file is read, so the columns can be mapped before anything is analysed."""
    done = QtCore.Signal(object, str)
    failed = QtCore.Signal(str, str)
    progress = QtCore.Signal(int, int)

    def __init__(self, path: str, raw_only: bool = False, channels: list | None = None, data_folder: Path | None = None,
                 mapping: tuple | None = None, raw=None):
        super().__init__()
        self.path, self.cancelled, self.raw_only, self.channels = path, False, raw_only, channels
        self.data_folder, self.mapping, self.raw = data_folder, mapping, raw

    def run(self):
        try:
            raw = self.raw if self.raw is not None else read_file(
                self.path, channels=self.channels, progress=self.progress.emit, cancel=lambda: self.cancelled)
            if self.raw_only:
                if not self.cancelled:
                    self.done.emit(raw, self.path)
                return
            names, units = self.mapping or (None, None)
            session = build_session(raw, mapping=names, units=units or None)
            session.model.events, session.events_summary = detect_events(session, load_rules(self.data_folder)[0])
        except Exception as e:                     # anything a file can do wrong is a message, not a crash
            if not self.cancelled:
                self.failed.emit(self.path, f"{type(e).__name__}: {e}" if not isinstance(e, ValueError) else str(e))
            return
        if not self.cancelled:
            self.done.emit(session, self.path)


def default_maths_path() -> Path:
    """Where the app keeps its maths channels between runs."""
    return data_dir() / "maths.json"


def _user_name() -> str:
    try:
        import getpass
        return getpass.getuser()
    except Exception:                          # no login name (a service account, a container)
        return ""


class MainWindow(SessionCharts, QtWidgets.QMainWindow):
    def __init__(self, maths_path: str | Path | None = None):
        super().__init__()
        self.setWindowTitle("Veltrix Data")
        self.resize(1500, 900)
        self.session = None
        self.ws = Workspace()
        self.active = 0                       # the chart new channels go to
        self._plots, self._cursors, self._curves, self._loader = [], [], [], None
        self._extra_views, self._regions = [], []
        self._syncing = False
        self._apply_compare = True
        self._laps_shown: list = []
        self.author = _user_name()                # who reviews events here, saved with each verdict
        self._maths_seen: set = set()
        pg.setConfigOptions(antialias=False, background="#0A0A0D", foreground="#9A97A6")

        self.graphs = pg.GraphicsLayoutWidget()
        self._even = EvenRows(self.graphs)
        self.compare = CompareView()
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self.graphs, "Session")         # the whole session against time
        self.tabs.addTab(self.compare, "Loaded laps")    # the loaded laps over each other
        self.tabs.tabBar().hide()                        # which one shows follows whether laps are loaded
        self.compare.lapsChanged.connect(self._laps_changed)
        self.compare.cursorMoved.connect(self._lap_cursor_moved)
        self.compare.cornerChosen.connect(lambda corner: self.map.highlight(corner))
        self.setCentralWidget(self.tabs)
        self._build_browser()
        self._build_readout()
        self._build_range()
        self.maths = MathsDock(maths_path or default_maths_path(), self)
        self.maths.changed.connect(self._maths_changed)
        self.maths.editor.view_range = lambda: self.ws.window()        # the editor previews what the charts show
        self.maths.preview.connect(self._plot_maths)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.maths)
        self.data_folder = Path(maths_path or default_maths_path()).parent
        self.notes = AnnotationStore(self.data_folder / "annotations")
        self.events = EventsDock(self)
        self.events.jump.connect(self.go_to)
        self.events.add_note.connect(self.add_annotation)
        self.events.remove_note.connect(self.remove_annotation)
        self.events.review.connect(self.review_event)
        self.events.clear_review.connect(self.clear_event_review)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.events)
        self.videos = VideoStore(Path(maths_path or default_maths_path()).parent / "videos")
        self.video = VideoDock(self)
        self.video.cursor_from_video.connect(lambda t: self.set_cursor(t, from_video=True))
        self.video.changed.connect(self._save_videos)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.video)
        self.map = MapDock(self)
        self.map.moved.connect(self.set_cursor)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.map)
        self.browser_dock.setObjectName("channels")
        # The charts get the room. On the right: readout, maths channels and events share one tabbed
        # panel on top; the map and the video share another underneath.
        self.tabifyDockWidget(self.readout_dock, self.maths)
        self.tabifyDockWidget(self.maths, self.events)
        self.tabifyDockWidget(self.readout_dock, self.range_dock)
        self.splitDockWidget(self.readout_dock, self.map, QtCore.Qt.Vertical)
        self.tabifyDockWidget(self.map, self.video)
        self.readout_dock.raise_()
        self.resizeDocks([self.browser_dock, self.readout_dock], [340, 400], QtCore.Qt.Horizontal)
        self.resizeDocks([self.readout_dock, self.map], [520, 330], QtCore.Qt.Vertical)
        self._build_actions()
        self.profiles_dir = Path(maths_path or default_maths_path()).parent / "profiles"
        self.progress = QtWidgets.QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setVisible(False)
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().showMessage("Open a telemetry file to begin.")
        self._redraw_timer = QtCore.QTimer(self, singleShot=True, interval=15, timeout=self._draw_curves)

    # ── construction ──────────────────────────────────────────────────────────

    def _build_browser(self):
        dock = self.browser_dock = QtWidgets.QDockWidget("Measures and laps", self)
        box = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(6, 6, 6, 6)
        self.search = QtWidgets.QLineEdit(placeholderText="Search channels, units, source names…")
        self.search.textChanged.connect(self._filter_browser)
        self.browser = QtWidgets.QTableWidget(0, len(BROWSER_COLUMNS))
        self.browser.setHorizontalHeaderLabels(BROWSER_COLUMNS)
        self.browser.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.browser.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.browser.verticalHeader().hide()
        self.browser.horizontalHeader().setStretchLastSection(True)
        self.browser.itemDoubleClicked.connect(lambda item: self.add_channel(self.browser.item(item.row(), 0).data(QtCore.Qt.UserRole)))
        hint = QtWidgets.QLabel("Double-click a channel to add it to the selected chart.")
        hint.setWordWrap(True)
        layout.addWidget(self.search)
        layout.addWidget(self.browser)
        layout.addWidget(hint)
        # the lap selection and the channels, as race-data programs lay them out; the table above is its third tab
        self.panel = MeasuresPanel(details=box)
        self.panel.lapToggled.connect(self.toggle_lap)
        self.panel.lapsRequested.connect(self.load_laps)
        self.panel.referenceRequested.connect(self.make_reference)
        self.panel.measureToggled.connect(self.set_measure)
        dock.setWidget(self.panel)
        self.addDockWidget(QtCore.Qt.LeftDockWidgetArea, dock)

    def _build_readout(self):
        dock = self.readout_dock = QtWidgets.QDockWidget("At the cursor", self)
        box = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(6, 6, 6, 6)
        self.where = QtWidgets.QLabel("—")
        self.readout = QtWidgets.QTableWidget(0, len(READOUT_COLUMNS))
        self.readout.setHorizontalHeaderLabels(READOUT_COLUMNS)
        self.readout.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.readout.verticalHeader().hide()
        self.readout.horizontalHeader().setStretchLastSection(True)
        self.unavailable = QtWidgets.QLabel("")
        self.unavailable.setWordWrap(True)
        layout.addWidget(self.where)
        layout.addWidget(self.readout)
        layout.addWidget(self.unavailable)
        dock.setWidget(box)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, dock)

    def _build_range(self):
        dock = self.range_dock = QtWidgets.QDockWidget("Marked range", self)
        box = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(6, 6, 6, 6)
        self.range_label = QtWidgets.QLabel("No range marked. Charts ▸ Mark a range (⌘R) puts one on the charts; drag its edges.")
        self.range_label.setWordWrap(True)
        self.range_table = QtWidgets.QTableWidget(0, len(RANGE_COLUMNS))
        self.range_table.setHorizontalHeaderLabels(RANGE_COLUMNS)
        self.range_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.range_table.verticalHeader().hide()
        zoom, clear = QtWidgets.QPushButton("Zoom to it"), QtWidgets.QPushButton("Clear")
        zoom.clicked.connect(self.zoom_to_range)
        clear.clicked.connect(lambda: self.mark_range(False))
        row = QtWidgets.QHBoxLayout()
        row.addWidget(zoom)
        row.addWidget(clear)
        row.addStretch(1)
        layout.addWidget(self.range_label)
        layout.addWidget(self.range_table, 1)
        layout.addWidget(QtWidgets.QLabel("Figures are from every logged sample in the range, not from what is drawn."))
        layout.addLayout(row)
        dock.setWidget(box)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, dock)
        self._range_timer = QtCore.QTimer(self, singleShot=True, interval=80, timeout=self._show_range)

    def _build_actions(self):
        """The menu bar and the toolbar. Everything is in the menus; the toolbar holds the everyday ones."""
        bar = self.addToolBar("Main")
        bar.setMovable(False)
        bar.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        menus = {name: self.menuBar().addMenu(name) for name in ("File", "Charts", "Maths", "View")}

        def action(menu, text, slot, shortcut=None, toolbar=True, tip=""):
            a = QtGui.QAction(text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(QtGui.QKeySequence(shortcut))
            a.setToolTip(tip or text.rstrip("…"))
            menus[menu].addAction(a)
            if toolbar:
                bar.addAction(a)
            return a
        action("File", "Open…", self.choose_file, QtGui.QKeySequence.Open)
        action("File", "Open with mapping…", lambda: self.choose_file(mapping=True), "Ctrl+Shift+O", toolbar=False)
        menus["File"].addSeparator()
        action("File", "Open workspace…", self.choose_workspace, toolbar=False)
        action("File", "Save workspace…", self.save_workspace, QtGui.QKeySequence.Save, toolbar=False)
        bar.addSeparator()
        action("Charts", "Add chart", lambda: self.add_chart(), "Ctrl+T")
        action("Charts", "Remove chart", self.remove_chart, "Ctrl+W")
        action("Charts", "Whole session", self.reset_view, "Ctrl+0")
        menus["Charts"].addSeparator()
        self.range_action = action("Charts", "Mark a range", self.mark_range, "Ctrl+R",
                                   tip="Mark a stretch of the session and see every channel's min, max and mean over it (Ctrl+R)")
        self.range_action.setCheckable(True)
        action("Charts", "Zoom to the marked range", self.zoom_to_range, "Ctrl+Shift+R", toolbar=False)
        bar.addSeparator()
        self.editor_action = action("Maths", "Maths channel editor…", self.open_maths_editor, "Ctrl+M",
                                    tip="Write, check and preview formula channels (Ctrl+M)")
        action("Maths", "New maths channel…", lambda: self.maths.open_editor(None), "Ctrl+Shift+M", toolbar=False)
        action("Maths", "Show the maths channels list", lambda: (self.maths.show(), self.maths.raise_()), toolbar=False)
        menus["Events"] = self.menuBar().addMenu("Events")
        action("Events", "Event rules…", self.open_rules_editor, "Ctrl+E", toolbar=False,
               tip="See and change what the detectors look for, and their thresholds")
        action("Events", "Look for events again", self.detect_again, toolbar=False)
        menus["File"].addSeparator()
        action("File", "Make report…", self.choose_report, "Ctrl+P", toolbar=False,
               tip="Write this session's report, with your maths channels, the events and your notes, and open it")
        bar.addSeparator()
        self.cancel = action("File", "Cancel loading", self.cancel_loading)
        self.cancel.setVisible(False)
        # every panel can be brought back from the View menu after it has been closed
        for dock in (self.browser_dock, self.readout_dock, self.range_dock, self.maths, self.events, self.map, self.video):
            menus["View"].addAction(dock.toggleViewAction())

    def open_rules_editor(self, dialog=None):
        """Open the event rule editor; when rules are saved there, look for events again."""
        dialog = dialog or RulesEditor(self.data_folder / "event_rules.json", self.session.model if self.session else None, self)
        dialog.saved.connect(self.detect_again)
        dialog.exec()

    def detect_again(self):
        """Run the saved rules over the open session and refresh the events list."""
        if self.session is None:
            return
        rules, complaint = load_rules(self.data_folder)
        self.session.model.events, self.session.events_summary = detect_events(self.session, rules)
        self._apply_reviews(self.session)
        self.events.show_model(self.session.model, self.session.events_summary)
        self.statusBar().showMessage(complaint or f"{len(self.session.model.events)} candidate event(s) with the saved rules.")

    def choose_report(self):
        if self.session is None:
            return
        stem = Path(self.session.raw.source or "session").name.rsplit(".", 1)[0]
        (self.data_folder / "reports").mkdir(parents=True, exist_ok=True)
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save the report", str(self.data_folder / "reports" / f"{stem}_report.html"),
                                                        "Web page (*.html)")
        if path:
            self.make_report(path)

    def make_report(self, path: str | Path, open_after: bool = True) -> Path | None:
        """Write the open session's report — its maths channels, the events
        found and your notes included — and open it in the browser."""
        if self.session is None:
            return None
        from veltrix_engine import maths as maths_engine
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            maths_engine.on_view(self.session, self.maths.results)      # so maths channels are in the lap comparison too
            out = write_report(self.session, path)
        except (ValueError, KeyError, OSError) as e:
            QtWidgets.QApplication.restoreOverrideCursor()
            QtWidgets.QMessageBox.warning(self, "Could not write the report", str(e))
            return None
        QtWidgets.QApplication.restoreOverrideCursor()
        self.statusBar().showMessage(f"Report written to {out}")
        if open_after:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(out)))
        return out

    def open_maths_editor(self):
        """Open the maths channel editor on the channel selected in the list, or on a blank one."""
        self.maths.open_editor(self.maths.selected())

    # ── sessions ──────────────────────────────────────────────────────────────

    def choose_file(self, mapping: bool = False):
        extensions = sorted({e for row in supported_formats() for e in row["extensions"].split()} | {".gz", ".ldx"})
        pattern = "Telemetry (" + " ".join(f"*{e}" for e in extensions) + ");;All files (*)"
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open telemetry", "", pattern)
        if path:
            self.open_file(path, mapping=mapping)

    def pick_channels(self, path: str, dialog=None) -> list | None | bool:
        """For a file that is read lazily (MDF), ask which channels to load.
        Returns the names chosen, None for "everything" (other formats are
        read whole anyway), or False if the user cancelled."""
        try:
            with open(path, "rb") as f:
                lazy = f.read(8)[:3] == b"MDF" and not str(path).lower().endswith(".gz")
            if not lazy:
                return None
            info = inspect_file(path)
        except (ValueError, OSError):
            return None                            # the loader will report what is wrong with the file
        dialog = dialog or ChannelPicker(info, self)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return False
        chosen = dialog.chosen()
        return None if len(chosen) == info["channel_count"] else chosen

    def open_file(self, path: str, mapping: bool = False, channels: list | None = None):
        """Load a file in the background; the window stays usable meanwhile.
        With `mapping`, the columns are shown for checking before the file is
        analysed — and a large MDF file first asks which channels to load."""
        self.cancel_loading()
        if mapping and channels is None:
            channels = self.pick_channels(path)
            if channels is False:
                self.statusBar().showMessage("Import cancelled.")
                return
        self._loader = _Loader(str(path), raw_only=mapping, channels=channels, data_folder=self.data_folder)
        self._loader.done.connect((lambda raw, p: self.map_and_show(raw, p)) if mapping else
                                  (lambda session, p: self.show_session(session, p)))
        self._loader.failed.connect(self._load_failed)
        self._loader.progress.connect(self._show_progress)
        self._loader.finished.connect(lambda: (self.cancel.setVisible(False), self.progress.setVisible(False)))
        self.cancel.setVisible(True)
        self.statusBar().showMessage(f"Loading {Path(path).name}…")
        self._loader.start()

    def cancel_loading(self):
        if self._loader is not None and self._loader.isRunning():
            self._loader.cancelled = True          # the read finishes on its thread; its result is dropped
            self.statusBar().showMessage("Loading cancelled.")
        self.cancel.setVisible(False)

    def _show_progress(self, done: int, total: int):
        self.progress.setVisible(True)
        self.progress.setMaximum(total)
        self.progress.setValue(done)

    def map_and_show(self, raw, path: str = "", dialog=None, wait: bool = False):
        """Show the mapping dialog for a file that has been read, then analyse
        it — in the background — with the mapping the user settled on. `wait`
        analyses before returning (used when there is no event loop to wait in)."""
        dialog = dialog or MappingDialog(raw, profiles_dir=self.profiles_dir, parent=self)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            self.statusBar().showMessage("Import cancelled.")
            return
        decisions = [vars(d).copy() for d in dialog.decisions]
        profile = dialog.profile.name if dialog.profile else ""

        def show(session, p):
            # how each column was decided, as the dialog had it, stays with the session
            session.model.mapping = [{**d, "profile": profile} for d in decisions if d["channel"]]
            self.show_session(session, p)
        self._loader = _Loader(str(path), raw=raw, mapping=dialog.import_args(), data_folder=self.data_folder)
        self._loader.done.connect(show)
        self._loader.failed.connect(lambda p, message: QtWidgets.QMessageBox.warning(self, "Could not analyse the file", message))
        self._loader.finished.connect(lambda: self.cancel.setVisible(False))
        self.statusBar().showMessage(f"Analysing {Path(path).name or 'the file'}…")
        if wait:
            self._loader.run()                     # same code, on this thread
        else:
            self.cancel.setVisible(True)
            self._loader.start()

    def _load_failed(self, path, message):
        self.statusBar().showMessage(f"Could not open {Path(path).name}.")
        QtWidgets.QMessageBox.warning(self, "Could not open the file", f"{Path(path).name}\n\n{message}")

    def show_session(self, session, path: str = ""):
        """Show a loaded session, keeping the current layout if there is one."""
        self.session = session
        if self.ws.charts:
            missing = self.ws.set_model(session.model)
        else:
            name = self.ws.name
            self.ws = default_layout(session.model)
            self.ws.name, missing = name, []
        self.active = min(self.active, max(len(self.ws.charts) - 1, 0))
        self._apply_compare = True
        if not hasattr(session, "events_summary"):              # a session handed over already loaded
            session.model.events, session.events_summary = detect_events(session, load_rules(self.data_folder)[0])
        session.model.annotations = self.notes.load(session.raw.sha256)
        self._apply_reviews(session)
        self.events.show_model(session.model, session.events_summary)
        self.video.set_links(self.videos.load(session.raw.sha256), self.ws.cursor or 0.0)
        failed = [n for n, r in self.maths.compute(session.model).items() if not r.ok]
        self._maths_seen = set(self.maths.mset.channels)
        self.map.set_session(session, self.ws.cursor if self.ws.cursor is not None else 0.0)
        self._corner_analysis(session)
        source = Path(path).name if path else (session.raw.source or "session")
        self._fill_browser()
        self.panel.set_session(Path(source).name, session.model.laps, self._measures())
        self._rebuild_charts()
        self.setWindowTitle(f"Veltrix Data — {source}")
        m = session.model.describe()
        self.statusBar().showMessage(
            f"{source}: {m['channels']['normalised']} channels of {m['channels']['raw']} recognised, {m['laps']} laps, "
            f"{len(session.warnings())} import warning(s)." + (f" Not in this session: {', '.join(missing)}." if missing else "")
            + (f" Maths channels not calculated: {', '.join(failed)}." if failed else ""))

    # ── events and annotations ────────────────────────────────────────────────

    def go_to(self, t: float, span: float = 10.0):
        """Put the cursor on a moment and show the seconds around it."""
        if self.session is None or not self._plots:
            return
        self.tabs.setCurrentIndex(0)
        self._plots[0].setXRange(*self.ws.zoom(t - span / 2, t + span / 2), padding=0)
        self.set_cursor(t)

    # ── event review ──────────────────────────────────────────────────────────

    def _apply_reviews(self, session):
        orphans = apply_reviews(session, self.data_folder)
        base = session.events_summary.split(" Earlier reviews:")[0]
        session.events_summary = base + (
            f" Earlier reviews: {orphans} refer to events no longer detected (rules or thresholds changed); kept with "
            "the events as first detected." if orphans else "")

    def review_event(self, row: int, verdict: str, note: str = ""):
        """Confirm or reject one detected event, or note it. Saved per file, with the
        event as it was detected, so the detector's classification can be audited."""
        if self.session is None or not 0 <= row < len(self.session.model.events):
            return
        e = self.session.model.events[row]
        ReviewStore(self.data_folder / "reviews").put(self.session.raw.sha256, e, verdict, note, author=self.author)
        self._apply_reviews(self.session)
        self.events.show_model(self.session.model, self.session.events_summary)
        self.statusBar().showMessage(f"{e.kind}: {verdict or 'note'} saved.")

    def clear_event_review(self, row: int):
        if self.session is None or not 0 <= row < len(self.session.model.events):
            return
        ReviewStore(self.data_folder / "reviews").clear(self.session.raw.sha256, self.session.model.events[row])
        self._apply_reviews(self.session)
        self.events.show_model(self.session.model, self.session.events_summary)

    def add_annotation(self, text: str, category: str = ""):
        if self.session is None:
            return
        try:
            annotate(self.session.model, text, self.ws.cursor, category)
            self.notes.save(self.session.raw.sha256, self.session.model.annotations)
        except ValueError as e:
            self.statusBar().showMessage(str(e))
            return
        self.events.show_model(self.session.model, self.session.events_summary)
        self._draw_annotations()

    def remove_annotation(self, index: int):
        if self.session is not None and 0 <= index < len(self.session.model.annotations):
            del self.session.model.annotations[index]
            self.notes.save(self.session.raw.sha256, self.session.model.annotations)
            self.events.show_model(self.session.model, self.session.events_summary)
            self._draw_annotations()

    def _draw_annotations(self):
        """Notes as dashed lines on the top chart, labelled; events are not drawn as notes."""
        for line in getattr(self, "_note_lines", []):
            if line.scene() is not None:
                line.scene().removeItem(line)
        self._note_lines = []
        if self.session is not None:
            self.map.set_notes(self.session.model.annotations)
        if self.session is None or not self._plots:
            return
        for a in self.session.model.annotations:
            if a.time is None:
                continue
            for k, plot in enumerate(self._plots):
                line = pg.InfiniteLine(pos=a.time, angle=90, movable=False,
                                       pen=pg.mkPen("#E0B25C", width=1, style=QtCore.Qt.DashLine),
                                       label=a.text if k == 0 else None,
                                       labelOpts={"position": 0.92, "color": "#E0B25C", "fill": "#0A0A0D"})
                plot.addItem(line)
                self._note_lines.append(line)

    # ── maths channels ────────────────────────────────────────────────────────

    def _maths_changed(self):
        """A maths channel was saved, changed or deleted in the editor (which has already recomputed)."""
        if self.session is not None:
            for chart in self.ws.charts:           # a deleted or renamed channel leaves the charts it was on
                chart.channels[:] = [c for c in chart.channels if self.session.model.has(c) or c not in self._maths_seen]
            self._maths_seen = set(self.maths.mset.channels)
            self._fill_browser()
            self.panel.set_channels(self._measures())
            self._rebuild_charts()

    def _plot_maths(self, name: str):
        if self.session is not None and self.session.model.has(name, "calculated"):
            self.add_channel(name)
        else:
            self.statusBar().showMessage(f"Save '{name}' first; it can be plotted once it has been calculated.")

    # ── channel browser ───────────────────────────────────────────────────────

    def _fill_browser(self):
        model = self.session.model
        rows = [c for layer in ("calculated", "corrected", "normalised") for c in model.channels(layer) if c.id != "time"]
        mapped = {c.source_name for c in model.channels("normalised")}
        rows += [c for c in model.channels("raw") if c.id not in mapped and c.values.dtype.kind in "biuf"]
        decided = {m["channel"]: m for m in model.mapping}
        self.browser.setRowCount(len(rows))
        for r, c in enumerate(rows):
            rate = c.sample_rate
            how = decided.get(c.id) if c.layer == "normalised" else None
            if how:
                by = how["basis"] + (", confirmed" if how.get("confirmed") else "") + (f" ({how['profile']})" if how.get("profile") else "")
                unit_from = {"file": "unit from the file", "stated": "unit stated at import", "profile": "unit from the profile",
                             "user": "unit typed at import", "plausibility": "unit worked out from the values",
                             "assumed": "unit assumed", "none": "no unit"}.get(how.get("unit_source", ""), "")
                by = "; ".join(x for x in (by, unit_from) if x)
            else:
                by = {"calculated": "calculated", "corrected": "rebuilt", "raw": "not mapped"}.get(c.layer, "")
            origin = c.source_name if c.layer in ("normalised", "raw") else ", ".join(d.split(":", 1)[-1] for d in c.dependencies)
            cells = (c.id, c.unit, f"{rate:g}" if rate else "", c.layer, c.label, origin, by, "; ".join(c.quality))
            for k, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                item.setData(QtCore.Qt.UserRole, c.ref if c.layer == "raw" else c.id)
                item.setToolTip(f"{c.description or c.name}\n{c.ref}"
                                + (f"\nformula: {c.formula}" if c.formula else "")
                                + (f"\nmapped by: {by}" if how else "")
                                + ("\n" + "\n".join(how["problems"]) if how and how.get("problems") else "")
                                + (f"\nunit from: {c.provenance.unit_source}" if c.provenance.unit_source else ""))
                if c.quality:
                    item.setForeground(QtGui.QColor("#CDBFA6"))
                self.browser.setItem(r, k, item)
        self.browser.resizeColumnsToContents()
        self._filter_browser(self.search.text())

    def _filter_browser(self, text: str):
        words = text.lower().split()
        for r in range(self.browser.rowCount()):
            row = " ".join(self.browser.item(r, k).text().lower() for k in range(self.browser.columnCount()))
            self.browser.setRowHidden(r, not all(w in row for w in words))

    def _corner_analysis(self, session):
        """The session's corners and per-lap corner figures, for the corner table
        under the overlaid laps and the zone marks on the map."""
        try:
            result = corner_engine.analyse(session)
            corners, metrics, reason = result["corners"], result["metrics"], ""
        except (ValueError, KeyError) as e:
            corners, metrics, reason = None, None, f"No corner analysis for this session: {e}"
        self.compare.set_corner_data(corners, metrics, reason)
        self.map.set_corners(corners)

    # ── measures and laps ─────────────────────────────────────────────────────

    def _measures(self) -> list:
        """The channels the panel lists as measures: [(id, unit), ...]."""
        model = self.session.model
        return [(c.id, c.unit) for layer in ("normalised", "corrected", "calculated") for c in model.channels(layer)
                if c.id not in ("time", "lap")]

    def loaded_laps(self) -> list:
        return list(self.compare.cmp.laps) if self.compare.cmp else []

    def load_laps(self, laps: list):
        """Draw exactly these laps over each other; the first is the reference.
        None loaded: the whole session against time."""
        if self.compare.cmp is None:
            if laps:
                self.statusBar().showMessage(self.compare.message.text())
            return
        self.compare.set_laps(list(laps))

    def toggle_lap(self, lap):
        loaded = self.loaded_laps()
        self.load_laps([x for x in loaded if x != lap] if lap in loaded else loaded + [lap])

    def make_reference(self, lap):
        self.load_laps([lap] + [x for x in self.loaded_laps() if x != lap])

    def set_measure(self, channel: str, on: bool):
        """A measure's button: pressed in, the channel gets a chart of its own;
        let out, it leaves every chart it is on."""
        if self.session is None:
            return
        if on:
            if not any(channel in chart.channels for chart in self.ws.charts):
                self.active = self.ws.add_chart([channel])
        else:
            for chart in self.ws.charts:
                chart.channels[:] = [c for c in chart.channels if c != channel]
            for index in reversed(range(len(self.ws.charts))):
                if not self.ws.charts[index].channels:
                    self.ws.remove_chart(index)
            self.active = max(min(self.active, len(self.ws.charts) - 1), 0)
        self._rebuild_charts()

    def _laps_changed(self):
        """Laps were loaded or unloaded: the panel lists them, and the centre
        shows them over each other — or the whole session when none is loaded."""
        laps = self.loaded_laps()
        self.panel.set_loaded(laps)
        self.tabs.setCurrentIndex(1 if laps else 0)
        if self.compare.cmp:
            self.ws.compare = self.compare.cmp.to_dict()
        if self.session is not None:
            self._show_values()
        if self.session is not None and laps != self._laps_shown:
            self._laps_shown = laps
            self.statusBar().showMessage(
                f"{len(laps)} lap(s) loaded and drawn over each other; reference: lap {laps[0]}." if laps
                else "No laps loaded: the whole session is drawn against time.")

    def _lap_cursor_moved(self, x: float):
        """The cursor moved on the loaded laps: the values beside the measures
        follow, and so do the map, the video and the readout, at the place the
        reference lap was then."""
        cmp_ = self.compare.cmp
        if self.session is None or not cmp_ or not cmp_.laps:
            return
        t = float(cmp_.session_time(cmp_.reference, x)[0])
        if np.isfinite(t):
            self.set_cursor(t)                 # ends by showing the values
        else:
            self._show_values()

    def _show_values(self):
        """Beside each drawn measure, its value at the cursor: one per loaded
        lap in the lap's colour, or the session's own when no lap is loaded."""
        if self.session is None:
            return
        drawn = [c for chart in self.ws.charts for c in chart.channels if self.session.model.has(c)]
        cmp_, values = self.compare.cmp, {}

        def text(v):
            return "—" if v is None else f"{v:.4g}" if abs(v) < 1e5 else f"{v:.0f}"
        if cmp_ and cmp_.laps:
            colour = {lap: lap_colour(i) for i, lap in enumerate(cmp_.laps)}
            for r in cmp_.readout(self.compare.cursor_x, drawn):
                values.setdefault(r["channel"], []).append((text(r["value"]), colour[r["lap"]]))
        else:
            for r in (r for chart in self.ws.readout() for r in chart):
                values[r.channel] = [(text(r.value), "#F4F2F8")]
        self.panel.show_values(values)

    # ── cursor ────────────────────────────────────────────────────────────────

    def _clicked(self, event):
        for k, plot in enumerate(self._plots):
            if plot.sceneBoundingRect().contains(event.scenePos()):
                self.select_chart(k)
                self.set_cursor(plot.vb.mapSceneToView(event.scenePos()).x())
                return

    def _cursor_dragged(self, line):
        if not self._syncing:
            self.set_cursor(line.value())

    def _save_videos(self):
        if self.session is not None:
            try:
                self.videos.save(self.session.raw.sha256, self.video.links)
            except ValueError as e:
                self.statusBar().showMessage(str(e))

    def set_cursor(self, t: float, from_video: bool = False):
        """Move the cursor on every chart and refresh the readout, the map and
        the video (unless the video is what moved it)."""
        if self.session is None:
            return
        t = self.ws.set_cursor(t)
        if not from_video:
            self.video.follow_cursor(t)
        self._syncing = True
        for line in self._cursors:
            line.setValue(t)
        self._syncing = False
        self.map.set_cursor(t)
        lap = self.ws.lap_at()
        self.where.setText(f"t = {t:.3f} s" + (f"   ·   lap {lap.lap} ({lap.status}), {t - lap.t_start:.3f} s into it" if lap else ""))
        rows = [r for chart in self.ws.readout() for r in chart]
        self.readout.setRowCount(len(rows))
        for k, r in enumerate(rows):
            value = "—" if r.value is None else f"{r.value:.4g}" if abs(r.value) < 1e5 else f"{r.value:.0f}"
            for col, text in enumerate((r.channel, value, r.unit, HOW_MARK[r.how], r.label)):
                item = QtWidgets.QTableWidgetItem(text)
                if r.how in ("interpolated", "held", "none"):
                    item.setForeground(QtGui.QColor("#9A97A6"))
                self.readout.setItem(k, col, item)
        self.readout.resizeColumnsToContents()
        self._show_values()

    # ── workspaces ────────────────────────────────────────────────────────────

    def save_workspace(self, path: str | None = None):
        if not path:
            path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save workspace", f"{self.ws.name}.veltrix-workspace.json",
                                                            "Workspace (*.json)")
        if path:
            if self.compare.cmp:
                self.ws.compare = self.compare.cmp.to_dict()
            self.ws.name = Path(path).name.split(".")[0]
            self.ws.save(path)
            self.statusBar().showMessage(f"Workspace saved to {path}")

    def choose_workspace(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open workspace", "", "Workspace (*.json)")
        if path:
            self.open_workspace(path)

    def open_workspace(self, path: str):
        try:
            self.ws = Workspace.load(path, self.session.model if self.session else None)
        except (ValueError, KeyError, OSError) as e:
            QtWidgets.QMessageBox.warning(self, "Could not open the workspace", str(e))
            return
        self.active = 0
        self._apply_compare = True
        if self.session is not None:
            self._rebuild_charts()
        self.statusBar().showMessage(f"Workspace {self.ws.name} opened." + (
            f" Not in this session: {', '.join(self.ws.missing())}." if self.ws.missing() else ""))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["Veltrix Data"])
    app.setApplicationName("Veltrix Data")
    from veltrix_engine.logs import setup as setup_log
    from . import crash
    setup_log()
    window = MainWindow()
    crash.install(window)
    window.show()
    if argv:
        window.open_file(argv[0])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
