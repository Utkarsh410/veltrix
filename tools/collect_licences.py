"""
The licence folder that ships inside the app and the installers:

    LICENSE.txt                  Veltrix Data's own licence (packaging/EULA.txt)
    THIRD-PARTY-NOTICES.txt      every bundled package: version, licence, where it is in the app
    licences/<package>/…         each package's own licence files, as it ships them
    licences/standard/…          standard texts (GNU, Apache) for packages that ship none

The packages are the dependency tree of veltrix-engine with the ui and all
extras, read from the installed metadata, so a package the app does not use is
not listed. Run by packaging/veltrix_data.spec; on its own:

    .venv/bin/python tools/collect_licences.py OUT_DIR
"""

from __future__ import annotations

import re
import shutil
import sys
import sysconfig
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTRAS = ("ui", "all")
LICENCE_FILE = re.compile(r"(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", re.I)
STANDARD = ROOT / "packaging" / "licences"

QT_NOTE = """\
Qt for Python (PySide6, shiboken6) and Qt 6 — LGPL-3.0
  Licensed under the GNU Lesser General Public License version 3 (licences/standard/LGPL-3.0.txt,
  which supplements licences/standard/GPL-3.0.txt). Qt WebEngine includes Chromium, whose
  third-party components are listed at https://doc.qt.io/qt-6/qtwebengine-licensing.html;
  the other components Qt uses are listed at https://doc.qt.io/qt-6/licenses-used-in-qt.html.
  Source: https://download.qt.io/official_releases/QtForPython/ and https://www.qt.io/download-open-source

FFmpeg (inside Qt Multimedia) — LGPL-2.1 or later
  Built by Qt as an LGPL build (licences/standard/LGPL-2.1.txt). Source: https://ffmpeg.org/download.html
"""

REPLACING = """\
REPLACING THE LGPL LIBRARIES

Veltrix Data uses these LGPL libraries as separate files that are loaded when it runs, not
linked into it, so you may replace them with your own modified versions:

  Qt 6 and PySide6     macOS: Veltrix Data.app/Contents/Frameworks/PySide6/
                       Windows: <install folder>\\_internal\\PySide6\\
  asammdf, python-can  macOS: Veltrix Data.app/Contents/Resources/asammdf/ and can/
                       Windows: <install folder>\\_internal\\asammdf\\ and can\\
                       (plain Python source files)

A replacement must keep the same interface. On macOS, re-sign the app afterwards
(codesign --force --deep --sign - "Veltrix Data.app"). Nothing in Veltrix Data's licence
restricts modifying these libraries or reverse engineering to debug such modifications.
"""


def _name(dist) -> str:
    return re.sub(r"[-_.]+", "-", dist.metadata["Name"]).lower()


def _own_requirements(extras) -> list:
    """Veltrix Data's requirements with these extras, from pyproject.toml — the installed
    metadata of an editable install can predate the extras."""
    import tomllib
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    return project["dependencies"] + [r for e in extras for r in project["optional-dependencies"][e]]


def bundled(extras=EXTRAS) -> list:
    """The installed distributions in Veltrix Data's dependency tree."""
    from packaging.requirements import Requirement
    seen, found = set(), []
    todo = [(r.name, set(r.extras)) for r in map(Requirement, _own_requirements(extras))]
    while todo:
        name, wanted = todo.pop()
        key = re.sub(r"[-_.]+", "-", name).lower()
        if key in seen:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue                                     # an optional package that is not installed
        seen.add(key)
        found.append(dist)
        for line in dist.requires or []:
            req = Requirement(line)
            # Kept when it has no marker, or its marker holds here for one of the extras asked for
            # (a plain platform marker holds with no extra at all).
            if req.marker is None or any(req.marker.evaluate({"extra": e}) for e in wanted | {""}):
                todo.append((req.name, set(req.extras)))
    return sorted(found, key=_name)


def licence_of(dist) -> str:
    meta = dist.metadata
    expr = meta.get("License-Expression")
    if expr:
        return expr
    classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    short = (meta.get("License") or "").strip().splitlines()
    return "; ".join(classifiers) or (short[0][:80] if short else "see its licence files")


def collect(out: Path) -> Path:
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    (out / "licences").mkdir(parents=True)
    shutil.copy(ROOT / "packaging" / "EULA.txt", out / "LICENSE.txt")
    shutil.copytree(STANDARD, out / "licences" / "standard")

    rows, missing = [], []
    python_licence = Path(sysconfig.get_paths()["stdlib"]) / "LICENSE.txt"
    if python_licence.exists():
        (out / "licences" / "python").mkdir()
        shutil.copy(python_licence, out / "licences" / "python" / "LICENSE.txt")
    rows.append(("Python", sys.version.split()[0], "PSF-2.0", "licences/python/"))
    for dist in bundled():
        files = [f for f in dist.files or [] if LICENCE_FILE.search(f.name) and ".dist-info" in str(f)] or \
                [f for f in dist.files or [] if LICENCE_FILE.search(f.name)]
        where = f"licences/{_name(dist)}/"
        for f in files:
            target = out / "licences" / _name(dist) / Path(*[p for p in f.parts if not p.endswith(".dist-info")])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(f.locate(), target)
        if not files:
            if _name(dist).startswith(("pyside6", "shiboken6")):
                where = "licences/standard/ (see the Qt section)"
            elif "Apache" in licence_of(dist):
                where = "licences/standard/Apache-2.0.txt"
            else:
                where = "—"
                missing.append(dist.metadata["Name"])
        rows.append((dist.metadata["Name"], dist.version, licence_of(dist), where))

    width = max(len(r[0]) for r in rows)
    lines = ["THIRD-PARTY SOFTWARE IN VELTRIX DATA", "",
             "Veltrix Data includes the open-source software below. Each is used under its own licence,",
             "whose text is in the folder named. Veltrix Data's own licence is LICENSE.txt.", ""]
    lines += [f"{n:<{width}}  {v:<14} {lic}\n{'':<{width}}  {'':<14} {where}" for n, v, lic, where in rows]
    lines += ["", QT_NOTE, REPLACING]
    if missing:
        lines += ["No licence file was shipped by: " + ", ".join(missing) + " (licence as listed above).", ""]
    (out / "THIRD-PARTY-NOTICES.txt").write_text("\n".join(lines), encoding="utf-8")
    return out


if __name__ == "__main__":
    print(collect(Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "build" / "licences")))
