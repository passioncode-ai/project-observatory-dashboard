#!/usr/bin/env python3
"""This machine's organization enrollments, from a terminal (contract DEC-0031, DEC-0056, DEC-0086; OBS-161).

    project-observatory full org enroll --server https://org.example --token-slot my-org/prod/ENROLLMENT_TOKEN --org ID
        enroll this machine in organization ID with an enrollment token an administrator issued. The token is read
        from the vault slot named, used for this one request, and never stored, printed or logged. The certificate
        request is signed by this machine's `#sig` device key (created on first use). Run again with a new token,
        it re-enrolls (a new stream epoch).
    project-observatory full org status [--org ID]
        enrolled organizations: server, the certificate's expiry date and state (a warning in its last three days),
        whether rotation is due, last check-in, outbox depth, the last batch's result, share registrations, anything
        degraded, and, when a person has something to do, the step (`nextStep`) and its `command`. A machine its
        organization removed (a revoked certificate, an unknown one) or whose organization closed reads `removed`,
        with the date and the server's code, and sends nothing more; a server's `unknown-org` on a device request is
        retried as any other failure and never ends an enrollment (DEC-0103). An expired certificate still inside its renewal
        window says until when (`renewUntil`); one the server refused to renew says why (`renewalRefused`)
    project-observatory full org check-in [--org ID]
        one signed check-in now. A certificate that expired at most 30 days ago (on the server's clock) is first
        renewed by this machine's key on record, with no token (contract DEC-0100); past those 30 days, after the
        server refused the renewal, or for a removed machine, it is refused with the step to take
    project-observatory full org rotate [--org ID]
        a new certificate for the same key before expiry
    project-observatory full org tick [--org ID]
        everything due: a renewal by the key on record after expiry, rotation, check-in, the repairs whose notice has
        stood long enough, share registrations, grant usage, the outbox (the maintenance job runs it)
    project-observatory full org flush [--org ID]
        send the outbox now
    project-observatory full org summary [--org ID]
        the harness summary (`node-summary/1`, contract DEC-0101) the next check-in would send to that organization,
        exactly as it would leave, and what was left out (counts: `omitted` for a name's shape, `excluded` by the
        exclusion list `config/node-summary.json`); nothing is saved, sent or spent, and it works before consent
    project-observatory full org summary --org ID --consent on|off
        share this machine's harness summary with organization ID, or stop; off until a person turns it on, recorded
        with its time. A renewal by the key on record keeps it; a new token's enrollment, a removal and a closed
        organization end it, so it is asked again. Once on, a server that lists `node-summary/1` gets one in a check-in when it changed and at
        least once a day; `org status` says when the last one was taken (`nodeSummary`), `not shared (consent off)`,
        why none could be built, or that the server refused one (check-ins go on without it; it is tried a day later)
    project-observatory full org repair [--org ID]
        the repairs an organization asked of this machine (`repair-request/1`, contract DEC-0101): each one waiting to
        run, with the line the organization wrote, the package and when it runs, and the latest results; and whether a
        repair could run here at all (`enabled`, with the reason when it cannot: until the organization's signed policy
        and key set are applied, every repair is refused, and the check-in does not ask for any)
    project-observatory full org repair --org ID --decline REPAIR_ID
        decline a repair that has not started: it never runs, and the organization is answered `person-declined`.
        A repair runs no earlier than 15 minutes after it is shown, only a local action the owner allow-listed for its
        package in `config/node-repair.json` (a `node.lifecycle` restart or start), never a command
    project-observatory full org leave --org ID
        remove this machine's local enrollment state for one organization, its own device keys with it
    project-observatory full org records apply --record FILE [--authorities FILE]
    project-observatory full org records apply --delta FILE
        apply an estate revision this machine verifies (against the one applied before it, or the first under the
        passkeys it pins) or a revocation-delta page: what share registrations are derived from

    project-observatory full org accounts [--json]
        every provider account this space reaches (repository owners, DigitalOcean teams and the `doctl` contexts
        with no stashed token, Cloudflare accounts, Heroku teams and user, OpenRouter accounts), each under exactly
        one organization (the default Personal, another configured one, or Not ours) with the source of that
        answer (`rule`, `default`, `external`), or under "needs a decision" with the reason (`conflict`: two
        organizations claim it; `none`: a provider account no rule names; or seen and not connected, with the
        door's command that connects it); what holds each, and what could not be read (`degraded`). Read-only
        (OBS-411; schema `org-accounts/1`)
    project-observatory full org keys --org NAME [--json]
        each vault slot of the projects organization NAME holds, and each one issued from its accounts, classified
        `organization`, `personal` or `ambiguous` with why (the project's organization, the issuing account from
        the door's record, or put by hand); the door admin and provisioning keys as never moved. Names only: no
        value is read (OBS-415; schema `org-keys/1`)

    project-observatory full org select --org NAME [plan|show|apply|undo|confirm-replacement KEY] [--json]
        the local selection record of what goes to organization NAME (OBS-417; `org-selection/1`, owner-only, never
        sent): `plan` (the default) shows the projects, keys and accounts proposed for it with the layout, what the
        organization would receive and what stays personal, and its digest; `--include ID`, `--exclude ID`,
        `--include-group proposed|needs-you`, `--exclude-group ...`, `--label PROJECT=TEXT`, `--key KEY=CHOICE`
        (hand-over-revoke, hand-over-keep, keep), `--layout separate-space|this-space|one-space`, `--serves SPACE`
        and `--reset` change the draft item by item. `apply --digest D` writes the next revision only in the form the
        preview showed and starts its journal: a project waits for Fabric's adaptation plan (nothing is sent), a key
        waits for the request queue and the rotation (no value is read or copied), the layout waits for its step.
        `show` reads the journal back with its counts and what was found since; `undo` restores the revision before;
        `confirm-replacement KEY` records that the organization's replacement for KEY works (separate-space layout).
        An agent passes the operator's words with `--decision JSON` or `--decision-file FILE` (contract DEC-0121).
        Gates: `plan` without a change and `show` none; a change, `apply`, `undo` and `confirm-replacement`
        relayed (the operator's words for an agent); your passkey signing in Fabric, the macOS administrator
        password and the administrator's computer token human

`accounts`, `keys` and `select` print text, or with `--json` one JSON document; every other answer is one JSON document on stdout; a refusal is `{"error": code, "message": ...}` on stderr, exit 1. A
refusal a person resolves also carries `nextStep` (in words) and `command`. Exit 1 too when every organization asked
failed, a check-in the server refused included.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths  # noqa: E402

NODE = paths.setting_path("OBSERVATORY_NODE", paths.HOME / "node")


class _Parser(argparse.ArgumentParser):
    """argparse quotes a rejected value back (`invalid choice: '...'`, `unrecognized arguments: ...`): each value
    the caller typed is named as a refusal names it (`credential_shape.echo`: quoted only when it is a well-formed
    identifier, otherwise only its length), and anything shaped like a credential is redacted."""

    def error(self, message):  # noqa: D401 - argparse's own hook
        import re
        import credential_shape
        message = re.sub(r"'([^'\n]*)'", lambda m: credential_shape.echo(m.group(1)), message)
        super().error(credential_shape.redact(message))

    def parse_known_args(self, args=None, namespace=None):
        # argparse joins unrecognized arguments with spaces before `error` sees them: each is named here, whole.
        namespace, extras = super().parse_known_args(args, namespace)
        if extras and self.prog == "project-observatory full org":
            import credential_shape
            self.error("unrecognized arguments: " + " ".join(credential_shape.echo(a) for a in extras))
        return namespace, extras


def parser() -> argparse.ArgumentParser:
    p = _Parser(prog="project-observatory full org",
                                description="This machine's organization enrollments (fabric-device/0.1).")
    sub = p.add_subparsers(dest="command", required=True)
    enroll = sub.add_parser("enroll", help="enroll with an enrollment token from the vault")
    enroll.add_argument("--server", required=True, help="the organization server, https:// and an authority")
    enroll.add_argument("--token-slot", required=True, metavar="PROJECT/ENV/NAME",
                        help="the vault slot that holds the enrollment token")
    enroll.add_argument("--org", required=True, help="the organization id the token was issued for")
    for name, text in (("status", "enrollments and their state"), ("check-in", "one signed check-in now"),
                       ("rotate", "a new certificate for the same key"), ("tick", "everything that is due"),
                       ("flush", "send the outbox now"),
                       ("summary", "the harness summary the next check-in would send, sending nothing")):
        one = sub.add_parser(name, help=text)
        one.add_argument("--org", help="one organization; default every enrolled one")
        if name == "summary":
            one.add_argument("--consent", choices=("on", "off"),
                             help="share this machine's harness summary with that organization, or stop (needs --org)")
    repair = sub.add_parser("repair", help="the repairs an organization asked of this machine, and declining one")
    repair.add_argument("--org", help="one organization; default every enrolled one (needed with --decline)")
    repair.add_argument("--decline", metavar="REPAIR_ID", help="decline a repair that has not started")
    leave = sub.add_parser("leave", help="remove this machine's local state for one organization")
    leave.add_argument("--org", required=True)
    accounts = sub.add_parser("accounts", help="every provider account and its organization (read-only)")
    accounts.add_argument("--json", action="store_true", help="one JSON document (org-accounts/1)")
    keys = sub.add_parser("keys", help="an organization's keys classified, by name only (read-only)")
    keys.add_argument("--org", required=True, help="an organization of config/organizations.json")
    keys.add_argument("--json", action="store_true", help="one JSON document (org-keys/1)")
    select = sub.add_parser("select", help="the local selection of what goes to an organization (owner-only, "
                                           "never sent)")
    select.add_argument("--org", required=True, help="an organization of config/organizations.json")
    select.add_argument("action", nargs="?", default="plan",
                        choices=("plan", "show", "apply", "undo", "confirm-replacement"))
    select.add_argument("key", nargs="?", metavar="KEY", help="confirm-replacement: the key (vault:FOLDER/ENV/NAME)")
    select.add_argument("--include", action="append", metavar="ID", help="choose an item (repeatable)")
    select.add_argument("--exclude", action="append", metavar="ID", help="leave an item out (repeatable)")
    select.add_argument("--include-group", choices=("proposed", "needs-you"),
                        help="choose every member of a group as listed now")
    select.add_argument("--exclude-group", choices=("proposed", "needs-you"),
                        help="leave out every member of a group as listed now")
    select.add_argument("--label", action="append", metavar="PROJECT=TEXT",
                        help="the name the organization reads for a project; empty TEXT clears it")
    select.add_argument("--key", dest="keys", action="append", metavar="KEY=CHOICE",
                        help="hand-over-revoke, hand-over-keep or keep")
    select.add_argument("--layout", choices=("separate-space", "this-space", "one-space"))
    select.add_argument("--serves", metavar="SPACE", help="the space the selection serves (separate-space layout)")
    select.add_argument("--reset", action="store_true", help="drop the draft before any other change")
    select.add_argument("--digest", help="apply: the digest the preview showed")
    select.add_argument("--decision", metavar="JSON", help="the operator's words, contract DEC-0121's shape")
    select.add_argument("--decision-file", metavar="FILE", help="the same, from a file")
    select.add_argument("--json", action="store_true", help="one JSON document (org-select/1)")
    records = sub.add_parser("records", help="the estate revision and delta this machine applied")
    rsub = records.add_subparsers(dest="action", required=True)
    apply = rsub.add_parser("apply", help="verify and apply an estate revision or a delta page")
    which = apply.add_mutually_exclusive_group(required=True)
    which.add_argument("--record", type=Path, help="an estate-record/1 revision (JSON)")
    which.add_argument("--delta", type=Path, help="a revocation-delta page {head, entries} (JSON)")
    apply.add_argument("--authorities", type=Path, help="the passkeys this machine pins, for its first revision")
    return p


def _enrollment(org_id: str):
    from node.org import Enrollment
    meter_db = None
    try:
        import meter
        meter_db = meter.default_meter().db_path
    except Exception:                                      # noqa: BLE001 — no meter: no grant usage, nothing else
        meter_db = None
    return Enrollment(NODE.resolve(), org_id, meter_db=meter_db)


def _orgs(args) -> list:
    from node.org import enrolled_orgs
    return [args.org] if getattr(args, "org", None) else enrolled_orgs(NODE)


def run(args) -> dict:
    import time
    from node import org
    if args.command == "enroll":
        token = org.read_token(args.token_slot)
        return _enrollment(args.org).enroll(args.server, token)
    if args.command == "leave":
        return _enrollment(args.org).leave()
    if args.command == "records":
        at_ms = int(time.time() * 1000)
        if args.record:
            authorities = json.loads(args.authorities.read_text(encoding="utf-8")) if args.authorities else None
            applied = org.apply_record(NODE.resolve(), json.loads(args.record.read_text(encoding="utf-8")), at_ms,
                                       authorities)
        else:
            applied = org.apply_delta_page(NODE.resolve(), json.loads(args.delta.read_text(encoding="utf-8")), at_ms)
        # A changed revision or delta changes registrations: each enrollment sends what changed now, not at the next tick.
        applied["registrations"] = {org_id: _enrollment(org_id)._register_quietly()
                                    for org_id in org.enrolled_orgs(NODE)}
        return applied
    if args.command == "summary" and args.consent:
        if not args.org:
            raise org.OrgError("org-required", "consent is given per organization: name it with --org",
                               command="project-observatory full org summary --org ID --consent on"
                               if args.consent == "on" else "project-observatory full org summary --org ID --consent off")
        return {"schema": "org-summary-consent/1",
                "organizations": [_enrollment(args.org).set_summary_consent(args.consent == "on")]}
    if args.command == "repair" and args.decline:
        if not args.org:
            raise org.OrgError("org-required", "a repair is declined per organization: name it with --org",
                               command="project-observatory full org repair --org ID --decline REPAIR_ID")
        return {"schema": "org-repair/1", "organizations": [_enrollment(args.org).decline_repair(args.decline)]}
    action = {"status": "describe", "check-in": "check_in", "rotate": "rotate", "tick": "tick", "flush": "flush",
              "summary": "summary_preview", "repair": "repairs"}
    results = []
    for org_id in _orgs(args):
        one = _enrollment(org_id)
        try:
            results.append({"org": org_id, **getattr(one, action[args.command])()})
        except org.OrgError as error:
            results.append({"org": org_id, **_refusal(error)})
    return {"schema": "org-status/1" if args.command == "status" else "org-%s/1" % args.command,
            "organizations": results}


def _refusal(error) -> dict:
    """An OrgError for the terminal: its code and message, and the person's step and command when it has one."""
    out = {"error": error.code, "message": str(error)[:300]}
    for key, value in (("nextStep", getattr(error, "next_step", None)), ("command", getattr(error, "command", None))):
        if value:
            out[key] = value
    return out


