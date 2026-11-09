"""Whose every provider account and key is, read-only (OBS-411, OBS-415; organization onboarding ORG-W1, ORG-W6).

WHY. The organization onboarding design (`docs/ux/organization-onboarding.md`) starts from one question, "whose is
everything here", and the answers were spread over five doors and three registry documents, each read separately
(`digitalocean.py teams`, `cloudflare.py list`, `heroku.py teams`, `openrouter.py list`, the GitHub scan). Two
answers come from here, the same to the CLI (`full org accounts`, `full org keys`) and to an agent
(`observatory_org_accounts`, `observatory_org_keys`):

* `accounts()`: every provider account this space reaches (repository owners, DigitalOcean teams and the `doctl`
  contexts with no stashed token, Cloudflare accounts, Heroku teams and the Heroku user, OpenRouter accounts), each
  under exactly one organization with the source of that answer, or under "needs a decision".
* `keys(org)`: each vault slot of the projects an organization holds, classified `organization`, `personal` or
  `ambiguous` with why (ORG-R06), and the door admin and provisioning keys that are never moved (ORG-R07).

READ-ONLY, AND NO VALUE. Nothing here writes a file, sends a request or reads a credential's value: vault slots and
admin stashes are listed by name (a directory listing), door records and ledgers are records of names and places,
and the movements journal is names only. The one exception is inherited, named and bounded: `doctl` contexts
whose token is not a stashed one are found by the DigitalOcean door's own offline comparison
(`digitalocean.unstashed_contexts`, the one `digitalocean.py teams` prints), which compares token values in memory
and never prints or keeps one. `keys` never calls it.

THE ANSWERS ARE THE ENGINE'S OWN, not a second set of rules. An account's organization is
`organizations.Assigner.account` (a rule names it, two rules claim it, or none does); a project's is the one the
emit wrote to `registry/projects.json`, read through `organizations.reading` like every other reader. An account no rule names reads what its projects already
read: a repository owner outside `config/ownership.json` is Not ours (the projects' `external` rule), an owner
of yours is the default organization's (its projects' `default`), and a provider account is `none` and waits
under `needsDecision`, as its own registry row says (ORG-R26: it contributes no organization to its projects).

FOR THE SELECTION THAT COMES NEXT (OBS-417). Each account and key carries a stable `id`, the organization it would
go to, the source and why, and for a key the default choice ORG-R15 makes (`selected`: true only when project and
issuer agree). The selection record will consume these rows unchanged; nothing here is a decision.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import sys

import paths
import organizations

ACCOUNTS_SCHEMA = "org-accounts/1"
KEYS_SCHEMA = "org-keys/1"
#: The vault's environments until environments are data (OBS-234); a slot in another one needs a choice (ORG-R22).
ENVS = ("local", "stage", "prod")
#: A default organization when `organizations.json` configures none: everything is personal (ORG-W1, empty state).
IMPLICIT_DEFAULT = "personal"
NOT_OURS = "external"
#: An organization name a refusal may quote: the keys of `organizations.json` are free text, so any name is
#: looked up, and only one of this shape (and not shaped like a credential) is echoed back.
ORG_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._@+-]{0,79}")
DOORS = {
    "openrouter": "openrouter-admin",
    "cloudflare": "cloudflare-admin",
    "digitalocean": "digitalocean-admin",
    "heroku": "heroku-admin",
}
ACCOUNTS_CMD = "project-observatory full org accounts"


class Refusal(ValueError):
    """A question this view cannot answer, with the step that changes that."""

    def __init__(self, code: str, message: str, command: str | None = None):
        super().__init__(message)
        self.code = code
        self.command = command

    def answer(self) -> dict:
        out = {"error": self.code, "message": str(self), "degraded": []}
        if self.command:
            out["command"] = self.command
        return out


# ── reading, never writing ─────────────────────────────────────────────────────

def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _registry(name: str, degraded: list, effect: str) -> dict | None:
    """A registry document, or None with its absence named in `degraded`."""
    path = paths.REGISTRY / name
    if not path.is_file():
        degraded.append({"source": f"registry/{name}", "code": "missing",
                         "reason": f"registry/{name} does not exist yet", "effect": effect})
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        degraded.append({"source": f"registry/{name}", "code": "unreadable",
                         "reason": f"registry/{name} does not read ({type(exc).__name__})", "effect": effect})
        return None
    if not isinstance(doc, dict):
        degraded.append({"source": f"registry/{name}", "code": "unreadable",
                         "reason": f"registry/{name} is not one JSON object", "effect": effect})
        return None
    return doc


def _secret_store() -> pathlib.Path:
    return paths.source_path("secret_store", paths.SECRETS)


def _vault_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("OBSERVATORY_VAULT_DIR", _secret_store() / "projects"))


def _private_json(path: pathlib.Path, degraded: list, source: str) -> object:
    """A door's record or ledger (names and places, never a value), read the way the door reads it."""
    if not path.exists():
        return None
    try:
        import private_io
        return json.loads(private_io.read(path))
    except (OSError, ValueError, RuntimeError) as exc:
        degraded.append({"source": source, "code": "unreadable",
                         "reason": f"{path.name} does not read ({type(exc).__name__})",
                         "effect": "what it records is missing from this answer"})
        return None


def _labels(folder: pathlib.Path, degraded: list, source: str) -> list[str]:
    """The labels of a door's stash: its value files, by NAME. None is opened."""
    if any(p.is_symlink() for p in (folder, *folder.parents)):
        degraded.append({"source": source, "code": "refused",
                         "reason": f"{folder.name} passes through a symbolic link; not read",
                         "effect": "its accounts are missing from this answer"})
        return []
    if not folder.is_dir():
        return []
    try:
        return sorted(p.name for p in folder.iterdir()
                      if p.is_file() and not p.name.startswith(".") and not p.name.endswith((".meta.json", ".tmp")))
    except OSError as exc:
        degraded.append({"source": source, "code": "unreadable",
                         "reason": f"{folder.name} cannot be listed ({type(exc).__name__})",
                         "effect": "its accounts are missing from this answer"})
        return []


def _stash_meta(door: str, label: str, degraded: list) -> dict:
    doc = _private_json(_secret_store() / DOORS[door] / f"{label}.meta.json", degraded, f"{door}:{label}")
    return doc if isinstance(doc, dict) else {}


