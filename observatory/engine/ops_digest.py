"""Operations digests (fabric-agent-contract DEC-0153, release tag v0.1.0+2026-11-06.1): the engine's reading of a line.

A product keeps its own operations log and folds it, by arithmetic, into one `ops.digest.hourly@1` line for each hour
it observed itself running in and one `ops.digest.daily@1` line for each closed UTC day. Counts only: no log line, no
path, no identifier. `ops.digest.record` (`ops_digest_store.py`, OBS-330) takes daily lines from products; this module
is what decides whether a line is one.

PORTED, NOT INVENTED. Everything here is the contract's own code at that tag, rule for rule
(`src/ops-digest.ts`, `src/secret-shape.ts` `secretValue`, `src/activity-rules.ts` `hasHomePath`), and the vendored
copies in `fabric/ops-digest/` (schemas, vectors, fixtures) are what `tests/test_ops_digest.py` holds it to:

- `check(kind, value)`: the closed schema (`fabric/ops-digest/*.schema.json`), OD-2's name grammar read the way
  JavaScript reads it, then `FAC-SEM-166` (the window and the arithmetic) and `FAC-SEM-167` (no operation's name shaped
  like a credential or holding a home folder). An empty list is a line every reader takes.
- `fold_hour`, `fold_day`: the reference folds, run over the contract's vectors, so the engine's reading of a line
  agrees with the contract's (the contract's `foldHour` and `foldDay`).
- `source_refused(source)`: the one clause of `FAC-SEM-168` a receiver applies, a `source` label shaped like a
  credential. The rest of that rule (a descriptor's folder declaration) is `ops_digest_folders.py`'s, which reads
  the declared folders since OBS-533.

WHERE PYTHON AND JAVASCRIPT DIFFER, the JavaScript reading wins, because the contract's kit is JavaScript:

- `$` in a JavaScript pattern without the `m` flag is the end of the input; Python's `$` also matches before a final
  line feed. The schema's `pattern` keyword is run by `jsonschema` with Python's `re.search`, so an operation named
  `x` followed by a line feed would pass it here and fail there. `check` therefore reads every name again with
  `fullmatch` (and every window with `whole_hour` and `real_day`, which also use `fullmatch`).
- `\\s` is spelled with ECMAScript's own set (`delegation.JS_SPACE`), and every case-insensitive pattern carries
  `re.ASCII`, so `re.IGNORECASE` does not let U+017F or U+212A stand for `s` or `k` as Python's Unicode folding would.
- A count is an integer from 0 to 2^53 - 1. JSON has one number type: `3.0` is the integer JavaScript's
  `Number.isSafeInteger` takes, so it is one here too, and `normalized` writes it back as `3`.

A message never copies a value shaped like a credential, holding a home folder or not printable ASCII: it names the
place instead (OD-3).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from delegation import JS_SPACE

ROOT = Path(__file__).resolve().parent
#: The vendored contract files (fabric/ops-digest/README.md names the tag, the commit and the digests).
VENDORED = ROOT / "fabric" / "ops-digest"

OPS_DIGEST_HOURLY = "ops.digest.hourly@1"
OPS_DIGEST_DAILY = "ops.digest.daily@1"
KINDS = {"ops-digest-hourly": OPS_DIGEST_HOURLY, "ops-digest-daily": OPS_DIGEST_DAILY}
#: OD-4: a rise is at least this many failures in the hour, and at least this many times the hour before's (an
#: hour before with none, or none recorded, counts as one).
RISE_MIN_FAILED = 10
RISE_FACTOR = 2
#: OD-2: an operation's name.
OP_NAME = re.compile(r"[a-z][A-Za-z0-9_]*(?:[.:/-][A-Za-z0-9_]+)*")
OP_NAME_MAX = 64
#: OD-3: at most this many operations in a digest's `ops` (and so in `risen`).
OPS_MAX = 4096
MAX_COUNT = 2 ** 53 - 1
#: The `source` label a collector sends with each daily line (OD-9).
SOURCE = re.compile(r"[a-z][a-z0-9-]{0,31}")

_HOUR = timedelta(hours=1)
_WHOLE_HOUR = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):00:00(?:\.000)?Z")
_DAY = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")

# ── the shared detectors (`src/secret-shape.ts`, `src/activity-rules.ts`) ─────────────────────────────────────

#: A JWT or JWS anywhere in a value: three base64url segments, the first a JSON object's `eyJ`. Case-sensitive.
JWT_SHAPE = re.compile(r"eyJ[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]*")
#: A value that says it is a credential, or a provider key's shape, in either case. JavaScript's `.` is any code unit
#: but a line terminator, written out here; `re.ASCII` keeps the case folding to ASCII, as JavaScript's (no `u` flag).
SECRET_VALUE = re.compile(
    r"(\Abearer[" + JS_SPACE + r"][^\n\r  ]|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|xox[abposr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|glpat-[A-Za-z0-9_-]{20,})", re.IGNORECASE | re.ASCII)
_LONG_RUN = re.compile(r"[A-Za-z0-9+/=_-]{40,}")


def long_secret(value: str) -> bool:
    """A run of 40 or more base64 or base64url characters that mixes upper case, lower case and a digit."""
    return any(re.search(r"[a-z]", run) and re.search(r"[A-Z]", run) and re.search(r"[0-9]", run)
               for run in _LONG_RUN.findall(value))


def secret_value(value: str) -> bool:
    """`secretValue`: a JWT anywhere, a provider key or `Bearer ...`, or a long secret-shaped run."""
    return bool(JWT_SHAPE.search(value) or SECRET_VALUE.search(value) or long_secret(value))


_SLASH = r"(?:[/\\]|%(?:25)?2[fF])"
#: `HOME_PATH`: the user-name segment form of a home folder (the boundary docs/specification/ops-digest.md states
#: for FAC-SEM-167). `\Z`, not `$`: JavaScript's `$` is the end of the input.
HOME_PATH = (
    re.compile(r"(?:\A|[^A-Za-z0-9]|%(?:25)?2[fF])(?:[Uu]sers|home)" + _SLASH + r"[A-Za-z0-9_]"),
    re.compile(r"(?:\A|[^A-Za-z0-9])-(?:[Uu]sers|home)-[A-Za-z0-9]"),
    re.compile(r"(?:\A|[" + JS_SPACE + r"\"'=/\\])~(?:[A-Za-z_][A-Za-z0-9_.-]*)?(?:[/\\]|\Z)"),
)


def has_home_path(value: str) -> bool:
    return any(pattern.search(value) for pattern in HOME_PATH)


def hidden(value: str) -> bool:
    """Shaped like a credential, or holding a home folder: a message names its place, never the value."""
    return secret_value(value) or has_home_path(value)


_PRINTABLE_ASCII = re.compile(r"[\x20-\x7e]+")


def shown(value: Any, instead: str) -> str:
    return value if isinstance(value, str) and _PRINTABLE_ASCII.fullmatch(value) and not hidden(value) else instead


def op_shown(op: str) -> str:
    return shown(op, "this operation") if isinstance(op, str) and op_grammar(op) else "this operation"


def op_grammar(op: Any) -> bool:
    """OD-2's grammar, as JavaScript reads `^...$` on the whole name, at most 64 characters."""
    return isinstance(op, str) and len(op) <= OP_NAME_MAX and OP_NAME.fullmatch(op) is not None


