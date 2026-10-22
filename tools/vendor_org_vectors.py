#!/usr/bin/env python3
"""Vendors the contract files organization enrollment (OBS-161) is pinned to, in one step.

    python3 tools/vendor_org_vectors.py --contract <fabric-agent-contract checkout> --commit <sha>

Copies, byte for byte from that commit (read with `git show`, so the checkout's working tree and branch do not
matter), `fixtures/vectors/http-signature.json`, `fixtures/vectors/share-registration.json` and
`schemas/device-enrollment.schema.json` into `observatory/engine/fabric/node-vectors/`, and the device request and
check-in fixtures CT-65 added into its `device/` folder, then rewrites the commit and those files' digest rows in that
folder's README, and the commit of the engine's contract pin (`observatory/engine/fabric-contract.lock.json`, OBS-196):
the contract asks a consumer to pin it once and to move the pin with what it vendors, in one change.
`tests/test_node_foundation.py` checks the digests, `tests/test_node_org.py` runs the files and
`tests/test_contract_lock.py` holds the pin equal to the README's commit, so a re-vendored commit is checked by the next
test run. Prints what changed; exits 1 when the commit or a file cannot be read, and changes nothing then.
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
FILES = {"http-signature.json": "fixtures/vectors/http-signature.json",
         "share-registration.json": "fixtures/vectors/share-registration.json",
         "device-enrollment.schema.json": "schemas/device-enrollment.schema.json",
         # The device fixtures the node's requests and answers are held to (CT-65, DEC-0094).
         "device/device-enrollment-request-file-key.json": "fixtures/positive/device-enrollment-request-file-key.json",
         "device/device-enrollment-file-key-not-exportable.json":
             "fixtures/negative/device-enrollment-file-key-not-exportable.json",
         "device/device-enrollment-file-key-attested.json": "fixtures/negative/device-enrollment-file-key-attested.json",
         "device/device-enrollment-hardware-key-exportable.json":
             "fixtures/negative/device-enrollment-hardware-key-exportable.json",
         "device/device-check-in-response-protocols.json": "fixtures/positive/device-check-in-response-protocols.json"}
#: The README sentence that names the OBS-161 commit; the vendored commit is the one 40-hex id in it.
PIN = re.compile(r"(\(OBS-161 pin\)[^`]*`)([0-9a-f]{40})(`)")


def _safe_git():
    spec = importlib.util.spec_from_file_location("observatory_vendor_safe_git",
                                                  ROOT / "observatory" / "engine" / "safe_git.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    readme_path = VECTORS / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    if not PIN.search(readme):
        raise SystemExit(json.dumps({"error": "readme-pin-missing"}))
    readme = PIN.sub(lambda m: m.group(1) + full + m.group(3), readme, count=1)
    changed = []
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
