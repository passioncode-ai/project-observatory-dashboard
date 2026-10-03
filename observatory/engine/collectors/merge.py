import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import atomic
import estate
import identity
import paths
import configuration
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import local_scan
import scan_remotes
import json, re, subprocess, sys
from pathlib import Path
SP=Path(sys.argv[1]) if len(sys.argv)>1 else paths.SCRATCH; V=paths.VAULT
ownership_file = paths.config_file("ownership.json")
ownership = json.loads(ownership_file.read_text()) if ownership_file.is_file() else {}
for field in ("organizations", "work_organizations"):
    value = ownership.get(field, [])
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise ValueError("Ownership organizations must be lists of non-empty names")
OWNED_ORGS = set(ownership.get("organizations", []))
WORK_ORGS = set(ownership.get("work_organizations", []))
degraded: list[dict] = []
OWNED_DOMAINS={d["name"] for d in json.load(open(paths.REGISTRY/"domains.json"))["domains"]}
# The Heroku chain (a zone's host -> the app serving it -> its project) is
# joined in emit_registry.py, not here: the app->project link is computed there,
# and reading the previous emit's projection from the merge was a read that ran
# before its writer — a cycle the pipeline graph refuses.
# Zone names the estate holds (store/raw/cloudflare_zones.json, scanned before
# the merge): an env or nginx line naming one of them is the project's own
# surface; naming anything else is the project CONSUMING a provider's host.
try:
    ZONE_NAMES={z["name"] for z in json.load(open(paths.SCRATCH/"cloudflare_zones.json"))["zones"]}
except (OSError, ValueError, KeyError):
    ZONE_NAMES=set()
# Curated host -> project adoptions (the other side of collectors/host_boundary.json).
try:
    CLAIMS={h.lower():c for h,c in json.load(open(paths.config_file('domain_claims.json')))["claims"].items()}
except (OSError, ValueError, KeyError):
    CLAIMS={}
THIRD_PARTY={"github.com","replit.com","stackblitz.com","twitter.com","lobehub.com",
             "getoutline.com","postiz.com","desktop.telegram.org","officialskills.sh",
             "leomehlig.github.io","t.me","help.crypt.bot"}
gh={}
for f in sorted((SP/"gh").glob("*.json")):
    if f.name.startswith("_"): continue
    for r in json.load(open(f)): gh.setdefault(r["nameWithOwner"], r)
# Through `local_scan`, which accepts the bare list the scan used to write and
# the object it writes now. A reader that crashed on one shape would turn a
# shape change into a half-hour outage.
local={l["folder"]: l for l in local_scan.folders(SP/"local.json")}
if (SP / "vault.json").is_file():
    vault={v["folder"]: v for v in json.loads((SP / "vault.json").read_text())}
else:
    vault={}
    degraded.append({"source":"wiki", "reason":"wiki scan unavailable; project notes were not measured"})
if not gh:
    degraded.append({"source":"github", "reason":"no repository listing available; remote account coverage was not measured"})

def _optional(name, key, default):
    """A collector that needs the network is optional here, never assumed.

    Missing input must read as "not measured this run", not as "measured and
    empty" — the second is a claim, and it is the one that quietly deletes
    fields the last run had filled in."""
    f = SP/name
    if not f.is_file():
        degraded.append({"source":name, "reason":"optional collector output unavailable; not measured"})
        return default
    return json.loads(f.read_text()).get(key, default)

#: The last session date per project, from claude-mem. Optional by the same rule
#: as every other collector output: a missing file means "not measured", not "no
#: work happened" — and the second reading is what made `last_activity_on` a
#: measure of COMMITS while calling itself activity.
SESSIONS={}
for _s in _optional("sessions.json", "sessions", []):
    _d = _s.get("ended_on") or _s.get("started_on") or ""
    if _d and _d > SESSIONS.get(_s["project_id"], ""):
        SESSIONS[_s["project_id"]] = _d

#: The same dates keyed by the SLUG, so a project whose id was overridden still
#: finds its sessions. `ID_OVERRIDE` lives in the emitter, so neither map alone
#: is complete and both are consulted.
SESSIONS_BY_SLUG={k.split(":",1)[1]: v for k, v in SESSIONS.items()}

