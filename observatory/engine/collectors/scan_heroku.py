#!/usr/bin/env python3
"""Ask Heroku what is actually running, and what it costs to leave it running.

WHY. The registry knows what a project IS (repositories, folders, commits) and
nothing about where it is HOSTED. A crashed dyno, an application Heroku has
suspended, or an application whose whole content is a database nobody deploys
to are all facts about projects this repository already watches, and none of
them can reach a finding rule unless a collector records them.

WHAT IT DOES NOT DO. It never decides which project an application belongs to.
It records what Heroku says (the GitHub repository the Deploy tab is wired to,
and the application's own name) and `collectors/merge.py` ties that to a
project through a repository or a folder the registry ALREADY owns. Inferring a
project from an application's name is a guess the repository rules forbid, and
it is often wrong: an application's name need not match the repository it
deploys from.

CREDENTIALS. The token comes from `heroku auth:token` at run time and is never
written anywhere: not to the raw file, not to the registry, not to a log. The
CLI already holds the operator's session; this borrows it for one process.

DEGRADATION. Four things can go wrong and each is NAMED rather than swallowed:
the CLI is absent, the CLI is not logged in, the API refuses, or one application
answers 403 because Heroku SUSPENDED it. The last one is the reason this file
does not treat an error as an empty list: a suspended application returns 403
on `/dynos`, and reading that as "no dynos" turns "Heroku turned this off" into
"scaled but never started", which is a different diagnosis with a different
remedy.

    scan_heroku.py store/raw/heroku.json [--only <app> ...]
"""
from __future__ import annotations
import concurrent.futures as cf
import json
import re, pathlib, re, shutil, subprocess, sys, urllib.error, urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import atomic              
import paths              
import slow_command

API = "https://api.heroku.com"
#: The Deploy tab's GitHub link is NOT in the Platform API. It lives on the
#: dashboard's own service, and it is the only thing that says where a running
#: application is deployed FROM. Nineteen of the fifty-nine have one.
KOLKRABBI = "https://kolkrabbi.heroku.com"
WORKERS = 10
TIMEOUT = 60
#: Deep enough to walk past a run of config releases and find the last one
#: that moved code. A shallow window misreads an application whose newest
#: releases are all add-on or config changes as never deployed.
RELEASE_WINDOW = 200

#: On-demand list price per dyno per month. NOT an invoice: Heroku bills per
#: second, so an estimate built from this is an upper bound and the registry
#: says so in the document's own note. A size absent from this map is reported
#: in `unpriced` rather than silently costed at zero — a price list that grows a
#: hole when Heroku adds a tier would understate the estate for ever.
DYNO_PRICE = {"Eco": 5, "Basic": 7, "Standard-1X": 25, "Standard-2X": 50,
              "Performance-M": 250, "Performance-L": 500, "Performance-L-RAM": 500,
              "Performance-XL": 750, "Performance-2XL": 1500}

#: A release description that moved code. Everything else Heroku calls a release
#: too: `Set X config vars`, `Enable Logplex`, `Attach DATABASE`, and the
#: releases the add-on services write for themselves.
CODE_RELEASE = ("Deploy ", "Deployed ", "Promote ", "Rollback ")

#: `https://git.heroku.com/<app>.git` in a checkout's `.git/config`. Both the
#: https and the ssh spelling, because the CLI has written both over the years.
HEROKU_REMOTE = re.compile(r"git\.heroku\.com[:/]+([A-Za-z0-9][A-Za-z0-9-]*)\.git")


#: The token's first limit, and the longer one a second attempt gets.
TOKEN_TIMEOUTS = (30, 60)