def _openrouter_ledger(degraded: list) -> dict:
    doc = _private_json(_secret_store() / "openrouter-issued.json", degraded, "openrouter:ledger")
    issued = (doc or {}).get("issued") if isinstance(doc, dict) else None
    return issued if isinstance(issued, dict) else {}


def _heroku_ledger(degraded: list) -> dict:
    doc = _private_json(_secret_store() / "heroku-issued.json", degraded, "heroku:ledger")
    issued = (doc or {}).get("issued") if isinstance(doc, dict) else None
    return issued if isinstance(issued, dict) else {}


def _digitalocean_records(degraded: list) -> list[dict]:
    folder = _secret_store() / "digitalocean-issued"
    if not folder.is_dir() or folder.is_symlink():
        return []
    out = []
    for p in sorted(folder.glob("*.meta.json")):
        rec = _private_json(p, degraded, "digitalocean:records")
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _movements(degraded: list) -> list[dict]:
    """The vault's movements journal (names and places only), oldest first."""
    path = _vault_root() / "movements.jsonl"
    if not path.exists():
        return []
    try:
        import private_io
        text = private_io.read(path)
    except (OSError, RuntimeError) as exc:
        degraded.append({"source": "vault:movements", "code": "unreadable",
                         "reason": f"movements.jsonl does not read ({type(exc).__name__})",
                         "effect": "keys issued by the Cloudflare door read as put by hand"})
        return []
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _slots(degraded: list) -> list[tuple[str, str, str]]:
    """(folder, env, NAME) of every vault slot, by its path: no slot file is opened."""
    root = _vault_root()
    if not root.is_dir():
        if root.parent.is_dir():
            return []                     # a workspace that never stored a key: an empty vault, not an unread one
        degraded.append({"source": "vault", "code": "missing", "reason": "the vault folder does not exist here",
                         "effect": "no key is listed, which is not evidence there is none"})
        return []
    out = []

    def listed(folder: pathlib.Path) -> list[pathlib.Path]:
        try:
            return sorted(folder.iterdir())
        except OSError as exc:
            degraded.append({"source": "vault", "code": "unreadable",
                             "reason": f"a vault folder cannot be listed ({type(exc).__name__})",
                             "effect": "the slots under it are missing from this answer"})
            return []
    for project in (p for p in listed(root) if p.is_dir() and not p.is_symlink()):
        for env in (e for e in listed(project) if e.is_dir() and not e.is_symlink()):
            for slot in listed(env):
                if (slot.is_file() and not slot.name.startswith(".") and ".retired-" not in slot.name
                        and not slot.name.endswith((".meta.json", ".tmp"))):
                    out.append((project.name, env.name, slot.name))
    return out


# ── who issued each key ────────────────────────────────────────────────────────

def issuers(degraded: list, cloudflare_accounts: dict[str, str] | None = None,
            slots: list[str] | None = None, digitalocean_keys: dict[str, list[str]] | None = None) -> dict[str, dict]:
    """vault slot `FOLDER/ENV/NAME` -> the account that issued its value, from the door that minted it:
    the OpenRouter ledger (`vault:` destinations), the DigitalOcean records, the Heroku ledger, and the
    movements journal for the Cloudflare door (its vault writers keep no record of their own). Each:
    {provider, rule (the `organizations` provider a rule names it under), keys, account, record}.
    A slot no door recorded is absent: it was put by hand, or by a door that keeps no record.

    The Cloudflare door journals an R2 pair as one movement, `FOLDER/ENV/PREFIX_*`: it stands for every slot
    of `slots` under that folder and environment whose name starts with `PREFIX_`. The journal names an
    account by its name; `cloudflare_accounts` (`cloudflare_names`) turns it into the id a rule names, and a
    name it cannot turn is said in `degraded`, never read as an account no rule names. A DigitalOcean team
    is named by what a rule may name it by (`digitalocean_keys`, team uuid -> `organizations.account_keys`):
    its uuid, and its name only while no other team here bears it.

    A door's own record wins over the journal: the journal only names slots no other door recorded, and of
    its rows the latest for a slot."""
    out: dict[str, dict] = {}
    for name, row in sorted(_openrouter_ledger(degraded).items()):
        if not isinstance(row, dict):
            continue
        dest = str(row.get("destination") or "")
        if dest.startswith("vault:") and row.get("account"):
            out[dest[len("vault:"):]] = {"provider": "openrouter", "rule": "openrouter", "keys": [str(row["account"])],
                                         "account": str(row["account"]), "accountId": f"openrouter:account:{row['account']}",
                                         "record": f"openrouter ledger ({name})"}
    for rec in _digitalocean_records(degraded):
        if isinstance(rec, dict) and rec.get("vault"):
            uuid = rec.get("team_uuid")
            keys = ((digitalocean_keys or {}).get(str(uuid)) or [str(uuid)]) if uuid else \
                [str(rec["team_name"])] if rec.get("team_name") else []
            out[str(rec["vault"])] = {"provider": "digitalocean", "rule": "digitalocean", "keys": keys,
                                      "account": rec.get("team_name") or rec.get("team_label") or "?",
                                      "accountId": f"digitalocean:team:{rec.get('team_uuid') or rec.get('team_label')}",
                                      "record": f"digitalocean record ({rec.get('label')})"}
    for slot, row in sorted(_heroku_ledger(degraded).items()):
        if not isinstance(row, dict):
            continue
        team = row.get("team")
        if team and team != "personal":
            out[slot] = {"provider": "heroku", "rule": "heroku", "keys": [str(team)], "account": str(team),
                         "accountId": f"heroku:team:{team}", "record": "heroku ledger"}
        else:
            out[slot] = {"provider": "heroku", "rule": None, "keys": [], "account": "your Heroku user",
                         "accountId": "heroku:user", "record": "heroku ledger"}
    names = cloudflare_accounts or {}
    unresolved: set[str] = set()
    recorded = set(out)
    moves = _movements(degraded)
    for row in moves:
        if row.get("tool") != "cloudflare.py" or row.get("event") not in ("issue", "rotate"):
            continue
        slot = str(row.get("secret") or "")
        if slot.count("/") != 2 or not row.get("account"):
            continue
        acct = str(row["account"])
        aid = names.get(acct.lower())
        if not aid:
            unresolved.add(acct)
        who = {"provider": "cloudflare", "rule": "cloudflare", "keys": [aid] if aid else [],
               "account": acct, "accountId": f"cloudflare:account:{aid}" if aid else f"cloudflare:name:{acct}",
               "record": "movements journal (cloudflare.py)"}
        if slot.endswith("_*"):
            stem = slot[:-1]                                     # FOLDER/ENV/PREFIX_
            for one in slots or []:
                if one.startswith(stem) and one not in recorded:
                    out[one] = who
        elif slot not in recorded:
            out[slot] = who
    # A VALUE PUT BY HAND AFTER THE DOOR DELIVERED IT is somebody else's, whichever door it was: every door
    # journals its own issue or rotate row after its delivery (the vault's own `put` line comes first), so a put
    # or rotate by anything but a door (`vault.py`, the secret prompt) later than the door's last row for a slot
    # means the door no longer says who issued it.
    door_at: dict[str, int] = {}
    hand_at: dict[str, int] = {}
    for i, row in enumerate(moves):
        tool, event, secret = row.get("tool"), row.get("event"), str(row.get("secret") or "")
        if event in ("put", "rotate") and tool not in DOOR_TOOLS:
            hand_at[secret] = i                                  # `vault.py`, or the secret prompt a person typed into
        elif tool in DOOR_TOOLS and event in ("issue", "rotate"):
            to = str(row.get("to") or "")
            target = to[len("vault:"):] if tool == "openrouter.py" and to.startswith("vault:") else secret
            hit = ([one for one in slots or [] if one.startswith(target[:-1])] if target.endswith("_*")
                   else [target])
            for one in hit:
                door_at[one] = i
    for one in list(out):
        if one in door_at and hand_at.get(one, -1) > door_at[one]:
            del out[one]
    for acct in sorted(unresolved):
        degraded.append({"source": "cloudflare:journal", "code": "unresolved",
                         "reason": f"the movements journal names a Cloudflare account {acct!r} that no Cloudflare "
                                   "scan or admin token here names by id",
                         "effect": "the keys issued in it cannot be matched to a rule; they read as ambiguous"})
    return out


