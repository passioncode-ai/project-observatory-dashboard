"""Where a product's daily operations digests are read on this machine (fabric-agent-contract DEC-0153 OD-9, OBS-533).

A product with a local service descriptor may name, under the extension key `extensions/ops-digest/0.1`, 1 to 8
folders `{dir, source}` inside its data folder (`paths.data`) that hold its `daily-YYYY-MM.jsonl` files. A collector in
the same OS account (DEC-0154) reads them. Before 0.35.18 Observatory only took daily lines through
`ops.digest.record` and read no folder, so a product that declared its folders reached no receiver.

PORTED, NOT INVENTED. `view` and `rules` are the contract's `digestFoldersView` and `digestFoldersRules`
(`src/ops-digest.ts`) at the release tag `v0.1.0+2026-11-06.1`, the same tag `fabric/ops-digest/` is vendored at, so
`tests/test_ops_digest_folders.py` holds them to that tag's `ops-digest-folders-*` fixtures: a declaration with any
`FAC-SEM-168` finding is not declared, and is reported.

THE COLLECTOR (`collect`). For each local descriptor whose declaration is usable: every `daily-YYYY-MM.jsonl` in
`<paths.data>/<dir>`, line by line (OD-6): a line that is not JSON, names another schema or fails `ops_digest.check` is
skipped and counted, never a stop; of two lines for one day the first. Each line is recorded once, as written, with its
`source` and the descriptor's `{id, instance}`, under the project the registry attributes that service to
(`ops_digest_store.service_projects`); a service the registry attributes to no project, or to two, is reported and sent
nowhere. A folder that is a symbolic link, or that resolves outside `paths.data`, is not read.

WHERE PYTHON AND JAVASCRIPT DIFFER. The fold's case mappings are the runtime's: JavaScript's ICU (Unicode 17.0 when the
contract wrote it) and Python's `unicodedata` (16.0 in Python 3.14), so a code point new in 17.0 folds unchanged here.
Both directions are the safe side: two dirs a file system may keep apart are at worst a duplicate finding.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable

import ops_digest

EXTENSION_KEY = "https://fabric.passioncode.ai/agent-contract/extensions/ops-digest/0.1"
FOLDERS_PATH = "/extensions/" + EXTENSION_KEY.replace("~", "~0").replace("/", "~1")
FOLDERS_MIN, FOLDERS_MAX = 1, 8
CODE = "FAC-SEM-168"
#: `fabric-package.schema.json#/$defs/path`: relative, forward slashes, no `.` or `..` segment, no drive or `:`, no
#: backslash, no `//`, no C0 control, no trailing `/`, 1 to 256 code points (ajv reads the pattern with the `u` flag).
PATH = re.compile(r"(?![/~])(?![\s\S]*\\)(?![\s\S]*//)(?!(?:[\s\S]*/)?\.{1,2}(?:/|\Z))[^\x00-\x1f:]{1,256}(?<!/)")
DAILY_FILE = re.compile(r"daily-[0-9]{4}-[0-9]{2}\.jsonl")
#: The most one daily file is read for: a product's month of days is a few kilobytes.
DAILY_FILE_MAX = 8 * 1024 * 1024

_HFS_IGNORABLE = re.compile("[‌-‏‪-‮⁪-⁯﻿]")
_NUSKHURI = re.compile("[ⴀ-ⴥ]")
#: Unicode's Default_Ignorable_Code_Point (DerivedCoreProperties), which JavaScript names `\p{Default_Ignorable_Code_Point}`.
_DEFAULT_IGNORABLE = re.compile(
    "[­͏؜ᅟᅠ឴឵᠋-᠏​-‏‪-‮⁠-⁯ㅤ"
    "︀-️﻿ﾠ￰-￸\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]")
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")
_TRAILING = re.compile(r"[. ]+\Z")
_PRINTABLE_ASCII = re.compile(r"[\x20-\x7e]+")

LONG_RUN_DIR = ("a dir is shaped like a credential: a run of 40 or more letters, digits, +, /, =, _ or - (across segments "
                "too) that mixes upper case, lower case and a digit reads as a key; a space or a dot inside a segment (not "
                "at its end) breaks it. It names a folder, never a value")
EMPTY_SEGMENT = ("a dir segment is empty once its dots, spaces and code points a file system may ignore are dropped; a tool "
                 "may read it as empty, '.' or '..', another folder than the one declared, possibly outside paths.data")


def _normal(form: str, text: str) -> str:
    try:
        return unicodedata.normalize(form, text)
    except (ValueError, UnicodeError):                       # a lone surrogate: JavaScript leaves it as it is
        return text


def fold_dir(dir_: str) -> str:
    """`foldDir`: canonical caseless matching per segment (the code points HFS+ ignores removed, NFD, lower, upper and
    lower case, NFC, Georgian Nuskhuri onto Mkhedruli), trailing dots and spaces dropped."""
    def segment(part: str) -> str:
        part = _normal("NFC", _normal("NFD", _HFS_IGNORABLE.sub("", part)).lower().upper().lower())
        part = _NUSKHURI.sub(lambda m: chr(ord(m.group(0)) - 0x2D00 + 0x10D0), part)
        return _TRAILING.sub("", part)
    return "/".join(segment(part) for part in dir_.split("/"))


def _trim_segments(dir_: str) -> str:
    return "/".join(_TRAILING.sub("", part) for part in dir_.split("/"))


def _empty_segment(part: str) -> bool:
    return _trim_segments(_DEFAULT_IGNORABLE.sub("", part)) == ""


def _readings(dir_: str) -> list[str]:
    bare = _DEFAULT_IGNORABLE.sub("", dir_)
    return [dir_, bare, _trim_segments(dir_), _trim_segments(bare)]


def _hidden_dir(dir_: str) -> bool:
    return any(ops_digest.hidden(r) for r in [*_readings(dir_), fold_dir(dir_)])


def _finding(at: str, message: str) -> dict:
    return {"code": CODE, "instancePath": at, "message": message}


def shape(descriptor: Any) -> dict:
    """The declaration against `service-ops-digest-extension.schema.json`: `{declared: False}`, or
    `{declared: True, valid, folders | errors}` (errors as `<instancePath or /> <keyword>`, as ajv names them)."""
    extensions = descriptor.get("extensions") if isinstance(descriptor, dict) else None
    if not isinstance(extensions, dict) or EXTENSION_KEY not in extensions:
        return {"declared": False}
    value = extensions[EXTENSION_KEY]
    errors: list[str] = []
    if not isinstance(value, list):
        errors.append("/ type")
    else:
        if len(value) < FOLDERS_MIN:
            errors.append("/ minItems")
        if len(value) > FOLDERS_MAX:
            errors.append("/ maxItems")
        for index, folder in enumerate(value):
            at = "/%d" % index
            if not isinstance(folder, dict):
                errors.append(at + " type")
                continue
            for key in ("dir", "source"):
                if key not in folder:
                    errors.append(at + " required")
            if any(key not in ("dir", "source") for key in folder):
                errors.append(at + " additionalProperties")
            if "dir" in folder:
                if not isinstance(folder["dir"], str):
                    errors.append(at + "/dir type")
                elif not PATH.fullmatch(folder["dir"]):
                    errors.append(at + "/dir pattern")
            if "source" in folder:
                if not isinstance(folder["source"], str):
                    errors.append(at + "/source type")
                elif not ops_digest.SOURCE.fullmatch(folder["source"]):
                    errors.append(at + "/source pattern")
    if errors:
        return {"declared": True, "valid": False, "errors": errors}
    return {"declared": True, "valid": True, "folders": value}


def rules(descriptor: dict) -> list[dict]:
    """`FAC-SEM-168` on a service descriptor carrying the ops-digest key (`digestFoldersRules`)."""
    view = shape(descriptor)
    if not view["declared"]:
        return []
    findings: list[dict] = []
    paths = descriptor.get("paths")
    if descriptor.get("placement") == "remote":
        findings.append(_finding(FOLDERS_PATH, "a remote placement has no folder on this machine; the ops-digest key is "
                                               "a local descriptor's (DEC-0153 OD-9)"))
    elif not (isinstance(paths, dict) and isinstance(paths.get("data"), str)):
        findings.append(_finding(FOLDERS_PATH, "a dir is relative to paths.data, which this descriptor does not name "
                                               "(DEC-0153 OD-9)"))
    if not view["valid"]:
        findings.append(_finding(FOLDERS_PATH, "the ops-digest declaration is invalid (%s); a reader treats it as not "
                                               "declared and reports it (DEC-0153 OD-9)" % "; ".join(view["errors"])))
        return findings
    folders = view["folders"]
    hidden_class = {fold_dir(f["dir"]) for f in folders if _hidden_dir(f["dir"])}
    seen: set[str] = set()
    dirs: set[str] = set()
    for index, folder in enumerate(folders):
        at = "%s/%d" % (FOLDERS_PATH, index)
        dir_, source = folder["dir"], folder["source"]
        if source in seen:
            findings.append(_finding(at + "/source", "%s is named twice as a source; each folder sends its own label"
                                     % ops_digest.shown(source, "this source")))
        seen.add(source)
        folded = fold_dir(dir_)
        if folded in dirs:
            name = "this dir" if folded in hidden_class or not _PRINTABLE_ASCII.fullmatch(dir_) else json.dumps(dir_)
            findings.append(_finding(at + "/dir", "%s is named twice as a dir (compared under canonical caseless "
                                                  "matching (the code points HFS+ ignores removed, NFD, lower, upper and "
                                                  "lower case, NFC, Georgian Nuskhuri onto Mkhedruli), trailing dots and "
                                                  "spaces dropped); its lines would be sent twice" % name))
        dirs.add(folded)
        if ops_digest.secret_value(source):
            findings.append(_finding(at + "/source", "a source is shaped like a credential; it is a label, never a value"))
        if any(_empty_segment(part) for part in dir_.split("/")):
            findings.append(_finding(at + "/dir", EMPTY_SEGMENT))
        if _LONE_SURROGATE.search(dir_):
            findings.append(_finding(at + "/dir", "a dir holds a lone surrogate, which no file system can name; write it "
                                                  "as well-formed Unicode"))
        readings = _readings(dir_)
        if any(ops_digest.secret_value(r) for r in readings):
            run_only = not any(ops_digest.JWT_SHAPE.search(r) or ops_digest.SECRET_VALUE.search(r) for r in readings)
            findings.append(_finding(at + "/dir", LONG_RUN_DIR if run_only else
                                     "a dir is shaped like a credential; it names a folder, never a value"))
        elif any(ops_digest.has_home_path(r) for r in readings):
            findings.append(_finding(at + "/dir", "a dir holds a home folder; it is relative to the product's data folder"))
    return findings


def view(descriptor: Any) -> dict:
    """`digestFoldersView`: the folders a collector may read (`usable`), or why the declaration is not one."""
    found = shape(descriptor)
    if not found["declared"]:
        return found
    if not found["valid"]:
        return {**found, "usable": False}
    return {**found, "usable": not rules(descriptor)}


# ---- the collector ----------------------------------------------------------------------------------------------

def _expand(raw: str) -> Path:
    return Path(os.path.expanduser(raw))


def daily_lines(folder: Path) -> Iterable[tuple[str, Any]]:
    """Every line of every `daily-YYYY-MM.jsonl` in `folder`, oldest month first: ("line", doc) or ("skipped", why)."""
    try:
        names = sorted(e.name for e in os.scandir(folder) if DAILY_FILE.fullmatch(e.name) and e.is_file(follow_symlinks=False))
    except OSError:
        return
    for name in names:
        path = folder / name
        try:
            if path.stat().st_size > DAILY_FILE_MAX:
                yield "skipped", "file-too-large"
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            yield "skipped", "unreadable"
            continue
        # The piece after the last line feed is a line still being written (or nothing): it is read next pass.
        for raw in text.split("\n")[:-1]:
            if not raw.strip():
                continue
            try:
                yield "line", json.loads(raw)
            except ValueError:
                yield "skipped", "not-json"


def collect(descriptors: list[tuple[Path, dict]] | None = None, *,
            record: Callable[..., dict] | None = None,
            projects: Callable[[dict], set[str]] | None = None) -> dict:
    """One pass over the local descriptors' declared folders. Returns the receipt: what was read, recorded, repeated,
    skipped and why, and each declaration that was not usable with its findings."""
    import ops_digest_store
    if descriptors is None:
        import fabric_service
        descriptors = fabric_service.read_descriptors()
    record = record or ops_digest_store.record
    projects = projects or (lambda service: ops_digest_store.service_projects(service, None))
    receipt: dict = {"services": 0, "folders": 0, "recorded": 0, "repeated": 0, "skipped": {}, "refused": {},
                     "notDeclared": [], "unattributed": []}
    for _, descriptor in descriptors:
        if not isinstance(descriptor, dict):
            continue
        seen = view(descriptor)
        if not seen["declared"]:
            continue
        service = {"id": descriptor.get("id"), "instance": descriptor.get("instance", "default")}
        name = "%s.%s" % (service["id"], service["instance"])
        if not seen["usable"]:
            receipt["notDeclared"].append({"service": name, "findings": [f["instancePath"] for f in rules(descriptor)]
                                           or ["invalid"]})
            continue
        owners = projects(service)
        if len(owners) != 1:
            receipt["unattributed"].append({"service": name, "projects": len(owners)})
            continue
        (project,) = owners
        receipt["services"] += 1
        data = _expand(descriptor["paths"]["data"])
        for folder in seen["folders"]:
            where = data / folder["dir"]
            try:
                if where.is_symlink() or not where.resolve().is_relative_to(data.resolve()) or not where.is_dir():
                    continue
            except OSError:
                continue
            receipt["folders"] += 1
            days: set[str] = set()
            for kind, value in daily_lines(where):
                if kind == "skipped":
                    receipt["skipped"][value] = receipt["skipped"].get(value, 0) + 1
                    continue
                if not isinstance(value, dict) or value.get("schema") != ops_digest.OPS_DIGEST_DAILY:
                    receipt["skipped"]["other-schema"] = receipt["skipped"].get("other-schema", 0) + 1
                    continue
                if ops_digest.check("ops-digest-daily", value):
                    receipt["skipped"]["refused-line"] = receipt["skipped"].get("refused-line", 0) + 1
                    continue
                if value["day"] in days:                       # OD-6: of two lines for one day, the first
                    continue
                days.add(value["day"])
                try:
                    answer = record(project, service, folder["source"], ops_digest.normalized(value), "folder")
                except ops_digest_store.Refused as refused:
                    receipt["refused"][refused.code] = receipt["refused"].get(refused.code, 0) + 1
                    continue
                receipt["repeated" if answer.get("repeated") else "recorded"] += 1
    return receipt
