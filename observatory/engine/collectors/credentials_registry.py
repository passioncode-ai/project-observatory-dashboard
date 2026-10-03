#!/usr/bin/env python3
"""Every credential this estate holds, as typed facts, and who may use it.

WHY A REGISTRY DOCUMENT AND NOT A SECOND VAULT. `tools/vault.py` holds VALUES
and does it well: mode 600 in the machine's secret store, stdin only, and an
encrypted backup on its own timer. What it cannot do is answer estate
questions: which projects share one hosting account, what has leaked and not
been rotated, which OpenRouter key the tick is actually spending.
Those are facts about what EXISTS, which is what `registry/` is for, and the
split is the same one this repository draws everywhere: values in the store,
facts in git, never the reverse.

**Nothing here is a secret and the shape enforces it.** A record carries a name,
a provider, the provider's own label (a truncated form that the provider itself
calls the key by, not the key), a limit, a date. `tools/check_secrets.py` reads
every tracked file including this one on every gate run.

MANY-TO-MANY, because one account serves several projects and one project uses
several accounts. The edge is `credential_used_by`, from a credential to a
project, and a credential with three projects has three edges, not one field
with a list, so the same query answers both directions.

WHERE THE EDGE COMES FROM, in order:

  measured   a vault slot is `projects/<project>/<env>/<NAME>`, so the project
             is the path; an OpenRouter key's `serves` names its consumer
  curated    `collectors/credential_owners.json`, for a SHARED account no path
             can express, such as one DNS login used by several projects. Every
             row carries its evidence, and a row without one is refused, which
             is the same rule `collectors/heroku_links.json` follows

An account nothing claims stays unclaimed and says so. Guessing which projects
use a shared login from its name is exactly the inference AGENTS.md rule 2
forbids, and here it would be worse than usual: the wrong answer sends somebody
to rotate a credential several projects are quietly using.
"""
from __future__ import annotations
import json, os, pathlib, re, stat, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import leak_register  # noqa: E402
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import environments  # noqa: E402  — one spelling of an environment name (DEPLOYMENTS.md)
import paths                                                                             
import safe_git  # noqa: E402  — the engine's one git door

ROOT = pathlib.Path(__file__).resolve().parents[1]
OWNERS = paths.config_file('credential_owners.json')
#: The operator's half of S9: what a credential is FOR, who answers for it, and
#: how often it should be rotated. Written only by `tools/sign_credential.py`
#: and by the doors' auto-signature; merged here so the page and the board read
#: one record rather than two files (plan v2, T-07).
ANNOTATIONS = pathlib.Path(os.environ.get(
    "OBSERVATORY_ANNOTATIONS",
    paths.config_file('credential_annotations.json')))


#: A register that EXISTS and could not be read, named for the document. The
#: first version read such a file as empty, so a corrupt annotations file
#: would have silently unsigned every credential and put `credential.unsigned`
#: on the board for all of them: the same class of fault already closed for the
#: leak register, closed here for the two curated ones. A MISSING file is still
#: empty: not having curated anything is a state, not a fault.
REGISTER_PROBLEMS: list[dict] = []


def _register_problem(path: pathlib.Path, exc: Exception) -> None:
    REGISTER_PROBLEMS.append({"register": path.name, "path": str(path),
                              "problem": f"{type(exc).__name__}: {str(exc)[:160]}"})


def load_annotations() -> dict[str, dict]:
    if not ANNOTATIONS.is_file():
        return {}
    try:
        return json.loads(ANNOTATIONS.read_text(encoding="utf-8")).get("annotations") or {}
    except (OSError, ValueError, AttributeError) as exc:
        _register_problem(ANNOTATIONS, exc)
        return {}

#: A vault slot is exactly three segments deep. Anything else is not a secret
#: slot — `leaks.jsonl` and the retired archive live beside them.
SLOT = 3


