#!/usr/bin/env python3
"""Vendors the node's contract files (`observatory/engine/fabric/node-vectors/`) at one commit, in one step.

    python3 tools/vendor_org_vectors.py --contract <fabric-agent-contract checkout> --commit <sha>

Copies, byte for byte from that commit (read with `git show`, so the checkout's working tree and branch do not
matter), the node's vectors of `fixtures/vectors/` (the estate record, the relay, the ceremonies and the organization
enrollment's `http-signature.json` and `share-registration.json`) and `schemas/device-enrollment.schema.json` into
`observatory/engine/fabric/node-vectors/`, the device request and check-in fixtures CT-65 added and the key-on-record
renewal fixtures of DEC-0100 into its `device/` folder, the node summary's schema, catalogue and fixtures
(OBS-192) into its `summary/` folder, and every `estate-*` fixture of `fixtures/semantic/` into its
`semantic/` folder but the one the public-release gate refuses (`SEMANTIC_LEFT_OUT`), removing a vendored `estate-*`
fixture the commit no longer has (OBS-171). Then it rewrites every commit the folder's README names and the digest
rows of the files outside `semantic/`, re-vendors the interop schemas `tools/vendor_contract_schemas.py` keeps at the
pin (OBS-205), and moves the commit of the engine's contract pin (OBS-196): the contract asks a consumer to pin it once
and to move the pin with what it vendors, in one change.
`tests/test_node_foundation.py` checks the digests, the node suites run the files and `tests/test_contract_lock.py`
holds the pin equal to the READMEs' commits, so a re-vendored commit is checked by the next test run.

The pin is read through `observatory/engine/contract_lock.py`, the one reader of it, and checked before anything is
read from the contract; every file is read and every README checked before anything is written; each file is written
to a temporary file beside it and renamed over it, and the pin is written last (OBS-205). Prints what changed; exits 1
when the pin is missing or invalid, or the commit, a file or a README cannot be read, and changes nothing then.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "observatory" / "engine" / "fabric" / "node-vectors"
ENGINE = ROOT / "observatory" / "engine"
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
             "fixtures/negative/device-enrollment-key-on-record-with-token.json",
         # The node's harness summary and its drift (DEC-0101, DEC-0102, OBS-192): the schema with the two it refers
         # to, the coding-agent catalogue its ids come from (DEC-0093), the policy that carries a baseline, and every
         # node-summary fixture, positive, negative and semantic, but one: `node-summary-path-in-name.json` names a home
         # folder, a shape the public-release gate refuses, so tests/test_node_summary.py rebuilds it from the positive.
         "summary/node-summary.schema.json": "schemas/node-summary.schema.json",
         "summary/common.schema.json": "schemas/common.schema.json",
         "summary/activity-common.schema.json": "schemas/activity-common.schema.json",
         "summary/coding-agents.json": "catalogue/coding-agents.json",
         "summary/device-policy-harness-baseline.json": "fixtures/positive/device-policy-harness-baseline.json",
         "summary/positive/node-summary.json": "fixtures/positive/node-summary.json",
         **{"summary/negative/node-summary-%s.json" % name: "fixtures/negative/node-summary-%s.json" % name
            for name in ("dotted-suffix", "drift-dotted-suffix", "endpoint-query", "mcp-dotted-suffix",
                         "personal-context", "server-arguments", "skill-two-at")},
         **{"summary/semantic/node-summary-%s.json" % name: "fixtures/semantic/node-summary-%s.json" % name
            for name in ("drift-hidden", "drift-without-baseline", "duplicate", "home-after-at", "home-after-dot",
                         "ok", "other-org", "person")},
         # A baseline the policy schema refuses (DEC-0102): a version or digest a kind does not carry, a name in
         # another shape than a summary's (`node_summary.baseline_problems`).
         **{"summary/negative/device-policy-baseline-%s.json" % name:
            "fixtures/negative/device-policy-baseline-%s.json" % name
            for name in ("coding-agent-digest", "coding-agent-name", "dotted-suffix", "mcp-server-digest",
                         "mcp-server-min-version", "service-digest", "service-min-version", "service-name",
                         "skill-min-version")},
         # What a summary lists follows `machine.mode` (DEC-0118): an organization's policy with its own
         # `workspace.folders`, an `mdm` layer that sets mode A, and two folder lists the policy schema refuses.
         "summary/device-policy-organization.json": "fixtures/positive/device-policy-organization.json",
         "summary/device-policy-mdm-managed.json": "fixtures/positive/device-policy-mdm-managed.json",
         **{"summary/negative/device-policy-workspace-folder-%s.json" % name:
            "fixtures/negative/device-policy-workspace-folder-%s.json" % name for name in ("absolute", "home")},
         # A check-in's summary read with the check-in and judged against the client certificate (DEC-0114).
         **{"summary/semantic/%s.json" % name: "fixtures/semantic/%s.json" % name
            for name in ("credential-check-in-summary-ok", "credential-check-in-summary-other-device",
                         "credential-check-in-summary-duplicate", "device-attribution-records-ok",
                         "device-attribution-records-other-org", "device-attribution-summary-other-device",
                         "device-attribution-summary-other-org")},
         # An organization that ended (DEC-0103): the 410 body and every negative fixture but `-path`, which names a
         # home folder the public-release gate refuses; tests/test_node_org.py rebuilds it from the positive.
         "device-organization-closed.schema.json": "schemas/device-organization-closed.schema.json",
         "device/device-organization-closed.json": "fixtures/positive/device-organization-closed.json",
         **{"device/device-organization-closed-%s.json" % name: "fixtures/negative/device-organization-closed-%s.json"
            % name for name in ("bad-time", "error-extra", "extra", "flat", "no-time", "other-code")},
         # A purpose rejection, a reason that holds nothing, and the acknowledged notice a held collector resumes on
         # (DEC-0100, DEC-0102).
         "device/activity-batch-ack-purpose.json": "fixtures/positive/activity-batch-ack-purpose.json",
         "device/activity-batch-ack-before-purpose.json": "fixtures/positive/activity-batch-ack-before-purpose.json",
         "device/device-check-in-response-acknowledged.json":
             "fixtures/positive/device-check-in-response-acknowledged.json",
         "device/device-check-in-response-acknowledged-bad.json":
             "fixtures/negative/device-check-in-response-acknowledged-bad.json",
         # The repair runner (DEC-0101, DEC-0102, DEC-0117; OBS-193): `repairDecision`'s shared vectors, the request
         # and result schemas with the two they refer to that `summary/` does not hold (`device-common`, and
         # `fabric-package` for its `agentRef`), every repair fixture, and FAC-SEM-068's remote-execution case.
         "repair/repair-decision.json": "fixtures/vectors/repair-decision.json",
         "repair/repair-request.schema.json": "schemas/repair-request.schema.json",
         "repair/repair-result.schema.json": "schemas/repair-result.schema.json",
         "repair/device-common.schema.json": "schemas/device-common.schema.json",
         "repair/fabric-package.schema.json": "schemas/fabric-package.schema.json",
         **{"repair/positive/%s.json" % name: "fixtures/positive/%s.json" % name
            for name in ("repair-request", "repair-result", "repair-result-refused", "repair-result-org-cloud")},
         **{"repair/negative/%s.json" % name: "fixtures/negative/%s.json" % name
            for name in ("repair-request-task-two-lines", "repair-result-already-answered",
                         "repair-result-refused-without-reason", "repair-result-succeeded-unnotified")},
         "repair/semantic/device-action-expired-remote-execution.json":
             "fixtures/semantic/device-action-expired-remote-execution.json"}
#: The contract's estate fixtures, vendored into `semantic/` without a digest row (`test_node_pairing.py` runs each).
SEMANTIC = re.compile(r"^estate-[A-Za-z0-9-]+\.json$")
#: Its slot is the AWS documentation's example access key id, a shape the public-release gate refuses;
#: `test_node_pairing.py` rebuilds it from `estate-record-ok.json`.
SEMANTIC_LEFT_OUT = frozenset({"estate-secret-value-as-slot.json"})
#: Every commit the README names ("... at commit `<40 hex>`"): the whole folder is at one commit.
PIN = re.compile(r"(at commit\s+`)([0-9a-f]{40})(`)")


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


def write_atomic(target: Path, data: bytes) -> None:
    """`data` into `target` through a temporary file in the same folder and a rename: a failed write leaves the old
    file whole, never a truncated one."""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix="." + target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


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


def _write_semantic(blobs: dict, vectors: Path = VECTORS) -> list:
    """Writes `semantic/` as the commit has it: each fixture byte for byte, and an `estate-*` fixture it dropped gone."""
    folder = vectors / "semantic"
    folder.mkdir(exist_ok=True)
    changed = []
    for stale in sorted(p for p in folder.glob("estate-*.json") if p.name not in blobs):
        stale.unlink()
        changed.append("semantic/" + stale.name)
    for name, data in blobs.items():
        target = folder / name
        if not target.exists() or target.read_bytes() != data:
            changed.append("semantic/" + name)
            write_atomic(target, data)
    return changed


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
    semantic = _semantic_blobs(git, contract, full)
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
    # Everything is read and checked; from here on the run only writes, and the pin last.
    readme = PIN.sub(lambda m: m.group(1) + full + m.group(3), readme)
    changed = _write_semantic(semantic, vectors)
    for name, data in blobs.items():
        digest = hashlib.sha256(data).hexdigest()
        row = "| `%s` | `%s` |" % (name, digest)
        pattern = re.compile(r"^\| `%s` \| `[0-9a-f]{64}` \|$" % re.escape(name), re.M)
        readme = pattern.sub(row, readme) if pattern.search(readme) else readme.rstrip("\n") + "\n" + row + "\n"
        target = vectors / name
        if not target.exists() or target.read_bytes() != data:
            changed.append(name)
            write_atomic(target, data)
    encoded = readme.encode("utf-8")
    if readme_path.read_bytes() != encoded:
        write_atomic(readme_path, encoded)
    changed += schemas.write(schema_files, engine)
    if pin["commit"] != full:
        write_atomic(engine / lock.CONTRACT_PIN, (json.dumps(moved, indent=2) + "\n").encode("utf-8"))
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