def name_refusal(op: Any) -> str | None:
    """What OD-2 refuses in an operation's name, its grammar or FAC-SEM-167; None for a lawful name."""
    if not op_grammar(op):
        return "fails OD-2's name grammar"
    return "is refused by FAC-SEM-167" if hidden(op) else None


def source_refused(source: Any) -> str | None:
    """A `source` label: its grammar (OD-9), and `FAC-SEM-168`'s clause that it is not shaped like a credential
    (a lower-case label can still hold `sk-` and sixteen more characters). None for a lawful label."""
    if not isinstance(source, str) or SOURCE.fullmatch(source) is None:
        return "a source is a lower-case label: a letter, then up to 31 letters, digits or '-'"
    if secret_value(source):
        return "a source is shaped like a credential; it is a label, never a value (FAC-SEM-168)"
    return None


# ── the window and the counts ─────────────────────────────────────────────────────────────────────────────────

def whole_hour(value: Any) -> datetime | None:
    """The instant of a whole UTC hour in the schema's form, or None for anything else (a 25th hour, 30 February)."""
    match = _WHOLE_HOUR.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    year, month, day, hour = (int(g) for g in match.groups())
    try:
        return datetime(year, month, day, hour, tzinfo=timezone.utc)
    except ValueError:
        return None


