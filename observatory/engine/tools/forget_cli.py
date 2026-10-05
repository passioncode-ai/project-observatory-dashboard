#!/usr/bin/env python3
"""The operator's door to erasure: forget one record, everywhere this machine can reach.

    project-observatory full forget MEMORY_ID --why TEXT
    project-observatory full forget MEMORY_ID --plan

`--plan` reads only and says what erasure would touch. Without it the record is withdrawn
(tombstoned) and its text erased from the ledger and from every copy the engine made, and a
receipt names each backend: erased, absent, or retained/unverified with the reason. Backups
taken earlier keep the text until they rotate, so the receipt says `complete: false` while
any exist. Erasing needs a terminal: an erasure is a person's decision. There is no --yes.

The rules and the receipt: docs/design/FORGET.md (PB-137 N-013).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _is_terminal() -> bool:
    return sys.stdin.isatty()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="project-observatory full forget",
                                description="Withdraw a record and erase its text, with a receipt.")
    p.add_argument("memory_id")
    p.add_argument("--why", help="the reason, recorded with the receipt (required to erase)")
    p.add_argument("--plan", action="store_true", help="say what would be touched; change nothing")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str]) -> int:
    a = parser().parse_args(argv)
    from store import db as store_db
    from store import forget as F
    conn = store_db.connect()
    try:
        if a.plan:
            out = F.plan(conn, a.memory_id)
        else:
            if not _is_terminal():
                raise SystemExit("forget: refusing to erase without a terminal. An erasure is the "
                                 "operator's decision, and a script must not be able to make it. "
                                 "Run it yourself; there is no --yes.")
            if not a.why:
                raise SystemExit("forget: --why is required to erase")
            out = F.forget(conn, a.memory_id, reason=a.why)
    except F.ForgetError as exc:
        raise SystemExit(f"forget: refused — {exc}") from None
    finally:
        conn.close()
    if a.json or a.plan:
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return 0
    print(f"{out['memoryId']}: withdrawn, text erased in the store: {out['erasedInStore']}; "
          f"complete: {out['complete']}")
    for b in out["backends"]:
        print(f"  {b['backend']}: {b['status']} — {b['detail']}")
    return 0 if out["erasedInStore"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
