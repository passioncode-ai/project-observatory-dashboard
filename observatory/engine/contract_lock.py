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
import os
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


# -- pin drift: `pnpm pin:check`, read in Python (OBS-205) -----------------------------------------------------
#
# The contract's `src/pin-check.ts` (`findPinDrift` and its walk) fails a consumer on any commit-like hash, on a
# line about the contract, that is not the pin. The engine runs no pnpm, so CI keeps the drift at zero through
# this port, rule for rule; `tests/test_contract_lock.py` ports the contract's own cases of `findPinDrift`.

#: A line is about the contract when it names it or its pin, or sits under a Markdown heading that does.
MENTIONS_CONTRACT = re.compile(r"fabric[- ]agent[- ]contract|contract[-_ ]?(pin|commit|revision)|CONTRACT_COMMIT",
                               re.IGNORECASE)
HEADING = re.compile(r"^#{1,6}\s")
HEX = re.compile(r"(?<![0-9a-zA-Z:])[0-9a-f]{7,40}(?![0-9a-zA-Z])")
#: The files the walk reads, and the names it never enters.
PIN_CHECK_SUFFIX = re.compile(r"\.(md|json|ya?ml|py|m?js|ts|toml|txt)$")
PIN_CHECK_SKIPPED = (".git", "node_modules")
#: Dated records name the revision of their day; the check skips them by default.
DATED_RECORDS = ("docs/handoffs", "docs/evidence", ".task-pipeline")
DATED_FILE = re.compile(r"^CHANGELOG")

#: Paths this engine passes to `pin:check` as `--exclude`, each with the reason it names another commit (OBS-205):
#:     pnpm --dir <contract checkout> pin:check observatory/engine --exclude fabric/service-schemas
#: Every other mention `pin:check` reads equals the pin.
PIN_CHECK_EXCLUDES: dict[str, str] = {
    "fabric/service-schemas": "vendored at `7931c1d`: at the pin `service-descriptor.schema.json` names a word the "
                              "maintainer's private identifier list holds, so the public-release privacy gate refuses "
                              "the copy until that word is reviewed (OBS-212)",
}


def find_pin_drift(files: list[tuple[str, str]], commit: str) -> list[tuple[str, int, str]]:
    """`findPinDrift`: (path, line, found) for every commit-like hash on a line about the contract that is
    not `commit`, in full or abbreviated to at least seven characters. `files` are (path, text) pairs."""
    drift = []
    for path, text in files:
        section = False
        for number, line in enumerate(text.split("\n"), 1):
            if HEADING.match(line):
                section = bool(MENTIONS_CONTRACT.search(line))
            if not section and not MENTIONS_CONTRACT.search(line):
                continue
            for match in HEX.finditer(line):
                found = match.group(0)
                if not re.search(r"[a-f]", found) or not re.search(r"[0-9]", found):
                    continue  # words and plain numbers are not commits
                if commit.startswith(found):
                    continue
                drift.append((path, number, found))
    return drift


def is_dated_record(relative: str) -> bool:
    """`isDatedRecord`: a changelog, or anything under a dated-records folder."""
    if DATED_FILE.match(relative.rsplit("/", 1)[-1]):
        return True
    return any(relative == prefix or relative.startswith(prefix + "/") for prefix in DATED_RECORDS)


def pin_check_files(root: Path = ROOT, excluded: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """The files `pin:check <root>` reads, as (`/`-separated relative path, text): text files outside `.git` and
    `node_modules`, not the pin itself, no dated record, nothing under an `excluded` path. A folder or file it
    cannot read holds no mention it could check, and is skipped, as the contract's walk skips it."""
    skipped = (CONTRACT_PIN,) + tuple(path.rstrip("/") for path in excluded)
    out: list[tuple[str, str]] = []

    def walk(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            if directory == root:
                raise
            return
        for entry in entries:
            if entry.name in PIN_CHECK_SKIPPED:
                continue
            relative = Path(entry.path).relative_to(root).as_posix()
            if any(relative == prefix or relative.startswith(prefix + "/") for prefix in skipped):
                continue
            if is_dated_record(relative):
                continue
            if entry.is_dir(follow_symlinks=False):
                walk(Path(entry.path))
            elif PIN_CHECK_SUFFIX.search(entry.name):
                try:
                    out.append((relative, Path(entry.path).read_text(encoding="utf-8", errors="replace")))
                except OSError:
                    continue

    walk(root)
    return out


def pin_drift(root: Path = ROOT, excluded: tuple[str, ...] = tuple(PIN_CHECK_EXCLUDES)) -> list[tuple[str, int, str]]:
    """What `pnpm pin:check <root> --exclude <each excluded path>` reports as drift, against the pin `root` holds."""
    pin = contract_pin(root)
    if pin is None:
        raise FileNotFoundError(f"no contract pin in {root}")
    return find_pin_drift(pin_check_files(root, excluded), pin["commit"])


# -- vendored folders: which contract commit each README names ------------------------------------------------
#
# `pin:check` reads a hash only on a line about the contract. A vendored folder's README may name its commit on the
# line after the one that names the contract, where `pin:check` does not look; this check reads every commit-like id
# in each `fabric/*/README.md` that names the contract, and holds it to the pin or to the list below.

#: Folders whose README names a contract commit other than the pin, each commit with why it stays (OBS-205). Moving
#: one is a re-vendor with its own review, row OBS-212; a commit missing from here, or listed and gone, fails the test.
VENDORED_AT_OTHER_COMMITS: dict[str, dict[str, str]] = {
    "fabric/service-schemas": {"7931c1d": PIN_CHECK_EXCLUDES["fabric/service-schemas"]},
    "fabric/delegation-schemas": {
        "7931c1d": "at the pin `delegation-token.schema.json` admits a partner actor and a fifth actor level, and "
                   "`common`, `delegation-jwks` and `governance` changed; `delegation.py` and the meter validate "
                   "against this copy, so moving it changes what they accept. The grant-check request and answer "
                   "are the same at the pin (the answer byte for byte, the request but for descriptions)."},
    "fabric/delegation-vectors": {
        "07042181bfae2a481aa0da1a5651f24c76669c48":
            "the six fixtures are byte-identical at the pin, but `tests/test_delegation_sealed.py` ports the "
            "contract's `test/delegation-sealed.test.ts` at this commit, not yet compared with the pin's."},
    "fabric/memory-schemas": {
        "aa2f0977bbbb32e9940e16b2654af84960abfb54":
            "both schemas differ at the pin, and `mcp/memory_wire.py` validates every memory call against them."},
}


def readme_commits(text: str) -> set[str]:
    """Every commit-like id a README names: seven to forty lower-case hex digits holding a digit and a letter
    (`findPinDrift`'s test), on any line, a 64-digit digest row being no commit."""
    found = set()
    for match in HEX.finditer(text):
        token = match.group(0)
        if re.search(r"[a-f]", token) and re.search(r"[0-9]", token):
            found.add(token)
    return found


def vendored_off_pin(root: Path = ROOT) -> dict[str, set[str]]:
    """{folder: commits other than the pin} for every `fabric/*/README.md` that names the contract."""
    pin = contract_pin(root)
    if pin is None:
        raise FileNotFoundError(f"no contract pin in {root}")
    off = {}
    for readme in sorted((root / "fabric").glob("*/README.md")):
        text = readme.read_text(encoding="utf-8")
        if not MENTIONS_CONTRACT.search(text):
            continue
        others = {commit for commit in readme_commits(text) if not pin["commit"].startswith(commit)}
        if others:
            off[readme.parent.relative_to(root).as_posix()] = others
    return off
