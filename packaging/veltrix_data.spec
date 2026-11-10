# PyInstaller spec for the self-contained "Veltrix Data.app" (one-folder mode,
# so the LGPL libraries stay ordinary, replaceable files inside the bundle).
# Built by tools/build_mac_bundle.sh; see docs/packaging.md.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT / "src"))
from veltrix_engine.importers import FORMATS  # noqa: E402

VERSION = "0.1.0"
BUNDLE_ID = "com.utkarsh410.veltrix-data"

# Optional readers are imported only when a file needs them, so name them.
hidden = collect_submodules("veltrix_engine") + collect_submodules("veltrix_app")
for optional in ("asammdf", "libxrk", "h5py", "can"):
    try:
        __import__(optional)
        hidden += collect_submodules(optional, filter=lambda name: not name.startswith("asammdf.gui"))
    except ImportError:
        pass

datas = collect_data_files("veltrix_engine") + copy_metadata("veltrix-engine")
for name in ("asammdf", "libxrk"):
    try:
        datas += collect_data_files(name)
    except Exception:
        pass

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
# The QML plugin tree (and the Qt 3D, Charts, Location… frameworks it pulls in).
a.binaries = [b for b in a.binaries if "/Qt/qml/" not in b[0]]
a.datas = [d for d in a.datas if "/Qt/qml/" not in d[0]]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Veltrix Data",
          console=False, argv_emulation=False, target_arch="arm64",
          codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, name="Veltrix Data")
app = BUNDLE(
    coll,
    name="Veltrix Data.app",
    icon=None,
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