def cloudflare_names(cf: dict | None, degraded: list) -> dict[str, str]:
    """Cloudflare account name (lower case) -> its id, from the scan's account rows and every admin
    stash's record: the movements journal names an account by name, and a rule names it by id."""
    out: dict[str, str] = {}
    for t in _rows(cf, "accounts"):
        if t.get("account_id") and t.get("name"):
            out.setdefault(str(t["name"]).lower(), str(t["account_id"]))
    for label in _labels(_secret_store() / DOORS["cloudflare"], degraded, "cloudflare:stash"):
        for a_ in _stash_accounts(_stash_meta("cloudflare", label, degraded)):
            if a_.get("name"):
                out.setdefault(str(a_["name"]).lower(), str(a_["id"]))
    return out


def _stash_accounts(meta: dict) -> list[dict]:
    """The accounts a Cloudflare admin stash's record lists (`accounts`, or the older single-account fields)."""
    listed = meta.get("accounts") or ([{"id": meta.get("account_id"), "name": meta.get("account_name")}]
                                      if meta.get("account_id") else [])
    return [x for x in listed if isinstance(x, dict) and x.get("id")]


# ── accounts ───────────────────────────────────────────────────────────────────

class _Accounts:
    def __init__(self):
        self.rows: dict[str, dict] = {}

    def add(self, ident: str, provider: str, kind: str, name: str, keys: list[str], rule: str | None, *,
            connected: bool, via: str, measured: str | None = None) -> dict:
        row = self.rows.get(ident)
        if row is None:
            row = self.rows[ident] = {"id": ident, "provider": provider, "kind": kind, "name": name, "keys": [],
                                      "rule": rule, "connected": False, "via": [], "measuredOn": None,
                                      "projects": set(), "resources": 0, "keys_issued": 0}
        row["connected"] = row["connected"] or connected
        for k in keys:
            if k and k not in row["keys"]:
                row["keys"].append(k)
        if via not in row["via"]:
            row["via"].append(via)
        if measured and not row["measuredOn"]:
            row["measuredOn"] = measured
        return row


