#!/usr/bin/env python3
"""Vendors the contract schemas the engine holds its interop answers to, at the contract pin (OBS-205).

    python3 tools/vendor_contract_schemas.py --contract <fabric-agent-contract checkout>

The commit is the engine's one pin of the contract, read through `observatory/engine/contract_lock.py`. For each folder
below the tool copies, byte for byte from that commit (read with `git show`, so the checkout's working tree and branch
do not matter), every schema the folder holds and the `$ref` closure of them in the contract's `schemas/`: a schema may
start to refer to another at any commit, and a copy without it does not resolve. Then it rewrites, in the folder's
README, the commit on the line that names the contract and the digest table, which `tests/test_interop.py` checks.

Everything is read and checked before anything is written: a missing or invalid pin, a commit the checkout does not
hold, a file that cannot be read or a README without its commit line exits 1 and changes nothing.
`tools/vendor_org_vectors.py`, which moves the pin, runs the same plan at the new commit in the same step.
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
ENGINE = ROOT / "observatory" / "engine"
#: The folders vendored at the pin, relative to the engine. `fabric/service-schemas` joins them once the word its
#: descriptor schema names at the pin is reviewed for the privacy gate (`contract_lock.PIN_CHECK_EXCLUDES`, OBS-212).
FOLDERS = ("fabric/interop-schemas",)
#: The README line that names the vendored commit: on the same line as the contract's name, so `pin:check` reads it.
COMMIT_LINE = re.compile(r"(`passioncode-ai/fabric-agent-contract` at commit `)([0-9a-f]{40})(`)")
DIGEST_ROW = re.compile(r"^\| `[^`]+` \| `[0-9a-f]{64}` \|\n", re.M)
TABLE_HEAD = "| File | sha256 |\n|---|---|\n"


class VendorError(Exception):
    """A reason to write nothing, as the JSON the tool prints."""

    def __init__(self, **detail: str) -> None:
        super().__init__(json.dumps(detail))
        self.detail = detail


def _load(name: str):
    spec = importlib.util.spec_from_file_location("observatory_vendor_" + name, ENGINE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pinned_commit(engine: Path = ENGINE) -> str:
    """The pin's commit, through `contract_lock`; a missing or invalid pin is a `VendorError`."""
    contract_lock = _load("contract_lock")
    try:
        pin = contract_lock.contract_pin(engine)
    except (ValueError, OSError) as error:
        raise VendorError(error="pin-invalid", detail=str(error)) from None
    if pin is None:
        raise VendorError(error="pin-missing", file=contract_lock.CONTRACT_PIN)
    return pin["commit"]


def _refs(node: object):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and not value.startswith("#"):
                yield value.split("#", 1)[0]
            else:
                yield from _refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs(value)


def plan(contract: Path, commit: str, engine: Path = ENGINE) -> dict[Path, bytes]:
    """{path: bytes} the folders and their READMEs hold at `commit`, read and checked, nothing written."""
    git = _load("safe_git")
    resolved = git.run(["rev-parse", "--verify", commit + "^{commit}"], repo=contract)
    if resolved.returncode != 0:
        raise VendorError(error="commit-unreadable", commit=commit)
    full = resolved.stdout.strip()
    out: dict[Path, bytes] = {}
    for folder in FOLDERS:
        directory = engine / folder
        todo = sorted(path.name for path in directory.glob("*.schema.json"))
        blobs: dict[str, bytes] = {}
        while todo:
            name = todo.pop(0)
            if name in blobs:
                continue
            if "/" in name or name.startswith("."):
                raise VendorError(error="ref-outside-schemas", ref=name, commit=full)
            shown = git.run(["show", "%s:schemas/%s" % (full, name)], repo=contract, text=False)
            if shown.returncode != 0:
                raise VendorError(error="file-unreadable", file="schemas/" + name, commit=full)
            blobs[name] = shown.stdout
            try:
                todo += sorted(set(_refs(json.loads(shown.stdout))) - set(blobs))
            except ValueError:
                raise VendorError(error="file-not-json", file="schemas/" + name, commit=full) from None
        readme = (directory / "README.md").read_text(encoding="utf-8")
        if not COMMIT_LINE.search(readme) or TABLE_HEAD not in readme:
            raise VendorError(error="readme-unrecognised", file=folder + "/README.md")
        readme = COMMIT_LINE.sub(lambda m: m.group(1) + full + m.group(3), readme, count=1)
        rows = "".join("| `%s` | `%s` |\n" % (name, hashlib.sha256(blobs[name]).hexdigest()) for name in sorted(blobs))
        head, tail = readme.split(TABLE_HEAD, 1)
        readme = head + TABLE_HEAD + rows + DIGEST_ROW.sub("", tail)
        out.update({directory / name: data for name, data in blobs.items()})
        out[directory / "README.md"] = readme.encode("utf-8")
    return out


def write_atomic(target: Path, data: bytes) -> None:
    """`data` into `target` through a temporary file in the same folder and a rename: a failed write leaves the old
    file whole, never a truncated one."""
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


def write(files: dict[Path, bytes], root: Path = ROOT) -> list[str]:
    """Writes `files`, each through a temporary file and a rename; the paths, relative to `root`, whose bytes
    changed."""
    changed = []
    for path, data in files.items():
        if not path.exists() or path.read_bytes() != data:
            changed.append(path.relative_to(root).as_posix())
            write_atomic(path, data)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--contract", type=Path, required=True, help="a fabric-agent-contract checkout")
    args = parser.parse_args(argv)
    try:
        commit = pinned_commit()
        files = plan(args.contract.expanduser().resolve(), commit)
    except VendorError as error:
        print(json.dumps(error.detail), file=sys.stderr)
        return 1
    print(json.dumps({"commit": commit, "changed": write(files)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