def token() -> tuple[str | None, str | None]:
    """The CLI's session token, or the reason there is none."""
    if not shutil.which("heroku"):
        return None, ("the heroku CLI is not on PATH; install it or run this "
                      "step on a machine that has it")
    # ASKED AGAIN BEFORE IT IS REPORTED. The CLI is a Node program that starts
    # slowly on a loaded machine: measured at load average ~200 it took 31 s once
    # and 8-13 s on the next three tries, and that one miss emptied the whole
    # Heroku survey for a tick. A second, longer attempt rides out the spike; a
    # CLI that misses both is reported with both durations and the load.
    try:
        p, slow = slow_command.run(["heroku", "auth:token"], timeouts=TOKEN_TIMEOUTS,
                                   backoff=5.0, capture_output=True, text=True)
    except OSError as exc:
        return None, f"heroku auth:token could not run: {type(exc).__name__}"
    if p is None:
        return None, f"{slow} — the CLI did not answer, so no app could be read"
    if p.returncode != 0:
        # The CLI prints its own reason and it is more useful than ours.
        return None, ("heroku auth:token failed: "
                      + (p.stderr.strip().splitlines() or ["no output"])[-1])
    tok = (p.stdout.strip().splitlines() or [""])[-1].strip()
    if not tok:
        return None, "heroku auth:token printed nothing; the CLI is not logged in"
    return tok, None


def _headers(tok: str, accept: str) -> dict:
    return {"Accept": accept, "Authorization": f"Bearer {tok}",
            "User-Agent": "project-observatory"}


def get(url: str, tok: str, *, accept: str = "application/vnd.heroku+json; version=3",
        extra: dict | None = None):
    """One request. Returns the decoded body, or a dict naming the failure.

    A failure is a VALUE here rather than an exception because a suspended
    application is a legitimate answer to `/dynos` and the caller has to be able
    to tell it apart from a network problem.
    """
    head = _headers(tok, accept)
    if extra:
        head.update(extra)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=head),
                                    timeout=TIMEOUT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:400]
        reason = ""
        try:
            reason = json.loads(body).get("id") or ""
        except Exception:
            pass
        return {"__error__": f"{e.code}", "__reason__": reason, "__body__": body}
    except Exception as e:                                                  
        return {"__error__": "transport", "__reason__": type(e).__name__,
                "__body__": str(e)[:200]}


CONFIG_SET = re.compile(r"^Set (?P<names>.+?) config vars?$")
CONFIG_ADDON = re.compile(r"^Update (?P<name>[A-Z0-9_]+) by (?P<addon>[a-z0-9-]+)$")


_DEPLOY_SHA = re.compile(r"^Deploy ([0-9a-f]{7,40})$")


def deployed_commit(desc: str | None) -> str | None:
    """The commit a code release says it deployed, or None.

    Heroku writes "Deploy 1a2b3c4d" for a git or GitHub deploy. A rollback or a
    promotion names a release, not a commit, and gets None rather than a guess.
    The match is exact on purpose: "Deploy main" or "Deploy feature-cafe123"
    names a branch, and a branch is where deploys come from, not what is
    running (PB-123). heroku_registry publishes it as `deployed_commit` with its
    source, so a reader can tell a stated commit from a missing one.
    """
    m = _DEPLOY_SHA.match(desc or "")
    return m.group(1) if m else None


def config_trail(rels: list) -> list[dict]:
    """Releases that changed config vars, newest first — version, date, the
    variable NAMES, and the addon if one did it. Values never appear in a
    release description, and none is read here."""
    out = []
    for r in rels or []:
        desc = (r.get("description") or "").strip()
        m = CONFIG_SET.match(desc)
        if m:
            out.append({"version": r.get("version"), "at": r.get("created_at"),
                        "vars": [v.strip() for v in m.group("names").split(",") if v.strip()],
                        "by": "config:set"})
            continue
        m = CONFIG_ADDON.match(desc)
        if m:
            out.append({"version": r.get("version"), "at": r.get("created_at"),
                        "vars": [m.group("name")], "by": m.group("addon")})
    return out[:40]


def paged(path: str, tok: str) -> list | dict:
    """`GET` a collection to the end of its `Next-Range`.

    Heroku pages with a `Range` header and answers 206 with `Next-Range` when
    more remains. A collector that reads page one and stops reports a small
    estate, which looks exactly like a small estate.
    """
    out, rng = [], None
    while True:
        head = _headers(tok, "application/vnd.heroku+json; version=3")
        if rng:
            head["Range"] = rng
        try:
            req = urllib.request.Request(API + path, headers=head)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                out.extend(json.loads(r.read().decode()))
                nxt = r.headers.get("Next-Range")
        except urllib.error.HTTPError as e:
            return {"__error__": f"{e.code}", "__body__": e.read().decode()[:200]}
        except Exception as e:                                              
            return {"__error__": "transport", "__body__": str(e)[:200]}
        if not nxt:
            return out
        rng = nxt