def accounts() -> dict:
    """Every provider account this space reaches, under exactly one organization or "needs a decision"."""
    degraded: list[dict] = []
    problem = organizations.load_problem()
    doc = organizations.load()
    a = organizations.Assigner(doc=doc)
    problems = ([f"config/organizations.json: {problem}"] if problem else []) + (
        organizations.validate(doc) if doc.get("organizations") else [])
    default = a.default if a.active else IMPLICIT_DEFAULT
    estate = organizations._estate_owners()
    acc = _Accounts()

    # Repository owners: every owner a project's repositories name, and what the GitHub scan reaches.
    pdoc = _registry("projects.json", degraded, "no project is counted under any account")
    projects = _project_rows(pdoc)
    rdoc = _registry("repositories.json", degraded, "a repository owner's host is not known; GitHub is assumed")
    hosts: dict[str, set[str]] = {}
    names: dict[str, str] = {}
    for r in (rdoc or {}).get("repositories") or []:
        nwo = str(r.get("name_with_owner") or "") if isinstance(r, dict) else ""
        if "/" in nwo:
            owner = nwo.split("/", 1)[0]
            hosts.setdefault(owner.lower(), set()).add(str(r.get("host") or "github"))
            names.setdefault(owner.lower(), owner)
    listing = paths.SCRATCH / "gh"
    reached = set()
    if listing.is_dir():
        reached = {p.stem.lower() for p in listing.glob("*.json") if not p.name.startswith("_")}
        try:
            for d in json.loads((listing / "_degraded.json").read_text(encoding="utf-8")) or []:
                degraded.append({"source": str(d.get("source") or "github"), "code": "degraded",
                                 "reason": str(d.get("reason") or "")[:200],
                                 "effect": "that owner's repositories are as an earlier scan saw them"})
        except (OSError, ValueError, AttributeError):
            pass
    else:
        degraded.append({"source": "github", "code": "not-scanned",
                         "reason": "the GitHub scan has not run here",
                         "effect": "owners are taken from the registry's projects only"})
    for p in projects:
        for o in p.get("owners") or []:
            names.setdefault(str(o).lower(), str(o))
    for o in sorted(reached):
        names.setdefault(o, o)
        hosts.setdefault(o, set()).add("github")
    for low, o in sorted(names.items()):
        # One owner name on two hosts is two accounts. A GitHub owner is connected when the GitHub scan reached
        # it; an owner on another host when ownership.json declares it (its scan reads the declared ones).
        for h in sorted(hosts.get(low) or {"github"}):
            reached_here = low in reached if h == "github" else low in estate
            row = acc.add(f"{h}:{low}", h, "owner", o, [o], "owner", connected=reached_here,
                          via="github scan" if (h == "github" and low in reached) else "registry")
            row["projects"].update(p["id"] for p in projects
                                   if low in {str(x).lower() for x in p.get("owners") or []})

    # DigitalOcean: teams the collector measured, teams whose token the door stashed, contexts it did not.
    do = _quiet("digitalocean-account.json")
    do_keys = organizations.account_keys("digitalocean", _rows(do, "accounts"))
    for t in _rows(do, "accounts"):
        if not isinstance(t.get("id"), str):
            continue
        acc.add(t.get("id"), "digitalocean", "team", t.get("name") or t.get("label") or "?",
                      do_keys.get(t.get("id")) or [], "digitalocean", connected=True,
                      via="registry/digitalocean-account.json", measured=(do or {}).get("scanned_on"))
    for r in _rows(do, "resources"):
        if r.get("account_ref") in acc.rows:
            acc.rows[r["account_ref"]]["resources"] += 1
            if r.get("project"):
                acc.rows[r["account_ref"]]["projects"].add(r["project"])
    _passed_on("digitalocean", do, degraded)
    stashed = _labels(_secret_store() / DOORS["digitalocean"], degraded, "digitalocean:stash")
    for label in stashed:
        m = _stash_meta("digitalocean", label, degraded)
        ident = f"digitalocean:team:{m.get('team_uuid') or label}"
        acc.add(ident, "digitalocean", "team", m.get("team_name") or label,
                [str(k) for k in (m.get("team_uuid"),) if k], "digitalocean", connected=True,
                via=f"stashed team token ({label})")
    if stashed and do is None:
        _not_scanned("digitalocean", degraded)
    try:
        if str(paths.ROOT / "tools") not in sys.path:
            sys.path.insert(0, str(paths.ROOT / "tools"))
        import digitalocean
        import credential_shape
        for i, c in enumerate(digitalocean.unstashed_contexts(probe=False)):
            shown = credential_shape.shown(c["context"])
            # A name shaped like a credential is never printed: its row is told apart by its place in the list.
            ident = f"digitalocean:doctl:{shown}" if shown == c["context"] else f"digitalocean:doctl:#{i + 1}"
            row = acc.add(ident, "digitalocean", "doctl-context", shown, [],
                          "digitalocean", connected=False, via="doctl config")
            row["decision"] = {"reason": "seen through doctl: its token is not a stashed one, so its team is unknown offline",
                               "command": None if shown != c["context"] else
                               f"python \"$(project-observatory full-path)/tools/digitalocean.py\" stash --from-doctl "
                               f"{_quote(c['context'])}"}
    except Exception as exc:                                              # noqa: BLE001: a door that cannot answer
        degraded.append({"source": "digitalocean:doctl", "code": "unreadable",
                         "reason": f"doctl's contexts were not read ({type(exc).__name__})",
                         "effect": "a team reached only through doctl is missing from this answer"})

    # Cloudflare: accounts the collector measured, and the accounts each admin stash records.
    cf = _quiet("cloudflare-account.json")
    _passed_on("cloudflare", cf, degraded)
    cf_names = cloudflare_names(cf, degraded)
    for t in _rows(cf, "accounts"):
        if not isinstance(t.get("id"), str):
            continue
        acc.add(t.get("id"), "cloudflare", "account", t.get("name") or t.get("label") or "?",
                [str(t["account_id"])] if t.get("account_id") else [], "cloudflare", connected=True,
                via="registry/cloudflare-account.json", measured=(cf or {}).get("scanned_on"))
    for r in _rows(cf, "resources"):
        if r.get("account_ref") in acc.rows:
            acc.rows[r["account_ref"]]["resources"] += 1
            if r.get("project"):
                acc.rows[r["account_ref"]]["projects"].add(r["project"])
    cf_stashed = _labels(_secret_store() / DOORS["cloudflare"], degraded, "cloudflare:stash")
    if cf_stashed and cf is None:
        _not_scanned("cloudflare", degraded)
    for label in cf_stashed:
        for a_ in _stash_accounts(_stash_meta("cloudflare", label, [])):
            acc.add(f"cloudflare:account:{a_['id']}", "cloudflare", "account", a_.get("name") or a_["id"],
                    [str(a_["id"])], "cloudflare", connected=True, via=f"admin token ({label})")

    # Heroku: the teams the scan saw, and the user's own apps.
    hk = _quiet("heroku-apps.json")
    _passed_on("heroku", hk, degraded)
    hteams = {str(t) for t in (hk or {}).get("teams") or [] if isinstance(t, str)}
    for app in _rows(hk, "apps"):
        team = app.get("team")
        ident = f"heroku:team:{team}" if team and team != "personal" else "heroku:user"
        row = acc.add(ident, "heroku", "team" if ident != "heroku:user" else "user",
                      team if ident != "heroku:user" else "your Heroku user",
                      [str(team)] if ident != "heroku:user" else [], "heroku" if ident != "heroku:user" else None,
                      connected=True, via="registry/heroku-apps.json", measured=(hk or {}).get("scanned_on"))
        row["resources"] += 1
        if app.get("project"):
            row["projects"].add(app["project"])
    for team in sorted(hteams - {"personal"}):
        acc.add(f"heroku:team:{team}", "heroku", "team", team, [team], "heroku", connected=True,
                via="registry/heroku-apps.json", measured=(hk or {}).get("scanned_on"))
    hk_stashed = _labels(_secret_store() / DOORS["heroku"], degraded, "heroku:stash")
    if hk_stashed and hk is None:
        _not_scanned("heroku", degraded)
    for label in hk_stashed:
        acc.add("heroku:user", "heroku", "user", "your Heroku user", [], None, connected=True,
                via=f"admin token ({label})")

    # OpenRouter: the provisioning keys stashed by label, and the accounts the ledger names.
    for label in _labels(_secret_store() / DOORS["openrouter"], degraded, "openrouter:stash"):
        acc.add(f"openrouter:account:{label}", "openrouter", "account", label, [label], "openrouter",
                connected=True, via="provisioning key stashed")
    vault_projects = _vault_resolver(projects)
    slots = ["/".join(x) for x in _slots(degraded)]
    slot_set = set(slots)
    for name, row in sorted(_openrouter_ledger(degraded).items()):
        if not isinstance(row, dict) or not row.get("account"):
            continue
        r = acc.add(f"openrouter:account:{row['account']}", "openrouter", "account", str(row["account"]),
                    [str(row["account"])], "openrouter", connected=False, via="openrouter ledger")
        dest = str(row.get("destination") or "")
        if not dest.startswith("vault:") or dest[len("vault:"):] in slot_set:
            r["keys_issued"] += 1                              # a key whose vault slot is gone is not counted
        pid = vault_projects(dest[len("vault:"):].split("/", 1)[0]) if dest.startswith("vault:") else None
        if pid:
            r["projects"].add(pid)

    # Keys each account issued into the vault (by the doors' records), counted beside what it holds. An account
    # known only from such a record is an account this space reached too, and not connected: a team or account
    # whose own token is stashed is already listed above. The Heroku user is the one exception, reached through
    # the Heroku admin token whenever one is stashed.
    do_by_uuid = {str(t["team_uuid"]): do_keys.get(t.get("id")) or [str(t["team_uuid"])]
                  for t in _rows(do, "accounts") if t.get("team_uuid")}
    for slot, who in sorted(issuers(degraded, cf_names, slots, do_by_uuid).items()):
        if who["provider"] == "openrouter" or slot not in slot_set:
            continue                                            # counted from the ledger above; or no slot now
        row = acc.rows.get(who["accountId"])
        if row is None:
            row = acc.add(who["accountId"], who["provider"], "user" if who["accountId"] == "heroku:user" else
                          "team" if who["provider"] in ("heroku", "digitalocean") else "account", who["account"],
                          who["keys"], who["rule"],
                          connected=who["provider"] == "heroku" and bool(hk_stashed), via=who["record"])
            if not row["connected"]:
                row["decision"] = {"reason": "a door's record names it, and no token for it is stashed here",
                                   "command": STASH[who["provider"]]}
        row["keys_issued"] += 1
        pid = vault_projects(slot.split("/", 1)[0])
        if pid:
            row["projects"].add(pid)

    for row in acc.rows.values():
        if row["provider"] == "openrouter" and not row["connected"] and "decision" not in row:
            row["decision"] = {"reason": "the ledger names this account, and its provisioning key is not stashed here",
                               "command": "python \"$(project-observatory full-path)/tools/openrouter.py\" stash "
                                          f"--as {_quote(row['name'])} < key-file"}

    if a.active and not estate and any(r["kind"] == "owner" for r in acc.rows.values()):
        problems.append("config/ownership.json declares no repository owner, so every owner (and every project of "
                        "one) reads Not ours; list yours under `organizations` there")
    out_rows = [_decide(row, a, default, estate) for row in sorted(acc.rows.values(), key=lambda r: r["id"])]
    groups = _groups(a, default, out_rows, projects)
    needs = [r["id"] for r in out_rows if r.get("decision")]
    degraded = _once(degraded)
    state = "empty" if not out_rows else ("partial" if degraded else "complete")
    return {"schema": ACCOUNTS_SCHEMA, "generatedAt": _now(), "state": state, "configured": a.active,
            "default": default, "organizations": groups, "accounts": out_rows, "needsDecision": needs,
            "problems": problems, "degraded": degraded}


