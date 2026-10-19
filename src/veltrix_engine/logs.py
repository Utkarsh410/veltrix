"""
The log: what Veltrix did and what went wrong, kept in the data folder
(logs/veltrix.log, three files of 1 MB at most). Nothing from a session's
data is written to it — file names, formats, timings and errors only.

The app and the command line call `setup()` once; the library itself only
writes to the "veltrix" logger and leaves where it goes to the program using it.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_NAME = "veltrix.log"
_HANDLER_TAG = "_veltrix_file_handler"


def log_dir(folder: str | Path | None = None) -> Path:
    from .user_data import data_dir
    return Path(folder or data_dir()) / "logs"


def setup(folder: str | Path | None = None, level: int = logging.INFO) -> Path:
    """Send the "veltrix" logger to the log file in this folder (moving it there
    if it was going elsewhere). Returns the file's path."""
    path = log_dir(folder) / LOG_NAME
    logger = logging.getLogger("veltrix")
    logger.setLevel(level)
    for h in [h for h in logger.handlers if getattr(h, _HANDLER_TAG, False)]:
        if Path(h.baseFilename) == path.resolve():
            return path                        # already writing there
        logger.removeHandler(h)                # another folder: the log follows the new one
        h.close()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    except OSError:
        return path                        # a read-only data folder costs the log, not the program
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    setattr(handler, _HANDLER_TAG, True)
    logger.addHandler(handler)
    return path


def tail(folder: str | Path | None = None, lines: int = 60) -> str:
    """The last lines of the log, for a crash report."""
    path = log_dir(folder) / LOG_NAME
    try:
        return "".join(path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)[-lines:])
    except OSError:
        return ""
