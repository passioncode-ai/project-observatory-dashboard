#!/usr/bin/env python3
"""Sign a credential: what it is for, who answers for it, how it is known.

    sign_credential.py set <credential-id> --purpose "..." --evidence "..." \\
                       [--owner "..."] [--rotation-days N] [--tag t --tag t]
    sign_credential.py show [<credential-id>]

Why a tool and not a file. `collectors/credential_annotations.json` is the
operator's half of the credential picture, the half no scan can measure, and a
file whose only editor is a text editor stays empty: the scenario then has no
surface at all. This is the one writer, and the page and the keyserver both
call `write()` rather than assembling JSON of their own.

WHAT IS REFUSED, AND WHY EACH REFUSAL IS LOAD-BEARING.

  no purpose      an unsigned credential is the state this exists to end
  no evidence     a purpose nobody can check is a guess that will be read as a
                  fact — the rule `collectors/credential_owners.json` already
                  enforces for membership, for the same reason
  a value-shaped  purpose or evidence that looks like a key. The annotation file
    purpose       is in git; a credential pasted into a "purpose" field would be
                  committed, and no later edit removes it from the history
  an unknown id   the board carries no such credential today, so the signature
                  would describe nothing and hide a typo

`auto` — what the door knows at issue time — is written by the doors and never
by this tool: a re-issue replaces it and leaves `purpose` and `owner` standing.
"""
from __future__ import annotations
import argparse
import datetime
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import atomic                                                       # noqa: E402
import credential_shape                                             # noqa: E402
import paths                                                        # noqa: E402

#: Redirectable for the same reason every other artefact here is: a test that
#: writes the real file signs a real credential, and the gate's hygiene check
#: would be the thing that noticed.
FILE = pathlib.Path(os.environ.get("OBSERVATORY_ANNOTATIONS",
                                   paths.config_file("credential_annotations.json")))
#: What "looks like a value" means is `credential_shape`, shared with the vault,
#: the keyserver's journal and every refusal that would echo input. This file
#: once kept its own pattern (`sk-…` or a 40+ run), which let a 38-character
#: token through as a purpose and any token through as an owner or a tag.


def today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def load() -> dict:
    """The register, or a fresh one when there is none yet — and a REFUSAL when
    there is one that will not read. The first version answered a corrupt file
    with an empty document, so the next `set` would have written one annotation
    over every other signature the file held. A file that exists and cannot be
    parsed is the operator's to open, not this tool's to replace."""
    if not FILE.is_file():
        return {"schema_version": 1, "note": "", "annotations": {}}
    try:
        doc = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"sign_credential: {FILE} exists and could not be read "
                         f"({type(exc).__name__}: {str(exc)[:120]}) — refusing to write "
                         f"over it; open it by hand, the board carries "
                         f"`credential.register_unreadable` for it")
    if not isinstance(doc, dict) or not isinstance(doc.get("annotations", {}), dict):
        raise SystemExit(f"sign_credential: {FILE} is not the register's shape — refusing "
                         f"to write over it")
    doc.setdefault("annotations", {})
    return doc