#: The doors whose journal rows name the slot they delivered to.
DOOR_TOOLS = ("openrouter.py", "heroku.py", "digitalocean.py", "cloudflare.py")
#: How each door stashes the token that connects an account.
STASH = {
    "digitalocean": 'python "$(project-observatory full-path)/tools/digitalocean.py" stash < token-file',
    "cloudflare": 'python "$(project-observatory full-path)/tools/cloudflare.py" stash < token-file',
    "heroku": 'heroku auth:token | python "$(project-observatory full-path)/tools/heroku.py" stash',
}


class _NoDestinations:
    """For `organizations.reading` where only the state is wanted: no organization's destinations are read,
    so a hand-edited `figma` or `label` cannot fail a view that never shows them."""

    @staticmethod
    def destinations(_organization):
        return {}


_NO_DESTINATIONS = _NoDestinations()


def _project_rows(doc: dict | None) -> list[dict]:
    """The registry's project rows that can be read: an object with a string id."""
    return [p for p in (doc or {}).get("projects") or [] if isinstance(p, dict) and isinstance(p.get("id"), str)]


def _rows(doc: dict | None, key: str) -> list[dict]:
    """A registry document's list of objects; anything else in it is skipped, never fails the view."""
    value = (doc or {}).get(key) if isinstance(doc, dict) else None
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _quiet(name: str) -> dict | None:
    """A provider's registry document, absent without a word: a provider never connected has none, and that
    absence is named only where its door holds a token (`_not_scanned`)."""
    return _registry(name, [], "")


def _not_scanned(provider: str, degraded: list) -> None:
    degraded.append({"source": provider, "code": "not-scanned",
                     "reason": f"its door holds a token and the {provider} scan has not written its registry document",
                     "effect": "what its accounts hold is not counted"})


