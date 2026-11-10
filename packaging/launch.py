"""Entry point of the bundled Veltrix Data.app: the desktop workspace.
Files dropped on the icon arrive as open events (veltrix_app.file_open)."""

import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()     # batch and library imports can use worker processes
    from veltrix_app.window import main
    sys.exit(main([a for a in sys.argv[1:] if not a.startswith("-psn_")]))
