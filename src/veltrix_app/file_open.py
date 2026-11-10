"""
Files dropped on the app's icon (or opened with it from Finder). macOS sends
these as open events rather than command-line arguments, one per file, so they
are gathered for a moment and then handled as a group: one file opens in the
workspace, several make their reports — as the launcher app did with
`veltrix app` and `veltrix reports`.
"""

from __future__ import annotations

from PySide6 import QtCore

GATHER_MS = 400


class FileOpenHandler(QtCore.QObject):
    """Installed on the QApplication; hands each group of opened files to the window."""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window
        self.pending: list[str] = []
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(GATHER_MS)
        self.timer.timeout.connect(self.flush)

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.FileOpen and event.file():
            self.pending.append(event.file())
            self.timer.start()
            return True
        return False

    def flush(self):
        files, self.pending = list(dict.fromkeys(self.pending)), []
        if len(files) == 1:
            self.window.open_file(files[0])
        elif files:
            from .reports import make_reports
            make_reports(self.window, files, self.window.data_folder / "reports", self.window.data_folder)
