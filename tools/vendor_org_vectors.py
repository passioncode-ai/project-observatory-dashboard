#!/usr/bin/env python3
"""Vendors the node's contract files (`observatory/engine/fabric/node-vectors/`) at one commit, in one step.

    python3 tools/vendor_org_vectors.py --contract <fabric-agent-contract checkout> --commit <sha>

Copies, byte for byte from that commit (read with `git show`, so the checkout's working tree and branch do not
matter), the node's vectors of `fixtures/vectors/` (the estate record, the relay, the ceremonies and the organization
enrollment's `http-signature.json` and `share-registration.json`) and `schemas/device-enrollment.schema.json` into
`observatory/engine/fabric/node-vectors/`, the device request and check-in fixtures CT-65 added and the key-on-record
renewal fixtures of DEC-0100 into its `device/` folder, and every `estate-*` fixture of `fixtures/semantic/` into its
`semantic/` folder but the one the public-release gate refuses (`SEMANTIC_LEFT_OUT`), removing a vendored `estate-*`
fixture the commit no longer has (OBS-171). Then it rewrites every commit the folder's README names and the digest
rows of the files outside `semantic/`, and the commit of the engine's contract pin
(`observatory/engine/fabric-contract.lock.json`, OBS-196): the contract asks a consumer to pin it once and to move the
pin with what it vendors, in one change.
`tests/test_node_foundation.py` checks the digests, the node suites run the files and `tests/test_contract_lock.py`
holds the pin equal to the README's commit, so a re-vendored commit is checked by the next test run. Prints what
changed; exits 1 when the commit or a file cannot be read, and changes nothing then.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "observatory" / "engine" / "fabric" / "node-vectors"
#: The engine's one pin of the contract, under the contract's own file name.
CONTRACT_PIN = ROOT / "observatory" / "engine" / "fabric-contract.lock.json"
FILES = {**{name: "fixtures/vectors/" + name for name in (
             # The estate record, its revocation delta, the secret envelope, the relay and the ceremonies (OBS-109,
             # OBS-113, OBS-133); the estate fixtures of `fixtures/semantic/` go to `semantic/` (SEMANTIC below).
             "canonical.json", "enroll-token.json", "estate-record.json", "identifiers.json", "p256.json",
             "pairing.json", "relay.json", "revocation-delta.json", "secret-envelope.json")},
         "http-signature.json": "fixtures/vectors/http-signature.json",
         "share-registration.json": "fixtures/vectors/share-registration.json",
         "device-enrollment.schema.json": "schemas/device-enrollment.schema.json",
         # The device fixtures the node's requests and answers are held to (CT-65, DEC-0094).
         "device/device-enrollment-request-file-key.json": "fixtures/positive/device-enrollment-request-file-key.json",
         "device/device-enrollment-file-key-not-exportable.json":
             "fixtures/negative/device-enrollment-file-key-not-exportable.json",
         "device/device-enrollment-file-key-attested.json": "fixtures/negative/device-enrollment-file-key-attested.json",
         "device/device-enrollment-hardware-key-exportable.json":
             "fixtures/negative/device-enrollment-hardware-key-exportable.json",
         "device/device-check-in-response-protocols.json": "fixtures/positive/device-check-in-response-protocols.json",
         # A renewal by the key on record (DEC-0100): the request, and the same request carrying a token id.
         "device/device-enrollment-request-key-on-record.json":
             "fixtures/positive/device-enrollment-request-key-on-record.json",
         "device/device-enrollment-key-on-record-with-token.json":
             "fixtures/negative/device-enrollment-key-on-record-with-token.json"}
#: The contract's estate fixtures, vendored into `semantic/` without a digest row (`test_node_pairing.py` runs each).
SEMANTIC = re.compile(r"^estate-[A-Za-z0-9-]+\.json$")
#: Its slot is the AWS documentation's example access key id, a shape the public-release gate refuses;
#: `test_node_pairing.py` rebuilds it from `estate-record-ok.json`.
SEMANTIC_LEFT_OUT = frozenset({"estate-secret-value-as-slot.json"})
#: Every commit the README names ("... at commit `<40 hex>`"): the whole folder is at one commit.
PIN = re.compile(r"(at commit\s+`)([0-9a-f]{40})(`)")


def _safe_git():
    spec = importlib.util.spec_from_file_location("observatory_vendor_safe_git",
                                                  ROOT / "observatory" / "engine" / "safe_git.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _semantic_blobs(git, contract: Path, full: str) -> dict:
    """The commit's `estate-*` fixtures of `fixtures/semantic/` but SEMANTIC_LEFT_OUT, read before anything is written."""
    listed = git.run(["ls-tree", "--name-only", full, "fixtures/semantic/"], repo=contract)
    if listed.returncode != 0:
        raise SystemExit(json.dumps({"error": "file-unreadable", "file": "fixtures/semantic/", "commit": full}))
    names = sorted(name for name in (Path(line).name for line in listed.stdout.splitlines())
                   if SEMANTIC.match(name) and name not in SEMANTIC_LEFT_OUT)
    if not names:
        raise SystemExit(json.dumps({"error": "file-unreadable", "file": "fixtures/semantic/estate-*", "commit": full}))
    blobs = {}
    for name in names:
        shown = git.run(["show", "%s:fixtures/semantic/%s" % (full, name)], repo=contract, text=False)
        if shown.returncode != 0:
            raise SystemExit(json.dumps({"error": "file-unreadable", "file": "fixtures/semantic/" + name,
                                         "commit": full}))
        blobs[name] = shown.stdout
    return blobs


