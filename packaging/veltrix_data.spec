# PyInstaller spec for the self-contained Veltrix Data app, on macOS ("Veltrix Data.app") and
# Windows (a "Veltrix Data" folder with "Veltrix Data.exe", which the installer packs). One-folder
# mode, so the LGPL libraries stay ordinary, replaceable files.
# Built by tools/build_mac_bundle.sh and tools/build_windows.ps1; see docs/packaging.md.

import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
MAC = sys.platform == "darwin"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
from veltrix_engine.importers import FORMATS  # noqa: E402
from collect_licences import collect  # noqa: E402

PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
VERSION = PROJECT["project"]["version"]
COPYRIGHT = PROJECT["tool"]["veltrix"]["copyright"]
BUNDLE_ID = "com.utkarsh410.veltrix-data"

# Optional readers are imported only when a file needs them, so name them.
hidden = collect_submodules("veltrix_engine") + collect_submodules("veltrix_app")
for optional in ("asammdf", "libxrk", "h5py", "can"):
    try:
        __import__(optional)
        hidden += collect_submodules(optional, filter=lambda name: not name.startswith("asammdf.gui"))
    except ImportError:
        pass

# This app's licence and every bundled package's, in a "licences" folder inside the app.
licences = collect(Path(workpath) / "licences")
datas = (collect_data_files("veltrix_engine") + collect_data_files("veltrix_app") + copy_metadata("veltrix-engine")
         + [(str(licences), "licences")])
for name in ("asammdf", "libxrk"):
    try:
        datas += collect_data_files(name)
    except Exception:
        pass

# App icon: .icns for the macOS bundle, .ico for the Windows exe (made by packaging/icons/make_icon.py).
ICONS = ROOT / "packaging" / "icons"

extensions = sorted({e.lstrip(".") for f in FORMATS for e in f.extensions} | {"gz"})

a = Analysis(
    [str(ROOT / "packaging" / "launch.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    hiddenimports=hidden,
    # asammdf's own viewer and the QML/Quick bindings are not used; Qt WebEngine
    # (for PDFs) still links the QtQml and QtQuick libraries, which stay.
    excludes=["tkinter", "pytest", "IPython", "matplotlib", "PyInstaller", "asammdf.gui",
              "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtUiTools"],
    noarchive=False,
    # The LGPL Python libraries go in as plain .py files beside the app, so they can be replaced.
    module_collection_mode={"asammdf": "py", "can": "py"},
)


def _qml(entry) -> bool:
    """The QML plugin tree (PySide6/Qt/qml on macOS, PySide6/qml on Windows), which pulls in
    Qt 3D, Charts, Location and more."""
    dest = entry[0].replace("\\", "/")
    return dest.startswith("PySide6/") and "/qml/" in dest


a.binaries = [b for b in a.binaries if not _qml(b)]
a.datas = [d for d in a.datas if not _qml(d)]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Veltrix Data",
          console=False, argv_emulation=False, target_arch="arm64" if MAC else None,
          icon=None if MAC else str(ICONS / "veltrix-data.ico"),
          codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, name="Veltrix Data")
if MAC:
    app = BUNDLE(
        coll,
        name="Veltrix Data.app",
        icon=str(ICONS / "veltrix-data.icns"),
        bundle_identifier=BUNDLE_ID,
        version=VERSION,
        info_plist={
            "CFBundleName": "Veltrix Data",
            "CFBundleDisplayName": "Veltrix Data",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "13.0",
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.developer-tools",
            "NSHumanReadableCopyright": f"Copyright © 2026 {COPYRIGHT}. All rights reserved.",
            # Alternate: offered for these files and accepts drops, without taking
            # over .csv, .txt or .log from the apps that already open them.
            "CFBundleDocumentTypes": [{
                "CFBundleTypeName": "Telemetry file",
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "Alternate",
                "CFBundleTypeExtensions": extensions,
            }],
        },
    )
