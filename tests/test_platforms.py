"""What differs by operating system: where the app keeps its data, and file
paths on Windows drives. Run on macOS by imitating the other platform."""

import os
import sys
from pathlib import Path

from veltrix_engine.batch import common_folder
from veltrix_engine.user_data import data_dir


def test_the_data_folder_follows_the_platform(monkeypatch, tmp_path):
    monkeypatch.delenv("VELTRIX_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert data_dir() == Path.home() / "Library" / "Application Support" / "Veltrix Data"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert data_dir() == tmp_path / "Roaming" / "Veltrix Data"
    monkeypatch.delenv("APPDATA")
    assert data_dir() == Path.home() / "AppData" / "Roaming" / "Veltrix Data"
    monkeypatch.setenv("VELTRIX_DATA_DIR", str(tmp_path / "mine"))
    assert data_dir() == tmp_path / "mine"


def test_files_with_no_common_folder_are_shown_by_name(tmp_path, monkeypatch):
    """Files on C: and D: share no folder, and os.path.commonpath raises."""
    a, b = tmp_path / "one" / "a.csv", tmp_path / "two" / "b.csv"
    assert common_folder([a, b]) == tmp_path.resolve() and common_folder([]) == Path(".")

    def different_drives(paths):
        raise ValueError("Paths don't have the same drive")
    monkeypatch.setattr(os.path, "commonpath", different_drives)
    assert common_folder([a, b]) == a.resolve().parent