def _passed_on(provider: str, doc: dict | None, degraded: list) -> None:
    """The scan's own `degraded` rows (a team or account it could not read), named as this view's. A
    `doctl` context the scan named for having no stashed token is not a read that failed: this view lists
    it under `needsDecision` itself (`digitalocean.unstashed_contexts`), so it is not repeated here."""
    for d in (doc or {}).get("degraded") or []:
        if isinstance(d, dict):
            source = str(d.get("source") or "scan")
            if provider == "digitalocean" and (source.startswith("digitalocean:doctl") or source.startswith("doctl")):
                continue
            degraded.append({"source": source if source.startswith(provider + ":") else f"{provider}:{source}",
                             "code": "degraded",
                             "reason": str(d.get("reason") or "")[:200],
                             "effect": "that account's rows are as the last good scan saw them",
                             **({"since": (doc or {}).get("scanned_on")} if (doc or {}).get("scanned_on") else {})})


def _once(rows: list[dict]) -> list[dict]:
    """`degraded` with each row once: two readers of one unreadable file each name it."""
    seen, out = set(), []
    for d in rows:
        key = (d.get("source"), d.get("code"), d.get("reason"))
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def _quote(text: str) -> str:
    import shlex
    return shlex.quote(text)


def _vault_resolver(projects: list[dict]):
    import vault_project

    def resolve(folder: str) -> str | None:
        res = vault_project.resolve(folder, projects, loaded=True) if folder else None
        return res.project_id if res is not None and res.how != "ambiguous" else None
    return resolve


def _decide(row: dict, a: "organizations.Assigner", default: str | None, estate: set[str]) -> dict:
    """One account's organization, by the engine's rules (`Assigner.account`). An account no rule names
    follows what its projects already read: a repository owner outside ownership.json is Not ours
    (`external`, the projects' rule), an owner of yours belongs to the default organization (`default`, the
    projects' last answer), and a provider account reads `none` and waits for a person under `needsDecision`,
    as its own registry row says (ORG-R26: it decides no project). Without organizations.json everything is
    the implicit Personal; the Heroku user, which no rule kind can name, is the default's."""
    out = {k: row[k] for k in ("id", "provider", "kind", "name", "connected", "via", "measuredOn")}
    out["holds"] = {"projects": sorted(row["projects"]), "resources": row["resources"], "keys": row["keys_issued"]}
    ans = a.account(row["rule"], row["keys"]) if (a.active and row["rule"]) else None
    if ans and ans["organization_source"] == "rule":
        out.update(organization=ans["organization"], source="rule", why=ans["organization_why"])
    elif ans and ans["organization_source"] == "conflict":
        claimed = sorted(a._rule_hits(row["rule"], row["keys"]))
        out.update(organization=None, source="conflict", why=ans["organization_why"], claimedBy=claimed)
        row.setdefault("decision", {"reason": f"claimed by {' and '.join(claimed)}: decide per project, or narrow "
                                              "the rule that should not name it", "command": None})
    elif row["kind"] == "owner" and row["name"].lower() not in estate and (estate or a.active):
        out.update(organization=NOT_OURS, source="external",
                   why="not declared in config/ownership.json, so its repositories are read as somebody else's code")
    elif not a.active:
        why = "no organizations.json: everything is personal"
        if row["kind"] == "doctl-context":
            why = "its team is unknown until its token is stashed; " + why
        out.update(organization=default, source="default", why=why)
    elif default and (row["kind"] == "owner" or row["rule"] is None):
        why = ("no rule names it, so its projects take the default organization" if row["kind"] == "owner" else
               "no rule kind names a Heroku user account; its own apps are the default organization's")
        out.update(organization=default, source="default", why=why)
    else:
        if row["rule"] is None:
            why = _no_rule_kind(a)["organization_why"]
        elif row["kind"] == "doctl-context":
            why = "its team is unknown until its token is stashed, so no rule can name it yet"
        else:
            rules = ("repository_owners or match.github_owners" if row["kind"] == "owner"
                     else organizations._rule_of(row["rule"]))
            why = f"no organization's match.{rules} names it"
        if not default and row["kind"] == "owner":
            why += ", and organizations.json marks no default organization"
        out.update(organization=None, source="none", why=why)
        row.setdefault("decision", {"reason": why + ": say which organization it belongs to with a rule in "
                                                    "config/organizations.json; until then it decides no project",
                                    "command": None})
    out["decision"] = row.get("decision")
    return out


def _groups(a, default: str, rows: list[dict], projects: list[dict]) -> list[dict]:
    """Organizations in display order: the default first, then the others by name, then Not ours. An account
    that needs a decision is listed under `needsDecision`, not in a group, so it appears exactly once."""
    names = ([default] if default else []) + sorted(n for n in (a.orgs if a.active else {})
                                                    if n not in (default, NOT_OURS)) + [NOT_OURS]
    out = []
    for name in names:
        org = (a.orgs.get(name) or {}) if a.active else {}
        if not isinstance(org, dict):
            org = {}
        members = [r["id"] for r in rows if r["organization"] == name and not r.get("decision")]
        # Without organizations.json the registry carries no organization: a group holds its accounts' projects.
        held = (sorted(p["id"] for p in projects if p.get("organization") == name) if a.active else
                sorted({pid for r in rows if r["id"] in members for pid in r["holds"]["projects"]}))
        if name == NOT_OURS and not members and not held:
            continue
        out.append({"name": name,
                    "label": "Not ours" if name == NOT_OURS else ("Personal" if name == default and not org.get("label")
                                                                 else org.get("label") or name),
                    "kind": "not-ours" if name == NOT_OURS else ("personal" if name == default else "local"),
                    "implicit": name == default and not a.active,
                    "accounts": members, "projects": held})
    return out


# ── keys ───────────────────────────────────────────────────────────────────────

