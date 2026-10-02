#!/bin/bash
# Install a built "Project Observatory.app" so the Dock, Launchpad and Spotlight open it.
#
#   macos/scripts/install-app.sh [path/to/Project Observatory.app] [--open]
#
# Copies the bundle to /Applications (~/Applications when /Applications is not
# writable), quits a running copy first, and forgets every OTHER registration of
# the bundle id whose path is gone or lies outside the Applications folders —
# Launch Services kept QA builds from temporary folders registered, and Spotlight
# could open one of those instead of the installed app.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SRC="${1:-$ROOT/dist/macos/Project Observatory.app}"
OPEN=0; [[ "${2:-}" == "--open" || "${1:-}" == "--open" ]] && OPEN=1
[[ "$SRC" == "--open" ]] && SRC="$ROOT/dist/macos/Project Observatory.app"
ID=ai.passioncode.observatory
LSREG=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister

[[ -d "$SRC" ]] || { echo "not built: $SRC (run macos/scripts/build-app.sh)" >&2; exit 2; }
codesign --verify --strict "$SRC"
[[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$SRC/Contents/Info.plist")" == "$ID" ]] \
  || { echo "not the Observatory bundle: $SRC" >&2; exit 2; }

DEST_DIR=/Applications; [[ -w "$DEST_DIR" ]] || DEST_DIR="$HOME/Applications"
mkdir -p "$DEST_DIR"; DEST="$DEST_DIR/Project Observatory.app"

# Quit a running copy politely; the window state it saves is the operator's.
if pgrep -x ProjectObservatory >/dev/null; then
  osascript -e "tell application id \"$ID\" to quit" >/dev/null 2>&1 || true
  for _ in $(seq 1 50); do pgrep -x ProjectObservatory >/dev/null || break; sleep 0.1; done
fi

STAGE="$DEST_DIR/.Project Observatory.app.installing"
rm -rf "$STAGE"; ditto "$SRC" "$STAGE"
rm -rf "$DEST"; mv "$STAGE" "$DEST"
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true
codesign --verify --strict "$DEST"

# Forget stale registrations of this bundle id; register the installed one.
"$LSREG" -dump 2>/dev/null | awk -v id="$ID" '
  /^path:/ { sub(/^path:[ \t]+/, ""); sub(/ \(0x[0-9a-f]+\)$/, ""); path = $0 }
  /^identifier:/ && $2 == id && path != "" { print path; path = "" }' | sort -u |
while IFS= read -r p; do
  case "$p" in
    "$DEST") ;;
    /Applications/*|"$HOME/Applications/"*) [[ -d "$p" ]] || "$LSREG" -u "$p" 2>/dev/null || true ;;
    *) "$LSREG" -u "$p" 2>/dev/null || true; echo "forgot: $p" ;;
  esac
done
"$LSREG" -f "$DEST"
echo "installed: $DEST ($(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$DEST/Contents/Info.plist"))"
(( OPEN )) && open "$DEST"
exit 0
