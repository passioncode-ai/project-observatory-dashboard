#!/usr/bin/env python3
"""Silence a finding, on the record: one verb, and the JSON is never edited by hand.

    tools/ack.py <finding-id> --why "..." [--until YYYY-MM-DD] [--by operator]
    tools/ack.py --undo <finding-id>
    tools/ack.py --list

Why a tool. When the only way to add an acknowledgement is to open the file,
know the finding's exact id and type a JSON object by hand, nobody does it, so
the board keeps showing rows the operator has already judged, and "show me
what I silenced, and only what I silenced" has no surface at all. A verb that
takes the id and a reason is the difference.

What an ack is NOT. It does not fix, delete or hide the cause: the finding is
withheld from the page and the counts while the ack stands, and the "silenced"
section shows it with the reason, because silencing that nobody can see later
is the silence this repository refuses. `--until` makes it speak again on a
date; without it the ack holds for as long as the cause does. A finding whose
id the board does not carry today is refused: an ack for a row that does not
exist is a typo waiting to hide something else.
"""
from __future__ import annotations
import argparse
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import atomic                                                                   
import paths                                                                    


def today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


class AckStoreError(ValueError):
    """The acknowledgement store is unreadable or malformed, so nothing is written over it."""


def load_acks() -> dict:
    try:
        doc = json.loads(paths.FINDING_ACKS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"note": "", "acks": []}
    except (OSError, ValueError):
        raise AckStoreError from None
    if not isinstance(doc, dict) or not isinstance(doc.get("acks"), list):
        raise AckStoreError
    ids = []
    for row in doc["acks"]:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not row["id"].strip() or not isinstance(row.get("why"), str)
                or not row["why"].strip()):
            raise AckStoreError
        ids.append(row["id"])
        if row.get("until") is not None:
            try:
                datetime.date.fromisoformat(row["until"])
            except (TypeError, ValueError):
                raise AckStoreError from None
    if len(ids) != len(set(ids)):
        raise AckStoreError
    return doc


def save_acks(doc: dict) -> None:
    doc["acks"] = sorted(doc.get("acks") or [], key=lambda a: a["id"])
    atomic.write_text(paths.FINDING_ACKS, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")


def board_ids() -> dict[str, dict]:
    try:
        doc = json.loads((paths.REGISTRY / "findings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {f["id"]: f for f in doc.get("findings", [])}


def cmd_ack(a) -> int:
    if not (a.why or "").strip():
        print("refused: --why is required — a silenced finding with no reason is "
              "indistinguishable from one nobody understood", file=sys.stderr)
        return 2
    if a.until:
        try:
            if datetime.date.fromisoformat(a.until) < datetime.date.fromisoformat(today()):
                print(f"refused: --until {a.until} is already past — the finding would "
                      f"speak again immediately", file=sys.stderr)
                return 2
        except ValueError:
            print(f"refused: --until must be YYYY-MM-DD, got {a.until!r}", file=sys.stderr)
            return 2
    ids = board_ids()
    if a.id not in ids:
        near = [i for i in ids if a.id.split(":", 1)[0] == i.split(":", 1)[0]][:5]
        print(f"refused: the board carries no finding {a.id!r}"
              + (f" — same type: {', '.join(near)}" if near else "")
              + "\n  (ids are `<type>:<subject>`, as registry/findings.json prints them)",
              file=sys.stderr)
        return 1
    doc = load_acks()
    rows = [r for r in doc.get("acks") or [] if r.get("id") != a.id]
    rows.append({"id": a.id, "why": a.why.strip(), "by": a.by, "on": today(),
                 **({"until": a.until} if a.until else {})})
    doc["acks"] = rows
    save_acks(doc)
    f = ids[a.id]
    print(f"silenced: {a.id}\n  [{f['severity']}] {f['title'][:90]}\n"
          f"  until {a.until or 'its cause is gone'} — reason: {a.why.strip()[:100]}\n"
          f"  the board withholds it from the next build; `project-observatory full findings` now, "
          f"or the tick within 30 minutes")
    return 0


def cmd_undo(a) -> int:
    doc = load_acks()
    before = len(doc.get("acks") or [])
    doc["acks"] = [r for r in doc.get("acks") or [] if r.get("id") != a.undo]
    if len(doc["acks"]) == before:
        print(f"nothing silenced under {a.undo!r}", file=sys.stderr)
        return 1
    save_acks(doc)
    print(f"unsilenced: {a.undo} — it speaks again at the next build")
    return 0


def cmd_list() -> int:
    rows = load_acks().get("acks") or []
    ids = board_ids()
    if not rows:
        print("nothing silenced")
        return 0
    for r in sorted(rows, key=lambda r: r["id"]):
        live = r["id"] in ids
        lapsed = bool(r.get("until")) and r["until"] < today()
        state = "lapsed" if lapsed else ("silenced" if live else "cause gone")
        print(f"  {state:10s} {r['id']}\n             {r.get('why', '')[:100]}"
              f"  — {r.get('by', '?')}, {r.get('on', '?')}"
              + (f", until {r['until']}" if r.get("until") else ""))
    print(f"{len(rows)} ack(s); `--undo <id>` to let one speak again")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("id", nargs="?", help="the finding id, `<type>:<subject>`")
    ap.add_argument("--why", help="why this is silenced — required")
    ap.add_argument("--until", help="YYYY-MM-DD after which it speaks again")
    ap.add_argument("--by", default="operator", help="who decided (default: operator)")
    ap.add_argument("--undo", metavar="ID", help="let a silenced finding speak again")
    ap.add_argument("--list", action="store_true", help="what is silenced, and why")
    a = ap.parse_args(argv[1:])
    try:
        if a.list:
            return cmd_list()
        if a.undo:
            return cmd_undo(a)
        if not a.id:
            ap.print_usage()
            return 2
        return cmd_ack(a)
    except AckStoreError:
        print("Saved acknowledgements could not be read; no changes were written. Repair the file or restore a known good copy.", file=sys.stderr)
        print(f"  {paths.FINDING_ACKS}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
