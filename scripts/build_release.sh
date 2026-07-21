#!/usr/bin/env bash
# Builds a self-contained BambuScheduler.app (bundled Python backend + Swift
# menu bar UI), ad-hoc signs it, and zips it for GitHub Releases.
#
# Usage: scripts/build_release.sh
# Output: dist/BambuScheduler.zip

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

APP_NAME="BambuScheduler"
BUILD_VENV=".build-venv"
RELEASE_DIR="release"
APP_BUNDLE="$RELEASE_DIR/$APP_NAME.app"

echo "==> Setting up build environment"
python3 -m venv "$BUILD_VENV"
source "$BUILD_VENV/bin/activate"
pip install -q --upgrade pip
pip install -q -r requirements.txt pyinstaller pillow

echo "==> Bundling Python backend with PyInstaller"
rm -rf build dist *.spec
# --onedir (not --onefile): the executable and its deps ship unpacked inside
# the .app, so it starts immediately instead of self-extracting to a temp dir
# on every launch.
pyinstaller --onedir --name bambuscheduler-backend \
    --add-data "templates:templates" \
    --collect-submodules paho \
    web.py

echo "==> Building Swift menu bar app"
(cd BambuMenu && swift build -c release)

echo "==> Generating app icon"
python3 generate_icon.py

echo "==> Assembling app bundle"
rm -rf "$RELEASE_DIR"
mkdir -p "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources"

cp "BambuMenu/.build/release/$APP_NAME" "$APP_BUNDLE/Contents/MacOS/"
cp -R dist/bambuscheduler-backend "$APP_BUNDLE/Contents/Resources/"
chmod +x "$APP_BUNDLE/Contents/Resources/bambuscheduler-backend/bambuscheduler-backend"
cp packaging/Info.plist "$APP_BUNDLE/Contents/Info.plist"
cp /tmp/AppIcon.icns "$APP_BUNDLE/Contents/Resources/AppIcon.icns"

echo "==> Ad-hoc code signing"
# Sign the nested Mach-O in the bundled backend first, then the app as a whole.
find "$APP_BUNDLE/Contents/Resources/bambuscheduler-backend" \
    -type f \( -name "*.dylib" -o -name "*.so" \) -exec codesign --force --sign - {} +
codesign --force --sign - "$APP_BUNDLE/Contents/Resources/bambuscheduler-backend/bambuscheduler-backend"
codesign --force --deep --sign - "$APP_BUNDLE"

echo "==> Zipping for release"
(cd "$RELEASE_DIR" && ditto -c -k --keepParent "$APP_NAME.app" "$APP_NAME.zip")

deactivate
rm -rf "$BUILD_VENV" build dist *.spec

echo "==> Done: $APP_BUNDLE"
echo "==> Zip:  $RELEASE_DIR/$APP_NAME.zip"