def real_day(value: Any) -> bool:
    """Whether `value` is a real `YYYY-MM-DD` UTC day."""
    return isinstance(value, str) and _DAY.fullmatch(value) is not None and whole_hour(f"{value}T00:00:00Z") is not None


def iso(at: datetime) -> str:
    """As `Date.prototype.toISOString` writes an instant: milliseconds and `Z`."""
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def count(value: Any) -> int | None:
    """An integer from 0 to 2^53 - 1 as JavaScript's `Number.isSafeInteger` reads a JSON number, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    if isinstance(value, int) and 0 <= value <= MAX_COUNT:
        return value
    return None


def is_rise(failed: int, previous: int) -> bool:
    """OD-4."""
    return failed >= RISE_MIN_FAILED and failed >= RISE_FACTOR * max(1, previous)


COUNTERS = ("ok", "failed", "unknown")


def _sum(ops: Iterable[dict]) -> dict:
    total = {"ok": 0, "failed": 0, "unknown": 0}
    for c in ops:
        for k in COUNTERS:
            total[k] += c[k]
    return total


def _safe_counts(digest: dict) -> dict:
    counts = [digest["total"][k] for k in COUNTERS] + [c[k] for c in digest["ops"] for k in COUNTERS]
    for r in digest["risen"]:
        counts += [r["failed"]] + ([r["previous"]] if "previous" in r else [])
    if not all(count(n) is not None for n in counts):
        raise ValueError("a sum is beyond 2^53 - 1")
    return digest


# ── the reference folds (`foldHour`, `foldDay`) ───────────────────────────────────────────────────────────────

def fold_hour(counts: list[dict], start: str, previous: dict | None) -> dict:
    """The digest of the hour starting at `start`: `counts` summed by operation and sorted by name, each operation
    whose failures rose against `previous` (only when it is the hour just before; a gap compares with none), `from`
    and `to` written as `toISOString` writes them. Raises ValueError where the contract's `foldHour` throws."""
    at = whole_hour(start)
    if at is None:
        raise ValueError(f"{shown(start, 'from')} is not a whole UTC hour")
    if not isinstance(counts, list) or not all(isinstance(c, dict) for c in counts):
        raise ValueError("counts is not a list of objects")
    if whole_hour(iso(at + _HOUR)) is None:
        raise ValueError(f"the hour after {shown(start, 'from')} is not a whole UTC hour the schema writes")
    merged: dict[str, dict] = {}
    for index, c in enumerate(counts):
        refused = name_refusal(c.get("op"))
        if refused is not None:
            raise ValueError(f"the operation at {index} {refused}: no hourly digest (OD-3)")
        values = [count(c.get(k)) for k in COUNTERS]
        if None in values:
            raise ValueError(f"a count of {shown(c['op'], f'the operation at {index}')} is not an integer from 0 "
                             f"to 2^53 - 1")
        m = merged.setdefault(c["op"], {"op": c["op"], "ok": 0, "failed": 0, "unknown": 0})
        for k, v in zip(COUNTERS, values):
            m[k] += v
    if len(merged) > OPS_MAX:
        raise ValueError(f"the hour counts {len(merged)} operations, more than {OPS_MAX}: no hourly digest (OD-3)")
    ops = sorted(merged.values(), key=lambda c: c["op"])
    if previous is not None and not (isinstance(previous, dict) and isinstance(previous.get("to"), str)):
        raise ValueError("the previous hour is neither absent nor a digest with a string to")
    adjoining = previous["ops"] if previous is not None and whole_hour(previous["to"]) == at else []
    if not isinstance(adjoining, list) or not all(isinstance(c, dict) for c in adjoining):
        raise ValueError("the previous hour's ops is not a list of objects")
    before: dict[str, Any] = {}
    for index, c in enumerate(adjoining):
        if not isinstance(c.get("op"), str):
            raise ValueError(f"the operation at {index} of the previous hour's ops has no string name")
        if c["op"] in before:
            raise ValueError(f"{shown(c['op'], f'the operation at {index}')} is named twice in the previous hour's ops")
        before[c["op"]] = c.get("failed")
    for c in ops:
        if c["op"] in before and count(before[c["op"]]) is None:
            raise ValueError(f"the previous hour's failures of {shown(c['op'], 'an operation')} are not an integer "
                             f"from 0 to 2^53 - 1")
    risen = [{"op": c["op"], "failed": c["failed"], "previous": count(before.get(c["op"], 0))}
             for c in ops if is_rise(c["failed"], count(before.get(c["op"], 0)))]
    return _safe_counts({"schema": OPS_DIGEST_HOURLY, "from": iso(at), "to": iso(at + _HOUR), "ops": ops,
                         "total": _sum(ops), "risen": risen})


