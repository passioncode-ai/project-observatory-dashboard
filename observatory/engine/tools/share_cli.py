#!/usr/bin/env python3
"""Share a key with another person's Observatory, and use a key shared with you (contract DEC-0142, OBS-264, OBS-276).

Sender:

    project-observatory full share create PROJECT ENV SLOT --to LABEL --expires DAYS
        a share of vault slots (SLOT,SLOT for several) for one recipient machine, through the mailbox
        `full cloud deploy` recorded (or --mailbox URL); prints the link once, to your terminal
    project-observatory full share status ID
        what the recipient did; the six-digit code when it is ready
    project-observatory full share confirm ID
        type the six digits the recipient read to you; a match seals and sends, a mismatch seals nothing
        (a terminal)
    project-observatory full share confirm ID --without-comparing
        seal without comparing: trust on first use (a terminal, and a typed phrase)
    project-observatory full share revoke ID
        take it back: the mailbox refuses from now on
    project-observatory full share provider-ended ID
        you revoked or rotated the key at its provider (a terminal)

Recipient:

    project-observatory full share accept
        open a link the sender gave you: it asks for the link, which keeps it out of your shell history (a terminal)
    project-observatory full share status ID
        the code to read to the sender, then the keys you received
    python "$(project-observatory full-path)/tools/use_secret.py" run --share ID SLOT -- COMMAND
        an agent's command with the shared key in its environment, opened at that use only

Both:

    project-observatory full share list --json
        every share, sent and received, after one pass to the mailbox
    project-observatory full share differ ID
        the codes differ: before sealing, nothing is sealed; after it, the sender takes the share back
    project-observatory full share sync ID
        one pass to the mailbox for one share, or every share without ID

The value is sealed to the recipient's machine with a key made for this share alone; the mailbox carries only
ciphertext and names. The link carries a one-time claim code and the sender's key fingerprint in its fragment. A
person compares the six-digit code by a call or a thread they already trust: `confirm`, `accept` and
`provider-ended` are a person's acts and need a terminal; there is no --yes. A key that reached another machine can
have been copied there: taking a share back stops it at the mailbox and on the recipient's machine, and only the
provider ends a copy, so `revoke` names that step and the share reads "taken back" only after `provider-ended`.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
# The engine root first: `node` is the engine's package, never tools/node.py (`full node`).
sys.path.insert(0, str(ROOT))
import paths  # noqa: E402

#: Built from two parts: the bare `full share` is no command a person runs, and the handed-command check reads every
#: literal that starts with the program's name as one.
PROG = "project-observatory full" + " share"


def _is_terminal() -> bool:
    return sys.stdin.isatty()


def _shows_code() -> bool:
    """The comparison code is printed only to a terminal: never into --json, a pipe or a file an agent reads. On this
    machine any program running as you can still drive a terminal; the limit is stated in ONBOARDING."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def require_terminal(action: str) -> None:
    if not _is_terminal():
        raise SystemExit(f"share: refusing to {action} without a terminal. It is a person's act (DEC-0142: comparing "
                         f"codes, consenting to the reports, stating the provider ended a key), and an agent must not "
                         f"do it for you. Run it yourself; there is no --yes.")


def mailbox_origin(explicit: str | None) -> str | None:
    """The mailbox a new share uses: --mailbox, else the one `full cloud` recorded in config/cloud.json."""
    if explicit:
        return explicit
    path = paths.CONFIG / "cloud.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    origin = doc.get("origin") if isinstance(doc, dict) else None
    return origin if isinstance(origin, str) else None


def store():
    from node import shares as SH
    import use_secret

    def read_value(project: str, env: str, slot: str) -> str:
        value, _where = use_secret.resolve(use_secret.project_for_reading(project), slot, env, vault_only=True)
        return value
    root = paths.setting_path("OBSERVATORY_NODE", paths.HOME / "node") / "shares"
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise SystemExit("share: the share store's path holds a symbolic link; keys and sealed copies are kept only "
                         "where no link can redirect them")
    return SH, SH.Shares(root.resolve(), read_value=read_value)


