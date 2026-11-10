# Packaging for macOS

`sh tools/build_mac_bundle.sh` builds a self-contained **Veltrix Data.app** and a disk image
(`dist/Veltrix-Data-<version>.dmg`). The app carries its own Python 3.12 and every library, and does
not depend on this folder or its virtual environment. Built and checked on this Mac on 2026-11-10;
**it is signed ad hoc, so it opens only on this Mac**, until it is signed with a Developer ID
certificate and notarised (below).

`sh tools/build_mac_app.sh` still builds the small launcher that runs this folder's code, for
development. Both are called Veltrix Data.app; the bundle is written to `dist/`, the launcher to the
project folder.

## What it does

| | |
|---|---|
| Double-click | opens the desktop workspace |
| Drop one file on it (or Open With) | opens that session in the workspace |
| Drop several files on it | the workspace opens and writes a report of each, with its debrief (HTML and PDF) and coaching cards, into `~/Library/Application Support/Veltrix Data/reports`, and opens an index page |

macOS hands dropped files to a bundled app as open events, not arguments: `veltrix_app/file_open.py`
gathers them for 0.4 s and treats them as a group. The launcher passed them as arguments to `veltrix
app` and `veltrix reports`, and wrote reports into `reports/` in the project folder instead.

The app offers itself (`LSHandlerRank` Alternate) for every extension the engine reads, without
becoming the default app for `.csv`, `.txt` or `.log`. The command line (`veltrix …`) is not inside
the bundle.

## How it is built

| Step | What | Where |
|---|---|---|
| Bundle | PyInstaller, one-folder mode: the libraries stay ordinary files in `Contents/Frameworks` | `packaging/veltrix_data.spec`, `packaging/launch.py` |
| Trim | asammdf's own viewer, the QML/Quick bindings and the QML plugin tree (which pulled in Qt 3D, Charts, Location…) are left out. Qt WebEngine stays: it prints the PDFs, and it links QtQml and QtQuick | the spec |
| Sign | inner-most first: Qt WebEngine's helper (with Qt's own entitlements), every `.so` and `.dylib`, each framework, then the app; then `codesign --verify --deep --strict` | the build script |
| Disk image | the app and an Applications link, UDZO | the build script |

| Setting | Effect |
|---|---|
| nothing | ad-hoc signature |
| `VELTRIX_SIGN_IDENTITY="Developer ID Application: Name (TEAMID)"` | signed with hardened runtime and a timestamp; `packaging/entitlements.plist` allows only unsigned executable memory (ctypes and libffi) |
| `VELTRIX_NOTARY_PROFILE=<profile>` | also submitted with `notarytool`, then stapled to the disk image and the app, and checked with `spctl` |
| `VELTRIX_BUILD_DIR=<folder>` | `dist/` and the working files go there (default: the project folder) |

PyInstaller comes with the `package` extra: `uv pip install --python .venv ".[package]"`.

### Checked on 2026-11-10

- Three files dropped on the bundle (synthetic GT3 CSV, Monza AC CSV, the Spa `.ibt`): every report,
  debrief, PDF and coaching card written. Each file's samples, laps, best lap and warnings are the same
  as `veltrix reports` from the virtual environment gives, and so are the report pages, apart from the
  PDF link.
- Size: about 655 MB for the app. Most of it is Qt WebEngine (about 230 MB) and PyArrow (110 MB).

### Found while building

- An **empty folder `Versions/Resources`** in the virtual environment's
  `QtWebEngineCore.framework` (not from the PySide6 wheel) made PyInstaller take it for the
  framework's version. Qt WebEngine then could not find its helper process, and the app quit when it
  came to print the first PDF. The build script removes empty folders of that kind before it builds.
- Building with the working files on the USB drive was slow, and once stalled for good: a process hung
  waiting on the drive and could not be stopped. `VELTRIX_BUILD_DIR` on the internal disk avoids it.

## Licences

| Part | Licence | How it ships |
|---|---|---|
| Python 3.12 | PSF | `Contents/Frameworks/Python.framework` |
| NumPy, pandas, SciPy, PyArrow, h5py | BSD / Apache-2.0 | ordinary files in `Contents/Frameworks` |
| PySide6 / Qt 6.12, with Qt Multimedia and Qt WebEngine | LGPL-3.0 | `Contents/Frameworks/PySide6`, replaceable |
| FFmpeg inside Qt Multimedia | LGPL-2.1+ | inside PySide6's Qt |
| asammdf, python-can | LGPL-3.0 | plain `.py` files in `Contents/Resources` (`module_collection_mode` "py" in the spec), not compiled into the archive, so replaceable |
| pyqtgraph, libxrk | MIT | |

**Still to do:** put the licence texts and a note on where the LGPL libraries live into
`Contents/Resources` before the app is given to anyone else.

## Not done yet

- **Developer ID certificate.** There is none on this Mac (`security find-identity -v -p codesigning`
  finds no identities). It needs a paid Apple Developer Program membership. With it, set
  `VELTRIX_SIGN_IDENTITY`, store a notary profile once with
  `xcrun notarytool store-credentials`, and set `VELTRIX_NOTARY_PROFILE`. The hardened-runtime build
  has not been run, because it needs the certificate.
- An app icon (it uses the default one).
- Opening the quarantined disk image on a second Mac: that has to wait for notarisation.
- Intel Macs: the build is arm64 only.
