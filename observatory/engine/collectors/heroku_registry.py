#!/usr/bin/env python3
"""Turn what Heroku said into typed records, and tie each one to a project.

TWO JOBS, AND THE SECOND IS THE HARD ONE.

The first is arithmetic: a scan row becomes a registry record with a derived
`state`, so that "is this thing running" is answered in ONE place rather than
re-derived by every reader. Five states, and the pairs that look alike are the
reason there are five: `suspended` is Heroku's decision and `down` is the
application's own crash; `resources-only` pays for a database with no dyno and
`idle` pays for nothing.

The second is the link, and one rule governs it: **never infer from a
name.** An app can deploy from a folder with an unrelated name, or from a
monorepo while a folder carrying the app's own name sits beside it with a remote
to the same application. A name-match gets both wrong and looks confident doing
it. So a link is made only where something was MEASURED, and every link carries
the rule that made it:

  `heroku-github-link`  Heroku's own Deploy tab names a repository, and the
                        registry already ties that repository to a project.
  `heroku-remote`       a folder holds `git.heroku.com/<app>.git` in its
                        `.git/config`, and the registry already ties that folder
                        to a project.
  `heroku-remote-nested` the same, for a checkout that sits INSIDE a folder the
                        registry ties to a project — containment on disk, which
                        is measured, not a name that looks similar.
  `verified`            a human resolved it and left the evidence in
                        `collectors/heroku_links.json` — the mechanism rule 2
                        names for the cases measurement cannot reach.

An application no rule reaches is UNLINKED and says so. That is a finding, not a
gap to paper over with a guess: five of them are applications whose source lives
in a GitHub account this machine cannot read.
"""
from __future__ import annotations
import json, pathlib, sys

# NOTHING RUNS AT IMPORT. This file is a transformer, not a collector: it takes
# a scan and the registry and returns records. A module-level `sys.path.insert`
# plus `import paths` made it indistinguishable from a collector to
# `tests/test_gate_purity.py`, whose rule is that a suite importing one runs a
# live collection — so the estate root is resolved where it is needed instead.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import paths                                            

LINKS = paths.config_file('heroku_links.json')

#: Stacks Heroku has superseded twice over. Not a judgement about whether they
#: still work — they do — but the count belongs on a screen, because the upgrade
#: is a decision somebody has to make and nothing else on this machine asks.
OLD_STACKS = {"heroku-18", "heroku-20", "heroku-22"}


def state_of(app: dict) -> str:
    """The one derivation of "is this running", so no reader invents a second.

    Order matters. `suspended` outranks everything because Heroku has taken the
    application away and the formation still says what it WOULD run. `down`
    means scaled and nothing up — the application's own crash, which is a
    different remedy from a suspension. `resources-only` is the expensive one:
    no dyno, but a database still billing.
    """
    if app.get("suspended"):
        return "suspended"
    if app.get("scaled", 0) > 0:
        return "running" if app.get("running", 0) > 0 else "down"
    return "resources-only" if app.get("addons") else "idle"


def _repo_owner_index(repos: list[dict], relations: list[dict]) -> dict[str, str]:
    """`name_with_owner` (lowercased) -> project id, through the registry's own edges."""
    by_id = {r["id"]: r for r in repos}
    out: dict[str, str] = {}
    for rel in relations:
        if rel.get("type") != "implemented_by":
            continue
        repo = by_id.get(rel.get("to"))
        if repo and repo.get("name_with_owner"):
            out.setdefault(repo["name_with_owner"].lower(), rel["from"])
    return out


def _data_root() -> pathlib.Path:
    """The estate root, resolved on use — env-overridable like everything here."""
    root = pathlib.Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import paths
    return paths.DATA


def _folder_index(projects: list[dict]) -> dict[str, str]:
    """Absolute folder path -> project id, for every folder a project claims."""
    data = _data_root()
    out: dict[str, str] = {}
    for p in projects:
        for f in p.get("local_folders") or []:
            out.setdefault(str(data / f) if not f.startswith("/") else f, p["id"])
    return out


