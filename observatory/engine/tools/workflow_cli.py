#!/usr/bin/env python3
"""The operator's view of agent workflows, and the two acts only the operator may do.

    project-observatory full workflow list [--project ID] [--status open|closed|all] [--json]
    project-observatory full workflow show WORKFLOW_ID [--json]
    project-observatory full workflow handoff WORKFLOW_ID --to-provider PROVIDER --reason REASON [--force]
    project-observatory full workflow close WORKFLOW_ID --why WHY

`handoff` also takes `--to-model` and `--to-account`; REASON is limit, crash, restart,
operator or plan_route, and WHY says what happened, in words.

`list` and `show` read. `handoff` without `--force` follows the agents' rule: a
`limit`, `crash` or `restart` handoff once the executor has been silent. With
`--force` it is the operator taking the workflow, for any reason, and the pack
records `operator-force`; `close` ends a workflow nobody will continue. Both
need a terminal: they act with the operator's authority, which a script must
not be able to mint — the same rule `tools/review.py` keeps.

Nothing here prints a lease token; reading a workflow never hands out the right
to write it.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import secrets
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from store import db as store_db                                                   # noqa: E402
from store import ledger as L                                                      # noqa: E402
from store import workflow as W                                                    # noqa: E402

#: The identity a non-forced handoff is created under from this command.
CLI_OWNER = "service:observatory-cli"


def _is_terminal() -> bool:
    return sys.stdin.isatty()


def require_terminal(action: str) -> None:
    if not _is_terminal():
        raise SystemExit(
            f"workflow: refusing to {action} without a terminal. This acts with the operator's "
            f"authority, and minting that from a script would let anything with shell access "
            f"forge it. Run it yourself; there is no --yes.")


def _age(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h"


def cmd_list(args, conn) -> int:
    out = W.workflow_list(conn, project_id=args.project, status=args.status, limit=args.limit)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    if not out["workflows"]:
        print(f"no {args.status if args.status != 'all' else ''} workflow"
              + (f" in {args.project}" if args.project else "")
              + ". An agent starts one with observatory_checkpoint_write.")
        return 0
    for w in out["workflows"]:
        step = w["step"] or {}
        who = (w["lease"] or {}).get("executor") or {}
        executor = "/".join(x for x in (who.get("provider"), who.get("model"),
                                         who.get("accountRef")) if x) or "—"
        flags = []
        if w["pendingHandoff"]:
            flags.append("handoff waiting")
        if w["keptSteps"]:
            flags.append(f"{w['keptSteps']} kept step(s)")
        print(f"{w['workflowId']}  {w['status']:<6}  {w['projectId'] or '—'}")
        print(f"    step {step.get('stepId', '—')} {step.get('status') or ''}, "
              f"{_age(w['silentSeconds'])} ago · {executor} · {w['handoffs']} handoff(s)"
              + (f" · {', '.join(flags)}" if flags else ""))
        print(f"    {w['goal']}")
    print(f"\n{out['count']} of {out['total']}"
          + (" — more with --limit" if out.get("nextCursor") else ""))
    return 0


def cmd_show(args, conn) -> int:
    out = W.checkpoint_latest(conn, args.workflow_id)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    ck = out["checkpoint"] or {}
    body = ck.get("body") or {}
    print(f"{out['workflowId']}  {out['status']}  {out['projectId']}")
    print(f"goal: {body.get('goal', '—')}")
    print(f"step: {ck.get('stepId', '—')} {ck.get('status') or ''} (revision "
          f"{ck.get('revision', '—')}, {ck.get('createdAt', '—')}, by {ck.get('writtenBy', '—')})")
    for c in body.get("constraints", []):
        print(f"  constraint: {c}")
    for o in body.get("open", []):
        print(f"  open {o['step_id']}: {o['next_action']}")
    lease = out["lease"]
    print("executor: " + (json.dumps(lease["executor"]) + f" since {lease['grantedAt']}"
                          if lease else "none"))
    if out["pendingHandoff"]:
        print(f"handoff waiting: {out['pendingHandoff'].get('handoffId')} until "
              f"{out['pendingHandoff'].get('expiresAt')}")
    if out.get("lapsedHandoff"):
        print(f"handoff lapsed: {out['lapsedHandoff'].get('handoffId')}")
    for c in out.get("credentials", []):
        print(f"  key {c['project']}/{c['env']}/{c['name']}: {c['state']}")
    return 0


def cmd_handoff(args, conn) -> int:
    if args.force:
        require_terminal("force a handoff")
    owner = L.OPERATOR if args.force else CLI_OWNER
    to = {k: v for k, v in (("provider", args.to_provider), ("model", args.to_model),
                            ("accountRef", args.to_account)) if v}
    out = W.handoff_create(conn, owner=owner, idempotency_key=f"cli-{secrets.token_hex(8)}",
                           workflow_id=args.workflow_id, to=to, reason=args.reason,
                           force=args.force)
    print(f"handoff {out['handoffId']} offered until {out['expiresAt']}"
          + (" — a declared credential is not in the vault" if out.get("credentialsMissing")
             else ""))
    print("the next session accepts it with observatory_handoff_accept")
    return 0


def cmd_close(args, conn) -> int:
    require_terminal("close a workflow")
    out = W.close_workflow(conn, workflow_id=args.workflow_id, by=L.OPERATOR, why=args.why,
                           idempotency_key=f"cli-{secrets.token_hex(8)}")
    print(f"closed {out['workflowId']} at {out['closedAt']}; {out['leasesEnded']} lease(s) ended")
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="project-observatory full workflow",
                                 description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="workflows, newest first")
    p.add_argument("--project")
    p.add_argument("--status", choices=("open", "closed", "all"), default="open")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("show", help="one workflow: its checkpoint, executor and keys")
    p.add_argument("workflow_id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("handoff", help="offer a workflow to another executor")
    p.add_argument("workflow_id")
    p.add_argument("--to-provider", required=True)
    p.add_argument("--to-model")
    p.add_argument("--to-account")
    p.add_argument("--reason", required=True, choices=W.REASONS)
    p.add_argument("--force", action="store_true",
                   help="the operator takes it, for any reason (needs a terminal)")
    p.set_defaults(fn=cmd_handoff)
    p = sub.add_parser("close", help="close a workflow nobody will continue (needs a terminal)")
    p.add_argument("workflow_id")
    p.add_argument("--why", required=True)
    p.set_defaults(fn=cmd_close)
    return ap


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    conn = store_db.connect()
    try:
        return args.fn(args, conn)
    except L.LedgerError as exc:
        print(f"workflow: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
