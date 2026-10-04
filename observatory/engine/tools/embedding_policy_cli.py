#!/usr/bin/env python3
"""The operator's door to embedding-policy/1: who may send which text to a remote model.

    project-observatory full embedding-policy show [--json]
    project-observatory full embedding-policy grant PROJECT --statement TEXT [--class CLASS ...]
    project-observatory full embedding-policy revoke PROJECT

`grant` records an explicit consent for ONE project (`project:<slug>`), for the
embedding provider and model the engine is configured with now, covering the
listed classes (`public`, `project-internal`; default both — `confidential` cannot
be granted). The statement says, in the operator's words, where the texts go, and
it must name the provider. `revoke` stops new export for the project at once;
texts already sent cannot be recalled, and vectors made under the consent are no
longer served.

`grant` and `revoke` need a terminal: a consent is given by a person, and minting
one from a script would let anything with shell access consent on the operator's
behalf — the rule `tools/review.py` and `full workflow` keep. Every change raises
`policyRevision`; the engine refuses a file older than the last revision it
applied, so a restored backup cannot re-open a revoked consent.

The decision and its rules: docs/design/EMBEDDING-POLICY.md (PB-137 N-002, N-003).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import embedding_policy as EP                                                       # noqa: E402


def _is_terminal() -> bool:
    return sys.stdin.isatty()


def require_terminal(action: str) -> None:
    if not _is_terminal():
        raise SystemExit(
            f"embedding-policy: refusing to {action} without a terminal. A consent to send a "
            f"project's texts off this machine is the operator's, and a script must not be able "
            f"to give it. Run it yourself; there is no --yes.")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read() -> dict:
    """The current document, validated, or a fresh one. A broken file is refused:
    rewriting it would silently drop whatever the operator meant by it."""
    path = EP.policy_path()
    policy = EP.load(path)   # raises PolicyError on a broken file
    if policy.revision == 0:
        return {"schema": EP.SCHEMA, "policyRevision": 0, "projects": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _write(doc: dict) -> EP.Policy:
    import atomic
    doc["policyRevision"] = int(doc["policyRevision"]) + 1
    policy = EP.parse(doc)
    path = EP.policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(path, doc, indent=2)
    EP.current()          # record the applied revision now, so a rollback is caught
    return policy


def cmd_show(as_json: bool) -> int:
    try:
        policy = EP.current()
    except EP.PolicyError as exc:
        print(f"embedding-policy: refused — {exc}. Nothing leaves this machine until it is "
              f"repaired.", file=sys.stderr)
        return 1
    model = EP.configured_model()
    rows = []
    for project, c in sorted(policy.projects.items()):
        rows.append({"project": project, "provider": c.provider, "model": c.model,
                     "classes": sorted(c.classes), "consentedAt": c.at, "by": c.by,
                     "revokedAt": c.revoked_at,
                     "inForce": c.revoked_at is None and (c.provider, c.model) ==
                     (model.get("provider"), model.get("model"))})
    if as_json:
        print(json.dumps({"policyRevision": policy.revision, "configured": model,
                          "projects": rows}, indent=1))
        return 0
    print(f"embedding-policy/1 · revision {policy.revision} · configured model "
          f"{model.get('provider', '—')}/{model.get('model', '—')}")
    if not rows:
        print("  no consents: every text is embedded locally or not at all")
        return 0
    for r in rows:
        state = ("in force" if r["inForce"] else
                 f"revoked {r['revokedAt']}" if r["revokedAt"] else "for another model")
        print(f"  {r['project']}: {r['provider']}/{r['model']} · {', '.join(r['classes'])} · {state}")
    return 0


def cmd_grant(project: str, statement: str, classes: list[str]) -> int:
    require_terminal("record a consent")
    model = EP.configured_model()
    if not model:
        raise SystemExit("embedding-policy: no embedding model is configured "
                         "(config/models.json); there is nothing to consent to")
    try:
        doc = _read()
        doc["projects"][project] = {"remote": {
            "provider": model["provider"], "model": model["model"],
            "classes": classes or ["public", "project-internal"],
            "consent": {"statement": statement, "by": "operator", "at": _now(), "via": "terminal"},
            "revokedAt": None}}
        policy = _write(doc)
    except EP.PolicyError as exc:
        raise SystemExit(f"embedding-policy: refused — {exc}") from None
    print(f"{project}: texts of the classes {', '.join(sorted(policy.projects[project].classes))} "
          f"may now be sent to {model['provider']}/{model['model']} (revision {policy.revision}). "
          f"Confidential text never leaves.")
    return 0


def cmd_revoke(project: str) -> int:
    require_terminal("revoke a consent")
    try:
        doc = _read()
        entry = doc["projects"].get(project)
        if entry is None:
            raise SystemExit(f"embedding-policy: {project} has no consent to revoke")
        if entry["remote"]["revokedAt"] is not None:
            print(f"{project}: already revoked {entry['remote']['revokedAt']}")
            return 0
        entry["remote"]["revokedAt"] = _now()
        policy = _write(doc)
    except EP.PolicyError as exc:
        raise SystemExit(f"embedding-policy: refused — {exc}") from None
    print(f"{project}: revoked (revision {policy.revision}). Nothing new is sent; texts already "
          f"sent cannot be recalled, and their vectors are no longer served.")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="project-observatory full embedding-policy",
                                description="Which project's texts may leave for a remote embedding model.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show", help="the consents in force")
    s.add_argument("--json", action="store_true")
    g = sub.add_parser("grant", help="consent for one project (terminal only)")
    g.add_argument("project")
    g.add_argument("--statement", required=True,
                   help='in your words, naming the provider: "texts of project X are sent to OpenAI"')
    g.add_argument("--class", dest="classes", action="append",
                   choices=sorted(EP.EXPORTABLE), help="repeatable; default both")
    r = sub.add_parser("revoke", help="withdraw a project's consent (terminal only)")
    r.add_argument("project")
    return p


def main(argv: list[str]) -> int:
    a = parser().parse_args(argv)
    if a.cmd == "show":
        return cmd_show(a.json)
    if a.cmd == "grant":
        return cmd_grant(a.project, a.statement, a.classes or [])
    return cmd_revoke(a.project)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
