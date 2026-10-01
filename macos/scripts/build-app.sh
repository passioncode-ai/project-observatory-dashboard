#!/bin/bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SCRATCH=${OBSERVATORY_SWIFT_BUILD:-"$ROOT/macos/.build"}
OUT=${1:-"$ROOT/dist/macos"}
CONFIG=${OBSERVATORY_SWIFT_CONFIGURATION:-release}
swift build --package-path "$ROOT/macos" --scratch-path "$SCRATCH" -c "$CONFIG"
BIN=$(swift build --package-path "$ROOT/macos" --scratch-path "$SCRATCH" -c "$CONFIG" --show-bin-path)
APP="$OUT/Project Observatory.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN/ProjectObservatory" "$APP/Contents/MacOS/ProjectObservatory"
python3 - "$ROOT" "$APP" <<'PY'
import json,plistlib,sys
from pathlib import Path
root,app=map(Path,sys.argv[1:])
import tomllib
version=tomllib.loads((root/'pyproject.toml').read_text())['project']['version']
doc={'CFBundleName':'Project Observatory','CFBundleDisplayName':'Project Observatory',
     'CFBundleIdentifier':'ai.passioncode.observatory','CFBundleExecutable':'ProjectObservatory',
     'CFBundlePackageType':'APPL','CFBundleShortVersionString':version,'CFBundleVersion':'1',
     'LSMinimumSystemVersion':'14.0','NSHighResolutionCapable':True,
     'NSHumanReadableCopyright':'AGPL-3.0-only OR LicenseRef-PassionCode-Commercial; see LICENSE.'}
(app/'Contents/Info.plist').write_bytes(plistlib.dumps(doc))
PY
# Explicit Developer ID only when supplied by the release operator; default is QA.
if [[ -n "${OBSERVATORY_SIGN_IDENTITY:-}" ]]; then
  codesign --force --options runtime --timestamp --sign "$OBSERVATORY_SIGN_IDENTITY" "$APP"
else
  codesign --force --sign - "$APP"
fi
codesign --verify --strict "$APP"
plutil -lint "$APP/Contents/Info.plist"
printf '%s\n' "$APP"