def fold_day(day: str, hourly: list[dict]) -> dict | None:
    """The day's hourly digests (those whose hour starts on `day`, the first of a repeated hour kept) folded into one,
    or None for a day with no hour of its own (OD-5). Deciding that `day` is closed is the caller's."""
    if not real_day(day):
        raise ValueError(f"{shown(day, 'day')} is not a real UTC day")
    if not isinstance(hourly, list) or not all(isinstance(h, dict) for h in hourly):
        raise ValueError("hourly is not a list of objects")
    seen: set[datetime] = set()
    mine = []
    for h in hourly:
        at = whole_hour(h.get("from"))
        if at is None or iso(at)[:10] != day or at in seen:
            continue
        seen.add(at)
        mine.append(h)
    if not mine:
        return None
    merged: dict[str, dict] = {}
    rose: dict[str, dict] = {}
    for h in mine:
        where = shown(h["from"], "an hour")
        for key in ("ops", "risen"):
            if not isinstance(h.get(key), list) or not all(isinstance(c, dict) for c in h[key]):
                raise ValueError(f"{where}'s {key} is not a list of objects")
            for index, c in enumerate(h[key]):
                refused = name_refusal(c.get("op"))
                if refused is not None:
                    raise ValueError(f"the operation at {index} of {where}'s {key} {refused}: no daily digest (OD-3)")
        for c in h["ops"]:
            values = [count(c.get(k)) for k in COUNTERS]
            if None in values:
                raise ValueError(f"a count of {shown(c['op'], 'an operation')} in {where} is not an integer from 0 "
                                 f"to 2^53 - 1")
            m = merged.setdefault(c["op"], {"op": c["op"], "ok": 0, "failed": 0, "unknown": 0})
            for k, v in zip(COUNTERS, values):
                m[k] += v
        named: set[str] = set()
        for r in h["risen"]:
            if r["op"] in named:
                raise ValueError(f"{shown(r['op'], 'an operation')} is named twice in the risen of {where}")
            named.add(r["op"])
            failed = count(r.get("failed"))
            if failed is None:
                raise ValueError(f"the rise of {shown(r['op'], 'an operation')} in {where} is not an integer "
                                 f"from 0 to 2^53 - 1")
            if not any(c["op"] == r["op"] for c in h["ops"]):
                raise ValueError(f"{shown(r['op'], 'an operation')} rose in {where} but that hour's ops does not "
                                 f"count it")
            x = rose.setdefault(r["op"], {"hours": 0, "failed": 0})
            x["hours"] += 1
            x["failed"] += failed
    if len(merged) > OPS_MAX:
        raise ValueError(f"the day folds {len(merged)} operations, more than {OPS_MAX}: no daily digest (OD-3)")
    ops = sorted(merged.values(), key=lambda c: c["op"])
    risen = sorted(({"op": op, **x} for op, x in rose.items()), key=lambda r: (-r["failed"], r["op"]))
    return _safe_counts({"schema": OPS_DIGEST_DAILY, "day": day, "hours": len(mine), "ops": ops,
                         "total": _sum(ops), "risen": risen})


