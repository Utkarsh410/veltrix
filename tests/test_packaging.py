"""What ships with the downloads: the licence folder written by tools/collect_licences.py."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from collect_licences import collect, copyright_holder  # noqa: E402


def test_the_licence_names_the_copyright_holder_and_every_package(tmp_path):
    out = collect(tmp_path / "licences")
    licence = (out / "LICENSE.txt").read_text(encoding="utf-8")
    assert f"Copyright (c) 2026 {copyright_holder()}." in licence and "{copyright}" not in licence
    notices = (out / "THIRD-PARTY-NOTICES.txt").read_text(encoding="utf-8")
    for package in ("PySide6", "numpy", "pandas", "asammdf", "python-can"):
        assert package in notices
    assert "No licence file was shipped by" not in notices, "every package has its licence text"
    assert {p.name for p in (out / "licences" / "standard").iterdir()} >= {"LGPL-3.0.txt", "GPL-3.0.txt"}