def board_ids() -> dict[str, dict]:
    try:
        doc = json.loads((paths.REGISTRY / "credentials.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {c["id"]: c for c in doc.get("credentials", [])}


def write(cred_id: str, fields: dict, *, by: str = "operator") -> dict:
    """THE ONE WRITER. The CLI, the keyserver route and the page all land here,
    so a refusal cannot be true of one path and not another.

    Raises ValueError with the operator's own wording on every refusal.
    """
    purpose = (fields.get("purpose") or "").strip()
    evidence = (fields.get("evidence") or "").strip()
    if not purpose:
        raise ValueError("--purpose is required: an unsigned credential is the "
                         "state this exists to end")
    if not evidence:
        raise ValueError("--evidence is required: a purpose nobody can check is a "
                         "guess that will be read as a fact")
    # EVERY FIELD THE ROW STORES, the id included: the id is the row's key, and
    # with no board to compare it against (a fresh workspace) it was the one
    # field nothing checked at all.
    tags = [str(t) for t in (fields.get("tags") or [])]
    for field, text in (("id", cred_id), ("purpose", purpose), ("evidence", evidence),
                        ("owner", str(fields.get("owner") or "")),
                        *(("tag", t) for t in tags)):
        kind = credential_shape.find(text)
        if kind:
            raise ValueError(f"refused: the {field} looks like it carries a value "
                             f"({credential_shape.describe(kind)}). This file is in git, "
                             f"and no later edit removes a credential from the history")
    ids = board_ids()
    if ids and cred_id not in ids:
        near = [i for i in ids if cred_id.split("/", 1)[0] == i.split("/", 1)[0]][:4]
        raise ValueError(f"the board carries no credential {credential_shape.echo(cred_id)}"
                         + (f" — same kind: {', '.join(near)}" if near else "")
                         + "\n  (ids are as registry/credentials.json prints them)")
    days = fields.get("rotation_days")
    if days is not None:
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise ValueError(f"--rotation-days must be a whole number of days, "
                             f"got {credential_shape.echo(days) if isinstance(days, str) else type(days).__name__}") from None
        if days < 1:
            raise ValueError("--rotation-days must be at least 1; a policy of zero "
                             "is a finding every day and a decision never")
    doc = load()
    row = dict(doc.setdefault("annotations", {}).get(cred_id) or {})
    row.update({"purpose": purpose, "evidence": evidence, "signed_on": today(),
                "signed_by": by})
    if fields.get("owner"):
        row["owner"] = str(fields["owner"]).strip()
    if days is not None:
        row["rotation_days"] = days
    if fields.get("tags"):
        row["tags"] = sorted({str(t).strip() for t in fields["tags"] if str(t).strip()})
    doc["annotations"][cred_id] = row
    doc["annotations"] = dict(sorted(doc["annotations"].items()))
    atomic.write_text(FILE, json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
    return row


def cmd_set(a) -> int:
    try:
        row = write(a.id, {"purpose": a.purpose, "evidence": a.evidence,
                           "owner": a.owner, "rotation_days": a.rotation_days,
                           "tags": a.tag}, by=a.by)
    except ValueError as exc:
        print(f"refused: {exc}" if not str(exc).startswith("refused")
              else str(exc), file=sys.stderr)
        return 2
    print(f"signed: {a.id}\n  for: {row['purpose'][:100]}\n"
          f"  owner: {row.get('owner', '—')}"
          + (f" · rotate every {row['rotation_days']} day(s)" if row.get("rotation_days") else "")
          + f"\n  known by: {row['evidence'][:100]}\n"
          f"  the board drops `credential.unsigned` for it at the next build")
    return 0


def cmd_show(a) -> int:
    doc = load().get("annotations") or {}
    ids = board_ids()
    if a.id:
        row = doc.get(a.id)
        if not row:
            print(f"{a.id} is not signed", file=sys.stderr)
            return 1
        print(json.dumps(row, ensure_ascii=False, indent=1))
        return 0
    if not doc:
        print("nothing is signed yet — "
              "`sign_credential.py set <id> --purpose … --evidence …`")
        return 0
    for cid, row in doc.items():
        state = "" if not ids or cid in ids else "  (no longer on the board)"
        print(f"  {cid}{state}\n     {row.get('purpose', '')[:90]}"
              f"  — {row.get('owner', 'no owner')}, {row.get('signed_on', '?')}"
              + (f", rotate every {row['rotation_days']}d" if row.get("rotation_days") else ""))
    unsigned = [i for i in ids if i not in doc]
    print(f"{len(doc)} signed, {len(unsigned)} not")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("set")
    p.add_argument("id")
    p.add_argument("--purpose", help="what it is for — required")
    p.add_argument("--evidence", help="how that is known — required")
    p.add_argument("--owner", help="a person or a team, never a file")
    p.add_argument("--rotation-days", type=int, dest="rotation_days",
                   help="opt-in: after this many days `credential.rotation_due` fires")
    p.add_argument("--tag", action="append", default=[])
    p.add_argument("--by", default="operator")
    p = sub.add_parser("show")
    p.add_argument("id", nargs="?")
    a = ap.parse_args(argv[1:])
    return {"set": cmd_set, "show": cmd_show}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
