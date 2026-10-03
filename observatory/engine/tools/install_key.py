#!/usr/bin/env python3
"""Put an OpenRouter key where its consumer will find it, without anyone seeing it.

    tools/install_key.py --for observatory < protected-key-file
    tools/install_key.py < protected-key-file      # provisioning keys need no --for

WHY THIS EXISTS. Each consumer reads its key from a different place:

    observatory   the store's key file             read by the scheduled tick; a
                                                   shell variable would shadow it,
                                                   and a scheduler passes none
    companion     the memory companion's .env      the only place its worker looks
    provisioning  the secret store                 mints, limits and revokes keys;
                                                   `tools/revoke_key.py` needs it

Telling an operator which file to edit is a chance to pick the wrong one, and a
key pasted into a chat, an argument list or a shell history is a key that has
to be rotated. So: the key arrives on STDIN and nowhere else, this program
decides which kind it is by ASKING the provider rather than by looking at the
prefix — an inference key and a provisioning key are spelled the same — and
writes it mode 600 to the one place that consumer reads.

Nothing here ever prints a key. What it prints is the kind, the length, and
what the provider says the key may spend.

A provisioning key removes a class of interruption: with one on the machine,
keys can be minted, capped and revoked from here; without one, every new key is
a trip to the provider's console.
"""
   
from __future__ import annotations
import argparse
import json
import os
import pathlib
import stat
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import private_io
import paths                                                                    
from agent.providers import KEY_SHAPES

#: The prefix an OpenRouter key must carry — the engine's own constant, so this
#: door cannot accept a key `project-observatory full key` then refuses.
KEY_PREFIX = KEY_SHAPES["OPENROUTER_API_KEY"][0]

#: consumer -> (file, what reads it). Enumerated, because a fourth consumer must
#: be a deliberate line here rather than a guess at install time.
DESTINATIONS: dict[str, tuple[pathlib.Path, str]] = {
    "observatory": (paths.STATE / ".openrouter-key",
                    "the tick's agent and indexer (`project-observatory full key` shows "
                    "what the SCHEDULED run resolves)"),
    "claude-mem": (paths.source_path("companion_home", paths.HOME / "disabled/companion") / ".env",
                   "the memory observer's worker, which reads this file and "
                   "nothing else"),
}
PROVISIONING = paths.source_path("secret_store", paths.SECRETS) / 'openrouter-provisioning'
API = "https://openrouter.ai"


def die(msg: str) -> None:
    sys.exit(f"install_key: {msg}")


def ask(path: str, key: str) -> tuple[int, dict]:
    req = urllib.request.Request(API + path, method="GET",
                                 headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}
    except OSError as e:
        die("provider unreachable")
    return 0, {}


def kind_of(key: str) -> tuple[str, dict]:
    """"provisioning" or "inference", decided by what the provider allows.

    NOT by the prefix: both kinds share the same key prefix, so a rule on
    the string would be a guess dressed as a check. `/api/v1/keys` is the
    provisioning endpoint and answers 401 to an inference key; `/api/v1/key`
    describes the key that calls it, including what it may spend.
    """
       
    status, _ = ask("/api/v1/keys", key)
    if status == 200:
        return "provisioning", {}
    status, body = ask("/api/v1/key", key)
    if status == 200:
        return "inference", (body.get("data") or {})
    die(f"the provider does not recognise this key (HTTP {status}) — check that "
        f"it was copied whole")
    return "", {}


def write(dest: pathlib.Path, key: str, consumer: str) -> None:
    if dest == PROVISIONING:
        dest = private_io.legacy_path(dest)
    private_io.check(dest)
    with private_io.lock(dest.with_name("." + dest.name + ".install.lock")):
        if dest.name == ".env":
            old = private_io.read(dest) if dest.exists() else ""
            lines, replaced = [], False
            for line in old.splitlines():
                if line.startswith("OPENROUTER_API_KEY="):
                    lines.append(f"OPENROUTER_API_KEY={key}")
                    replaced = True
                else:
                    lines.append(line)
            if not replaced:
                lines.append(f"OPENROUTER_API_KEY={key}")
            payload = "\n".join(lines) + "\n"
        else:
            payload = key
        private_io.write(dest, payload)
        if private_io.read(dest) != payload:
            raise RuntimeError("Credential delivery verification failed")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or 'Install a locally supplied provider key.').splitlines()[0])
    ap.add_argument("--for", dest="consumer", choices=sorted(DESTINATIONS),
                    help="which consumer reads this key (inference keys only)")
    a = ap.parse_args(argv[1:])
    if a.consumer == "observatory":
        # PB-091: the key lives in the selected state. A copy still in the legacy
        # store would make the next read refuse (two keys, no silent choice), so
        # the installer refuses first and names both files — before it reads the
        # key or asks the provider anything.
        legacy = [p for p in paths.key_locations(".openrouter-key")[1:] if p.exists()]
        if legacy:
            die(f"a key already sits at the legacy location {legacy[0]}; the selected state "
                f"is {paths.STATE}. Remove the legacy file (after checking it is not the only "
                f"copy you have), then install again.")

    if sys.stdin.isatty():
        die("the key must arrive on stdin, never in an argument — an argument "
            "lands in the shell history and in the process list.\n"
            f"    run install_key with --for {a.consumer or 'CONSUMER'} and redirect a protected file to stdin")
    key = sys.stdin.read().strip()
    if not key:
        die("stdin was empty")
    if not key.startswith(KEY_PREFIX):
        die(f"that does not look like an OpenRouter key (it should start `{KEY_PREFIX}`)")

    kind, info = kind_of(key)
    if kind == "provisioning":
        if a.consumer:
            print(f"note: --for {a.consumer} ignored — a provisioning key has one "
                  f"home and mints the others")
        write(PROVISIONING, key, "provisioning")
        print(f"provisioning key installed: {PROVISIONING} (600), "
              f"length {len(key)}; value hidden")
        print("  keys can now be minted, capped and revoked from this machine — "
              "`python \"$(project-observatory full-path)/tools/revoke_key.py\" --list` reads it")
        return 0

    if not a.consumer:
        die("this is an INFERENCE key, so it needs a consumer:\n"
            + "\n".join(f"    --for {c:<12} {why}" for c, (_, why) in
                        sorted(DESTINATIONS.items())))
    dest, why = DESTINATIONS[a.consumer]
    write(dest, key, a.consumer)
    print(f"inference key installed for {a.consumer}: {dest} (600), "
          f"length {len(key)}; value hidden")
    print(f"  read by {why}")
    limit, rem = info.get("limit"), info.get("limit_remaining")
    reset = info.get("limit_reset")
    if limit is None:
        print("  the provider reports NO limit on this key — it can spend the "
              "whole account balance")
    else:
        reset_label = reset or ('NEVER — a lifetime cap, which drifts from a monthly '
                                'budget until the key dies for no visible reason')
        print(f"  limit {limit}, remaining {rem}, reset {reset_label}")
    if a.consumer == "claude-mem":
        print("  restart the worker so it picks this up:")
        print("    use the restart command for your configured companion installation")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except (OSError, ValueError, RuntimeError):
        die("private credential operation refused; value hidden")
