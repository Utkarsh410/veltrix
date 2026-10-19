"""
What the app adds to a session beyond the file — maths channels, events, notes,
reviews. Moved to `veltrix_engine.user_data` so the command line reads them
without importing the app; kept here so existing imports keep working.
"""

from veltrix_engine.user_data import apply, load_rules  # noqa: F401