def load_owners() -> list[dict]:
    """Hand-curated credential -> projects, each row carrying its evidence.

    A row with no `evidence` is REFUSED rather than trusted. A shared account's
    membership cannot be measured, so the only thing standing between a fact and
    a guess is a human saying how they know, and an unsourced row is the guess
    wearing a curator's clothes.
    """
    if not OWNERS.is_file():
        return []
    try:
        doc = json.loads(OWNERS.read_text(encoding="utf-8"))
        rows = doc.get("owners", [])
    except (OSError, ValueError, AttributeError) as exc:
        # Recorded, not raised: the emit finishes and the board says which
        # register it could not read, rather than the whole registry stopping
        # on one file.
        _register_problem(OWNERS, exc)
        return []
    return [r for r in rows
            if r.get("credential") and r.get("projects") and r.get("evidence")]


def from_openrouter(scan: dict) -> list[dict]:
    """One record per key the account lists, plus the consumers it serves."""
    out = []
    for k in scan.get("keys", []):
        if not k.get("serves") and not (k.get("name") or "").startswith("project-"):
            # THE OTHER WORKSPACE'S KEYS ARE NOT THIS ESTATE'S. The account holds
            # over a thousand `PRODUCTION_user_…` keys minted by a different
            # product; carrying them here would bury four facts under twelve
            # hundred and make the document about somebody else's inventory.
            continue
        out.append({
            "id": f"credential:openrouter/{k.get('name') or k['label']}",
            "kind": "llm-api-key",
            "provider": "openrouter",
            "name": k.get("name"),
            "label": k.get("label"),
            "serves": k.get("serves"),
            "limit": k.get("limit"),
            "limit_reset": k.get("limit_reset"),
            "usage": k.get("usage"),
            "disabled": k.get("disabled"),
            "created_on": (k.get("created_at") or "")[:10] or None,
            "source": "openrouter-listing",
        })
    return out


def from_vault(store: pathlib.Path) -> list[dict]:
    """One record per project secret. Names and dates only — never a value."""
    out = []
    if not store.is_dir():
        return out
    for slot in sorted(store.rglob("*")):
        if not slot.is_file() or slot.name.endswith((".meta.json", ".tmp")) \
                or ".retired-" in slot.name or slot.name == "leaks.jsonl":
            continue
        rel = slot.relative_to(store).parts
        if len(rel) != SLOT:
            continue
        project, env, name = rel
        meta_path = slot.with_name(slot.name + ".meta.json")
        meta = {}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
        out.append({
            "id": f"credential:vault/{project}/{env}/{name}",
            "kind": "project-secret",
            "provider": None,
            "name": name,
            "env": env,
            "vault_project": project,
            "created_on": (meta.get("created") or "")[:10] or None,
            "rotated_on": (meta.get("rotated") or "")[:10] or None,
            "rotations": meta.get("rotations") or 0,
            "source": "vault",
        })
    return out


#: The machine's shared secret store — the third class of credential, after
#: OpenRouter keys and vault slots. These are the files collectors and plugins
#: authenticate WITH: two Google service accounts, one Cloudflare token per
#: account, the gateway's own upstream keys. They were invisible to this
#: registry until 2026-09-12, which meant the estate could not answer "what
#: does this machine hold, and what reads it" — the question the whole
#: projection exists for.
#:
#: Names, sizes and dates only. Never a value, and never a read of the content
#: beyond what a service-account file states about itself in the clear.
MACHINE_STORE = paths.source_path("secret_store", paths.SECRETS)
#: Readers this registry knows by hand — the ones that are NOT plugins. A
#: plugin's reader is derived instead, from the `path:` requirement its own
#: manifest already declares: naming a plugin's id in a core file is the
#: coupling `tests/test_metric_series.py` forbids, and it is right to, because a
#: hand-written map goes stale the day a plugin is renamed.
MACHINE_READERS = {
    "openrouter": "agent/providers.py",
    "openrouter-provisioning": "tools/install_key.py",
    # The two credential doors: each stash is read by its door and
    # nothing else — that is the door's whole contract.
    "openrouter-admin": "tools/openrouter.py",
    "openrouter-issued.json": "tools/openrouter.py",
    "cloudflare-admin": "tools/cloudflare.py",
}