REMOTES=_optional("remotes.json", "repositories", {})
BB={r["full_name"]: r for r in _optional("bitbucket.json", "repositories", [])}
def nwo(remote):
    if not remote: return None
    m=re.search(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?$", remote)
    if m: return ("github", f"{m.group(1)}/{m.group(2)}")
    m=re.search(r"bitbucket\.org[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?$", remote)
    if m: return ("bitbucket", f"{m.group(1)}/{m.group(2)}")
    return None
# ---------- repositories ----------
repos={}
for k,r in gh.items():
    repos[k]={"key":k,"host":"github","owner":k.split("/")[0],"name":k.split("/")[1],
        "url":r["url"],"description":(r.get("description") or "").strip(),
        "homepage":(r.get("homepageUrl") or "").strip(),"visibility":r["visibility"].lower(),
        "archived":r["isArchived"],"fork":r["isFork"],
        "language":(r.get("primaryLanguage") or {}).get("name") or "",
        "pushed_at":(r.get("pushedAt") or "")[:10],"created_at":(r.get("createdAt") or "")[:10],
        "topics":[t["name"] for t in (r.get("repositoryTopics") or [])],
        "default_branch":(r.get("defaultBranchRef") or {}).get("name") or "",
        "local":None,"source":"github-api"}
#: Everything this run could not measure, in the shape every other collector
#: uses. `merge.py` had NO degradation channel at all — measured 2026-09-07 with
#: `grep -c degraded` — and it is the one collector whose output every other
#: module reads. See `PREVIOUS_TRANSFERS` for what that cost.
# Preserve optional-input degradation gathered above.

#: The transfer map the LAST run wrote, read before this one overwrites it.
#: Without `gh` on PATH the transfer check would silently answer "not moved" for
#: every address, and a moved repository's old address would enter the model as
#: a repository of its own — sometimes anchoring a phantom project, always
#: producing a spurious duplicate-name pair — with nothing reported, and the
#: emitter would then write it into the canonical registry.
#:
#: So an answer that could not be re-measured falls back to the previous run's,
#: which is the same rule `collectors/listing_guard.py` applies to a listing:
#: older beats wrong, provided it says how old it is.
def _previous_transfers() -> dict:
    f = SP / "model.json"
    if not f.is_file():
        return {}
    try:
        return json.load(open(f)).get("transfers_followed") or {}
    except (ValueError, OSError):
        return {}

PREVIOUS_TRANSFERS = _previous_transfers()


def canonical(host: str, k: str) -> tuple[str, str | None, str]:
    """Follow a GitHub transfer. Returns (nwo, old_nwo_if_moved, why_unchecked).

    A repository that was transferred keeps its old path working as a redirect,
    so a local clone whose remote was never updated names an address that no
    longer belongs to anyone. Recorded as-is it becomes a PHANTOM repository,
    counted twice under two names. Only resolved for names the listing did not
    return, so a tick costs a handful of calls rather than one per checkout.

    THREE outcomes, not two. "Not moved" and "could not ask" must not be the
    same answer: a missing `gh`, an expired token or a 500 would otherwise all
    read as "this address is fine". `why_unchecked` is non-empty exactly when
    the question could not be asked, so the caller can degrade honestly.
    """
    if host != "github":
        return k, None, ""
    if not configuration.enabled("github"):
        return k, None, "GitHub integration disabled; repository transfer not checked"
    try:
        out = subprocess.run(["gh", "api", f"repos/{k}", "--jq", ".full_name"],
                             capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return k, None, "`gh` is not installed, so no transfer could be followed"
    except Exception as exc:
        return k, None, f"{type(exc).__name__}: {exc}"
    real = out.stdout.strip()
    if out.returncode != 0:
        # A 404 IS an answer: the address does not exist under any name. Anything
        # else — no credential, rate limit, network — is a question not asked.
        err = (out.stderr or "").strip()[:120]
        if "Not Found" in err or "404" in err:
            return k, None, ""
        return k, None, f"`gh api repos/{k}` exited {out.returncode}: {err}"
    if not real or real == k:
        return k, None, ""
    return real, k, ""


moved: dict[str, str] = {}
unchecked: dict[str, str] = {}
for folder,l in local.items():
    if not l["is_git"]: continue
    hk=nwo(l.get("remote"))
    if not hk: continue
    host,k=hk
    if k not in repos:
        asked = k                                                                 
        real, was, why = canonical(host, k)
        if why:
            # The previous run's answer, or an admitted gap. Either way the
            # address is named — and named as ASKED, not as resolved: a source
            # that names the new address while its reason talks about the old
            # one sends the reader to the wrong clone.
            carried = PREVIOUS_TRANSFERS.get(asked)
            if carried:
                moved[asked] = carried
                k = carried
                degraded.append({"source": f"transfer:{asked}", "reason":
                    f"could not re-check whether {asked} has moved ({why}); using "
                    f"the previous run's answer, {asked} -> {carried}"})
            else:
                unchecked[asked] = why
                degraded.append({"source": f"transfer:{asked}", "reason":
                    f"could not check whether {asked} has moved ({why}), and no "
                    f"earlier run had answered. It is recorded as it stands: if "
                    f"the clone's remote is stale, this is a repository that does "
                    f"not exist"})
        elif was:
            moved[was] = real
            k = real
    if k not in repos:
        repos[k]={"key":k,"host":host,"owner":k.split("/")[0],"name":k.split("/")[1],
            "url":(f"https://github.com/{k}" if host=="github" else f"https://bitbucket.org/{k}"),
            "description":"","homepage":"","visibility":"unknown","archived":False,"fork":False,
            "language":"","pushed_at":"","created_at":"","topics":[],"default_branch":"","local":None,
            "source":"local-remote-only"}
    b=BB.get(k)
    if b and repos[k].get("source")=="local-remote-only":
        # The listing is the only source for these; a local clone cannot know
        # whether its origin is private, archived, or described.
        repos[k].update(description=b.get("description") or "",
                        visibility="private" if b.get("is_private") else "public",
                        language=b.get("language") or "",
                        pushed_at=(b.get("updated_on") or "")[:10],
                        created_at=(b.get("created_on") or "")[:10],
                        default_branch=b.get("default_branch") or "",
                        source="bitbucket-api")
    rm=REMOTES.get(folder) or {}
    if rm.get("reachable") and not repos[k].get("default_branch"):
        # Costs no credential: `git ls-remote --symref` names the remote's HEAD.
        # This gives a default branch to repositories whose host listing did not
        # provide one, instead of assuming `master`.
        repos[k]["default_branch"]=rm.get("default_branch") or ""
    lr={"folder":folder,"path":l["path"],"symlink":l["symlink"],"kinds":l["kinds"],
        "sync":rm.get("sync") if rm.get("reachable") else ("unreachable" if rm else ""),
        # A DATE, and renamed to say so. `store/raw/remotes.json` keeps the precise
        # instant — it is not committed — and this is the boundary where a run's
        # clock becomes a fact in a tracked file. At second resolution it changed
        # for ~174 repositories on every tick, which is what made `git diff`
        # never empty: measured 2026-09-07 in one commit subject,
        # `remote_checked_at x174`. Its ONLY reader wanted the day anyway —
        # `tools/build_findings.py` truncated it with `[:10]`.
        "remote_head":rm.get("remote_head",""),
        "remote_checked_on":(rm.get("checked_at") or "")[:10],
        "branch":l.get("branch",""),"last_commit":l.get("last_commit",""),
        "commits":int(l.get("commits") or 0),"dirty":l.get("dirty",0),
        "readme":l["readme"],"homepage_hits":l["homepage_hits"]}
    # HOW MUCH IS AT STAKE, carried only when the probe measured it. Absent is
    # not zero: a state asserting work on this disk with a count of zero is a
    # contradiction, so `collectors/scan_remotes.at_stake` omits the keys rather
    # than reporting a reassurance, and this passes the omission through
    #.
    for key, out_key in (("unpushed", "unpushed"), ("newest_on", "unpushed_newest_on"),
                         ("nothing_exclusive", "nothing_exclusive")):
        if rm.get(key) is not None:
            lr[out_key] = rm[key]
    # THE LOCAL HALF, RECOUNTED EVERY TICK. `scan_remotes` runs on a slow cadence
    # because `git ls-remote` costs a network round trip per repository — the
    # right gate for the expensive question, "what does the remote have". The
    # cheap question, "how far ahead of that sha am I NOW", is `git rev-list`
    # inside the clone and costs nothing.
    #
    # Without this recount the unpushed count went stale between remote scans
    # even when the remote sha had not moved, so recent local work was missing
    # from the one number that says how much work exists solely on this disk,
    # printed beside a date that reads as today.
    #
    # `scan_remotes.at_stake` is called rather than reimplemented: it already
    # knows the range each at-risk state needs, and it already distinguishes
    # measured-as-nothing from unmeasurable. A second copy here would drift.
    # `branch_remote_sha`, NOT `remote_head`. The scanner compares against the
    # CHECKED-OUT branch's remote sha, while `remote_head` is the remote's
    # DEFAULT branch. Passing `remote_head` makes a checkout sitting on a
    # feature branch count nothing in `main..HEAD` and claim "no commit here is
    # exclusive" about a checkout that is ahead; `tests/test_at_stake.py`
    # guards this. Read from the RAW record, which holds both shas, so the
    # registry needs no new field.
    if lr["sync"] in scan_remotes.AT_RISK_STATES and l.get("path"):
        fresh = scan_remotes.at_stake(pathlib.Path(l["path"]), lr["sync"], "",
                                      rm.get("branch_remote_sha") or
                                      lr.get("remote_head", ""),
                                      lr.get("branch", ""))
        if fresh:
            # ABSENT STAYS ABSENT. An empty answer means the count could not be
            # taken now — the remote sha may be unknown to this clone — and
            # overwriting a measured older number with silence would lose the
            # only figure there is. The scan's value survives, and
            # `unpushed_recounted` is what tells a reader which one it is
            # holding.
            for key, out_key in (("unpushed", "unpushed"),
                                 ("newest_on", "unpushed_newest_on"),
                                 ("nothing_exclusive", "nothing_exclusive")):
                lr.pop(out_key, None)
                if fresh.get(key) is not None:
                    lr[out_key] = fresh[key]
            lr["unpushed_recounted"] = True
    lr["worktree_of"]=l.get("worktree_of","")
    # A WORKTREE never displaces a real checkout. `local.path` is what
    # `scan_events` reads history from and what the dashboard links to, and a
    # worktree is a temporary directory an agent deletes when it is done — left
    # to alphabetical order, one could become a project's canonical path.
    # DEMOTED, NOT DISCARDED. Keeping only a folder NAME would lose everything
    # measured about the losing checkout — such as a worktree on a branch that
    # exists on no remote — and nothing downstream could report it.
    # `extra_clones` keeps the names because `plugins/disk_usage.py` resolves
    # folders through it and a plugin must not change shape for a core need;
    # the state travels beside it.
    def demoted(rec):
        return {kk: rec.get(kk) for kk in
                ("folder","path","worktree_of","branch","sync","commits","dirty",
                 "last_commit")}
    if repos[k]["local"] is None:
        repos[k]["local"]=lr
    elif repos[k]["local"].get("worktree_of") and not lr.get("worktree_of"):
        # The real checkout arrived second; promote it and demote the worktree.
        repos[k].setdefault("extra_checkouts",[]).append(demoted(repos[k]["local"]))
        repos[k]["local"]=lr
    else:
        repos[k].setdefault("extra_checkouts",[]).append(demoted(lr))
# ---------- projects anchored on vault folders ----------
ORG_BY_LOWER={o.lower():o for o in OWNED_ORGS}
projects={}
# THE SLUG LIVES IN `identity.py` NOW, with the rule for how a folder-anchored
# project is named — because that name CHANGES when the project is published,
# and a second spelling of an id rule is how one project ends up with two ids
#.
slug = identity.slug
for folder,v in vault.items():
    projects[folder]={"key":slug(folder),"name":folder,"anchor":"vault-folder",
        "vault":{"folder":folder,"overview":v["overview"],"notes":v["note_count"],
                 "summary":v["summary"],"domains":v["domains"]},
        "repos":[],"rules":[],"sites":[]}
# specificity: a repo that has its own vault folder cannot be absorbed elsewhere
own_folder_of={}
for folder in vault:
    for k in repos:
        if repos[k]["name"]==folder or (repos[k]["local"] and repos[k]["local"]["folder"]==folder):
            own_folder_of[k]=folder
#: folder -> {nwo: submodule path}. A `.gitmodules` names, per module, the path
#: inside the tree and the repository it comes from — MEASURED composition, and
#: the strongest membership evidence this pass has: a wiki note can go stale,
#: an org can hold strangers, but a submodule pin is the project itself saying
#: "this repository is one of my parts". Without it, a project built from
#: submodules would show only some of its repositories, with the rest standing
#: beside it as standalone phantom projects.
SUBMODULES = {f: {s["nwo"]: s["path"] for s in (l.get("submodules") or []) if s.get("nwo")}
              for f, l in local.items()}

for folder,v in vault.items():
    p=projects[folder]
    for k in repos:
        if own_folder_of.get(k) not in (None, folder): continue
        why=None
        sub = SUBMODULES.get(folder, {})
        if k in sub: why=f"git submodule at {sub[k]}"
        elif k in v["github"] or k in v["bitbucket"]: why="named in vault notes"
        elif repos[k]["local"] and repos[k]["local"]["folder"]==folder: why="local folder name"
        elif repos[k]["name"]==folder: why="repository name"
        elif ORG_BY_LOWER.get(folder.lower())==repos[k]["owner"]: why="organisation"
        elif repos[k]["local"] and repos[k]["local"]["folder"] in v.get("datapaths",[]): why="DATA/ path in vault notes"
        if why: p["repos"].append(k); p["rules"].append(f"{k}: {why}")
DENIED={(d["project"], d["repo"]) for d in
        json.load(open(paths.config_file('denied_links.json')))["denied"]}
for folder,p in projects.items():
    dropped=[k for k in p["repos"] if (folder, k) in DENIED]
    for k in dropped:
        p["repos"].remove(k)
        p["rules"]=[r for r in p["rules"] if not r.startswith(f"{k}:")]
        print(f"  denied by the operator: {folder} does not hold {k}")
# A repository the operator has retired. It stays in the registry — it exists, and
# a reader following a link to it must be told what it is — but it stops anchoring
# a PROJECT. Otherwise an empty or abandoned repository would get a project row of
# its own from the standalone-repository rule, indistinguishable at a glance from
# real work.
INACTIVE = {k for k, v in json.load(
    open(paths.config_file('repo_status.json')))["repositories"].items()
    if v.get("status") == "inactive"}

VER=json.load(open(paths.config_file('verified_links.json')))["links"]

def apply_verified_links():
    """Attach a curated link, and dissolve the standalone project it absorbs.

    Called TWICE on purpose. Before the leftovers pass it can only reach
    projects anchored on a vault folder — the repository-anchored ones do not
    exist yet. Called again after, it reaches those too, which is the only way
    to express "repository B belongs to the project repository A anchors": a
    docs repo, a landing page, or a published contract surface. Without the
    second pass, such a repository would stand beside its own project as a
    sibling forever, and no curated entry could say otherwise.
    """
    for L in VER:
        p=projects.get(L["project"])
        if not p or L["repo"] not in repos or L["repo"] in p["repos"]: continue
        p["repos"].append(L["repo"]); p["rules"].append(f'{L["repo"]}: verified — {L["evidence"]}')
        # What was absorbed must stop being a project of its own. Only a
        # standalone is dissolved: a project holding more than this repository
        # is something else and is left alone.
        victim=projects.get(L["repo"])
        if victim is not None and victim is not p and victim["repos"]==[L["repo"]]:
            del projects[L["repo"]]

apply_verified_links()
assigned={k for p in projects.values() for k in p["repos"]}
# ---------- leftovers ----------
orgs_with_project={repos[k]["owner"] for p in projects.values() for k in p["repos"]}
from collections import defaultdict
left=defaultdict(list)
for k,r in repos.items():
    if k in assigned: continue
    left[r["owner"]].append(k)
for owner,ks in sorted(left.items()):
    # NO `or ks` FALLBACK. Writing `[k for k in ks if k not in INACTIVE] or ks`
    # would revert an owner whose every leftover was retired to the full list —
    # and two or more retired repositories would then form an ORGANISATION
    # project, which is precisely what the INACTIVE set exists to prevent ("a
    # retired repository stays in the registry but stops anchoring a project").
    # The else-branch below already skips retired repositories one by one, so
    # the fallback would buy nothing and cost that.
    ks = [k for k in ks if k not in INACTIVE]
    if owner not in orgs_with_project and owner in OWNED_ORGS and len(ks)>1:
        projects[owner]={"key":slug(owner),"name":owner,"anchor":"organisation",
            "vault":None,"repos":sorted(ks),"rules":[f"{k}: organisation with no vault note" for k in ks],"sites":[]}
    else:
        # `ks` is already free of retired repositories — filtered once above, so
        # the rule lives in one place instead of being re-stated per branch.
        for k in ks:
            projects[k]={"key":slug(k.replace("/","-")),"name":repos[k]["name"],"anchor":"repository",
                "vault":None,"repos":[k],"rules":[f"{k}: standalone repository"],"sites":[]}
apply_verified_links()                                                      
def _elsewhere_sync(folder, l):
    """`sync` and what is at stake for a git folder whose remote is on a host
    this merge cannot key into a repository row.

    Such a folder has no repository row, and every clone rule reads repository
    rows, so a commit ahead of its upstream there raised nothing while the same
    commit in a GitHub checkout raised `clone.ahead`. The probe's answer is used
    when it reached the remote. When it did not, or never ran, the clone's own
    tracking ref is asked — the ref the last fetch left, read without a network
    call and without writing to the repository — and the result says so with
    `remote_unreachable` / `compared_with`, because "as of the last fetch" is a
    weaker claim than "the remote lacks this".
    """
    rm = REMOTES.get(folder) or {}
    path, branch = pathlib.Path(l["path"]), l.get("branch", "") or ""
    out = {}
    if rm.get("reachable"):
        out["sync"] = rm.get("sync") or ""
        remote_sha = rm.get("branch_remote_sha") or ""
    else:
        if rm:
            out["remote_unreachable"] = True
        ref = f"refs/remotes/origin/{branch}" if branch and branch != "HEAD" else ""
        code, remote_sha, _ = scan_remotes.git(["rev-parse", "--verify", "--quiet", ref], cwd=path) if ref else (1, "", "")
        head = scan_remotes.git(["rev-parse", "HEAD"], cwd=path)[1] if code == 0 else ""
        if code != 0 or not head:
            # No tracking ref: nothing on this disk can say what the remote has.
            out["sync"] = "unreachable" if rm else ""
            return out
        out["compared_with"] = "last-fetch"
        if head == remote_sha:
            out["sync"] = "current"
        elif scan_remotes.git(["merge-base", "--is-ancestor", remote_sha, head], cwd=path)[0] == 0:
            out["sync"] = "ahead"
        elif scan_remotes.git(["merge-base", "--is-ancestor", head, remote_sha], cwd=path)[0] == 0:
            out["sync"] = "behind"
        else:
            out["sync"] = "diverged"
    fresh = scan_remotes.at_stake(path, out["sync"], "", remote_sha, branch)
    for key, out_key in (("unpushed", "unpushed"), ("newest_on", "unpushed_newest_on"),
                         ("nothing_exclusive", "nothing_exclusive")):
        if fresh.get(key) is not None:
            out[out_key] = fresh[key]
    return out
# ---------- folders that produced no repository ----------
# NOT "folders with no git". A folder holding a .git with no remote satisfies
# neither branch: the repository pass above needs a remote to derive an nwo,
# and skipping every git folder here would drop it from the registry entirely —
# still on disk, invisible, and with nothing reporting the loss.
# The test is therefore what the folder YIELDED, not what it contains.
#: Folders that are not projects, each with its own reason — an exclusion
#: nobody can justify is indistinguishable from a mistake. Same shape as
#: `collectors/session_name_exclusions.json`.
_EXCL=json.load(open(paths.config_file('folder_exclusions.json')))
EXCLUDED_PREFIXES=tuple(x["prefix"] for x in _EXCL["prefixes"])
EXCLUDED_NAMES={x["name"] for x in _EXCL["names"]}
for folder,l in local.items():
    if folder.startswith(EXCLUDED_PREFIXES) or folder in EXCLUDED_NAMES: continue
    if l["is_git"] and nwo(l.get("remote")): continue                                
    # A remote this merge cannot key (a host other than GitHub or Bitbucket) is
    # still a remote: the history is published somewhere. Only a checkout with
    # NO remote is "unpublished"; the other is named by its host alone — a URL
    # can carry a user or a token, so the whole address never reaches a rule.
    _remote = (l.get("remote") or "").strip() if l["is_git"] else ""
    _host = re.sub(r"^[a-z+]+://", "", _remote).split("@")[-1].split("/")[0].split(":")[0] if _remote else ""
    rule = (f"{folder}: git repository whose remote is on {_host or 'a host'}, which is not inventoried here"
            if _remote else
            f"{folder}: git repository with no remote, local only"
            if l["is_git"] else f"{folder}: local folder, not a git repository")
    projects["local:"+folder]={"key":identity.local_key(folder),"name":folder,"anchor":"local-folder",
        "vault":None,"repos":[],"rules":[rule],"sites":[],
        # THE GIT FACTS TRAVEL TOO. `mtime` alone is not enough: it is `null`
        # for remoteless repositories the scan measured, so a project committed
        # to today would come out `activity_tier: "unknown"` with an empty
        # `last_activity_on`, in a system whose central question is where work
        # happened. `last_commit` is read by the scan and must not be dropped.
        "local_only":{"folder":folder,"path":l["path"],"kinds":l["kinds"],"unpublished":bool(l["is_git"] and not _remote),
                      "commits":int(l.get("commits") or 0),"branch":l.get("branch","") or "",
                      "last_commit":l.get("last_commit","") or "","dirty":int(l.get("dirty") or 0),
                      "readme":l["readme"],"files":l.get("file_count",0),"mtime":l.get("mtime","")}}
    if _remote:
        projects["local:"+folder]["local_only"].update(_elsewhere_sync(folder, l))
# ---------- sites, by evidence rank ----------
def host_of(u):
    u=(u or "").strip().lower(); u=re.sub(r"^https?://","",u); u=re.sub(r"^www\.","",u)
    return u.split("/")[0].rstrip(".").rstrip("/")
def registrable(h):
    parts=h.split(".")
    for i in range(len(parts)-1):
        cand=".".join(parts[i:])
        if cand in OWNED_DOMAINS: return cand
    return None
for key,p in projects.items():
    cand={}
    my_id=identity.project_id(key)
    for k in p["repos"]:
        r=repos[k]
        if r["homepage"] and not r["fork"]:
            h=host_of(r["homepage"])
            if h and h not in THIRD_PARTY: cand.setdefault(h,[]).append(f"github:{k}:homepageUrl")
        if r["local"]:
            for src,val in r["local"]["homepage_hits"]:
                h=host_of(val)
                if not h or h in THIRD_PARTY: continue
                # An env or nginx line names a host the estate OWNS, or it is a
                # provider's host and says nothing about this project's surface.
                if src.startswith(("env:","nginx:")) and not (registrable(h) or ".".join(h.split(".")[-2:]) in ZONE_NAMES):
                    continue
                cand.setdefault(h,[]).append(f"repo-config:{r['local']['folder']}/{src}")
    if p["vault"] and p["vault"]["overview"]:
        try: ov=(V/p["vault"]["overview"]).read_text(encoding="utf-8",errors="replace")
        except Exception: ov=""
        for d in p["vault"]["domains"]:
            if re.search(r"(?<![\w.-])"+re.escape(d)+r"(?![\w-])", ov):
                cand.setdefault(d,[]).append(f"vault-overview:{p['vault']['overview']}")
    # THE OPERATOR'S ADOPTIONS, from collectors/domain_claims.json: a host tied
    # to this project by decision where no config, README or note names it.
    # Strongest evidence there is — `estate_surfaces.EVIDENCE_RANK` puts
    # `operator-claim` above every measurement — and it carries its reason.
    for h,claim in CLAIMS.items():
        if claim.get("project")==my_id:
            cand.setdefault(h,[]).append(f"operator-claim:{claim.get('why','')[:80]}")
    for h,ev in sorted(cand.items()):
        reg=registrable(h)
        p["sites"].append({"host":h,"owned_domain":reg,
            "confidence":"registry-confirmed" if reg else "declared-not-in-registry",
            "evidence":sorted(set(ev))})
# ---------- description, activity, kind ----------
for p in projects.values():
    desc=""; src=""
    if p["vault"] and p["vault"]["summary"]:
        desc, src = p["vault"]["summary"], "vault-overview"
    if not desc and p["anchor"]!="organisation":
        for k in p["repos"]:
            if repos[k]["description"]: desc, src = repos[k]["description"], f"github:{k}"; break
    if not desc and p["anchor"]!="organisation":
        for k in p["repos"]:
            if repos[k]["local"] and repos[k]["local"]["readme"]:
                desc, src = repos[k]["local"]["readme"], f"readme:{repos[k]['local']['folder']}"; break
    if not desc and p.get("local_only") and p["local_only"]["readme"]:
        desc, src = p["local_only"]["readme"], f"readme:{p['local_only']['folder']}"
    p["description"]=desc.strip(); p["description_source"]=src
    dates=[repos[k]["pushed_at"] for k in p["repos"] if repos[k]["pushed_at"]]
    dates+= [repos[k]["local"]["last_commit"] for k in p["repos"] if repos[k]["local"] and repos[k]["local"]["last_commit"]]
    if p.get("local_only") and p["local_only"]["mtime"]: dates.append(p["local_only"]["mtime"])
    # AND ITS OWN LAST COMMIT. For a folder with no remote there is no
    # `pushed_at` and no repository row, so `mtime` was the only candidate here —
    # and it is `null` for every one the scan measured. A git repository knows
    # when it was last committed to; asking the filesystem instead answered
    # "unknown" for the three most recently worked-on projects on this machine.
    if p.get("local_only") and p["local_only"].get("last_commit"):
        dates.append(p["local_only"]["last_commit"])
    # A SESSION is activity. Every other date here is a git or filesystem fact,
    # so a project worked on without a commit would read as silent — and
    # `activity.py` would then call it `cooling` on that silence while work
    # continued.
    # Keyed by `p["key"]`, because a project has no `id` at this stage — the
    # `project:<slug>` id is minted in `collectors/emit_registry.py:pid()`,
    # which also applies ID_OVERRIDE. So the session index is re-keyed there
    # rather than guessed here: this looks up both the plain slug and the id an
    # override would produce, and the emitter's own map is what makes it exact.
    _sess = SESSIONS.get(f"project:{p['key']}") or SESSIONS_BY_SLUG.get(p["key"], "")
    if _sess: dates.append(_sess)
    p["last_activity"]=max(dates) if dates else ""
    p["last_session_on"]=_sess or ""
    p["folders"]=sorted({repos[k]["local"]["folder"] for k in p["repos"] if repos[k]["local"]}
                        | ({p["local_only"]["folder"]} if p.get("local_only") else set()))
    kinds=set()
    for k in p["repos"]:
        if repos[k]["local"]: kinds|=set(repos[k]["local"]["kinds"])
    if p.get("local_only"): kinds|=set(p["local_only"]["kinds"])
    p["kinds"]=sorted(kinds)
    owners={repos[k]["owner"] for k in p["repos"]}
    p["owner"]=sorted(owners)[0] if len(owners)==1 else ("multi" if owners else "—")
    p["owners"]=sorted(owners)
    p["ownership"]=("owned" if owners and owners<=OWNED_ORGS else
                    "work-bitbucket" if owners and owners <= WORK_ORGS else
                    "external" if owners else "local-only")
    p["archived"]=bool(p["repos"]) and all(repos[k]["archived"] for k in p["repos"])
    p["dirty"]=sum(repos[k]["local"]["dirty"] for k in p["repos"] if repos[k]["local"])
    p["has_note"]=bool(p["vault"])
dups={}
for k,r in repos.items(): dups.setdefault(r["name"].lower(),[]).append(k)
duplicates=sorted([v for v in dups.values() if len(v)>1], key=lambda v:v[0])
# THE OWNER SET IS HARDCODED HERE AND DISCOVERED THERE. `scan_github.owners()`
# asks the API — "nothing is hardcoded, because an org added tomorrow must appear
# without editing this file" — and then this file decides ownership from a
# literal. The two agree today, measured 2026-09-07: ten owners on disk, the same
# ten in `OWNED_ORGS`. The drift is latent and its consequence is not small: an
# organisation created tomorrow would have every repository classed `external`,
# and `estate.py` refuses to record a session for anything but `owned` or
# `work-bitbucket` — so work on a new org would stop being recorded, silently.
#
# NOT auto-added. Which organisations are the operator's is the operator's fact,
# and a script that mints it would make "owned" mean "seen by a token".
_discovered={r["owner"] for r in repos.values() if r["host"]=="github"}
_undeclared=sorted(o for o in _discovered if o not in OWNED_ORGS
                   and any(k in gh for k in repos if repos[k]["owner"]==o))
if _undeclared:
    # THE SENTENCE LIVES IN `estate.py`, and the count of CHECKOUTS is why. The
    # first version stated "estate.py will refuse to record sessions for them"
    # whatever the case, and the live case had no checkout at all — nothing to
    # lose, and an alarm spent on a classification.
    _und_repos = [k for k, r in repos.items() if r["owner"] in _undeclared]
    _und_cloned = sum(1 for k in _und_repos if repos[k]["local"])
    degraded.append({"source": "ownership",
                     "reason": estate.undeclared_owner_reason(
                         _undeclared, len(_und_repos), _und_cloned)})
    print(f"  UNDECLARED owner(s): {', '.join(_undeclared)} — reported as external"
          f" ({_und_cloned} of {len(_und_repos)} cloned here)")

# DISTINCT PROJECTS MAY SLUG TO ONE KEY — a vault folder "Foo-Bar" and a repo
# foo/bar, or local folders "a b" and "a-b". Writing them into one dict kept the
# last and dropped the rest without a word. Keep every project: the highest-
# precedence anchor keeps the key, the others get a deterministic numeric suffix,
# and the collision is reported as degraded so an operator can pin names in
# identity_overrides.json. The persisted identity map designed in
# docs/design/IDENTITY.md replaces the suffix; until then this keeps data.
_ANCHOR_PRECEDENCE = {"vault-folder": 0, "organisation": 1, "repository": 2, "local-folder": 3}
_by_key = {}
for _p in projects.values():
    _by_key.setdefault(_p["key"], []).append(_p)
_taken = set(_by_key)
for _key, _group in sorted(_by_key.items()):
    if len(_group) < 2:
        continue
    _group.sort(key=lambda p: (_ANCHOR_PRECEDENCE.get(p["anchor"], 9), p["name"]))
    _renamed = []
    for _p in _group[1:]:
        _n = 2
        while f"{_key}-{_n}" in _taken:
            _n += 1
        _p["key"] = f"{_key}-{_n}"
        _taken.add(_p["key"])
        _renamed.append(f"{_p['name']} -> {_p['key']}")
    degraded.append({"source": "identity",
                     "reason": f"{len(_group)} projects share the key {_key!r}; "
                               f"{_group[0]['name']} keeps it, " + ", ".join(_renamed)
                               + ". Pin stable names in identity_overrides.json"})
    print(f"  KEY COLLISION {_key!r}: " + ", ".join(p["name"] for p in _group))

out={"repositories":repos,"projects":{p["key"]:p for p in projects.values()},
     "duplicate_repo_names":duplicates,"degraded":degraded}
if moved:
    print(f"followed {len(moved)} transfer(s) — a clone still pointed at the old address:")
    for was, real in sorted(moved.items()):
        print(f"  {was} -> {real}")
out["transfers_followed"] = moved
#: The folder whose `origin` still points at the old address, so a reader has
#: something to act on rather than a pair of names. A transfer is followed on
#: every tick and, until this ran, recorded for nobody: `transfers_followed` was
#: written into the model and read by NOTHING (measured 2026-09-07 — the
#: neighbouring `duplicate_repo_names` has a reader in the emitter, this had
#: none). The remedy is one command and the operator cannot run it without
#: knowing which checkout to run it in.
out["stale_remotes"] = [
    {"was": was, "now": real,
     "folder": (repos.get(real, {}).get("local") or {}).get("folder", ""),
     "path": (repos.get(real, {}).get("local") or {}).get("path", "")}
    for was, real in sorted(moved.items())]
out["transfers_unchecked"] = unchecked
atomic.write_json(SP / "model.json", out)
# THE CONSOLE SUMMARY, written for the person running `local`. It printed
# Python dicts and an empty "duplicate repo names: []", and listed integrations
# the user never switched on as "degraded". The model above still records every
# source in `degraded`; only what is SAID here separates "switched off" (a
# choice) from "degraded" (a switch that is on and could not be measured).
import degradations
_switch = {id(d): degradations.merge_source_off(str(d.get("source") or "")) for d in degraded}
off = sorted({name for name in _switch.values() if name})
real = [d for d in degraded if not _switch[id(d)]]
if real:
    print(f"degraded: {len(real)} source(s)")
    for d in real:
        print(f"  {d['source']}: {d['reason'][:120]}")
if off:
    print("not measured, switched off: " + ", ".join(off)
          + " (`project-observatory full configure integrations NAME true`)")
P=out["projects"]
from collections import Counter
def _tally(counter):
    return ", ".join(f"{k} {v}" for k, v in sorted(counter.items())) or "none"
print(f"projects {len(P)}, repositories {len(repos)}")
print("  anchored by:", _tally(Counter(p["anchor"] for p in P.values())))
print("  ownership:", _tally(Counter(p["ownership"] for p in P.values())))
print(f"  with a site {sum(1 for p in P.values() if p['sites'])} "
      f"(registry-confirmed {sum(1 for p in P.values() if any(s['confidence']=='registry-confirmed' for s in p['sites']))}), "
      f"with a wiki note {sum(1 for p in P.values() if p['has_note'])}, "
      f"with a local folder {sum(1 for p in P.values() if p['folders'])}")
if duplicates:
    print("  duplicate repository names:", ", ".join(map(str, duplicates)))
multi = [p for p in sorted(P.values(), key=lambda x: -len(x["repos"])) if len(p["repos"]) > 1]
if multi:
    print("  multi-repository projects:")
    for p in multi:
        print(f'    {p["name"]:<34} {len(p["repos"]):>2} repos  {", ".join(p["repos"][:6])}{" …" if len(p["repos"])>6 else ""}')