def views(args) -> int:
    """`accounts` and `keys`: read-only views over this space (`org_view.py`), which need no enrollment and none
    of the `full` extra's node code."""
    import org_view
    try:
        view = org_view.accounts() if args.command == "accounts" else org_view.keys(args.org)
    except org_view.Refusal as refusal:
        if args.json:
            print(json.dumps(refusal.answer()), file=sys.stderr)
        else:
            print(f"refused: {refusal}" + (f"\n  {refusal.command}" if refusal.command else ""), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(view, indent=1, sort_keys=True))
    else:
        print(org_view.accounts_text(view) if args.command == "accounts" else org_view.keys_text(view))
    return 0


def _pairs(values: list[str] | None, what: str) -> dict:
    out = {}
    for value in values or []:
        ident, sep, rest = value.partition("=")
        if not sep:
            raise ValueError(f"{what} takes ID=VALUE")
        out[ident] = rest
    return out


def select(args) -> int:
    """`select`: the local selection record (`org_selection.py`), which needs no enrollment and sends nothing."""
    import org_selection as S

    def refuse(refusal) -> int:
        if args.json:
            print(json.dumps(refusal.answer()), file=sys.stderr)
        else:
            print(f"refused: {refusal}" + (f"\n  {refusal.command}" if refusal.command else ""), file=sys.stderr)
        return 1
    try:
        plan_only = {"--include": args.include, "--exclude": args.exclude, "--include-group": args.include_group,
                     "--exclude-group": args.exclude_group, "--label": args.label, "--key": args.keys,
                     "--layout": args.layout, "--serves": args.serves, "--reset": args.reset}
        stray = [flag for flag, value in plan_only.items() if value] if args.action != "plan" else []
        if args.digest and args.action != "apply":
            stray.append("--digest")
        if args.key and args.action != "confirm-replacement":
            stray.append("KEY")
        if args.action == "show" and (args.decision is not None or args.decision_file):
            stray.append("--decision")
        if stray:
            # A flag the action does not take would be dropped without a word: refused instead.
            owner = {"--digest": "apply", "KEY": "confirm-replacement",
                     "--decision": "a change, apply, undo or confirm-replacement"}.get(stray[0], "plan")
            raise S.Refusal("invalid-change", f"{stray[0]} belongs to {owner}, not to {args.action}")
        decision = None
        if args.decision is not None or args.decision_file:
            try:
                raw = args.decision if args.decision is not None else Path(args.decision_file).read_text(encoding="utf-8")
                decision = json.loads(raw)
            except (OSError, ValueError):
                decision = None
            if not isinstance(decision, dict):
                # `null`, a list or a string is not a decision: never read as "no decision", which is the person's own.
                raise S.Refusal("decision-invalid", "the decision is not one JSON object in contract DEC-0121's shape")
        caller = "cli" if decision is None else "agent:cli"
        if args.action == "plan":
            try:
                changes = {"include": args.include, "exclude": args.exclude, "includeGroup": args.include_group,
                           "excludeGroup": args.exclude_group, "labels": _pairs(args.label, "--label"),
                           "keys": _pairs(args.keys, "--key"), "layout": args.layout, "serves": args.serves,
                           "reset": args.reset}
            except ValueError as error:
                raise S.Refusal("invalid-change", str(error))
            view = S.plan(args.org, changes, decision=decision, caller=caller)
        elif args.action == "show":
            view = S.show(args.org)
        elif args.action == "apply":
            view = S.apply(args.org, args.digest, decision=decision, caller=caller)
        elif args.action == "undo":
            view = S.undo(args.org, decision=decision, caller=caller)
        else:
            if not args.key:
                raise S.Refusal("not-in-record", "confirm-replacement names the key: vault:FOLDER/ENV/NAME")
            view = S.confirm_replacement(args.org, args.key, decision=decision, caller=caller)
    except S.Refusal as refusal:
        return refuse(refusal)
    if args.json:
        print(json.dumps(view, indent=1, sort_keys=True, ensure_ascii=False))
    else:
        print(S.text(view))
    return 1 if view.get("state") == "error" else 0          # a damaged record is never a success to a script


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command in ("accounts", "keys"):
        return views(args)
    if args.command == "select":
        return select(args)
    try:
        from node import org
    except RuntimeError as error:                          # the engine's `full` extra
        print(json.dumps({"error": "unavailable", "message": str(error)}), file=sys.stderr)
        return 2
    try:
        answer = run(args)
    except org.OrgError as error:
        print(json.dumps(_refusal(error)), file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"error": "refused", "message": type(error).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(answer, indent=1, sort_keys=True))
    failed = [r for r in answer.get("organizations", []) if "error" in r or r.get("ok") is False]
    return 1 if failed and len(failed) == len(answer.get("organizations", [])) else 0


if __name__ == "__main__":
    raise SystemExit(main())