def _plugin_readers() -> dict[str, str]:
    """secret file name -> the plugin script that authenticates with it.

    Read from the manifests, so a plugin that changes its credential says so in
    one place and this follows.
    """
    out: dict[str, str] = {}
    pdir = pathlib.Path(__file__).resolve().parents[1] / "plugins"
    for man in sorted(pdir.glob("*.json")):
        try:
            doc = json.loads(man.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        script = doc.get("script")
        if not script:
            continue
        for req in doc.get("requires") or []:
            # The public manifests name their credential as `secret:NAME`,
            # relative to the configured store, so that shape IS the name. Only
            # checking `path:` left every plugin secret with no reader.
            if req.startswith("secret:"):
                name = pathlib.PurePosixPath(req[7:])
                if len(name.parts) == 1 and name.name not in {"", ".", ".."}:
                    out[name.name] = f"plugins/{script}"
                continue
            if not req.startswith("path:"):
                continue
            target = pathlib.Path(req[5:]).expanduser()
            # BY NAME, against the DEFAULT store rather than the one being
            # scanned: "which plugin reads a secret called X" is a property of
            # the manifests, not of where the store happens to sit. Comparing
            # against the scanned root made every reader None under a sandbox,
            # which is the one place this is tested.
            if target.parent == MACHINE_STORE:
                out[target.name] = f"plugins/{script}"
    return out


def from_machine_secrets(store: pathlib.Path | None = None) -> list[dict]:
    """One record per file in the machine's secret store. Values never read."""
    root = store or MACHINE_STORE
    out: list[dict] = []
    if not root.is_dir():
        return out
    readers = {**MACHINE_READERS, **_plugin_readers()}
    for p in sorted(root.iterdir()):
        if p.name.startswith(".") or p.name.endswith((".meta.json", ".tmp")):
            continue
        # THE VAULT IS NOT A MACHINE SECRET. `secrets/projects/` is the project
        # vault's own store, and every slot inside it already has a record from
        # `from_vault` — counting the directory again would report one credential
        # for the whole vault and inflate the totals the validator checks.
        if p.name == "projects":
            continue
        if p.is_dir():
            # A directory of per-account tokens is ONE capability with several
            # holders — counted as one credential carrying its own holder count,
            # because three Cloudflare logins are not three kinds of access.
            files = [c for c in sorted(p.iterdir())
                     if c.is_file() and not c.name.startswith(".")
                     # a .meta.json is the RECORD of a holder, not a holder
                     and not c.name.endswith(".meta.json")]
            if not files:
                continue
            newest = max(c.stat().st_mtime for c in files)
            out.append({
                "id": f"credential:machine/{p.name}",
                "kind": "machine-secret", "provider": p.name.split(".")[0],
                "name": p.name, "env": "local", "holders": len(files),
                "holder_labels": [c.name for c in files],
                "identity": None,
                "installed_on": _stamp(newest), "rotated_on": None,
                "read_by": readers.get(p.name),
                "unclaimed_reason": _machine_reason(readers.get(p.name)),
                "mode": oct(p.stat().st_mode)[-3:], "source": "machine-store"})
            continue
        if not p.is_file():
            continue
        # A service-account file states its own identity in the clear, and that
        # identity is what an operator needs to grant or revoke access. The
        # private key beside it is never touched.
        identity = None
        if p.name.endswith(".json"):
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
                if doc.get("type") == "service_account":
                    identity = doc.get("client_email")
            except (OSError, ValueError):
                identity = None
        out.append({
            "id": f"credential:machine/{p.name}",
            "kind": "machine-secret", "provider": p.name.split("-")[0],
            "name": p.name, "env": "local", "holders": 1, "holder_labels": [],
            "identity": identity,
            "installed_on": _stamp(p.stat().st_mtime), "rotated_on": None,
            "read_by": readers.get(p.name),
            "unclaimed_reason": _machine_reason(readers.get(p.name)),
            "mode": oct(p.stat().st_mode)[-3:], "source": "machine-store"})
    return out


#: A project keeps its own secrets beside its code, and this registry once could
#: not see them: it read the machine store and the vault and nothing else, so a
#: project's own `secrets/` directory (service-account keys, signing keys) was
#: invisible to the system that exists to say what this estate holds. Both
#: directory names, because projects spell it both ways and neither is more
#: correct.
PROJECT_SECRET_DIRS = ("secrets", ".secrets")
#: Files that are the ABSENCE of a secret rather than one.
NOT_A_SECRET = {".gitkeep", ".gitignore", ".DS_Store", "README.md", "readme.md"}


def from_project_secrets(root: pathlib.Path | None = None) -> list[dict]:
    """One record per file in a project's own `secrets/` directory.

    MEASURED THE SAME WAY AS THE MACHINE STORE: names, modes, dates, and, for a
    service-account JSON, the `client_email` it states about itself in the
    clear, which is the identifier an operator grants and revokes. Nothing else
    is read. The project is decided by CONTAINMENT, which needs no rule and
    cannot be wrong: the file is inside that folder.

    Git state is asked per file rather than per directory, because the
    interesting answer is per file: `secrets/.gitkeep` is tracked on purpose and
    a service-account key beside it is ignored, and a directory-level verdict
    would report the first and hide the second.
    """
    import subprocess
    data = root or paths.DATA
    out: list[dict] = []
    if not data.is_dir():
        return out
    for proj in sorted(p for p in data.iterdir() if p.is_dir() and not p.name.startswith(".")):
        for dirname in PROJECT_SECRET_DIRS:
            d = proj / dirname
            if not d.is_dir():
                continue
            # THE MACHINE STORE IS NOT A PROJECT'S OWN SECRETS. The estate root
            # may hold a symlink to the machine's secret store, so walking it
            # here would count every machine secret a second time, roughly
            # doubling the records. Resolved paths compare; names do not.
            try:
                if d.resolve() == MACHINE_STORE.resolve():
                    continue
            except OSError:
                continue
            is_repo = (proj / ".git").exists()
            for f in sorted(d.rglob("*")):
                if not f.is_file() or f.name in NOT_A_SECRET:
                    continue
                rel = f.relative_to(proj)
                git = "no-repo"
                if is_repo:
                    try:
                        tracked = safe_git.run(["ls-files", "--error-unmatch", str(rel)],
                                               repo=proj, timeout=30).returncode == 0
                        ignored = safe_git.run(["check-ignore", str(rel)],
                                               repo=proj, timeout=30).returncode == 0
                        git = "tracked" if tracked else ("ignored" if ignored else "loose")
                    except (OSError, subprocess.SubprocessError):
                        git = "unknown"
                identity = None
                if f.suffix == ".json":
                    try:
                        doc = json.loads(f.read_text(encoding="utf-8"))
                        if doc.get("type") == "service_account":
                            identity = doc.get("client_email")
                    except (OSError, ValueError):
                        identity = None
                st = f.stat()
                out.append({
                    "id": f"credential:project/{proj.name}/{rel.as_posix()}",
                    "kind": "project-secret-file",
                    "provider": (identity.split("@")[-1].split(".")[0] if identity else None),
                    "name": f.name,
                    "env": "local",
                    "in_project": proj.name,
                    "path": str(rel),
                    "identity": identity,
                    "git": git,
                    "mode": oct(stat.S_IMODE(st.st_mode))[-3:],
                    "installed_on": _stamp(st.st_mtime),
                    "rotated_on": None,
                    "source": "project-secrets",
                })
    return out


def _machine_reason(reader: str | None) -> str | None:
    """Why no project claims this: the precise answer where there is one.

    A machine secret belongs to the machine: the useful fact is not which
    project owns it but which code authenticates with it, because that is who
    breaks when it is rotated. Where the reader is unknown the generic reason
    downstream still applies, and saying nothing here is what lets it.
    """
    if not reader:
        return None
    return (f"a machine credential, not a project's: `{reader}` authenticates "
            f"with it, and that is what breaks when it is rotated")


def _stamp(mtime: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(
        mtime, datetime.timezone.utc).date().isoformat()


def leaks(store: pathlib.Path) -> dict[str, dict]:
    """slot -> the open leak against it. A settled leak is not a debt.

    Reads the schema `tools/vault.py` WRITES: `event: "leaked"` rows, and
    `event: "settled"` rows whose `of` names the row they close. The first
    version looked for a `settles` key no writer has ever produced, so a
    rotation could never have cleared a leak from this document — and the
    fixture that kept it green wrote rows in the reader's imagined shape
    rather than the writer's (found 2026-09-13, before the first settlement
    existed to expose it live). `tools/serverd.py:refresh_leaks` already read
    the writer's shape; this reader now agrees with both."""
    path = store / "leaks.jsonl"
    if not path.is_file():
        return {}
    rows, settled = {}, set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("event") == "settled":
            if leak_register.settles(r):
                settled.add(r["of"])
            continue
        if r.get("event") == "leaked" and r.get("secret"):
            rows[r["secret"]] = r
    return {k: v for k, v in rows.items() if v.get("id") not in settled}


def from_leaks(open_leaks: dict, have: set[str]) -> list[dict]:
    """A credential known ONLY because it leaked.

    A leak can be against a secret the vault does not hold: it is recorded where
    it was SEEN (a database URL in an app's config, an access secret pasted
    somewhere) and nobody had put it into the store. Without this the board
    carries `secret.leaked_unrotated` for a credential the registry cannot show,
    which is the worst of both: a debt with no subject.
    """
    out = []
    for slot, leak in sorted(open_leaks.items()):
        cid = f"credential:vault/{slot}"
        if cid in have:
            continue
        parts = slot.split("/")
        out.append({
            "id": cid,
            "kind": "leaked-untracked",
            "provider": None,
            "name": parts[-1],
            "env": parts[1] if len(parts) > 2 else None,
            "vault_project": parts[0] if parts else None,
            "created_on": None, "rotated_on": None, "rotations": 0,
            "source": "leak-register",
            "known_only_from_the_leak": True,
        })
    return out


def destination_edges(scan: dict, projects: list[dict]) -> dict[str, str]:
    """destination -> project id, where the file physically lives inside one.

    Measured, not inferred: `store/.openrouter-key` sits inside the observatory's
    own folder, so that key belongs to that project by containment, the same
    rule the Heroku subsystem uses for a nested checkout.
    """
    # `paths.DATA`, not a second copy of its resolution: the estate root is
    # redirectable for exactly the machines that keep their projects elsewhere,
    # and a duplicate reader is a redirect that silently does not apply.
    data = paths.DATA
    folders = []
    for p in projects:
        for f in p.get("local_folders") or []:
            folders.append((str(data / f) if not f.startswith("/") else f, p["id"]))
    out = {}
    for dest, path in (scan.get("destination_paths") or {}).items():
        best = [(len(d), pid) for d, pid in folders
                if path == d or path.startswith(d.rstrip("/") + "/")]
        if best:
            out[dest] = max(best)[1]
    return out


#: WHERE THE VALUE IS READ, per the rule that made the edge (docs/design/DEPLOYMENTS.md
#: rule 5, PB-129). A vault slot, a destination file and a file inside the project
#: are all on this machine: `local`. A curated edge says the project uses the key
#: and not where: `unknown`. `run` is never inferred here; it is added in the emit
#: step only where a production config var's fingerprint equals a vault slot's
#: (`run_edges`). `build` needs CI secrets, which nothing scans yet.
BINDING_BY_RULE = {"vault-path": "local", "destination-path": "local",
                   "inside-the-project": "local", "curated": "unknown"}


def run_edges(remote_doc: dict, heroku_records: list[dict], credential_ids: set[str],
              env_serves: dict[str, str]) -> list[dict]:
    """`credential_used_by` edges with binding `run`: a Heroku app whose config var holds,
    by salted fingerprint, the value of a vault slot. The project is the app's project;
    the environment is the one the app serves, when an override or its provider says so."""
    project_of = {r["name"]: r.get("project") for r in heroku_records}
    out, seen = [], set()
    for app in remote_doc.get("apps") or []:
        project = project_of.get(app.get("app"))
        for hit in app.get("vault_in_use") or []:
            cred = f"credential:vault/{hit['slot']}"
            if not project or cred not in credential_ids or (cred, app["app"]) in seen:
                continue
            seen.add((cred, app["app"]))
            e = {"id": f"relation:{cred.split(':', 1)[1]}:used-by:{project.split(':', 1)[1]}:run:{app['app']}",
                 "type": "credential_used_by", "from": cred, "to": project,
                 "rule": "config-var-fingerprint", "binding": "run", "deployment": f"heroku:{app['app']}",
                 "variable": hit["name"], "source_refs": ["SRC-0018"]}
            if env_serves.get(f"heroku:{app['app']}"):
                e["environment"] = env_serves[f"heroku:{app['app']}"]
            out.append(e)
    return out


def records(scan: dict, store: pathlib.Path, projects: list[dict]) -> tuple[list[dict], list[dict]]:
    """The credential records and the project edges they justify."""
    known = {p["id"] for p in projects}
    by_name = {p["name"]: p["id"] for p in projects}
    by_folder = {f: p["id"] for p in projects for f in (p.get("local_folders") or [])}
    REGISTER_PROBLEMS.clear()                                                               
    open_leaks = leaks(store)
    creds = (from_openrouter(scan) + from_vault(store) + from_machine_secrets()
             + from_project_secrets())
    creds += from_leaks(open_leaks, {c["id"] for c in creds})
    dest_owner = destination_edges(scan, projects)

    curated: dict[str, list[dict]] = {}
    for row in load_owners():
        curated.setdefault(row["credential"], []).append(row)

    edges, seen = [], set()

    def edge(cred_id: str, project_id: str, rule: str, refs: list[str], env: str | None = None) -> None:
        key = (cred_id, project_id)
        if project_id not in known or key in seen:
            return
        seen.add(key)
        e = {"id": f"relation:{cred_id.split(':', 1)[1]}:used-by:{project_id.split(':', 1)[1]}",
             "type": "credential_used_by", "from": cred_id, "to": project_id,
             "rule": rule, "binding": BINDING_BY_RULE[rule], "source_refs": refs}
        if environments.normalise(env):
            e["environment"] = f"environment:{project_id}/{environments.normalise(env)}"
        edges.append(e)

    for c in creds:
        # BOTH VAULT-SHAPED KINDS. `leaked-untracked` exists precisely because a
        # leak has no slot, and the first version asked only about
        # `project-secret` — so the records invented FOR the leaks were the one
        # set that could never be marked as leaked.
        slot = (c["id"].split("credential:vault/", 1)[-1]
                if c["kind"] in ("project-secret", "leaked-untracked") else None)
        leak = open_leaks.get(slot) if slot else None
        c["leaked"] = bool(leak)
        if leak:
            c["leaked_on"] = (leak.get("at") or "")[:10]
            c["leaked_where"] = (leak.get("where") or "")[:400]
        # MEASURED FIRST. A vault slot names its project in the path; an
        # OpenRouter key names the consumer it serves.
        if c.get("serves") and dest_owner.get(c["serves"]):
            edge(c["id"], dest_owner[c["serves"]], "destination-path", ["SRC-0014"])
        # A VAULT SLOT NAMES ITS PROJECT BY FOLDER AS OFTEN AS BY NAME.
        # A slot under a project's folder name belongs to the project the
        # registry may call something shorter; joined by name alone, most slots
        # read as unowned on the keys page while the path plainly said whose
        # they were. Folder first, name second: the same order `in_project`
        # uses below.
        _vp = c.get("vault_project")
        _vault_owner = (by_folder.get(_vp) or by_name.get(_vp)) if _vp else None
        if c["kind"] in ("project-secret", "leaked-untracked") and _vault_owner:
            # Only a real slot says which environment it is filed under; a leak
            # record borrows the register's wording and proves no environment.
            edge(c["id"], _vault_owner, "vault-path", ["SRC-0014"],
                 c.get("env") if c["kind"] == "project-secret" else None)
        elif c["kind"] == "project-secret" and _vp and not c.get("unclaimed_reason"):
            # THE PATH NAMES SOMETHING, and the generic sentence below says it
            # names nothing, which sent a reader to look for a missing vault
            # path that was there all along. A slot named for an organisation
            # rather than a product (a company's ad account token, say) matches
            # no project here. Say which name the registry does not know.
            c["unclaimed_reason"] = (
                f"the vault slot names {c['vault_project']!r}, which is no project in "
                f"this registry — a credential of an organisation rather than of one "
                f"product; the projects that use it are named in the curated file, or "
                f"it stays an estate-level key on the record")
        # CONTAINMENT NEEDS NO RULE: the file is inside that project's folder.
        # The folder is what the file is in, and the folder is not always the
        # project's NAME, so the folder index decides and the name is only the
        # fallback.
        if c.get("in_project"):
            pid = by_folder.get(c["in_project"]) or by_name.get(c["in_project"])
            if pid:
                edge(c["id"], pid, "inside-the-project", ["SRC-0014"])
        for row in curated.get(c["id"], []):
            for name in row["projects"]:
                pid = by_name.get(name, name if name.startswith("project:") else None)
                if pid:
                    edge(c["id"], pid, "curated", ["SRC-0014"])
        c["used_by"] = sorted(e["to"] for e in edges if e["from"] == c["id"])
        if not c["used_by"] and not c.get("unclaimed_reason"):
            # `not c.get(...)` because a record may already know a BETTER reason
            # than this one: a machine secret names the code that authenticates
            # with it, which is the thing that breaks when it is rotated, and
            # overwriting that with the generic sentence lost information
            #.
            c["unclaimed_reason"] = (
                "no vault path and no curated row names a project for it — a shared "
                "account's membership cannot be measured, and guessing it would send "
                "somebody to rotate a credential other projects are using")
    signatures = load_annotations()
    for c in creds:
        sig = signatures.get(c["id"])
        if sig:
            # CARRIED WHOLE, not unpacked into the record: `signature.purpose`
            # says where the sentence came from, and a bare `purpose` field on a
            # measured record would read as measured.
            c["signature"] = sig
    creds.sort(key=lambda c: (c["kind"], c["id"]))
    return creds, edges


def document(creds: list[dict], scan: dict, obs_date: str) -> dict:
    leaked = [c for c in creds if c.get("leaked")]
    return {
        "schema_version": 1,
        "updated_on": obs_date,
        "note": ("Every credential this estate tracks, as facts. NO VALUE IS EVER "
                 "RECORDED HERE — a `label` is what the provider itself calls a key "
                 "(`sk-or-v1-…`), never the key. Values live in "
                 "tools/vault.py's store and in the four OpenRouter destinations; "
                 "this document answers what exists, who may use it, and what has "
                 "leaked and not been rotated. `credential_used_by` is many-to-many: "
                 "one account serves several projects and one project uses several "
                 "accounts, so the edge is repeated rather than folded into a field."),
        "source_refs": ["SRC-0014"],
        "scanned_on": (scan.get("scanned_at") or "")[:10],
        "totals": {
            "credentials": len(creds),
            "by_kind": {k: sum(1 for c in creds if c["kind"] == k)
                        for k in sorted({c["kind"] for c in creds})},
            "claimed_by_a_project": sum(1 for c in creds if c.get("used_by")),
            "unclaimed": sum(1 for c in creds if not c.get("used_by")),
            "leaked_unrotated": len(leaked),
        },
        "credentials": creds,
        "destinations": scan.get("destinations") or {},
        "unlisted_destinations": scan.get("unlisted_destinations") or [],
        "degraded": scan.get("degraded") or [],
        # A curated register that exists and would not read. Empty is the
        # normal state; a row here is a board row (`credential.register_unreadable`)
        # and withholds `credential.unsigned`, which would otherwise fire on every
        # credential at once and say the opposite of what happened.
        "registers_unreadable": list(REGISTER_PROBLEMS),
    }
