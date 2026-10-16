# Packaging for macOS (route, not yet done)

Nothing here has been built. This records the route and the choices made so
far that keep it open.

## What has to ship

| Part | Size, roughly | Licence | Note |
|---|---|---|---|
| Python 3.12 runtime | 60 MB | PSF | |
| NumPy, pandas, SciPy, PyArrow | 350 MB | BSD / Apache-2.0 | arm64 wheels |
| PySide6 (Qt 6.12) with Qt Multimedia | 400 MB unpacked with WebEngine; less if WebEngine is left out | LGPL-3.0 | arm64 wheels |
| pyqtgraph | 5 MB | MIT | pure Python |
| FFmpeg, as built into Qt Multimedia | inside PySide6 | LGPL-2.1+ build | no separate FFmpeg install is needed; video plays and the tests encode with it |
| asammdf, python-can | small | LGPL-3.0 | optional readers |
| libxrk, h5py | small | MIT, BSD | optional readers |

All dependencies were checked on this machine as native arm64 (see
[mdf-compatibility.md](mdf-compatibility.md) for asammdf; the full test suite
runs on Apple Silicon).

## Route

1. **Bundle.** `briefcase` or `py2app` to produce `Veltrix Data.app` with the
   interpreter and site-packages inside `Contents/Resources`, entry point
   `veltrix_app.window:main`. PyInstaller also works but its one-file mode
   makes the LGPL libraries hard to replace; use the one-folder mode if it is
   chosen.
2. **LGPL.** PySide6, asammdf and python-can must stay replaceable by the
   user. Shipped as ordinary files inside the bundle, not statically linked
   and not obfuscated, they are. Include their licence texts and a note on
   where the libraries live in the bundle.
3. **Trim.** Leave out `QtWebEngine` unless the HTML report is to be shown
   inside the app (it currently opens in the browser), and Qt modules the app
   does not import.
4. **Sign.** `codesign --deep --options runtime` with a Developer ID
   Application certificate; every `.so` and `.dylib` inside the bundle must be
   signed, inner-most first. Hardened runtime needs the entitlement
   `com.apple.security.cs.disable-library-validation` only if unsigned
   third-party libraries remain.
5. **Notarise.** `xcrun notarytool submit … --wait`, then `xcrun stapler staple`.
6. **File types.** `CFBundleDocumentTypes` for `.csv`, `.ld`, `.ibt`, `.vbo`,
   `.xrk`, `.mf4`, `.mat`, so files can be dropped on the icon.

## Choices already made with packaging in mind

- The engine does not import Qt; the app is a separate package.
- No runtime downloads and no network use: everything is local.
- App data (maths channels, profiles, annotations, video links) goes to
  `QStandardPaths.AppDataLocation`, not into the bundle or the repository.
- No dependency on a system FFmpeg, ffprobe or a browser.

## Not decided or not checked

- Which bundler. None has been tried.
- A Developer ID certificate is needed; none is on this machine.
- The existing `Veltrix Report.app` is an AppleScript launcher tied to this
  folder and its virtual environment; it is not a distributable app.
- Whether the bundle starts quickly from a signed, quarantined state.
