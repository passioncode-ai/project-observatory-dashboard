#!/usr/bin/env python3
"""What shape is a registry record? Derived from the data, never hand-listed.

`AGENTS.md` calls `registry/*.json` "typed facts", and until 2026-09-08 nothing
published those types. `docs/ARCHITECTURE.md` documents the store's tables and
the MCP tools; `fabric/schemas/*.json` describe the MCP output shapes; and
`tools/validate_registry.py` checks invariants — ids unique, domains lowercase,
the zone snapshot in step with `domains.json` — not the set of fields a record
carries. The shape lived only in the emitting code and in the data.

So every reader inferred it, and five inferences of mine were wrong inside two
iterations — `c["name"]` for `nwo`, `newest_on` for `unpushed_newest_on`,
`remote_head` where the code compares `branch_sha`, `rel["repository_id"]` for
`from`/`to`, and a bold `Статус:` label for a `- Status:` list item. Each time the
answer sat in a file I had edited minutes earlier. A lesson in the decision log
does not prevent the sixth; this repository's rule for that is a script.

**Generated, and that is the whole design.** A hand-written field table is the
next thing to drift — the lesson here about counts, dates, pointers and prose
alike. This derives the shape FROM the data, so it cannot disagree with it, and
`tools/check_docs.py` fails when the published copy is stale. `docs/AGENT_SYNC.md`
is the precedent: generated, stamped, checked.

**Every field carries how many records hold it.** `local.unpushed` sits on 15 of
177 repositories (measured 2026-09-08, and still exactly that — dated because an
undated true figure becomes an undated false one with nobody touching it), and a
list showing it beside `id` would teach a reader it is
always there — the same false confidence one level up. A count makes optionality
visible without a schema language.

**One level of nesting, no more.** `local` is where every one of my wrong guesses
lived, so it is described; going deeper would produce a document nobody reads
instead of the one page a reader needs. Where a value is a list, the element type
is named rather than walked.

    registry_shape.py            print the shape
    registry_shape.py --json     the same, machine-readable
    registry_shape.py --write    regenerate docs/REGISTRY_SHAPE.md
"""
from __future__ import annotations
import argparse
import hashlib
import json
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import atomic
import paths                                                                      

#: HOW TO REGENERATE, and it was written wrong for six iterations.
#: `./observatory.py shape --write` prints the shape and writes nothing:
#: `observatory.py`'s `main()` reads `argv[1]` and ignores the rest, so the flag
#: never reached this file. The instruction sat in this header, in
#: `tools/check_docs.py`'s failure message twice, and in a test's hint —
#: four copies of a command that does nothing, found when rule 14 fired on a
#: real change and the fix it recommended did not fix it.
WRITE_CMD = "`.venv/bin/python tools/registry_shape.py --write`"

DOC = ROOT / "docs/REGISTRY_SHAPE.md"

#: Blocks described one level down. `local` earned it by holding every field this
#: file's own author guessed wrong; the rest of a record is flat.
NESTED = ("local",)

#: How many example values a field shows. One is enough to recognise a shape and
#: short enough that the page stays a page.
EXAMPLES = 1