def _print(side: str, record: dict, SH, as_json: bool) -> None:
    view = SH.summary(side, record)
    if as_json:
        print(json.dumps(view, indent=1, sort_keys=True))
        return
    short = view["shareId"][:8]
    head = f"{short}  {'to' if side == 'sent' else 'from'} {view['label']}  {view['state']}"
    print(head)
    if view.get("burnt"):
        print(f"  burnt: {view['burnt']['reason']}: {view['burnt']['why']}")
    if view.get("mailboxError"):
        print(f"  the mailbox did not answer the last pass ({view['mailboxError']['error']}); the next pass tries "
              f"again (the maintenance job's comes within the hour)")
    if view.get("problem"):
        print(f"  waiting: {view['problem']['code']}: {view['problem']['message']}")
    if view.get("warning"):
        print(f"  warning: {view['warning']}")
    if view.get("codeReady"):
        digits = record.get("code") or ""
        shown = f"your code {digits[:3]} {digits[3:]}" if _shows_code() else "a code is ready (shown only in a terminal)"
        if side == "sent":
            print(f"  {shown}. Ask the recipient to read you the six digits their machine shows, by a call or a "
                  f"thread you already trust; then run `{PROG} confirm {short}` and type what they read. If they "
                  f"differ, it seals nothing.")
        else:
            print(f"  {shown}. Read it to the sender by a call or a thread you already trust; if theirs differs, "
                  f"run `{PROG} differ {short}`.")
    if side == "sent":
        for s in view["slots"]:
            sealed = f"sealed #{s['sequence']} until {s['exp']}" if s["sealedAt"] else "not sealed yet"
            print(f"  {view['project']}/{view['env']}/{s['slot']} ({s['provider']}): {sealed}")
        if view["state"] == "waiting":
            print(f"  waiting for the recipient until {view['claimDeadline']}")
        if view["confirmed"] is False:
            print("  shared without comparing: whoever claimed first may hold the value")
        usage = view["usage"]
        if view["state"] in ("active", "taking-back", "ended", "expired"):
            consumers = ", ".join(sorted({c["agent"] for c in usage["consumers"]})) or "none reported"
            print(f"  uses in 7 days: {usage['uses7d']} (reported by their machine); agents: {consumers}; "
                  f"exports with the button: {usage['exportsWithTheButton']}")
            d = view["deliveries"]
            print(f"  deliveries: {d['count']} (seen by your mailbox), last renewal {d['lastRenewal'] or 'none'}")
        taking = view["takingBack"]
        if taking:
            print(f"  taking back: mailbox refuses {'yes' if taking['mailboxRefuses'] else 'not observed yet'}; "
                  f"recipient deleted {'yes (signed receipt)' if taking['recipientDeleted'] else 'not yet'}; "
                  f"provider ended the key {'yes' if taking['providerEnded'] else 'not confirmed'}")
            if not taking["providerEnded"]:
                tools = '"$(project-observatory full-path)/tools'
                print("  a key that reached another machine can have been copied: revoke a key minted for this share "
                      f"at its provider (python {tools}/openrouter.py\" revoke, python {tools}/cloudflare.py\" revoke, "
                      f"python {tools}/heroku.py\" revoke), or rotate your own (python {tools}/vault.py\" rotate), "
                      f"then: {PROG} provider-ended {short}")
            print("  taken back" if taking["takenBack"] else "  not taken back yet")
    else:
        for s in view["slots"]:
            print(f"  {s['slot']} ({s['provider']}) until {s['exp']}: an agent uses it with")
            print(f"    python \"$(project-observatory full-path)/tools/use_secret.py\" run --share {short} "
                  f"{s['slot']} -- COMMAND")
        if view["state"] == "removed":
            print("  the sender took this share back; its keys were removed from this machine")


def cmd_create(args) -> int:
    SH, s = store()
    origin = mailbox_origin(args.mailbox)
    if origin is None:
        print("share: no mailbox: deploy yours into your Cloudflare account with `project-observatory full cloud "
              "deploy`, or pass --mailbox https://... for another one", file=sys.stderr)
        return 2
    if not (args.show_link or _is_terminal()):
        print("share: the link is printed once, to a person's terminal: run this yourself, or pass --show-link to "
              "print it here", file=sys.stderr)
        return 2
    providers = {}
    for pair in args.provider or []:
        slot, _, name = pair.partition("=")
        providers[slot] = name
    try:
        record, link = s.create(project=args.project, env=args.env, slots=[x for x in args.slots.split(",") if x],
                                label=args.to, origin=origin, providers=providers, expires_days=args.expires,
                                claim_days=args.claim_days, lease_seconds=args.lease)
    except SH.ShareError as exc:
        print(f"share: {exc.code}: {exc}", file=sys.stderr)
        return 1
    print(f"share {record['shareId'][:8]} for {record['label']}: send this link to them yourself, once (it opens "
          f"until {record['claimDeadline']}):")
    print(link)
    print(f"then: {PROG} status {record['shareId'][:8]}   (shows the six-digit code to compare)")
    return 0