def load_verified() -> dict[str, dict]:
    """Hand-verified app -> project links, each carrying its evidence.

    Rule 2 allows a link measurement cannot make ONLY with a human's evidence
    beside it, and this is where that evidence lives. A row with no `evidence`
    is refused rather than trusted: an unsourced hand link is the guess the rule
    exists to forbid, wearing a curator's clothes.
    """
    if not LINKS.is_file():
        return {}
    doc = json.loads(LINKS.read_text(encoding="utf-8"))
    out = {}
    for row in doc.get("links", []):
        if row.get("app") and row.get("project") and row.get("evidence"):
            out[row["app"]] = row
    return out


def curated_link_errors(links_doc: dict, project_ids: set[str],
                        app_names: set[str] | None = None) -> list[str]:
    """What is wrong with the curated links, as strings the gate can print.

    A PURE FUNCTION so the rule can be watched failing on a planted defect
    without writing into the operator's tree — `tools/validate_registry.py`
    calls it over the live files, and the suite calls it over fixtures.

    A curated link is a fact with a half-life: the project id it names is
    DERIVED from a folder or repository name, so renaming either kills the row
    silently and the application reads as unlinked with the human's evidence
    lost. Found by the 2026-09-09 audit; trap T11's shape.
    """
    out: list[str] = []
    for row in links_doc.get("links", []):
        app_name = row.get("app")
        if not row.get("evidence"):
            out.append(f"heroku_links row for {app_name} carries no evidence, so it is "
                       f"a guess rather than a verified link")
        project = row.get("project")
        if project and project not in project_ids:
            out.append(f"heroku_links names project {project} for {app_name}, which no "
                       f"longer exists — the curated link is dead and its application "
                       f"reads as unlinked")
        if app_names and app_name and app_name not in app_names:
            out.append(f"heroku_links names application {app_name}, which the scan no "
                       f"longer sees — it was deleted or renamed, and the row is stale")
    return out


def link(app: dict, repo_index: dict, folder_index: dict,
         verified: dict) -> tuple[str | None, str | None, str | None]:
    """(project id, rule, (kind, sentence)) — a link, or the reason there is none."""
    gh = (app.get("github") or "").lower()
    if gh and gh in repo_index:
        return repo_index[gh], "heroku-github-link", None
    for folder in app.get("local_folders") or []:
        if folder in folder_index:
            return folder_index[folder], "heroku-remote", None
    # A checkout INSIDE a project's folder. Physical containment, not a name:
    # a deploy checkout can be a repository of its own whose only remote is
    # heroku, sitting a level or two inside the project that owns it.
    # Longest containing folder wins, so a nested project beats its parent.
    for folder in app.get("local_folders") or []:
        owners = [(len(p), pid) for p, pid in folder_index.items()
                  if folder.startswith(p.rstrip("/") + "/")]
        if owners:
            return max(owners)[1], "heroku-remote-nested", None
    row = verified.get(app["name"])
    if row:
        return row["project"], "verified", None
    # THE REASON COMES IN TWO SIZES ON PURPOSE. The sentence is for a reader
    # who has stopped on one row; the kind is for a screen showing fifty, where
    # the same sentence printed fifteen times is not fifteen facts — it is one
    # fact and fourteen lines of noise, which is the defect `clone.stale`
    # already paid for once.
    if app.get("github"):
        return None, None, ("external-repo", f"Heroku deploys it from {app['github']}, "
                            f"which is not a repository this registry holds")
    if app.get("local_folders"):
        return None, None, ("folder-unclaimed", "a checkout on this machine carries its "
                            "remote, but no project claims that folder")
    return None, None, ("no-source", "Heroku names no repository, no checkout here "
                        "carries its remote, and no verified link names it")