def owned_addons(addons: list, app_name: str) -> tuple[list, list]:
    """Split what this application PAYS FOR from what it merely uses.

    `/apps/<id>/addons` returns add-ons attached from other applications
    alongside owned ones. Summing the list whole counts a shared database once
    per application that uses it, inflating the estate's cost and making an
    application look as if it pays for a database it does not own.
    """
    own, attached = [], []
    for a in addons:
        row = {"name": a.get("name"),
               "plan": ((a.get("plan") or {}).get("name")),
               "state": a.get("state"),
               "cents": ((a.get("billed_price") or {}).get("cents") or 0),
               "owner": ((a.get("app") or {}).get("name"))}
        (own if row["owner"] == app_name else attached).append(row)
    return own, attached


def scan_app(app: dict, tok: str) -> dict:
    aid, name = app["id"], app["name"]
    row = {
        "name": name, "id": aid,
        "team": ((app.get("team") or {}).get("name")
                 or (app.get("organization") or {}).get("name") or "personal"),
        "owner": (app.get("owner") or {}).get("email"),
        # The account ids the provider states (DEPLOYMENTS.md rule 3): a team's
        # id, or for a personal app its owner's user id. Never the e-mail.
        "team_id": ((app.get("team") or {}).get("id") or (app.get("organization") or {}).get("id")),
        "owner_id": (app.get("owner") or {}).get("id"),
        "region": (app.get("region") or {}).get("name"),
        "stack": (app.get("stack") or {}).get("name"),
        "created_at": app.get("created_at"), "released_at": app.get("released_at"),
        "maintenance": bool(app.get("maintenance")),
        "web_url": app.get("web_url"), "git_url": app.get("git_url"),
    }
    form = get(f"{API}/apps/{aid}/formation", tok)
    dynos = get(f"{API}/apps/{aid}/dynos", tok)
    addons = get(f"{API}/apps/{aid}/addons", tok)
    rels = get(f"{API}/apps/{aid}/releases", tok,
               extra={"Range": f"version ..; max={RELEASE_WINDOW}, order=desc"})
    gh = get(f"{KOLKRABBI}/apps/{aid}/github", tok, accept="application/json")
    # CUSTOM DOMAINS, because a zone's CNAME says `xyz.herokudns.com` and never
    # the app's name: the only way from a domain to the application serving it
    # is Heroku's own list of which hostnames it accepts for the app.
    # THE PIPELINE STAGE is Heroku's own statement of which environment an app
    # serves (docs/design/DEPLOYMENTS.md rule 1). 404 means the app is in no
    # pipeline, which is an answer; any other failure is kept as an error.
    pc = get(f"{API}/apps/{aid}/pipeline-couplings", tok)
    if isinstance(pc, dict) and not pc.get("__error__"):
        row["pipeline"] = {"id": (pc.get("pipeline") or {}).get("id"),
                           "name": (pc.get("pipeline") or {}).get("name"), "stage": pc.get("stage")}
        row["pipeline_error"] = None
    else:
        row["pipeline"] = None
        row["pipeline_error"] = (None if isinstance(pc, dict) and pc.get("__error__") == "404"
                                 else (pc.get("__error__") if isinstance(pc, dict) else "unexpected"))
    doms = get(f"{API}/apps/{aid}/domains", tok)
    row["domains"] = (sorted(d["hostname"] for d in doms
                             if isinstance(d, dict) and d.get("kind") == "custom" and d.get("hostname"))
                      if isinstance(doms, list) else [])
    row["domains_error"] = doms.get("__error__") if isinstance(doms, dict) else None

    row["formation"] = ([{"type": f["type"], "qty": f["quantity"], "size": f["size"],
                          "command": (f.get("command") or "")[:160]}
                         for f in form] if isinstance(form, list) else [])
    row["formation_error"] = form.get("__error__") if isinstance(form, dict) else None

    # SUSPENDED IS A STATE, NOT A FAILURE TO READ. Heroku answers 403 with
    # `{"id":"suspended"}` and an empty dyno list would read as "scaled but
    # never started" — a different diagnosis with a different remedy.
    row["suspended"] = (isinstance(dynos, dict)
                        and dynos.get("__reason__") == "suspended")
    row["dynos"] = ([{"name": d["name"], "state": d["state"], "size": d["size"]}
                     for d in dynos] if isinstance(dynos, list) else [])
    row["dynos_error"] = (None if isinstance(dynos, list) or row["suspended"]
                          else dynos.get("__error__"))

    if isinstance(addons, list):
        own, attached = owned_addons(addons, name)
    else:
        own, attached = [], []
    row["addons"] = own
    row["addons_attached"] = attached
    row["addons_error"] = addons.get("__error__") if isinstance(addons, dict) else None

    rels = rels if isinstance(rels, list) else []
    row["releases_seen"] = len(rels)
    row["release_version"] = rels[0]["version"] if rels else None
    code = next((r for r in rels
                 if (r.get("description") or "").startswith(CODE_RELEASE)), None)
    row["last_release"] = ({"version": rels[0]["version"], "at": rels[0]["created_at"],
                            "desc": (rels[0].get("description") or "")[:120]}
                           if rels else None)
    # THE CONFIG TRAIL, names only. Heroku describes a config change as
    # "Set A, B config vars" and a Postgres credential rotation as an update
    # of DATABASE by the add-on: variable NAMES in both, never values.
    # Kept so the board can say "a change touching DATABASE happened after the
    # leak; if that was the rotation, settle it" instead of carrying a critical
    # for a rotation that already happened.
    row["config_releases"] = config_trail(rels)
    row["last_deploy"] = ({"version": code["version"], "at": code["created_at"],
                           "desc": (code.get("description") or "")[:120],
                           "commit": deployed_commit(code.get("description"))}
                          if code else None)
    # `releases_seen == RELEASE_WINDOW` and no code release means the window ran
    # out, not that no deploy exists. Said out loud so a reader cannot mistake a
    # truncation for a fact.
    row["deploy_beyond_window"] = (code is None and len(rels) >= RELEASE_WINDOW)

    row["github"] = (gh.get("repo") if isinstance(gh, dict) and "repo" in gh else None)
    row["github_branch"] = (gh.get("branch") if row["github"] else None)
    row["auto_deploy"] = (bool(gh.get("auto_deploy")) if row["github"] else None)
    row["wait_for_ci"] = (bool(gh.get("wait_for_ci")) if row["github"] else None)

    sizes = [f["size"] for f in row["formation"] if f["qty"]]
    row["unpriced"] = sorted({s for s in sizes if s not in DYNO_PRICE})
    row["dyno_cost"] = sum(DYNO_PRICE.get(f["size"], 0) * f["qty"]
                           for f in row["formation"])
    row["addon_cost"] = sum(a["cents"] for a in own) / 100.0
    row["monthly_cost"] = round(row["dyno_cost"] + row["addon_cost"], 2)
    row["scaled"] = sum(f["qty"] for f in row["formation"])
    row["running"] = sum(1 for d in row["dynos"] if d["state"] == "up")
    row["crashed"] = sorted(d["name"] for d in row["dynos"] if d["state"] != "up")
    return row


