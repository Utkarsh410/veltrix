#!/bin/sh
# Build the self-contained "Veltrix Data.app": its own Python and libraries inside, nothing tied to
# this folder, so it can be copied to /Applications or to another Mac (Apple Silicon, macOS 13+).
#
#   sh tools/build_mac_bundle.sh
#
# Writes dist/Veltrix Data.app and dist/Veltrix-Data-<version>.dmg. VELTRIX_BUILD_DIR puts dist/ and the
# working files elsewhere — an internal disk builds faster than a USB drive, and has not stalled.
#
# Signing, by what is set:
#   nothing                       ad-hoc signature: runs on this Mac; another Mac's Gatekeeper refuses it
#   VELTRIX_SIGN_IDENTITY         "Developer ID Application: Name (TEAMID)": signed, hardened runtime
#   VELTRIX_NOTARY_PROFILE        also notarised and stapled; the profile is made once with
#                                 xcrun notarytool store-credentials <profile> --apple-id … --team-id …
#
# tools/build_mac_app.sh still builds the small launcher that runs this folder's code, for development.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
BUILD="${VELTRIX_BUILD_DIR:-$ROOT}"
DIST="$BUILD/dist"
APP="$DIST/Veltrix Data.app"
VERSION="$("$PY" -c 'import importlib.metadata as m; print(m.version("veltrix-engine"))')"
DMG="$DIST/Veltrix-Data-$VERSION.dmg"
IDENTITY="${VELTRIX_SIGN_IDENTITY:--}"
ENTITLEMENTS="$ROOT/packaging/entitlements.plist"

"$PY" -c 'import PyInstaller' 2>/dev/null || { echo "PyInstaller is missing: uv pip install --python .venv 'veltrix-engine[package]'"; exit 1; }

# An empty folder beside Versions/A in a Qt framework makes PyInstaller take it for the framework's
# version, and Qt WebEngine (which prints the PDFs) then cannot find its helper process.
find "$ROOT/.venv/lib/python3.12/site-packages/PySide6/Qt/lib" -maxdepth 3 -path "*.framework/Versions/*" \
	-type d -empty -print -delete | sed 's/^/Removed an empty framework folder: /'

"$PY" -m PyInstaller --noconfirm --clean --log-level WARN \
	--distpath "$DIST" --workpath "$BUILD/build/pyinstaller" "$ROOT/packaging/veltrix_data.spec"

# Sign inner-most first — Qt WebEngine's helper (with the entitlements Qt ships for it), every
# library, each framework, then the app — since each signature seals what is inside it.
echo "Signing with: $([ "$IDENTITY" = - ] && echo 'ad hoc (this Mac only)' || echo "$IDENTITY")"
if [ "$IDENTITY" = - ]; then RUNTIME=""; else RUNTIME="--options runtime --timestamp"; fi
sign() { codesign --force --sign "$IDENTITY" $RUNTIME "$@" 2>&1 | grep -v "replacing existing signature" || true; }
HELPER="$(find "$APP" -type d -name QtWebEngineProcess.app | head -1)"
sign --entitlements "$HELPER/Contents/Resources/QtWebEngineProcess.entitlements" "$HELPER"
find "$APP/Contents" -type f \( -name "*.so" -o -name "*.dylib" \) | while read -r f; do sign "$f"; done
find "$APP/Contents/Frameworks" -type d -name "*.framework" -prune | while read -r f; do sign "$f"; done
sign --entitlements "$ENTITLEMENTS" "$APP"
codesign --verify --deep --strict "$APP"
echo "Signature verified."

rm -f "$DMG"
STAGE="$(mktemp -d)"
ditto "$APP" "$STAGE/Veltrix Data.app"
ln -s /Applications "$STAGE/Applications"
hdiutil create -quiet -volname "Veltrix Data $VERSION" -srcfolder "$STAGE" -format UDZO "$DMG"
rm -rf "$STAGE"
[ "$IDENTITY" = - ] || codesign --force --sign "$IDENTITY" --timestamp "$DMG"

if [ -n "$VELTRIX_NOTARY_PROFILE" ]; then
	[ "$IDENTITY" = - ] && { echo "Notarising needs VELTRIX_SIGN_IDENTITY (a Developer ID Application certificate)."; exit 1; }
	xcrun notarytool submit "$DMG" --keychain-profile "$VELTRIX_NOTARY_PROFILE" --wait
	xcrun stapler staple "$DMG"
	xcrun stapler staple "$APP"
	spctl --assess --type execute --verbose "$APP"
fi

echo "Built: $APP ($(du -sh "$APP" | cut -f1))"
echo "       $DMG ($(du -sh "$DMG" | cut -f1))"