def keys(org: str) -> dict:
    """Each vault slot of the projects `org` holds, classified for it (ORG-R06), and the door admin keys that
    never move (ORG-R07). Names only: no slot, stash or value file is opened."""
    import credential_shape
    if not isinstance(org, str) or not org.strip() or len(org) > 200 or credential_shape.shaped(org):
        raise Refusal("invalid-organization", f"{credential_shape.echo(org)} is not an organization name",
                      ACCOUNTS_CMD)
    problem = organizations.load_problem()
    if problem:
        raise Refusal("unreadable-organizations", f"config/organizations.json is not read: {problem}; nothing is "
                                                  "classified until it is", ACCOUNTS_CMD)
    doc = organizations.load()
    a = organizations.Assigner(doc=doc)
    if not a.active:
        raise Refusal("no-organizations", "config/organizations.json configures no organization, so every key is "
                                          "personal and none is classified for another", ACCOUNTS_CMD)
    if org not in a.orgs:
        said = repr(org) if ORG_NAME.fullmatch(org) else credential_shape.echo(org)
        raise Refusal("unknown-organization", f"no organization {said} in config/organizations.json; it has: "
                                              + ", ".join(sorted(a.orgs)), ACCOUNTS_CMD)
    if org == a.default:
        raise Refusal("default-organization", f"{org!r} is the default organization (Personal): its keys stay where "
                                              "they are; name the organization they would go to", ACCOUNTS_CMD)
    degraded: list[dict] = []
    pdoc = _registry("projects.json", degraded, "no project is known to be the organization's, so no key is in scope")
    projects = _project_rows(pdoc)
    do, cf, hk = _quiet("digitalocean-account.json"), _quiet("cloudflare-account.json"), _quiet("heroku-apps.json")
    claims = organizations.project_accounts(do, cf, [x for x in (hk or {}).get("apps") or [] if isinstance(x, dict)])
    for provider, doc_ in (("digitalocean", do), ("cloudflare", cf), ("heroku", hk)):
        _passed_on(provider, doc_, degraded)
    scope: dict[str, dict] = {}
    for p in projects:
        r = organizations.reading(p, _NO_DESTINATIONS)
        if r["state"] == "decided" and r["organization"] == org:
            scope[p["id"]] = {"side": "organization", "why": f"the project is {org}'s ({r['source']}: {r['why']})"}
        elif p.get("organization_source") == "conflict" and org in a.hits(p, claims.get(p["id"])):
            scope[p["id"]] = {"side": "conflict", "why": f"the project's rules disagree ({p.get('organization_why')})"}
    slots = _slots(degraded)
    do_keys = organizations.account_keys("digitalocean", _rows(do, "accounts"))
    do_by_uuid = {str(t["team_uuid"]): do_keys.get(t.get("id")) or [str(t["team_uuid"])]
                  for t in _rows(do, "accounts") if t.get("team_uuid")}
    who = issuers(degraded, cloudflare_names(cf, degraded), ["/".join(x) for x in slots], do_by_uuid)
    resolve = _vault_resolver(projects)
    rows = []
    for folder, env, name in slots:
        slot = f"{folder}/{env}/{name}"
        pid = resolve(folder)
        side = scope.get(pid or "")
        issuer = who.get(slot)
        iorg = (a.account(issuer["rule"], issuer["keys"]) if issuer and issuer["rule"]
                else _no_rule_kind(a) if issuer else None)
        if side is None and not (iorg and iorg["organization"] == org):
            continue
        cls, why = _classify(org, a.default, side, issuer, iorg)
        row = {"id": f"vault:{slot}", "slot": slot, "project": pid, "env": env, "name": name,
               "environmentDeclared": env in ENVS,
               "issuer": None if issuer is None else {
                   "provider": issuer["provider"], "account": issuer["account"], "accountId": issuer["accountId"],
                   "organization": iorg["organization"], "source": iorg["organization_source"],
                   "record": issuer["record"]},
               "putBy": "door" if issuer else "hand",
               "class": cls, "why": why, "selected": cls == "organization",
               "proposal": {"organization": "hand-over", "personal": "stay", "ambiguous": "decide"}[cls]}
        if env not in ENVS:
            row["why"] += f"; environment {env!r} is not declared (local, stage, prod): it needs a choice (ORG-R22)"
            row["selected"] = False
        rows.append(row)
    heroku_teams = sorted(({str(t) for t in (hk or {}).get("teams") or [] if isinstance(t, str)}
                           | {str(x.get("team")) for x in _rows(hk, "apps") if x.get("team")}) - {"personal"})
    fixed = _not_movable(org, a, degraded, do_by_uuid, heroku_teams)
    degraded = _once(degraded)
    counts = {c: sum(1 for r in rows if r["class"] == c) for c in ("organization", "personal", "ambiguous")}
    counts["notMovable"] = len(fixed)
    state = "empty" if not rows and not fixed else ("partial" if degraded else "complete")
    return {"schema": KEYS_SCHEMA, "generatedAt": _now(), "organization": org,
            "label": (a.orgs.get(org) or {}).get("label") or org, "state": state,
            "projects": [{"id": pid, "side": s["side"], "why": s["why"]} for pid, s in sorted(scope.items())],
            "keys": rows, "notMovable": fixed, "counts": counts,
            "notChecked": ["values present twice in this space (ORG-R09) and names the organization already holds "
                           "(ORG-R10) need a comparison of values or the organization's context; neither is read here"],
            "problems": organizations.validate(doc), "degraded": degraded}


def _no_rule_kind(a) -> dict:
    """The Heroku user, which no rule kind can name: the default organization's, as `accounts()` reads it."""
    if a.default:
        return {"organization": a.default, "organization_source": "default",
                "organization_why": "no rule kind names a Heroku user account; its own apps are the default's"}
    return {"organization": None, "organization_source": "none",
            "organization_why": "no rule kind names a Heroku user account, and no organization is the default"}


def _classify(org: str, default: str | None, side: dict | None, issuer: dict | None, iorg: dict | None):
    """ORG-R06: the project's organization and the issuing account's, together."""
    psay = side["why"] if side else "the project is not the organization's"
    if issuer is None:
        return "ambiguous", f"{psay}; put by hand: no door recorded who issued it, so its account is unknown"
    acct = f"{issuer['provider']} account {issuer['account']}"
    src = iorg["organization_source"]
    if src == "rule" and iorg["organization"] == org:
        if side and side["side"] == "organization":
            return "organization", f"{psay}, and it was issued in {acct}, which is {org}'s"
        return "ambiguous", f"issued in {acct}, which is {org}'s, but {psay}"
    if src == "conflict":
        return "ambiguous", f"{psay}; it was issued in {acct}, which is shared ({iorg['organization_why']})"
    if src in ("rule", "default") and default and iorg["organization"] == default:
        if side and side["side"] == "conflict":
            return "personal", f"issued in {acct}, which is personal, and {psay}"
        return "ambiguous", f"{psay}, but it was issued in {acct}, which is personal"
    if src == "rule":
        return "ambiguous", f"{psay}, but it was issued in {acct}, which is {iorg['organization']}'s"
    return "ambiguous", f"{psay}; it was issued in {acct}, which no rule names"