# ── FAC-SEM-166 and FAC-SEM-167 (`opsDigestRules`) ────────────────────────────────────────────────────────────

def _indexed(items: Any):
    if isinstance(items, list):
        for index, entry in enumerate(items):
            if isinstance(entry, dict):
                yield entry, index


def rules(kind: str, value: dict) -> list[dict]:
    """FAC-SEM-166 and FAC-SEM-167 on one digest of either kind, as findings `{code, instancePath, message}`."""
    if kind not in KINDS:
        raise ValueError(f"{kind} is not an operations digest's kind")
    findings: list[dict] = []

    def window(path: str, message: str) -> None:
        findings.append({"code": "FAC-SEM-166", "instancePath": path, "message": message})

    def named(path: str, op: str) -> None:
        if secret_value(op):
            findings.append({"code": "FAC-SEM-167", "instancePath": path,
                             "message": "an operation's name is shaped like a credential; it names an operation, "
                                        "never a value"})
        elif has_home_path(op):
            findings.append({"code": "FAC-SEM-167", "instancePath": path,
                             "message": "an operation's name holds a home folder; it names an operation, never a path"})

    if kind == "ops-digest-hourly":
        start, end = whole_hour(value.get("from")), whole_hour(value.get("to"))
        if start is None:
            window("/from", "from is a whole UTC hour of a real day")
        if end is None:
            window("/to", "to is a whole UTC hour of a real day")
        if start is not None and end is not None and end - start != _HOUR:
            window("/to", "to is exactly one hour after from")
    elif not real_day(value.get("day")):
        window("/day", "day is a real UTC day, YYYY-MM-DD")

    by_op: dict[str, dict] = {}
    totals: dict[str, int | None] = {"ok": 0, "failed": 0, "unknown": 0}
    for entry, index in _indexed(value.get("ops")):
        op = entry.get("op")
        if isinstance(op, str):
            if op in by_op:
                window(f"/ops/{index}/op", f"{op_shown(op)} is counted once")
            else:
                by_op[op] = entry
            named(f"/ops/{index}/op", op)
        for k in COUNTERS:
            n = count(entry.get(k))
            totals[k] = None if n is None or totals[k] is None else totals[k] + n
    total = value.get("total") if isinstance(value.get("total"), dict) else None
    if total is not None and isinstance(value.get("ops"), list):
        for k in COUNTERS:
            want = totals[k]
            if want is None or count(total.get(k)) is None:
                continue
            if want > MAX_COUNT:
                window(f"/total/{k}", f"the sum of ops' {k} is beyond 2^53 - 1, so no total.{k} can be it")
            elif count(total.get(k)) != want:
                window(f"/total/{k}", f"total.{k} is the sum of ops' {k}, {want}")

    rose: set[str] = set()
    hours = count(value.get("hours"))
    for entry, index in _indexed(value.get("risen")):
        op = entry.get("op")
        if not isinstance(op, str):
            continue
        at = f"/risen/{index}"
        named(f"{at}/op", op)
        if op in rose:
            window(f"{at}/op", f"{op_shown(op)} is named once among the rises")
        rose.add(op)
        counted = by_op.get(op)
        if counted is None:
            window(f"{at}/op", f"{op_shown(op)} rose, so ops counts it")
            continue
        failed, counted_failed = count(entry.get("failed")), count(counted.get("failed"))
        if kind == "ops-digest-hourly":
            previous = count(entry.get("previous"))
            if failed is not None and counted_failed is not None and failed != counted_failed:
                window(f"{at}/failed", f"a rise names the hour's failures of {op_shown(op)}, {counted_failed}")
            if failed is not None and previous is not None and not is_rise(failed, previous):
                window(at, f"{failed} failures against {previous} is no rise: at least {RISE_MIN_FAILED} and "
                           f"{RISE_FACTOR} times the hour before (none counts as one)")
        else:
            risen_hours = count(entry.get("hours"))
            if risen_hours is not None and hours is not None and risen_hours > hours:
                window(f"{at}/hours", f"{op_shown(op)} rose in {risen_hours} hours; the day folds {hours}")
            if failed is not None and risen_hours is not None and failed < RISE_MIN_FAILED * risen_hours:
                window(f"{at}/failed", f"{risen_hours} hours of rises hold at least {RISE_MIN_FAILED * risen_hours} "
                                       f"failures")
            if failed is not None and counted_failed is not None and failed > counted_failed:
                window(f"{at}/failed", f"the rises' failures are at most the day's failures of {op_shown(op)}, "
                                       f"{counted_failed}")
    return findings


