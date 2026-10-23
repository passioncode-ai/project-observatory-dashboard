#!/usr/bin/env python3
"""Vendors the contract files organization enrollment (OBS-161) is pinned to, in one step.

    python3 tools/vendor_org_vectors.py --contract <fabric-agent-contract checkout> --commit <sha>

Copies, byte for byte from that commit (read with `git show`, so the checkout's working tree and branch do not
matter), `fixtures/vectors/http-signature.json`, `fixtures/vectors/share-registration.json` and
`schemas/device-enrollment.schema.json` into `observatory/engine/fabric/node-vectors/`, and the device request and
check-in fixtures CT-65 added and the key-on-record renewal fixtures of DEC-0100 into its `device/` folder, then
rewrites the commit and those files' digest rows in that folder's README, and the commit of the engine's contract
pin (OBS-196), and re-vendors the interop schemas `tools/vendor_contract_schemas.py` keeps at the pin (OBS-205): the contract
asks a consumer to pin it once and to move the pin with what it vendors, in one change.
`tests/test_node_foundation.py` checks the digests, `tests/test_node_org.py` runs the files and
`tests/test_contract_lock.py` holds the pin equal to the READMEs' commits, so a re-vendored commit is checked by the next
test run. The pin is read through `observatory/engine/contract_lock.py`, the one reader of it, and checked before
anything is read from the contract; every file is read and every README checked before anything is written. Prints what
changed; exits 1 when the pin is missing or invalid, or the commit, a file or a README cannot be read, and changes
nothing then.
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
ENGINE = ROOT / "observatory" / "engine"
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
         "device/device-check-in-response-protocols.json": "fixtures/positive/device-check-in-response-protocols.json",
         # A renewal by the key on record (DEC-0100): the request, and the same request carrying a token id.
         "device/device-enrollment-request-key-on-record.json":
             "fixtures/positive/device-enrollment-request-key-on-record.json",
         "device/device-enrollment-key-on-record-with-token.json":
             "fixtures/negative/device-enrollment-key-on-record-with-token.json"}
#: The README sentence that names the OBS-161 commit; the vendored commit is the one 40-hex id in it.
PIN = re.compile(r"(\(OBS-161 pin\)[^`]*`)([0-9a-f]{40})(`)")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location("observatory_vendor_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _safe_git():
    return _load(ENGINE / "safe_git.py", "safe_git")


def _contract_lock():
    return _load(ENGINE / "contract_lock.py", "contract_lock")


def _schemas():
    return _load(ROOT / "tools" / "vendor_contract_schemas.py", "contract_schemas")


def vendor(contract: Path, commit: str, engine: Path = ENGINE) -> dict:
    # The pin first: a missing or invalid pin stops the run before the contract is read or a file is written.
    lock = _contract_lock()
    try:
        pin = lock.contract_pin(engine)
    except (ValueError, OSError) as error:
        raise SystemExit(json.dumps({"error": "pin-invalid", "detail": str(error)})) from None
    if pin is None:
        raise SystemExit(json.dumps({"error": "pin-missing", "file": lock.CONTRACT_PIN}))
    git = _safe_git()
    resolved = git.run(["rev-parse", "--verify", commit + "^{commit}"], repo=contract)
    if resolved.returncode != 0:
        raise SystemExit(json.dumps({"error": "commit-unreadable", "commit": commit}))
    full = resolved.stdout.strip()
    moved = {**pin, "commit": full}
    problems = lock.pin_problems(moved)
    if problems:
        raise SystemExit(json.dumps({"error": "pin-invalid", "detail": "; ".join(problems)}))
    blobs = {}
    for name, path in FILES.items():
        shown = git.run(["show", "%s:%s" % (full, path)], repo=contract, text=False)
        if shown.returncode != 0:
            raise SystemExit(json.dumps({"error": "file-unreadable", "file": path, "commit": full}))
        blobs[name] = shown.stdout
    vectors = engine / VECTORS.relative_to(ENGINE)
    readme_path = vectors / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    if not PIN.search(readme):
        raise SystemExit(json.dumps({"error": "readme-pin-missing"}))
    schemas = _schemas()
    try:
        schema_files = schemas.plan(contract, full, engine)
    except schemas.VendorError as error:
        raise SystemExit(json.dumps(error.detail)) from None
    # Everything is read and checked; from here on the run only writes.
    readme = PIN.sub(lambda m: m.group(1) + full + m.group(3), readme, count=1)
    changed = []
    for name, data in blobs.items():
        digest = hashlib.sha256(data).hexdigest()
        row = "| `%s` | `%s` |" % (name, digest)
        pattern = re.compile(r"^\| `%s` \| `[0-9a-f]{64}` \|$" % re.escape(name), re.M)
        readme = pattern.sub(row, readme) if pattern.search(readme) else readme.rstrip("\n") + "\n" + row + "\n"
        target = vectors / name
        target.parent.mkdir(exist_ok=True)
        if not target.exists() or target.read_bytes() != data:
            changed.append(name)
        target.write_bytes(data)
    readme_path.write_text(readme, encoding="utf-8")
    changed += schemas.write(schema_files, engine)
    if pin["commit"] != full:
        pin_path = engine / lock.CONTRACT_PIN
        pin_path.write_text(json.dumps(moved, indent=2) + "\n", encoding="utf-8")
        changed.append(lock.CONTRACT_PIN)
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