def local_checkouts() -> tuple[dict[str, list[str]], list[str]]:
    """Which folders on this machine carry a git remote pointing at which app.

    `collectors/scan_remotes.py` records ONE remote per repository (the one it
    asks whether the clone is current), so it does not see a
    `git.heroku.com/<app>.git` remote held beside it. That remote is a fact
    about deployment, not about sync, which is why it is read here rather than
    added to a collector that exists to answer a different question.

    Returned as app -> folders, PLURAL, and the plural is the point: two folders
    can carry a remote for the same application, and the first one found is not
    reliably the live source.
    """
    found: dict[str, list[str]] = {}
    seen: list[str] = []
    root = paths.DATA
    if not root.is_dir():
        return found, seen
    # THREE LEVELS, and the third is not padding: an application can deploy
    # from a repository nested two folders inside a project whose only remote
    # is Heroku's. Two levels would report it as having no checkout at all.
    for cfg in (sorted(root.glob("*/.git/config")) + sorted(root.glob("*/*/.git/config"))
                + sorted(root.glob("*/*/*/.git/config"))):
        try:
            text = cfg.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        folder = str(cfg.parent.parent)
        seen.append(folder)
        for m in HEROKU_REMOTE.finditer(text):
            found.setdefault(m.group(1), []).append(folder)
    return found, seen


