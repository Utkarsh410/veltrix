"""
When the app hits an error it did not expect: a crash report is written to the
data folder (crash_reports/), the user is told where it is, and nothing is sent
anywhere — sending it is the user's choice. The report holds the error, the
versions and the end of the log; no telemetry, no file contents.
"""

from __future__ import annotations

import platform
import sys
import traceback
from datetime import datetime
from pathlib import Path

from veltrix_engine import __version__
from veltrix_engine.logs import tail
from veltrix_engine.user_data import data_dir


def write_report(exc_type, exc, tb, folder: str | Path | None = None) -> Path:
    folder = Path(folder or data_dir())
    out = folder / "crash_reports" / f"crash-{datetime.now():%Y%m%d-%H%M%S}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PySide6 import __version__ as qt
    except ImportError:
        qt = "not loaded"
    text = [f"Veltrix Data {__version__} — crash report, {datetime.now():%Y-%m-%d %H:%M:%S}",
            f"Python {sys.version.split()[0]} · {platform.platform()} · PySide6 {qt}", "",
            "".join(traceback.format_exception(exc_type, exc, tb)).rstrip(), "",
            "Last lines of the log:", tail(folder) or "(no log)"]
    out.write_text("\n".join(text) + "\n", encoding="utf-8")
    return out


def install(parent=None, folder: str | Path | None = None, interactive: bool = True):
    """Replace sys.excepthook while the app runs. Returns the hook, for tests."""
    previous = sys.excepthook

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return previous(exc_type, exc, tb)
        try:
            path = write_report(exc_type, exc, tb, folder)
        except OSError:
            path = None
        import logging
        logging.getLogger("veltrix.app").error("unexpected error", exc_info=(exc_type, exc, tb))
        hook.last_report = path
        if interactive:
            _tell(parent, exc, path)

    hook.last_report = None
    sys.excepthook = hook
    return hook


def _tell(parent, exc, path):
    from PySide6 import QtCore, QtGui, QtWidgets
    box = QtWidgets.QMessageBox(parent)
    box.setIcon(QtWidgets.QMessageBox.Warning)
    box.setWindowTitle("Veltrix Data hit an unexpected error")
    box.setText(f"Something went wrong that Veltrix did not expect:\n{type(exc).__name__}: {exc}\n\n"
                "Your data is not changed. You can carry on, or save your work and restart.")
    if path is not None:
        box.setInformativeText(f"A crash report was saved to:\n{path}\nIt holds the error and the versions, no telemetry. "
                               "Send it if you would like the problem looked at — nothing is sent for you.")
        show = box.addButton("Show report", QtWidgets.QMessageBox.ActionRole)
        show.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path.parent))))
    box.addButton(QtWidgets.QMessageBox.Close)
    box.exec()