def _find(s, SH, ref: str):
    try:
        return s.find(ref)
    except SH.ShareError as exc:
        print(f"share: {exc}", file=sys.stderr)
        return None


def cmd_status(args) -> int:
    SH, s = store()
    found = _find(s, SH, args.id)
    if found is None:
        return 1
    side, share_id = found
    record = s.sync_sent(share_id) if side == "sent" else s.sync_received(share_id)
    _print(side, record, SH, args.json)
    return 0


def cmd_list(args) -> int:
    SH, s = store()
    pairs = s.records() if args.offline else s.sync()
    if args.json:
        print(json.dumps([SH.summary(side, r) for side, r in pairs], indent=1, sort_keys=True))
        return 0
    if not pairs:
        print("no share yet: `full share create PROJECT ENV SLOT --to LABEL` shares a key; `full share accept` "
              "opens a link someone sent you")
    for side, record in pairs:
        _print(side, record, SH, False)
    return 0


def cmd_sync(args) -> int:
    SH, s = store()
    try:
        pairs = s.sync(args.id, due_only=args.due)
    except SH.ShareError as exc:
        print(f"share: {exc}", file=sys.stderr)
        return 1
    rows = [r if "failed" in r else SH.summary(side, r) for side, r in pairs]
    failed = [r for r in rows if "failed" in r or r.get("mailboxError") or r.get("problem")]
    print(json.dumps({"schema": "observatory-share-sync/1", "shares": rows, "problems": len(failed)}, indent=1,
                     sort_keys=True))
    return 0


def cmd_confirm(args) -> int:
    require_terminal("confirm a comparison code")
    SH, s = store()
    found = _find(s, SH, args.id)
    if found is None:
        return 1
    side, share_id = found
    if side != "sent":
        print("share: only the sender confirms; read your code to the sender", file=sys.stderr)
        return 2
    if args.without_comparing:
        typed = input("Seal without comparing codes: whoever claimed first may receive the key. "
                      "Type: share without comparing\n> ").strip().lower()
        if typed != "share without comparing":
            print("not sealed")
            return 1
        code = None
    else:
        # The digits the OTHER machine shows, read aloud by its person: never your own screen's, which an agent
        # could copy, and never an argument.
        code = input("Type the six digits the recipient read to you: ").strip()
    try:
        record = s.confirm(share_id, code=code, without_comparing=args.without_comparing)
    except SH.ShareError as exc:
        print(f"share: {exc.code}: {exc}", file=sys.stderr)
        return 1
    if record["state"] == "burnt":
        print("the codes differ: nothing was sealed and this link is spent. If you still want to share, make a new "
              "share and send its link again.")
        return 1
    _print("sent", record, SH, False)
    return 0


def cmd_differ(args) -> int:
    SH, s = store()
    found = _find(s, SH, args.id)
    if found is None:
        return 1
    record = s.differ(found[1])
    _print(found[0], record, SH, False)
    return 0


def cmd_revoke(args) -> int:
    SH, s = store()
    found = _find(s, SH, args.id)
    if found is None:
        return 1
    if found[0] != "sent":
        print("share: only the sender takes a share back; `full share differ` stops one you received", file=sys.stderr)
        return 2
    _print("sent", s.revoke(found[1]), SH, False)
    return 0