def main(argv: list[str]) -> int:
    import configuration
    if not configuration.enabled("heroku"):
        print("heroku: not configured (integration disabled)")
        return 0
    out_path = pathlib.Path(argv[1]) if len(argv) > 1 else paths.STORE / "raw/heroku.json"
    only = set(argv[argv.index("--only") + 1:]) if "--only" in argv else None
    started = datetime.now(timezone.utc)
    degraded: list[dict] = []

    tok, why = token()
    if not tok:
        # AN EMPTY SURVEY IS NEVER RETURNED IN PLACE OF A PARTIAL ONE
        # (honest degradation). The file is written so downstream steps find a
        # shape rather than a missing path, and it says why it is empty.
        atomic.write_json(out_path, {
            "schema_version": 1, "scanned_at": started.isoformat(),
            "account": None, "teams": [], "apps": [],
            "degraded": [{"source": "heroku", "reason": why}],
        }, indent=1)
        print(f"heroku: not scanned — {why}", file=sys.stderr)
        return 0

    account = get(f"{API}/account", tok)
    email = account.get("email") if isinstance(account, dict) and "email" in account else None
    teams = get(f"{API}/teams", tok)
    team_names = sorted(t["name"] for t in teams) if isinstance(teams, list) else []
    if isinstance(teams, dict):
        degraded.append({"source": "heroku:teams", "reason": teams.get("__error__")})

    apps = paged("/apps", tok)
    if isinstance(apps, dict):
        atomic.write_json(out_path, {
            "schema_version": 1, "scanned_at": started.isoformat(),
            "account": email, "teams": team_names, "apps": [],
            "degraded": degraded + [{"source": "heroku:apps",
                                     "reason": f"GET /apps failed: {apps.get('__error__')}"}],
        }, indent=1)
        print(f"heroku: GET /apps failed ({apps.get('__error__')})", file=sys.stderr)
        return 1
    if only:
        apps = [a for a in apps if a["name"] in only]

    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        rows = list(ex.map(lambda a: scan_app(a, tok), apps))
    rows.sort(key=lambda r: r["name"])

    checkouts, folders_read = local_checkouts()
    for r in rows:
        r["local_folders"] = sorted(checkouts.get(r["name"], []))
    # A remote pointing at an application this account cannot see is not noise:
    # it is either a deleted application still wired to a folder, or an account
    # this run was not logged into. Either way the folder is misleading and the
    # operator should hear the name.
    known = {r["name"] for r in rows}
    orphan_remotes = sorted(set(checkouts) - known)

    for r in rows:
        for field in ("formation_error", "dynos_error", "addons_error"):
            if r.get(field):
                degraded.append({"source": f"heroku:{r['name']}",
                                 "reason": f"{field.split('_')[0]} unreadable: {r[field]}"})
        if r["unpriced"]:
            degraded.append({"source": f"heroku:{r['name']}",
                             "reason": ("dyno size(s) absent from the price list, so this "
                                        f"application's cost is understated: {r['unpriced']}")})
        if r["deploy_beyond_window"]:
            degraded.append({"source": f"heroku:{r['name']}",
                             "reason": (f"no code release within the newest {RELEASE_WINDOW}; "
                                        "the last deploy is older than the window, not absent")})

    atomic.write_json(out_path, {
        "schema_version": 1,
        "scanned_at": started.isoformat(),
        "account": email,
        "teams": team_names,
        "price_list": DYNO_PRICE,
        "folders_read": len(folders_read),
        "remotes_to_unknown_apps": orphan_remotes,
        "apps": rows,
        "degraded": degraded,
    }, indent=1)
    cost = round(sum(r["monthly_cost"] for r in rows), 2)
    wired = sum(1 for r in rows if r["local_folders"])
    print(f"heroku.json: {len(rows)} app(s), {sum(r['scaled'] for r in rows)} dyno(s), "
          f"${cost}/month, {wired} with a local checkout, {len(degraded)} degradation(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
