#!/bin/bash
# Notarize a Developer ID-signed "Project Observatory.app", staple the ticket, assess it
# as Gatekeeper will on another Mac, and package the stapled app as the release download
# `<out>/ProjectObservatory-<version>-macos.zip`.
#
#   OBSERVATORY_SIGN_IDENTITY='Developer ID Application: …' macos/scripts/build-app.sh
#   OBSERVATORY_NOTARY_PROFILE=<keychain profile> macos/scripts/notarize.sh 'dist/macos/Project Observatory.app'
#
# Credentials, one of:
#   OBSERVATORY_NOTARY_PROFILE            a `xcrun notarytool store-credentials` profile (reads the Keychain)
#   OBSERVATORY_NOTARY_KEY                path to an App Store Connect API key (.p8), with
#   OBSERVATORY_NOTARY_KEY_ID and OBSERVATORY_NOTARY_ISSUER   (no Keychain involved)
# The key's path, id and issuer go to notarytool's arguments and nowhere else: nothing
# here prints them. Exit 2 is a refusal before Apple was asked; exit 1 is Apple's
# rejection (its log is printed) or a failed staple or assessment.
set -euo pipefail

APP=${1:-}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT=${2:-"$ROOT/dist"}

refuse() { printf 'notarize.sh: %s\n' "$1" >&2; exit 2; }

[[ -n "$APP" && -d "$APP/Contents" ]] || refuse "no app bundle at '${APP}'; build it first (macos/scripts/build-app.sh)"

CREDS=()
if [[ -n "${OBSERVATORY_NOTARY_PROFILE:-}" ]]; then
  CREDS=(--keychain-profile "$OBSERVATORY_NOTARY_PROFILE")
elif [[ -n "${OBSERVATORY_NOTARY_KEY:-}" || -n "${OBSERVATORY_NOTARY_KEY_ID:-}" || -n "${OBSERVATORY_NOTARY_ISSUER:-}" ]]; then
  [[ -n "${OBSERVATORY_NOTARY_KEY:-}" && -n "${OBSERVATORY_NOTARY_KEY_ID:-}" && -n "${OBSERVATORY_NOTARY_ISSUER:-}" ]] \
    || refuse "an App Store Connect API key needs all three of OBSERVATORY_NOTARY_KEY, OBSERVATORY_NOTARY_KEY_ID and OBSERVATORY_NOTARY_ISSUER"
  [[ -f "$OBSERVATORY_NOTARY_KEY" ]] || refuse "OBSERVATORY_NOTARY_KEY does not name a readable .p8 file"
  CREDS=(--key "$OBSERVATORY_NOTARY_KEY" --key-id "$OBSERVATORY_NOTARY_KEY_ID" --issuer "$OBSERVATORY_NOTARY_ISSUER")
else
  refuse "no notarization credentials: set OBSERVATORY_NOTARY_PROFILE, or OBSERVATORY_NOTARY_KEY with OBSERVATORY_NOTARY_KEY_ID and OBSERVATORY_NOTARY_ISSUER"
fi

# Apple notarizes only a Developer ID signature with the hardened runtime and a secure
# timestamp; build-app.sh gives all three when OBSERVATORY_SIGN_IDENTITY is set. Checked
# here so an ad hoc QA build is refused locally instead of after an upload.
SIGNATURE=$(codesign -dv --verbose=2 "$APP" 2>&1 || true)
grep -q '^Authority=Developer ID Application:' <<<"$SIGNATURE" \
  || refuse "the app is not signed with a Developer ID Application certificate; rebuild with OBSERVATORY_SIGN_IDENTITY set"
grep -q '^CodeDirectory .*flags=0x[0-9a-f]*([^)]*runtime' <<<"$SIGNATURE" \
  || refuse "the signature lacks the hardened runtime; rebuild with build-app.sh and OBSERVATORY_SIGN_IDENTITY set"

VERSION=$(plutil -extract CFBundleShortVersionString raw "$APP/Contents/Info.plist")
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
ditto -c -k --keepParent "$APP" "$WORK/upload.zip"

xcrun notarytool submit "$WORK/upload.zip" "${CREDS[@]}" --wait --timeout 30m --output-format json > "$WORK/submit.json"
read -r STATUS SUBMISSION < <(python3 - "$WORK/submit.json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
print(doc.get("status") or "unknown", doc.get("id") or "-")
PY
)
if [[ "$STATUS" != "Accepted" ]]; then
  printf 'notarize.sh: Apple answered %s for submission %s; its log follows\n' "$STATUS" "$SUBMISSION" >&2
  xcrun notarytool log "$SUBMISSION" "${CREDS[@]}" >&2 || true
  exit 1
fi
printf 'notarized: submission %s accepted\n' "$SUBMISSION"

# The ticket is stapled into the bundle so Gatekeeper can check it offline; the download
# is packaged only after that, from the stapled app.
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"
spctl --assess --type execute --verbose=2 "$APP"

mkdir -p "$OUT"
ARCHIVE="$OUT/ProjectObservatory-$VERSION-macos.zip"
rm -f "$ARCHIVE"
ditto -c -k --keepParent "$APP" "$ARCHIVE"
printf '%s\n' "$ARCHIVE"
