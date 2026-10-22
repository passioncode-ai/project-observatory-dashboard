"""The engine's two lock files, and the one place that reads either (OBS-196).

`fabric-contract.lock.json` is the name the Fabric Agent Contract gives a
consumer's one pin of the contract (`schemas/contract-pin.schema.json` in
passioncode-ai/fabric-agent-contract, checked there by `pnpm pin:check`):
`contract`, `version`, `repository`, the full 40-character `commit` and an
optional `profile`. The engine used to keep its own local-profile lock under that
name (`profile`, `version`, `schemaRelease`, `schemas`, `releases`), so a reader
keyed on the name took the profile lock for the pin, and `pin:check` on this
folder exited 2. The profile lock is now `observatory-profile.lock.json`, and the
pin sits beside it under the contract's name.

WHY A MODULE, NOT TWO FILE NAMES IN EACH READER. The defect was a name read in
three places that each assumed what the file held. Here a reader asks for the
profile or for the pin; each answer checks the shape before it is returned, so a
pin is never read as a profile and a profile never as a pin.

THE OLD NAME STAYS READABLE FOR ONE RELEASE: the release that ships the rename
keeps the fallback below, and the release after it removes the fallback. A tree from before the rename (a sandbox copied by an older
runner, a checkout mid-upgrade) holds the profile lock under the old name and no
new file: `profile_lock` then reads the old file, but only while it carries the
profile's shape. The shipped package no longer carries a profile lock under the
old name: that name is the pin's now, and one name cannot hold both documents.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent

#: The engine's local-profile lock: which profile and schema releases it publishes.
PROFILE_LOCK = "observatory-profile.lock.json"
#: The contract's own name for a consumer's pin (`src/pin-check.ts`, `LOCK_FILE`).
CONTRACT_PIN = "fabric-contract.lock.json"

#: `contract-pin.schema.json` at the pinned commit: its `properties`, required
#: members, constants and patterns. `additionalProperties` is false there.
PIN_MEMBERS = ("contract", "version", "repository", "commit", "profile")
PIN_REQUIRED = ("contract", "version", "repository", "commit")
PIN_CONTRACT = "fabric-agent-contract"
PIN_REPOSITORY = "https://github.com/passioncode-ai/fabric-agent-contract"
PIN_PROFILES = ("mcp", "a2a", "local-runner")
#: `common.schema.json#/$defs/semver`.
SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
                    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def _read(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_profile(doc: object) -> bool:
    return isinstance(doc, dict) and "profile" in doc and "contract" not in doc


def pin_problems(doc: object) -> list[str]:
    """What `contract-pin.schema.json` refuses in `doc`, one line each; empty when
    it is a pin. Stricter than `pin:check` on one point: a member the schema does
    not define is a problem here (the schema says so), where `pin:check` only
    notes it, because the engine writes its own pin and has no kit members."""
    if not isinstance(doc, dict):
        return ["the pin is not an object"]
    problems = [f"{member} is required" for member in PIN_REQUIRED if member not in doc]
    problems += [f"{member} is not a member of a pin" for member in doc if member not in PIN_MEMBERS]
    if "contract" in doc and doc["contract"] != PIN_CONTRACT:
        problems.append(f"contract must be {PIN_CONTRACT!r}")
    if "repository" in doc and doc["repository"] != PIN_REPOSITORY:
        problems.append(f"repository must be {PIN_REPOSITORY!r}")
    if "version" in doc and not (isinstance(doc["version"], str) and SEMVER.match(doc["version"])):
        problems.append("version must be a semantic version, major.minor.patch")
    if "commit" in doc and not (isinstance(doc["commit"], str) and FULL_COMMIT.match(doc["commit"])):
        problems.append("commit must be the full 40-character lower-case hex id")
    if "profile" in doc and doc["profile"] not in PIN_PROFILES:
        problems.append(f"profile must be one of {', '.join(PIN_PROFILES)}")
    return problems


def profile_lock(root: Path = ROOT) -> dict:
    """The local-profile lock: `observatory-profile.lock.json`, or, for one
    release, the same document under its old name `fabric-contract.lock.json`."""
    current = root / PROFILE_LOCK
    if current.is_file():
        doc = _read(current)
        if not _is_profile(doc):
            raise ValueError(f"{current} is not a profile lock (no `profile`)")
        return doc
    legacy = root / CONTRACT_PIN
    if legacy.is_file():
        doc = _read(legacy)
        if _is_profile(doc):
            return doc
    raise FileNotFoundError(f"no {PROFILE_LOCK} in {root}")


def contract_pin(root: Path = ROOT) -> dict | None:
    """The contract pin, or None when `fabric-contract.lock.json` is absent or is
    still the profile lock of a tree from before the rename. A file that claims to
    be a pin and is not one raises: a wrong pin is a defect, not an absence."""
    path = root / CONTRACT_PIN
    if not path.is_file():
        return None
    doc = _read(path)
    if _is_profile(doc):
        return None
    problems = pin_problems(doc)
    if problems:
        raise ValueError(f"{path} is not a contract pin: " + "; ".join(problems))
    return doc