def records(scan: dict, projects: list[dict], repos: list[dict],
            relations: list[dict]) -> tuple[list[dict], list[dict]]:
    """The registry's Heroku records, and the project->app edges they justify."""
    repo_index = _repo_owner_index(repos, relations)
    folder_index = _folder_index(projects)
    verified = load_verified()
    apps, edges = [], []
    for a in sorted(scan.get("apps", []), key=lambda x: x["name"]):
        project, rule, why = link(a, repo_index, folder_index, verified)
        rec = {
            "id": "heroku:" + a["name"],
            "name": a["name"],
            "team": a.get("team") or "personal",
            "region": a.get("region"),
            "stack": a.get("stack"),
            "stack_superseded": a.get("stack") in OLD_STACKS,
            "state": state_of(a),
            "maintenance": bool(a.get("maintenance")),
            "scaled": a.get("scaled", 0),
            "running": a.get("running", 0),
            "crashed": a.get("crashed") or [],
            "formation": a.get("formation") or [],
            "addons": a.get("addons") or [],
            "addons_attached": a.get("addons_attached") or [],
            "monthly_cost": a.get("monthly_cost", 0),
            "dyno_cost": a.get("dyno_cost", 0),
            "addon_cost": a.get("addon_cost", 0),
            "last_deploy_on": ((a.get("last_deploy") or {}).get("at") or "")[:10] or None,
            # What is running, as the release says it. Never the configured branch.
            "deployed_commit": ({"sha": a["last_deploy"]["commit"], "release": a["last_deploy"].get("version"),
                                 "source": "heroku-release-description"}
                                if (a.get("last_deploy") or {}).get("commit") else None),
            "last_release_on": ((a.get("last_release") or {}).get("at") or "")[:10] or None,
            "never_deployed": a.get("last_deploy") is None
                              and not a.get("deploy_beyond_window"),
            "github": a.get("github"),
            "auto_deploy": a.get("auto_deploy"),
            "local_folders": a.get("local_folders") or [],
            "web_url": a.get("web_url"),
            "project": project,
            "link_rule": rule,
        }
        if why:
            rec["unlinked_kind"], rec["unlinked_reason"] = why
        apps.append(rec)
        if project:
            edges.append({"id": f"relation:{project.split(':',1)[1]}:deployed-to:{a['name']}",
                          "type": "deployed_to", "from": project, "to": rec["id"],
                          "rule": rec.get("link_rule"), "source_refs": ["SRC-0013"]})
    return apps, edges


def document(scan: dict, apps: list[dict], obs_date: str) -> dict:
    linked = sum(1 for a in apps if a["project"])
    return {
        "schema_version": 1,
        "updated_on": obs_date,
        "note": ("What Heroku says is running, measured by collectors/scan_heroku.py. "
                 "`monthly_cost` is the published on-demand rate times what is scaled "
                 "plus billed_price for add-ons this application OWNS — an upper bound, "
                 "not an invoice, and add-ons attached from another application are "
                 "listed separately because their owner is billed for them. Every "
                 "`project` carries the `link_rule` that made it; an application no "
                 "rule reaches says why in `unlinked_reason` rather than being guessed."),
        "source_refs": ["SRC-0013"],
        "scanned_on": (scan.get("scanned_at") or "")[:10],
        "account": scan.get("account"),
        "teams": scan.get("teams") or [],
        "price_list": scan.get("price_list") or {},
        "totals": {
            "apps": len(apps),
            "linked_to_a_project": linked,
            "unlinked": len(apps) - linked,
            "dynos": sum(a["scaled"] for a in apps),
            "monthly_cost": round(sum(a["monthly_cost"] for a in apps), 2),
            "by_state": {s: sum(1 for a in apps if a["state"] == s)
                         for s in ("running", "down", "suspended",
                                   "resources-only", "idle")},
        },
        "apps": apps,
        "remotes_to_unknown_apps": scan.get("remotes_to_unknown_apps") or [],
        "degraded": scan.get("degraded") or [],
    }
