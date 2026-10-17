#!/usr/bin/env python3
"""This machine's identity in the agent network, from a terminal (node/, RM-21; contract DEC-0053, DEC-0056).

    project-observatory full node --init
        create this machine's device keys if it has none — one `#sig` (ES256) and one `#kem` (ECDH P-256),
        owner-only under the workspace's `node/device/` — and print what an enrollment lists: the device id and
        the two public keys. Run again, it changes nothing and prints the same.
    project-observatory full node --show
        print the same without creating anything; exit 1 when this machine has no device keys yet

Only public keys are ever printed. The private keys stay in their files; a replaced key is a lost machine,
because the estate record still names the old one, so neither option ever replaces one. Moving this machine's
identity to another machine is not done by copying the folder: the new machine runs `--init` and is enrolled
on its own (the estate's add-a-machine ceremony, DEC-0084).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths  # noqa: E402

#: Where this machine's device keys live: the workspace's `node/device/`, or `OBSERVATORY_NODE`/device.
NODE = paths.setting_path("OBSERVATORY_NODE", paths.HOME / "node")
DEVICE = NODE / "device"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="project-observatory full node",
                                description="This machine's device keys for the agent network: create or show them.")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--init", action="store_true", help="create the device keys if absent, then show them")
    group.add_argument("--show", action="store_true", help="show the device id and public keys; nothing is created")
    return p


def describe(keys) -> dict:
    return {"schema": "node-identity/1", "device": keys.device_id, "created": keys.created,
            "deviceKeys": keys.enrollment_keys(), "folder": str(DEVICE)}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        from node.device_keys import DeviceKeys
    except RuntimeError as error:                              # the engine's `full` extra
        print(json.dumps({"error": "unavailable", "message": str(error)}), file=sys.stderr)
        return 2
    try:
        if args.init:
            keys = DeviceKeys.load_or_create(DEVICE.resolve())
        else:
            if not (DEVICE / "device.json").exists():
                print(json.dumps({"error": "no-device-keys",
                                  "message": "this machine has no device keys yet: run `project-observatory full "
                                             "node --init`"}), file=sys.stderr)
                return 1
            keys = DeviceKeys.load(DEVICE.resolve())
    except (RuntimeError, ValueError, OSError) as error:
        # The message names the fault (a folder others may write, a half-written set); never a key.
        print(json.dumps({"error": "device-keys-refused", "message": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps(describe(keys), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
