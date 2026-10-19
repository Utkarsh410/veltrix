"""
HTML to PDF, through Qt's web engine (the `ui` extra, PySide6), rendered
offscreen. Optional: without PySide6 a PDF cannot be made and the caller is
told so; the HTML still is.
"""

from __future__ import annotations

import os
from pathlib import Path


def available() -> bool:
    try:
        import PySide6.QtWebEngineCore  # noqa: F401
        return True
    except ImportError:
        return False


def html_to_pdf(page: str, out: str | Path, timeout_s: float = 60.0) -> Path:
    """Print an HTML page (a string) to an A4 PDF. Raises RuntimeError when it cannot."""
    if not available():
        raise RuntimeError("A PDF needs PySide6 with Qt WebEngine (pip install 'veltrix-engine[ui]'); the HTML was written.")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen") if not os.environ.get("DISPLAY") and os.name != "nt" \
        and not _has_app() else None
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtWebEngineCore import QWebEnginePage
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["veltrix-pdf"])
    out = Path(out)
    page_obj = QWebEnginePage()
    done = {"ok": None}
    loop = QtCore.QEventLoop()

    def printed(path, ok):
        done["ok"] = ok
        loop.quit()

    def loaded(ok):
        if not ok:
            done["ok"] = False
            loop.quit()
            return
        layout = QtGui.QPageLayout(QtGui.QPageSize(QtGui.QPageSize.A4), QtGui.QPageLayout.Portrait,
                                   QtCore.QMarginsF(14, 14, 14, 14), QtGui.QPageLayout.Millimeter)
        page_obj.printToPdf(str(out), layout)

    page_obj.pdfPrintingFinished.connect(printed)
    page_obj.loadFinished.connect(loaded)
    page_obj.setHtml(page, QtCore.QUrl("about:blank"))
    QtCore.QTimer.singleShot(int(timeout_s * 1000), loop.quit)
    loop.exec()
    if not done["ok"] or not out.exists():
        raise RuntimeError(f"The PDF could not be made ({out}).")
    return out


def _has_app() -> bool:
    try:
        from PySide6 import QtWidgets
        return QtWidgets.QApplication.instance() is not None
    except ImportError:
        return False
