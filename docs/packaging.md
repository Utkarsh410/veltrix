# Packaging and downloads

Veltrix Data ships as two downloads, built by GitHub Actions (`.github/workflows/release.yml`) and
published on the repository's GitHub Releases page:

| Download | For | Built by |
|---|---|---|
| `Veltrix-Data-mac.dmg` | macOS 13 or later, Apple Silicon | `tools/build_mac_bundle.sh` |
| `Veltrix-Data-Windows-Setup.exe` | Windows 10 or 11, 64-bit | `tools/build_windows.ps1` |

Each carries its own Python 3.12 and every library, and does not depend on this folder. **Neither is
signed for distribution yet** (decided 2026-11-10: unsigned to start). What users see:

- **macOS:** the first open is blocked ("Apple could not verify 'Veltrix Data' is free of malware").
  Open **System Settings ▸ Privacy & Security**, scroll down, click **Open Anyway** next to Veltrix
  Data, and confirm. This is needed once per installed copy.
- **Windows:** SmartScreen shows "Windows protected your PC". Click **More info ▸ Run anyway**. This
  is needed once per downloaded file.

`sh tools/build_mac_app.sh` still builds the small launcher that runs this folder's code, for
development.

## Publishing a release

1. Set the version in `pyproject.toml` (`version = "0.2.0"`), commit, and merge to `main`.
2. Tag the commit and push the tag:
   ```bash
   git tag v0.2.0 && git push origin v0.2.0
   ```
3. The workflow builds both downloads, runs each built app on two sessions, and publishes a release
   named "Veltrix Data 0.2.0" with both files and notes made from the commits. It refuses a tag that
   does not match `pyproject.toml`.

To check a build without publishing: **Actions ▸ release ▸ Run workflow**. The two files are kept on
that run's page as artifacts.

### Links for the website

These always point at the newest release, so the buttons never need changing:

```
https://github.com/Utkarsh410/veltrix/releases/latest/download/Veltrix-Data-mac.dmg
https://github.com/Utkarsh410/veltrix/releases/latest/download/Veltrix-Data-Windows-Setup.exe
```

Next to each button, the page should say which systems it is for (above), and link the steps for the
macOS and Windows warnings, until the downloads are signed.

## What the app does

| | |
|---|---|
| Open it | the desktop workspace |
| Drop one file on it, or Open With | opens that session in the workspace |
| Drop several files on it | the workspace opens and writes a report of each, with its debrief (HTML and PDF) and coaching cards, into the data folder's `reports`, and opens an index page |

macOS hands dropped files to an app as open events; Windows passes them as arguments.
`veltrix_app/file_open.py` gathers either kind for 0.4 s and handles them as a group.

| | Data folder | Program |
|---|---|---|
| macOS | `~/Library/Application Support/Veltrix Data` | wherever the app is copied, usually /Applications |
| Windows | `%APPDATA%\Veltrix Data` | `%LOCALAPPDATA%\Programs\Veltrix Data` (for one user, no administrator needed), or Program Files if the user installs for all users |

Both offer the app for every file type the engine reads (Finder's and Explorer's Open With) without
becoming the default app for `.csv`, `.txt` or `.log`. The command line (`veltrix …`) is not in
either download.

## How it is built

| Step | What | Where |
|---|---|---|
| Bundle | PyInstaller, one-folder mode, the same spec on both systems: the libraries stay ordinary files | `packaging/veltrix_data.spec`, `packaging/launch.py` |
| Trim | asammdf's own viewer, the QML/Quick bindings and the QML plugin tree (which pulled in Qt 3D, Charts, Location…) are left out. Qt WebEngine stays: it prints the PDFs, and it links QtQml and QtQuick | the spec |
| Licences | Veltrix Data's licence, `THIRD-PARTY-NOTICES.txt` and every bundled package's licence files, in a `licences` folder inside the app; beside the app in the disk image; shown before installing on Windows | `tools/collect_licences.py`, `packaging/EULA.txt`, `packaging/licences/` |
| macOS: sign | inner-most first: Qt WebEngine's helper (with Qt's own entitlements), every `.so` and `.dylib`, each framework, then the app; then `codesign --verify --deep --strict` | `tools/build_mac_bundle.sh` |
| macOS: disk image | the app, an Applications link, LICENSE.txt and THIRD-PARTY-NOTICES.txt, UDZO | `tools/build_mac_bundle.sh` |
| Windows: installer | Inno Setup 6: a per-user install, the licence page, a Start-menu entry (and a desktop one if chosen), Open With for the telemetry types, an uninstaller | `tools/build_windows.ps1`, `packaging/windows_installer.iss` |
| Check | runs the built app on two sessions and fails unless every report, debrief, PDF and coaching card is written | `tools/smoke_test_bundle.py` |

macOS build settings:

| Setting | Effect |
|---|---|
| nothing | ad-hoc signature (what releases use today) |
| `VELTRIX_SIGN_IDENTITY="Developer ID Application: Name (TEAMID)"` | signed with hardened runtime and a timestamp; `packaging/entitlements.plist` allows only unsigned executable memory (ctypes and libffi) |
| `VELTRIX_NOTARY_PROFILE=<profile>` | also submitted with `notarytool`, then stapled to the disk image and the app, and checked with `spctl` |
| `VELTRIX_BUILD_DIR=<folder>` | `dist/` and the working files go there (default: the project folder) |
| `PYTHON=<python>` | the Python to build with (default: this folder's `.venv`) |

Building by hand: `uv pip install --python .venv ".[package]"` (PyInstaller), then the script for the
system you are on. Windows also needs Inno Setup 6; neither system can build the other's download.

### Checked

- **macOS, 2026-11-10, on this Mac:** three files dropped on the app (synthetic GT3 CSV, Monza AC
  CSV, the Spa `.ibt`). Every report, debrief, PDF and coaching card was written, and each file's
  samples, laps, best lap and warnings are the same as `veltrix reports` from the virtual
  environment gives. Size: about 657 MB for the app and 278 MB for the disk image; most of it is Qt
  WebEngine (about 230 MB) and PyArrow (110 MB).
- **GitHub Actions, 2026-11-10** (release workflow run by hand on `main`): both downloads built, and
  each built app read both smoke-test sessions and wrote every report, debrief PDF and coaching card
  (best laps 80.77 s and 157.68 s on both). Disk image 264 MB, Windows installer 192 MB. The test
  suite passes on both: 489 tests each.
- **Not yet checked by a person:** downloading either file and opening it on another machine, and on
  Windows the installer itself (installing, Start menu, Open With, uninstalling). CI ran the built
  app from its folder, not from an installation.

### Found while building

- An **empty folder `Versions/Resources`** in the virtual environment's `QtWebEngineCore.framework`
  (not from the PySide6 wheel) made PyInstaller take it for the framework's version. Qt WebEngine
  then could not find its helper process, and the app quit when it came to print the first PDF. The
  build script removes empty folders of that kind before it builds.
- Building with the working files on the USB drive was slow, and once stalled for good: a process
  hung waiting on the drive and could not be stopped. `VELTRIX_BUILD_DIR` on the internal disk avoids
  it.
- The tests workflow had been cancelled on every push since 2026-10-20: GitHub retired the
  `macos-14` runner it named. Both workflows now use `macos-latest` and `windows-latest`.

## Licences

Veltrix Data is proprietary (`LICENSE` for the source, `packaging/EULA.txt` for the app). The
copyright holder is written as "Veltrix Motorsport"; replace it with the business's legal name if that
is different, and have the EULA reviewed before a wide release.

| Part | Licence | How it ships |
|---|---|---|
| Python 3.12 | PSF-2.0 | the framework (macOS) or `python312.dll` (Windows) |
| NumPy, pandas, SciPy, PyArrow, h5py and the smaller packages | BSD / MIT / Apache-2.0 | ordinary files |
| PySide6 / Qt 6.12, with Qt Multimedia and Qt WebEngine | LGPL-3.0 | ordinary files, replaceable |
| FFmpeg inside Qt Multimedia | LGPL-2.1+ | inside Qt |
| asammdf, python-can | LGPL-3.0 | plain `.py` files (`module_collection_mode` "py" in the spec), not compiled into the archive, so replaceable |

`THIRD-PARTY-NOTICES.txt` lists every bundled package with its version and licence, says where the
LGPL libraries are in the app and how to replace them, and points to the Chromium and Qt component
lists for Qt WebEngine. PySide6 and Cython ship no licence file, so `packaging/licences/` holds the
standard GNU and Apache texts (from gnu.org and apache.org). The EULA does not restrict modifying the
LGPL libraries or reverse engineering to debug such changes, as LGPL-3.0 requires.

## App icon

`packaging/icons/` holds the app icon: `veltrix-data.svg` (vector master), `veltrix-data-1024.png`,
`veltrix-data.icns` (macOS bundle) and `veltrix-data.ico` (Windows exe and installer).
`src/veltrix_app/icon.png` is the window and taskbar icon. All are generated by
`packaging/icons/make_icon.py` (it needs Pillow, which the app does not:
`uv run --no-project --with pillow python packaging/icons/make_icon.py`); to change the icon, edit that script, run it, then rebuild the
`.icns` with `iconutil` as its header describes, and copy a 512 px PNG to `src/veltrix_app/icon.png`.

## Not done yet

- **Signing.** macOS: an Apple Developer Program membership ($99 a year) gives a Developer ID
  certificate; add it and an App Store Connect key as repository secrets, and the macOS job sets
  `VELTRIX_SIGN_IDENTITY` and `VELTRIX_NOTARY_PROFILE`. Windows: a code-signing certificate (for
  example Azure Trusted Signing) and a `signtool` step after Inno Setup. Neither path has been run.
- Intel Macs: the macOS build is Apple Silicon only.
