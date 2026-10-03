#!/usr/bin/env python3
"""Revoke an OpenRouter API key by its visible tail, using a provisioning key.

    openrouter.ai/settings/provisioning-keys  ->  create one  ->  save it to a protected file
    python "$(project-observatory full-path)/tools/revoke_key.py" --tail f8d < key-file

WHY THIS EXISTS. A key cannot revoke itself: `DELETE /api/v1/key` answers 404 and
`/api/v1/keys` answers 401 to anything that is not a provisioning key. So a leaked
key cannot be revoked from the machine it leaked on unless a provisioning key is
present.

The provisioning key is read from STDIN, never argv: argv lands in shell history
and in the process list. It is stored in the secret store as
`openrouter-provisioning` with mode 600, and a file readable by group or world is
REFUSED rather than used. No key value is ever printed, only its length, its
last four characters, and the label the provider reports.
"""
   
from __future__ import annotations
import argparse, json, os, pathlib, stat, sys, urllib.error, urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import private_io
import paths                                            

SECRET = paths.source_path("secret_store", paths.SECRETS) / 'openrouter-provisioning'
API = "https://openrouter.ai/api/v1/keys"


def die(msg: str) -> None:
    sys.exit(f"revoke_key: {msg}")


def call(method: str, url: str, key: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, method=method,
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except OSError as e:
        die("provider unreachable")


def load_or_store() -> str:
    """A key on stdin is stored; otherwise the stored one is used, if it is safe."""
    piped = "" if sys.stdin.isatty() else sys.stdin.read().strip()
    if piped:                                                                      
        key = piped                                                                
        if not key.startswith("sk-or-v1-"):
            die("stdin did not carry an OpenRouter key")
        private_io.write(private_io.legacy_path(SECRET), key)
        print(f"stored {SECRET} (600): length {len(key)}, value hidden")
        return key
    if not SECRET.is_file():
        die("no provisioning key is stored. Create one at openrouter.ai/settings/"
            "provisioning-keys, save it to a protected file, and give it on stdin once — "
            "it is stored at mode 600 and used from then on: "
            "python \"$(project-observatory full-path)/tools/revoke_key.py\" --list < key-file")
    target = private_io.legacy_path(SECRET)
    mode = stat.S_IMODE(target.stat().st_mode)
    if mode & 0o077:
        die(f"{SECRET} is mode {mode:o} — readable beyond the owner. "
            f"chmod 600 it, or replace it; a key this exposed is not used.")
    return private_io.read(target).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tail", help="last characters of the key's label, e.g. f8d")
    ap.add_argument("--list", action="store_true", help="list keys and stop")
    a = ap.parse_args()
    key = load_or_store()

    code, body = call("GET", API, key)
    if code == 401:
        die("that key cannot list keys — it is not a provisioning key. "
            "Create one at openrouter.ai/settings/provisioning-keys.")
    if code != 200:
        die(f"listing failed: HTTP {code}")
    keys = body.get("data", [])
    if a.list or not a.tail:
        for k in keys:
            print(f"  {k.get('label','?'):26s} limit={k.get('limit')} "
                  f"usage={k.get('usage', 0):.2f} disabled={k.get('disabled')}")
        if not a.tail:
            print("\npass --tail <tail> to revoke one")
        return 0

    hits = [k for k in keys if str(k.get("label", "")).endswith(a.tail)]
    if not hits:
        die(f"no key whose label ends in {a.tail!r} — "
            f"it may already be revoked. Run --list to see what is left.")
    if len(hits) > 1:
        die(f"{len(hits)} keys end in {a.tail!r}; use a longer tail")

    target = hits[0]
    code, body = call("DELETE", f"{API}/{target['hash']}", key)
    if code not in (200, 204):
        die(f"delete failed: HTTP {code}")
    print(f"revoked {target.get('label')} (limit {target.get('limit')}, "
          f"spent {target.get('usage', 0):.2f})")

    still = [k for k in call("GET", API, key)[1].get("data", [])
             if str(k.get("label", "")).endswith(a.tail)]
    if still:
        die("the provider still lists it — revocation did NOT take effect")
    print("verified: the provider no longer lists it")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError):
        die("private credential operation refused; value hidden")