def _not_movable(org: str, a, degraded: list, do_by_uuid: dict[str, list[str]] | None = None,
                 heroku_teams: list[str] | None = None) -> list[dict]:
    """ORG-R07: the admin and provisioning keys a door keeps. Never handed over: the organization's space
    stashes its own, then a personal copy for an account of the organization is revoked. Each key is read
    against every account it reaches, named as `accounts()` names them: a DigitalOcean team by what a rule may
    name it by (`do_by_uuid`), the Heroku admin token by the user and the teams the Heroku scan saw (the scan reads
    with the CLI's own login, `collectors/scan_heroku.py`; the admin token is taken to reach the same teams)."""
    out = []
    for provider, folder in DOORS.items():
        for label in _labels(_secret_store() / folder, degraded, f"{provider}:stash"):
            m = _stash_meta(provider, label, degraded) if provider in ("cloudflare", "digitalocean") else {}
            if provider == "openrouter":
                accts = [("openrouter", [label], label)]
            elif provider == "digitalocean":
                uuid = str(m.get("team_uuid") or "")
                accts = [("digitalocean", (do_by_uuid or {}).get(uuid) or ([uuid] if uuid else []),
                          m.get("team_name") or label)]
            elif provider == "cloudflare":
                accts = [("cloudflare", [str(x["id"])], x.get("name") or x["id"]) for x in _stash_accounts(m)]
            else:
                accts = [(None, [], "your Heroku user")] + [("heroku", [t], t) for t in heroku_teams or []]
            named = []
            for rule, keys_, name in accts:
                ans = (a.account(rule, keys_) if rule and keys_ else _no_rule_kind(a) if rule is None
                       else {"organization": None, "organization_source": "none"})
                named.append({"account": name, "organization": ans["organization"],
                              "source": ans["organization_source"]})
            ours = any(n["organization"] == org for n in named)
            mixed = ours and any(n["organization"] != org for n in named)
            action = ("stays with this space" if not ours else
                      "revoke after the organization's space stashes its own" if not mixed else
                      "replace: stash a token for your other accounts alone, then revoke this one after the "
                      "organization's space stashes its own")
            out.append({"store": folder, "label": label, "provider": provider,
                        "kind": "provisioning" if provider == "openrouter" else
                        "team token" if provider == "digitalocean" else "admin",
                        "accounts": named, "action": action,
                        "why": "a door's admin or provisioning key is never handed over (ORG-R07)"
                               + (f"; it reaches an account that is {org}'s" if ours else "")
                               + ("; it also reaches accounts that are not" if mixed else "")})
    return out


# ── text ──────────────────────────────────────────────────────────────────────

def accounts_text(view: dict) -> str:
    """`full org accounts` without `--json`: ORG-W1 as lines."""
    lines = [f"Organizations and accounts ({view['state']})"]
    unread = next((p for p in view["problems"] if p.startswith("config/organizations.json:")), None)
    if unread:
        lines.append(f"{unread}: it is read as if absent, so every account reads Personal until it is fixed.")
    elif not view["configured"] and view["problems"]:
        lines.append("config/organizations.json names no usable organization (the problems are below), so every "
                     "account of yours reads Personal.")
    elif not view["configured"]:
        lines.append("No config/organizations.json: you are in no organization, and every account of yours is "
                     "personal; repository owners outside config/ownership.json read Not ours.")
    rows = {r["id"]: r for r in view["accounts"]}
    for g in view["organizations"]:
        lines.append(f"{g['label']} ({g['kind']}): {len(g['accounts'])} account(s), {len(g['projects'])} project(s)")
        for ident in g["accounts"]:
            lines.append("  " + _account_line(rows[ident]))
    if view["needsDecision"]:
        lines.append(f"Needs a decision ({len(view['needsDecision'])})")
        for ident in view["needsDecision"]:
            r = rows[ident]
            lines.append("  " + _account_line(r) + f": {r['decision']['reason']}")
            if r["decision"].get("command"):
                lines.append(f"    {r['decision']['command']}")
    elif view["accounts"]:
        lines.append("Nothing needs a decision.")
    else:
        lines.append("No accounts connected yet: each door's stash command connects one "
                     "(digitalocean.py, cloudflare.py, heroku.py, openrouter.py), and the GitHub scan reads `gh`.")
    for p in view["problems"]:
        lines.append(f"problem: {p}")
    for d in view["degraded"]:
        lines.append(f"not read: {d['source']}: {d['reason']}" + (f" ({d['effect']})" if d.get("effect") else ""))
    return "\n".join(lines)


def _account_line(r: dict) -> str:
    h = r["holds"]
    held = ", ".join(x for x in (f"{len(h['projects'])} project(s)" if h["projects"] else "",
                                 f"{h['resources']} resource(s)" if h["resources"] else "",
                                 f"{h['keys']} key(s)" if h["keys"] else "") if x) or "nothing counted"
    state = "" if r["connected"] else ", not connected"
    return f"{r['provider']} {r['kind']} {r['name']}: {held} [{r['source']}{state}]"


def keys_text(view: dict) -> str:
    """`full org keys --org ID` without `--json`: ORG-W6 as lines."""
    c = view["counts"]
    lines = [f"Keys for {view['label']} ({view['state']}): {c['organization']} organization, {c['personal']} personal, "
             f"{c['ambiguous']} ambiguous, {c['notMovable']} not movable"]
    if not view["projects"]:
        lines.append(f"No project is {view['organization']}'s here, so no key is proposed for it.")
    for r in view["keys"]:
        lines.append(f"  {r['class']:12} {r['slot']}: {r['why']}")
    if view["notMovable"]:
        lines.append("Not movable: door admin keys stay with their space")
        for n in view["notMovable"]:
            lines.append(f"  {n['provider']} {n['kind']} {n['label']}: {n['action']}")
    for note in view["notChecked"]:
        lines.append(f"not checked: {note}")
    for p in view["problems"]:
        lines.append(f"problem: {p}")
    for d in view["degraded"]:
        lines.append(f"not read: {d['source']}: {d['reason']}")
    return "\n".join(lines)
