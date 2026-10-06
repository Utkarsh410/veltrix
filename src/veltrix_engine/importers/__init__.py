"""Importers turn a file into a RawTable. One module per format, no shared state."""

from .csv_importer import RawTable, parse_csv_text, read_csv

__all__ = ["RawTable", "parse_csv_text", "read_csv"]