def _type_of(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        inner = {_type_of(v) for v in value[:8]}
        return f"list[{'|'.join(sorted(inner)) or 'empty'}]"
    if isinstance(value, dict):
        return "dict"
    return "null"


def _fields(records: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        for key, value in rec.items():
            slot = out.setdefault(key, {"type": set(), "present": 0, "example": None})
            slot["present"] += 1
            slot["type"].add(_type_of(value))
            if slot["example"] is None and value not in (None, "", [], {}):
                slot["example"] = value
    for slot in out.values():
        kinds = sorted(k for k in slot["type"] if k != "null") or ["null"]
        slot["type"] = "|".join(kinds)
        ex = slot.pop("example")
        slot["example"] = (json.dumps(ex, ensure_ascii=False)[:60]
                           if ex is not None else "")
    return dict(sorted(out.items()))


#: Lists a document carries ABOUT itself rather than as its records: where the
#: facts came from, and what could not be measured. On an estate with more
#: degradations than repositories, "the longest list" of `repositories.json` was
#: its `degraded` list, and the published shape described the notices instead of
#: the repositories.
NOT_RECORDS = ("degraded", "source_refs")


def record_key(name: str, doc: dict) -> str | None:
    """The key holding a document's records.

    The list named after the document wins (`repositories` in
    `repositories.json`). Otherwise the longest list that is not provenance or
    a degradation notice, and only then the longest list of all.
    """
    lists = [(k, v) for k, v in doc.items() if isinstance(v, list)]
    if not lists:
        return None
    stem = pathlib.Path(name).stem
    for k, _ in lists:
        if k == stem or k == stem.replace("-", "_"):
            return k
    records = [kv for kv in lists if kv[0] not in NOT_RECORDS] or lists
    return max(records, key=lambda kv: len(kv[1]))[0]


def shapes() -> dict:
    """One entry per registry document: record count, fields, nested blocks."""
    out: dict[str, dict] = {}
    for path in sorted(paths.REGISTRY.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # NAMED, not skipped: a document that will not parse is a fact about
            # the registry, and omitting it would publish a shape that claims
            # completeness it does not have.
            out[path.name] = {"unreadable": f"{type(exc).__name__}: {exc}"}
            continue
        if not isinstance(doc, dict):
            out[path.name] = {"records": 0, "fields": {}, "nested": {},
                              "note": f"top level is {_type_of(doc)}, not an object"}
            continue
        # THE LONGEST list, not the first. `domain-liveness.json` carries both
        # `source_refs` (a two-element provenance list) and `hosts` (the records),
        # and "first" picked the provenance and reported zero records — an
        # arbitrary rule producing a confident wrong answer, which is the defect
        # this whole file exists to reduce.
        key = record_key(path.name, doc)
        if key is None:
            out[path.name] = {"records": 1, "collection": "",
                              "fields": _fields([doc]), "nested": {}}
            continue
        records = [r for r in doc[key] if isinstance(r, dict)]
        nested: dict[str, dict] = {}
        for block in NESTED:
            inner = [r[block] for r in records
                     if isinstance(r.get(block), dict) and r[block]]
            if inner:
                nested[block] = _fields(inner)
        out[path.name] = {"records": len(records), "collection": key,
                          "fields": _fields(records), "nested": nested}
    return out


def stamp(data: dict) -> str:
    """A content hash of the SHAPE, not of the data.

    The registry changes on every tick and its shape almost never does, so
    hashing the data would make this document stale hourly — a chore rather than
    a check, which is the distinction rule 13 was written for.
    """
    skeleton = {
        name: {"records_present": bool(d.get("records")),
               "fields": {k: v["type"] for k, v in (d.get("fields") or {}).items()},
               "nested": {b: {k: v["type"] for k, v in f.items()}
                          for b, f in (d.get("nested") or {}).items()}}
        for name, d in data.items()}
    blob = json.dumps(skeleton, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def render(data: dict) -> str:
    lines = [f"<!-- generated by tools/registry_shape.py — data={stamp(data)} -->",
             "# The registry's shape",
             "",
             "**Generated. Do not edit** — " + WRITE_CMD + " rewrites it,",
             "and `tools/check_docs.py` fails when it is stale.",
             "",
             "`AGENTS.md` calls these documents typed facts, and nothing published the",
             "types until this file existed. Every field below carries how many records",
             "hold it, because an optional field listed beside a mandatory one teaches a",
             "reader it is always there.",
             ""]
    for name in sorted(data):
        d = data[name]
        lines.append(f"## `{name}`")
        lines.append("")
        if d.get("unreadable"):
            lines += [f"Does not parse: {d['unreadable']}", ""]
            continue
        head = f"{d['records']} record(s)"
        if d.get("collection"):
            head += f" under `{d['collection']}`"
        if d.get("note"):
            head += f" — {d['note']}"
        lines += [head, "", "| field | type | present | example |",
                  "|---|---|---|---|"]
        for key, slot in d["fields"].items():
            ex = slot["example"].replace("|", "\\|")
            lines.append(f"| `{key}` | `{slot['type']}` | {slot['present']}"
                         f" of {d['records']} | {ex} |")
        lines.append("")
        for block, fields in (d.get("nested") or {}).items():
            lines += [f"### `{block}`", "",
                      "| field | type | present | example |", "|---|---|---|---|"]
            total = max((s["present"] for s in fields.values()), default=0)
            for key, slot in fields.items():
                ex = slot["example"].replace("|", "\\|")
                lines.append(f"| `{key}` | `{slot['type']}` | {slot['present']}"
                             f" of {total} | {ex} |")
            lines.append("")
    lines.append(f"<!-- {datetime.now(timezone.utc).strftime('%Y-%m-%d')} -->")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="machine-readable")
    ap.add_argument("--write", action="store_true",
                    help="regenerate docs/REGISTRY_SHAPE.md")
    args = ap.parse_args(argv)
    data = shapes()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return 0
    if args.write:
        DOC.parent.mkdir(parents=True, exist_ok=True)
        atomic.write_text(DOC, render(data))
        print(f"wrote {DOC.relative_to(ROOT)} — data={stamp(data)}")
        return 0
    for name in sorted(data):
        d = data[name]
        if d.get("unreadable"):
            print(f"{name}: {d['unreadable']}")
            continue
        print(f"{name}: {d['records']} record(s)"
              + (f" under `{d['collection']}`" if d.get("collection") else ""))
        for key, slot in d["fields"].items():
            print(f"    {key:26} {slot['type']:14} {slot['present']:>4}/{d['records']}"
                  f"  {slot['example'][:40]}")
        for block, fields in (d.get("nested") or {}).items():
            total = max((s["present"] for s in fields.values()), default=0)
            print(f"  {name} · {block}:")
            for key, slot in fields.items():
                print(f"    {key:26} {slot['type']:14} {slot['present']:>4}/{total}"
                      f"  {slot['example'][:40]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