def _write_semantic(blobs: dict) -> list:
    """Writes `semantic/` as the commit has it: each fixture byte for byte, and an `estate-*` fixture it dropped gone."""
    folder = VECTORS / "semantic"
    folder.mkdir(exist_ok=True)
    changed = []
    for stale in sorted(p for p in folder.glob("estate-*.json") if p.name not in blobs):
        stale.unlink()
        changed.append("semantic/" + stale.name)
    for name, data in blobs.items():
        target = folder / name
        if not target.exists() or target.read_bytes() != data:
            changed.append("semantic/" + name)
            target.write_bytes(data)
    return changed


def vendor(contract: Path, commit: str) -> dict:
    git = _safe_git()
    resolved = git.run(["rev-parse", "--verify", commit + "^{commit}"], repo=contract)
    if resolved.returncode != 0:
        raise SystemExit(json.dumps({"error": "commit-unreadable", "commit": commit}))
    full = resolved.stdout.strip()
    blobs = {}
    for name, path in FILES.items():
        shown = git.run(["show", "%s:%s" % (full, path)], repo=contract, text=False)
        if shown.returncode != 0:
            raise SystemExit(json.dumps({"error": "file-unreadable", "file": path, "commit": full}))
        blobs[name] = shown.stdout
    semantic = _semantic_blobs(git, contract, full)
    readme_path = VECTORS / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    if not PIN.search(readme):
        raise SystemExit(json.dumps({"error": "readme-pin-missing"}))
    readme = PIN.sub(lambda m: m.group(1) + full + m.group(3), readme)
    changed = _write_semantic(semantic)
    for name, data in blobs.items():
        digest = hashlib.sha256(data).hexdigest()
        row = "| `%s` | `%s` |" % (name, digest)
        pattern = re.compile(r"^\| `%s` \| `[0-9a-f]{64}` \|$" % re.escape(name), re.M)
        readme = pattern.sub(row, readme) if pattern.search(readme) else readme.rstrip("\n") + "\n" + row + "\n"
        target = VECTORS / name
        target.parent.mkdir(exist_ok=True)
        if not target.exists() or target.read_bytes() != data:
            changed.append(name)
        target.write_bytes(data)
    readme_path.write_text(readme, encoding="utf-8")
    pin = json.loads(CONTRACT_PIN.read_text(encoding="utf-8"))
    if pin.get("commit") != full:
        pin["commit"] = full
        CONTRACT_PIN.write_text(json.dumps(pin, indent=2) + "\n", encoding="utf-8")
        changed.append(CONTRACT_PIN.name)
    return {"commit": full, "changed": changed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--contract", type=Path, required=True, help="a fabric-agent-contract checkout")
    parser.add_argument("--commit", required=True, help="the contract commit to pin")
    args = parser.parse_args(argv)
    print(json.dumps(vendor(args.contract.expanduser().resolve(), args.commit), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
