#!/bin/bash
# Build "Project Observatory.app": the Swift package, its icon from the product mark,
# Info.plist, and a signature (ad hoc for local QA, Developer ID when supplied).
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SCRATCH=${OBSERVATORY_SWIFT_BUILD:-"$ROOT/macos/.build"}
OUT=${1:-"$ROOT/dist/macos"}
CONFIG=${OBSERVATORY_SWIFT_CONFIGURATION:-release}
# One universal app, every Mach-O carrying both slices (fabric-workspace platforms.md PL-01):
# a thin x86_64 file makes macOS warn "Support Ending for Intel-Based Apps", and an arm64-only
# one does not open on an Intel Mac. OBSERVATORY_SWIFT_ARCHS narrows a local QA build.
ARCHS=${OBSERVATORY_SWIFT_ARCHS:-"arm64 x86_64"}
ARCH_ARGS=()
for arch in $ARCHS; do ARCH_ARGS+=(--arch "$arch"); done
swift build --package-path "$ROOT/macos" --scratch-path "$SCRATCH" -c "$CONFIG" "${ARCH_ARGS[@]}"
BIN=$(swift build --package-path "$ROOT/macos" --scratch-path "$SCRATCH" -c "$CONFIG" "${ARCH_ARGS[@]}" --show-bin-path)
APP="$OUT/Project Observatory.app"
# The bundle this replaces is forgotten by LaunchServices first, so no stale copy
# answers an `open` (lifecycle LC-15).
LSREG=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
if [[ -d "$APP" && -x "$LSREG" ]]; then "$LSREG" -u "$APP" 2>/dev/null || true; fi
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN/ProjectObservatory" "$APP/Contents/MacOS/ProjectObservatory"
BUILT=$(lipo -archs "$APP/Contents/MacOS/ProjectObservatory")
for arch in $ARCHS; do
  case " $BUILT " in
    *" $arch "*) ;;
    *) echo "build-app.sh: the executable has slices '$BUILT', missing $arch (PL-01)" >&2; exit 1 ;;
  esac
done

# The icon is the product mark, rasterized at every size the iconset needs.
ICONSET="$SCRATCH/AppIcon.iconset"
swift "$ROOT/macos/scripts/make-icon.swift" "$ROOT/observatory/engine/dashboard/brand/observatory-mark.svg" "$ICONSET" >/dev/null
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

# The interface's words (L10N): the Russian dictionary and both languages' plural forms,
# in the bundle's own Resources — where the app looks first, and where macOS looks to
# draw its own menus (Quit, Hide, Window) in Russian. A file that does not parse stops
# the build rather than shipping an app that falls back to English.
for lproj in "$ROOT/macos/Sources/ObservatoryCore/Resources/"*.lproj; do
  for f in "$lproj"/*; do plutil -lint -s "$f"; done
  cp -R "$lproj" "$APP/Contents/Resources/"
done
test -f "$APP/Contents/Resources/ru.lproj/Localizable.strings"

# CFBundleVersion counts commits, so every build from a newer source is a newer
# bundle to Launch Services; the short version is the engine release it ships beside.
BUILD=$(git -C "$ROOT" rev-list --count HEAD 2>/dev/null || echo 1)
# Any python3 will do, the macOS 3.9 one included: no tomllib (3.11+), so the
# version is read from `[project]` with a pattern rather than a TOML parser.
python3 - "$ROOT" "$APP" "$BUILD" <<'PY'
import plistlib,re,sys
from pathlib import Path
root,app,build=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
project=re.search(r'^\[project\]\s*$(.*?)(?=^\[|\Z)',(root/'pyproject.toml').read_text(),re.M|re.S)
found=project and re.search(r'^version\s*=\s*"([^"]+)"',project.group(1),re.M)
if not found: sys.exit('build-app.sh: no [project] version in pyproject.toml')
version=found.group(1)
doc={'CFBundleName':'Project Observatory','CFBundleDisplayName':'Project Observatory',
     'CFBundleIdentifier':'ai.passioncode.observatory','CFBundleExecutable':'ProjectObservatory',
     'CFBundlePackageType':'APPL','CFBundleShortVersionString':version,'CFBundleVersion':build,
     'CFBundleIconFile':'AppIcon','CFBundleDevelopmentRegion':'en','CFBundleLocalizations':['en','ru'],
     'LSApplicationCategoryType':'public.app-category.developer-tools',
     'LSMinimumSystemVersion':'14.0','NSHighResolutionCapable':True,'NSSupportsAutomaticTermination':False,
     # The dashboard is served on 127.0.0.1 over plain http, loopback only.
     'NSAppTransportSecurity':{'NSAllowsLocalNetworking':True},
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
# Builds clean up after themselves (LC-15): other bundles beside this one, release
# files in dist/ older than the previous release, and a report of caches past their cap.
python3 "$ROOT/tools/prune_builds.py" --dist "$ROOT/dist" --app "$APP" >&2
printf '%s\n' "$APP"