def cmd_provider_ended(args) -> int:
    require_terminal("state that the provider ended the key")
    SH, s = store()
    found = _find(s, SH, args.id)
    if found is None or found[0] != "sent":
        print("share: no share sent from this machine is called that", file=sys.stderr)
        return 1
    try:
        _print("sent", s.provider_ended(found[1]), SH, False)
    except SH.ShareError as exc:
        print(f"share: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_accept(args) -> int:
    require_terminal("accept a share")
    SH, s = store()
    if args.link in (None, "-"):
        # Read, not taken from argv: a link on a command line stays in the shell's history.
        link = input("Paste the link you were sent and press Enter: ").strip()
    else:
        link = args.link
    print(SH.REPORT_CONSENT)
    answer = input("Accept this share on this machine? Type yes to accept: ").strip().lower()
    if answer != "yes":
        print("not accepted; nothing was sent")
        return 1
    try:
        record = s.accept(link, label=args.label)
    except SH.ShareError as exc:
        print(f"share: {exc.code}: {exc}", file=sys.stderr)
        return 1
    _print("received", record, SH, False)
    if record["state"] == "exchanging":
        print(f"  waiting for the sender's machine; run `{PROG} status {record['shareId'][:8]}` for the code")
    return 0


def cmd_mailbox_trial(args) -> int:
    """A loopback mailbox for a trial between two workspaces on this machine (never for another person)."""
    from node import mailbox as MB
    server = MB.serve_trial(MB.FakeMailbox(args.origin), args.port)
    host = args.origin.split("//", 1)[-1]
    print(f"trial mailbox {args.origin} on 127.0.0.1:{server.server_port}; in both workspaces' shells: "
          f"export {MB.RESOLVE_ENV}={host}=127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=PROG, description="Share a key with another person's Observatory, and use a key "
                                                       "shared with you (fabric-peer-share/1).")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="share vault slots with one recipient machine; prints the link once")
    c.add_argument("project")
    c.add_argument("env", choices=("local", "stage", "prod"))
    c.add_argument("slots", help="one SLOT or several separated by commas")
    c.add_argument("--to", required=True, help="the recipient's label, as you will read it (Alex laptop)")
    c.add_argument("--expires", type=int, default=30,
                   help="days until the share ends (default 30); each sealed copy lives at most 90 days and the "
                        "maintenance job seals it again before it ends")
    c.add_argument("--claim-days", type=int, default=7, help="days the link can be opened, 1 to 7 (default 7)")
    c.add_argument("--lease", type=int, default=3600, help="seconds a renewal lasts, 60 to 86400 (default 3600)")
    c.add_argument("--mailbox", help="the mailbox's https origin (default: the one `full cloud` recorded)")
    c.add_argument("--provider", action="append", metavar="SLOT=NAME",
                   help="the provider a slot belongs to (default: the slot's first word, lower case)")
    c.add_argument("--show-link", action="store_true", help="print the link even when not in a terminal")
    c.set_defaults(fn=cmd_create)
    for name, fn, text in (("status", cmd_status, "one share after one pass to its mailbox"),):
        q = sub.add_parser(name, help=text)
        q.add_argument("id")
        q.add_argument("--json", action="store_true")
        q.set_defaults(fn=fn)
    q = sub.add_parser("list", help="every share, sent and received")
    q.add_argument("--json", action="store_true")
    q.add_argument("--offline", action="store_true", help="do not ask the mailboxes")
    q.set_defaults(fn=cmd_list)
    q = sub.add_parser("sync", help="one pass to the mailbox for one share or all (JSON); the maintenance job runs "
                                    "it with --due")
    q.add_argument("id", nargs="?")
    q.add_argument("--due", action="store_true", help="renew a received share only when its lease ended or its "
                                                      "last report is a day old")
    q.set_defaults(fn=cmd_sync)
    q = sub.add_parser("confirm", help="type the code the recipient read to you; a match seals and sends (a terminal)")
    q.add_argument("id")
    q.add_argument("--without-comparing", action="store_true", help="seal without comparing (trust on first use)")
    q.set_defaults(fn=cmd_confirm)
    for name, fn, text in (("differ", cmd_differ, "the codes differ: stop this share"),
                           ("revoke", cmd_revoke, "take a share back: the mailbox refuses from now on"),
                           ("provider-ended", cmd_provider_ended,
                            "you revoked or rotated the key at its provider (a terminal)")):
        q = sub.add_parser(name, help=text)
        q.add_argument("id")
        q.set_defaults(fn=fn)
    q = sub.add_parser("accept", help="open a link someone sent you (a terminal; without LINK, or with -, it asks "
                                      "for the link)")
    q.add_argument("link", nargs="?")
    q.add_argument("--label", help="your name for this share (default share-<id>)")
    q.set_defaults(fn=cmd_accept)
    q = sub.add_parser("mailbox-trial", help="a loopback mailbox for a trial between two workspaces on this machine")
    q.add_argument("--origin", default="https://mailbox.localhost")
    q.add_argument("--port", type=int, default=0)
    q.set_defaults(fn=cmd_mailbox_trial)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.fn(args)
    except RuntimeError as exc:                                    # the engine's `full` extra is missing
        print(f"share: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
