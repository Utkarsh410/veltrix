"""
File ▸ Make reports for files…: a report of each file picked, with its debrief
(HTML and PDF) and coaching cards, and an index page linking them — what
dropping several files on Veltrix Data.app does, from inside the app. The
files are not opened in the workspace.
"""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from veltrix_engine.batch import common_folder, run_batch, write_index


def make_reports(parent, files, out_dir, data_folder=None, open_after: bool = True, pdf: bool = True) -> Path | None:
    """Write the reports with a progress dialog that can stop between files.
    Returns the index page, or None when nothing was written."""
    files = [Path(f) for f in files]
    if not files:
        return None
    dialog = QtWidgets.QProgressDialog("Reading files…", "Stop", 0, len(files), parent)
    dialog.setWindowTitle("Make reports")
    dialog.setWindowModality(QtCore.Qt.WindowModal)
    dialog.setMinimumDuration(0)
    dialog.setValue(0)

    def progress(done, total, row):
        dialog.setValue(done)
        dialog.setLabelText(f"{done} of {total}: {row.file}")
        QtWidgets.QApplication.processEvents()

    def cancel():
        QtWidgets.QApplication.processEvents()
        return dialog.wasCanceled()
    root = common_folder(files)
    rows = run_batch(files, root=root, out_dir=out_dir, outputs=("report", "documents"), progress=progress,
                     app_data=data_folder, pdf=pdf, cancel=cancel)
    dialog.close()
    if not rows:
        return None
    index = write_index(rows, out_dir)[2]
    failed = [r for r in rows if r.status == "failed"]
    if open_after:
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(index)))
    if parent is not None and hasattr(parent, "statusBar"):
        parent.statusBar().showMessage(f"{len(rows) - len(failed)} report(s) written to {Path(out_dir)}"
                                       + (f"; {len(failed)} file(s) could not be read (see the index)" if failed else "")
                                       + ("; stopped before the rest" if len(rows) < len(files) else ""))
    return index