# ── the whole check: the schema, the grammar, the rules ───────────────────────────────────────────────────────

_VALIDATORS: dict[str, Any] = {}


def validator(kind: str):
    """A validator for one kind against the vendored schemas, `$ref`s resolved locally (never fetched)."""
    if kind not in _VALIDATORS:
        import jsonschema
        from referencing import Registry, Resource
        docs = [json.loads((VENDORED / name).read_text(encoding="utf-8"))
                for name in ("ops-digest-hourly.schema.json", "ops-digest-daily.schema.json")]
        registry = Registry().with_resources([(d["$id"], Resource.from_contents(d)) for d in docs])
        schema = docs[0] if kind == "ops-digest-hourly" else docs[1]
        _VALIDATORS[kind] = jsonschema.Draft202012Validator(schema, registry=registry)
    return _VALIDATORS[kind]


def _pointer(path: Iterable[Any]) -> str:
    return "".join(f"/{str(p).replace('~', '~0').replace('/', '~1')}" for p in path) or "/"


def check(kind: str, value: Any) -> list[dict]:
    """Every reason a reader skips this line (OD-6), or [] for a line every reader takes.

    Schema failures come first, as `{code: "schema", instancePath, message}` naming the keyword, never the value; a
    line the schema refuses is not read further, as the contract's validator does not run its rules on it. Then
    OD-2's grammar on each name, as JavaScript reads it (see the module's note on `$`), then FAC-SEM-166/167."""
    if kind not in KINDS:
        raise ValueError(f"{kind} is not an operations digest's kind")
    schema_errors = sorted(validator(kind).iter_errors(value), key=lambda e: list(e.absolute_path))
    if schema_errors:
        return [{"code": "schema", "instancePath": _pointer(e.absolute_path),
                 "message": f"fails the schema's {e.validator}"} for e in schema_errors[:20]]
    grammar = []
    for key in ("ops", "risen"):
        for entry, index in _indexed(value.get(key)):
            if not op_grammar(entry.get("op")):
                grammar.append({"code": "schema", "instancePath": f"/{key}/{index}/op",
                                "message": "fails OD-2's name grammar"})
    for key in ("from", "to", "day"):
        raw = value.get(key)
        if isinstance(raw, str) and raw.endswith("\n"):
            grammar.append({"code": "schema", "instancePath": f"/{key}", "message": "fails the schema's pattern"})
    return grammar or rules(kind, value)


def normalized(value: dict) -> dict:
    """A line that passed `check`, every count written as an integer (a JSON `3.0` is the count 3)."""
    def ints(entry: dict) -> dict:
        return {k: (v if k == "op" else count(v)) for k, v in entry.items()}
    out = {**value, "ops": [ints(c) for c in value["ops"]], "total": ints(value["total"]),
           "risen": [ints(r) for r in value["risen"]]}
    if "hours" in value:
        out["hours"] = count(value["hours"])
    return out
