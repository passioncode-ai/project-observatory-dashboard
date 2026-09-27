import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import activity
import paths
import identity
import json, os, sys
from pathlib import Path
SP=Path(sys.argv[1]) if len(sys.argv)>1 else paths.SCRATCH; INV=paths.REGISTRY
M=json.load(open(SP/"model.json")); repos=M["repositories"]; projs=M["projects"]
ID_OVERRIDE=identity.ID_OVERRIDE                                        
OVERRIDES=json.load(open(paths.config_file('project_overrides.json')))["projects"]
import organizations
ORGS=organizations.Assigner()
REPO_STATUS=json.load(open(paths.config_file('repo_status.json')))["repositories"]
REPO_OVERRIDES=json.load(open(paths.config_file('repo_overrides.json')))["repositories"]
import atomic
import datetime as _dt


def _stamped(name, doc, stamps=("updated_on",)):
    """Every registry document, written ATOMICALLY and stamped by measurement.

    Two defects in one line, both found 2026-09-07:

    * `write_text(json.dumps(...))` truncates the destination and only then
      serialises. `atomic.py` exists in this repository for exactly that and its
      docstring names three collectors that had it — the emit, which writes the
      CANONICAL registry six times per tick, was not among them. The volume
      filled at 06:39 this morning and four collectors died mid-write; had one
      of them been this, `registry/projects.json` would have been left truncated
      for `merge.py` to read on the next tick.
    * the stamp was the literal `"2026-09-03"` (see `OBS`).

    `stamps=()` for a document whose dates live inside its rows: carrying a
    doc-level stamp it does not have would invent one.
    """
    _, changed = atomic.write_json_carrying(
        INV / name, doc, stamps=stamps, now=OBS, indent=2)
    if changed:
        _CHANGED.append(name)
    return changed


_CHANGED: list[str] = []


# Project ids come from the persisted identity map (docs/design/IDENTITY.md):
# a renamed project keeps its id through a strong anchor, and an id is never
# reused. RESOLVED is filled below, once OBS is known; pid() falls back to the
# name-derived id only for a key the map did not see (never in a normal run).
RESOLVED: dict = {}
def pid(key): return RESOLVED.get(key) or identity.project_id(key)
old_projects={p["id"]:p for p in json.load(open(INV/"projects.json"))["projects"]}
old_repos={r["id"]:r for r in json.load(open(INV/"repositories.json"))["repositories"]}
rel_doc=json.load(open(INV/"relations.json")); old_rel={r["id"]:r for r in rel_doc["relations"]}
src_doc=json.load(open(INV/"sources.json")); sources=src_doc["sources"]
have={s["id"] for s in sources}
# THE RUN'S OWN DATE, and it used to be a date literal. Every document below
# carries it, the tick rewrites all of them many times a day, and
# `docs/projects-dashboard.html` prints it as the operator's headline
# freshness, so the one line a person reads to judge how current the estate is
# was a constant typed once. It carried no information in either direction:
# not when the data was minutes old, and not when the emit had stopped and the
# data was a week old.
#
# `atomic.write_json_carrying` keeps the previous stamp when the rest of the
# document is byte for byte equal, so a measured date does NOT mean a commit every
# thirty minutes: the stamp moves when the CONTENT moves.
OBS=_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
import identity_map
_IDMAP_FILE = INV / "identity.json"
try:
    _idmap_prev = json.loads(_IDMAP_FILE.read_text()) if _IDMAP_FILE.is_file() else None
except (OSError, ValueError):
    raise SystemExit(f"{_IDMAP_FILE} is unreadable; restore it from the registry history "
                     "rather than letting every project be re-minted")
# A scan that could not see a project's source must not retire the project.
_complete = not any(d.get("source") in ("wiki", "github") or str(d.get("source", "")).endswith(".json")
                    for d in M.get("degraded", []))
RESOLVED, _idmap, _id_changes = identity_map.resolve(
    projs, repos, M.get("transfers_followed") or {}, identity.ID_OVERRIDE, _idmap_prev, OBS, _complete)
for _c in _id_changes:
    if _c["change"] in ("renamed", "retired", "returned"):
        print(f"  identity {_c['change']}: {_c['id']}" + (f" <- key {_c['key']}" if _c.get('key') else ""))
# Each source declares WHAT IT IS EVIDENCE FOR, in `evidence_for`, and the
# validator turns that into an assertion: a record carrying one of those fields
# must cite this source. Provenance used to be decoration: repositories carried
# `local.sync` and `local.remote_head`, measured over the network by
# `scan_remotes`, under the filesystem scan's source, whose description says it
# reads local folders. The validator checked only that a reference RESOLVED,
# never that it was the right one.
NEW_SOURCES = [
    ("SRC-0007", "Filesystem scan of the configured project roots: folder list, git remotes, checked-out branch, commit counts, project-type markers, README opening paragraph, CNAME/wrangler/package.json homepage fields.", []),
    ("SRC-0008", "GitHub REST/GraphQL listing via `gh repo list` for the explicitly connected account and its accessible organizations: name, description, homepageUrl, visibility, archived, fork, language, default branch, pushedAt.", []),
    ("SRC-0009", "Obsidian vault project notes: folder inventory, overview summary, GitHub/Bitbucket references and DATA/ paths quoted inside the notes.", []),
    ("SRC-0010", "`git ls-remote --symref` against each checkout's own remote, over the SSH keys that already clone it — no API credential involved. Answers whether a local clone is current, behind, ahead or on a branch that exists nowhere else, and the remote's default branch where no API reports one.", ["local.sync", "local.remote_head", "local.remote_checked_on"]),
    ("SRC-0012", "claude-mem's own store at ~/.claude-mem/claude-mem.db, read read-only: one row per (session, project) from `session_summaries`, attributed to a project by local folder name, id slug, project name, or the first path segment of a nested name. Answers WHERE WORK WAS DONE, which the git sources cannot: every other activity date here is a commit or an mtime, so a project worked on without a commit read as silent and `activity_tier` called it cooling on that silence.", ["last_session_on"]),
    ("SRC-0014", "The machine's credential surfaces, read for METADATA only and never for values: OpenRouter's provisioning listing (name, the provider's own label, limit, usage, reset, disabled), tools/vault.py's slot paths and meta.json beside each value, and its leaks.jsonl register. Answers WHAT CREDENTIALS EXIST, WHICH PROJECTS MAY USE THEM and WHAT HAS LEAKED AND NOT BEEN ROTATED.", ["registry:credentials.json"]),
    ("SRC-0013", "Heroku Platform API v3 through the CLI's own session token (`heroku auth:token`, never stored): every application the account and its teams can see, with its formation, dynos, add-ons and release history, plus the Deploy tab\'s GitHub link from kolkrabbi. Answers WHERE A PROJECT IS RUNNING and what leaving it running costs — a fact no other source here holds.", ["registry:heroku-apps.json"]),
    ("SRC-0015", "Every inventoried `.env` under the configured project directory, read by collectors/scan_env.py for NAMES and never for values: the file, its project, its mode, its git state, and each variable\'s name and class. The value is read to decide the class and then dropped; the one derivation that survives it is a salted fingerprint kept in the gitignored scan, which is what lets `shared_with` be MEASURED rather than declared. Answers WHAT EACH PROJECT HOLDS LOCALLY, WHAT BREAKS IF A VALUE IS ROTATED and WHERE AN EMPTY SLOT COULD BE FILLED FROM.", ["registry:env-inventory.json"]),
    ("SRC-0016", "Cloudflare zone listings through the narrow tokens `tools/cloudflare.py` issues (Zone Read, one per account), joined at emit time to the domain registry, the projects' sites and the operator's boundary and claim files; and the operator's curated product groupings in collectors/products.json with the groupings suggested from shared registrable domains. Answers WHICH DOMAINS THE ESTATE HOLDS, whose surface each is, and what several projects are one product.", ["registry:cloudflare-zones.json", "registry:products.json"]),
    ("SRC-0017", "The MCP declarations in each agent's own config (~/.claude.json with its project scopes, ~/.cursor/mcp.json, ~/.config/opencode/opencode.json) plus what `claude mcp list` reports for plugins and claude.ai connectors — read for names, transports, targets without query strings and the PRESENCE of a key, never a value. Answers WHICH MCP SERVERS THE AGENTS ARE TOLD TO REACH and whether Claude reached them.", ["registry:mcp-servers.json"]),
    ("SRC-0018", "Heroku\'s config-vars endpoint through the same CLI session token, read by collectors/scan_remote_env.py: the endpoint answers with names AND values in one object and has no names-only form, so the values are TRANSIT — classified, fingerprinted with the machine-local salt collectors/scan_env.py uses, and dropped. The fingerprints stay in the gitignored scan beside the vault\'s own retired-value fingerprints; the document built from them carries only the verdict. Answers WHAT PRODUCTION IS CONFIGURED WITH, whether it matches this machine, and whether a value the vault has already rotated away is still deployed.", ["registry:remote-env.json"]),
    ("SRC-0019", "Google Analytics Admin and Data APIs, and Search Console, through the service accounts in the machine\'s secret store: every account and property the credential can see, each property\'s own declared web hosts and app bundle ids, thirty days of active users, sessions and views, and the Search Console sites. The credential signs a JWT and never leaves the collector; what is recorded is the account\'s public `client_email`. Answers WHICH PRODUCTS PEOPLE ACTUALLY USE, and joins each property to a project by what the property declares about itself rather than by a hand-written map.", ["registry:google-properties.json"]),
    ("SRC-0011", "Public DNS through `dig`, HTTP reachability through `curl`, and registrar records through RDAP at rdap.org. Answers whether a host resolves and answers, and what a registry says about a domain — measured, never transcribed.", ["registry:domain-liveness.json"]),
]
SOURCE_INPUTS = {"SRC-0007": SP / "local.json", "SRC-0008": SP / "gh",
    "SRC-0009": SP / "vault.json", "SRC-0010": SP / "remotes.json",
    "SRC-0011": SP / "domains_live.json", "SRC-0012": SP / "sessions.json",
    "SRC-0013": SP / "heroku.json", "SRC-0014": SP / "openrouter.json",
    "SRC-0015": SP / "env.json", "SRC-0016": SP / "cloudflare_zones.json",
    "SRC-0017": SP / "mcp.json", "SRC-0018": SP / "remote-env.json",
    "SRC-0019": SP / "google.json"}
def source_available(sid):
    path = SOURCE_INPUTS.get(sid)
    return bool(path and (path.is_file() or (path.is_dir() and any(p.is_file() and not p.name.startswith("_") for p in path.glob("*.json")))))
for sid, desc, evidence in NEW_SOURCES:
    if sid not in have:
        rec = {"id": sid, "kind": "agent-measurement", "observed_on": OBS if source_available(sid) else None, "description": desc, "availability": "measured-input" if source_available(sid) else "not-measured"}
        if evidence:
            rec["evidence_for"] = evidence
        sources.append(rec)
    elif evidence:
        # A source declared before it had an `evidence_for` gets one now; the
        # validator's rule is only as good as the declaration behind it.
        for s in sources:
            if s["id"] == sid and s.get("evidence_for") != evidence:
                s["evidence_for"] = evidence
# Current coverage is independent of historical observation dates.
for record in sources:
    if record.get("id") in SOURCE_INPUTS:
        available = source_available(record["id"])
        record["availability"] = "measured-input" if available else "not-measured"
        if available and not record.get("observed_on"):
            record["observed_on"] = OBS
#: Bitbucket's own REST API has NO source id, and that is the honest state: the
#: collector is credential-degraded on this machine and contributes no field to
#: any record. A source declaring itself the evidence for nothing would
#: be a claim about a measurement that never happened.
SRC=[sid for sid in ("SRC-0007","SRC-0008","SRC-0009") if source_available(sid)]
# ---- repositories ----
out_repos=[]; cleared_repos: list[str] = []
for k in sorted(repos):
    r=repos[k]; rid="repository:"+k; prev=old_repos.get(rid,{})
    e={"id":rid,"host":r["host"],"name_with_owner":k,
       "url":r["url"],"visibility":r["visibility"],
       # `default_branch` is the ONE field that legitimately falls back to the
       # previous emit, and the difference from the others is the whole point:
       # it is measured by `scan_remotes`, which tick.sh age-gates to six hours,
       # so an absent value means THIS RUN DID NOT MEASURE — not "there is no
       # default branch". Every git repository has one. Clearing it on a run that
       # did not look would be inventing an absence.
       "default_branch":r.get("default_branch") or prev.get("default_branch",""),
       # `description` does NOT get that treatment. Absence here can mean the
       # description was removed at the forge, and stickiness made that
       # unrepresentable. Descriptions the forge listing never reported, once
       # transcribed by hand, survived only because of this fallback. They now
       # live in collectors/repo_overrides.json, which is where a curated value
       # belongs, and the fallback is gone.
       "description":r["description"] or "",
       "archived":r["archived"],"fork":r["fork"],"language":r["language"],
       "topics":r["topics"],"last_pushed_on":r["pushed_at"],"created_on":r["created_at"],
       "discovered_by":r["source"],
       "source_refs":sorted({"SRC-0008" if r["source"]=="github-api" else "SRC-0007"})}
    st = REPO_STATUS.get(k)
    if st:
        e["status"] = st["status"]
        e["status_evidence"] = st["evidence"]
        e["status_measured_on"] = st.get("measured_on")
        # `moved_to` and `superseded_by` are different claims and both are carried.
        # A move says one repository has a newer address; superseded says two
        # repositories competed and one won. Recording the second where the first
        # is true tells a reader to distrust an address that is simply old.
        for field in ("superseded_by", "moved_to"):
            if st.get(field):
                e[field] = st[field]
    if r["local"]:
        e["local"]={"path":r["local"]["path"],"folder":r["local"]["folder"],
                    "symlink":r["local"]["symlink"],"checked_out_branch":r["local"]["branch"],
                    "commits":r["local"]["commits"],"last_commit_on":r["local"]["last_commit"],
                    "uncommitted_files":r["local"]["dirty"],"stack":r["local"]["kinds"]}
        # Emitted only when measured. An absent `sync` means the remote scan did
        # not run; `""` would read as "measured, and nothing to say", which is a
        # different and false claim.
        # THE LIST IS ENUMERATED, so a key added upstream stops here unless it is
        # named. `unpushed` and `unpushed_newest_on` reached `store/raw/model.json`
        # for fifteen checkouts and none of the registry, which is where the loss
        # was found — measured, not guessed.
        for k_, v_ in (("sync", r["local"].get("sync")),
                       ("remote_head", r["local"].get("remote_head")),
                       ("remote_checked_on", r["local"].get("remote_checked_on")),
                       ("unpushed", r["local"].get("unpushed")),
                       ("unpushed_newest_on", r["local"].get("unpushed_newest_on")),
                       ("nothing_exclusive", r["local"].get("nothing_exclusive")),
                       # WHICH HALF THE NUMBER CAME FROM. `unpushed` is now
                       # recounted locally on every tick; when that recount
                       # could not be taken the scan's older figure survives,
                       # and this key is the only thing that distinguishes them
                       #.
                       ("unpushed_recounted", r["local"].get("unpushed_recounted"))):
            if v_: e["local"][k_]=v_
        # TWO VIEWS OF ONE LIST, so neither reader has to know about the other.
        # `extra_clones` is the folder NAMES: `plugins/disk_usage.py` walks them
        # and `plugins/disk-usage.json` documents that shape, and a plugin must
        # not change because the core needed more. `extra_checkouts` carries what
        # was measured about each — the branch, the sync state, the commit count
        # — which the merge used to throw away. Derived here from one
        # structure rather than assembled twice in `merge.py`.
        if r.get("extra_checkouts"):
            xs=r["extra_checkouts"]
            e["local"]["extra_clones"]=[x["folder"] for x in xs]
            e["local"]["extra_checkouts"]=[{kk:vv for kk,vv in x.items()
                                            if vv not in (None,"")} for x in xs]
        e["source_refs"]=sorted(set(e["source_refs"])|{"SRC-0007"})
        # The network probe cites itself. `sync`, `remote_head` and
        # `remote_checked_on` are `git ls-remote` answers, not filesystem facts,
        # and they rode under SRC-0007 — "Filesystem scan of ~/DATA" — on 87
        # repositories until this line.
        if any(e["local"].get(f) for f in ("sync", "remote_head", "remote_checked_on")):
            e["source_refs"]=sorted(set(e["source_refs"])|{"SRC-0010"})
    # A Bitbucket repository's default branch comes from `ls-remote` too: no API
    # listing reports it here, as measured when the question was open.
    if e.get("default_branch") and r["host"]=="bitbucket.org":
        e["source_refs"]=sorted(set(e["source_refs"])|{"SRC-0010"})
    rov=REPO_OVERRIDES.get(k)
    if rov:
        for ck,cv in rov.items():
            if ck=="why": continue
            # `source_refs` is UNIONED, never replaced. Provenance is cumulative
            # evidence: a curated value adds the witness that supplied it, it
            # does not erase the collectors that measured the rest of the record.
            # Replacing it wiped SRC-0010 off the two curated repositories the
            # moment the network probe started citing itself — caught by the
            # validator's new rule the first time it ran.
            e[ck]=sorted(set(e.get(ck) or [])|set(cv)) if ck=="source_refs" else cv
        e["curated_fields"]=sorted(ck for ck in rov if ck!="why")
    for field in ("description","default_branch"):
        if prev.get(field) and not e.get(field):
            cleared_repos.append(f"{rid}.{field} was {str(prev[field])[:40]!r}, measurement says nothing")
    out_repos.append(e)
# ---- projects ----
#: Relation types this emitter DERIVES from the model on every run. They are
#: rebuilt rather than accumulated: a link the model no longer makes must not
#: survive because it was written once. T26 pruned relations whose endpoints
#: vanished; this is the other half — a relation whose JUSTIFICATION vanished,
#: which is what an operator's correction produces.
DERIVED_TYPES = {"implemented_by", "public_domain_of", "deployed_to", "credential_used_by", "part_of", "in_account", "serves"}
authored = [r for r in rel_doc["relations"] if r["type"] not in DERIVED_TYPES]
stale = len(rel_doc["relations"]) - len(authored)
if stale:
    print(f"rebuilding {stale} derived relation(s) from the model; "
          f"keeping {len(authored)} authored one(s)")
out_projs=[]; relations=list(authored); seen_rel={r["id"] for r in relations}
seen_edge={(r["type"],r["from"],r["to"],r.get("deployment")) for r in relations}
#: Fields a derived edge may carry beyond its endpoints (DEPLOYMENTS.md rule 5):
#: where a credential is read and in which environment and deployment.
EDGE_EXTRAS = ("binding", "environment", "deployment", "variable")
def add_rel(rid,typ,frm,to,refs,rule=None,extra=None):
    # `rule` is the edge's provenance: WHY these two endpoints are linked
    # (a membership rule, a Heroku link rule, a credential path rule). It was
    # dropped here, so an edge in relations.json could not say why it existed.
    extra={k:v for k,v in (extra or {}).items() if k in EDGE_EXTRAS and v}
    # One pair may be linked twice when the second edge says something the first
    # does not: a key held locally AND read at run time by one deployment.
    key=(typ,frm,to,extra.get("deployment"))
    if rid in seen_rel or key in seen_edge: return
    rel={"id":rid,"type":typ,"from":frm,"to":to,"source_refs":refs}
    if rule: rel["rule"]=rule
    rel.update(extra)
    relations.append(rel)
    seen_rel.add(rid); seen_edge.add(key)
cleared: list[str] = []
for key in sorted(projs):
    p=projs[key]; i=pid(key); prev=old_projects.get(i,{})
    # NOTHING here reads `prev` for a VALUE. The emitter used to fall back to its
    # own previous output for lifecycle, description, canonical_page and
    # source_refs, which is trap T4 ("`or prev.get(...)` made a field impossible
    # to clear") wearing four more coats. A value written once could never be
    # removed by measurement: archive every repository of a project on GitHub and
    # its `lifecycle` stays `active` for ever, because the emit reads the answer
    # it gave last time. It also made the emit only EVENTUALLY idempotent: after
    # the underlying data moved it needed two passes to converge, which is what
    # made T4 go red.
    #
    # Measured before removal, the way T11 requires: on every project,
    # lifecycle, description and canonical_page from measurement alone were
    # IDENTICAL to what stickiness was carrying, so nothing was lost. What a
    # curated value has instead is `project_overrides.json`, applied below: the
    # mechanism created for exactly this, which stickiness quietly duplicated and
    # undermined.
    e={"id":i,"name":p["name"],"anchor":p["anchor"],"ownership":p["ownership"],
       "owners":p["owners"],"lifecycle":"archived" if p["archived"] else "active",
       "description":p["description"] or "",
       "stack":p["kinds"],"local_folders":p["folders"],"last_activity_on":p["last_activity"],
       # Observed, beside the DECLARED `lifecycle` rather than inside it. Derived
       # on every emit and never carried forward: the validator re-derives it and
       # fails on a value that disagrees with the date next to it.
       "activity_tier":activity.tier_of(p["last_activity"]),
       "has_vault_note":p["has_note"],"description_source":p.get("description_source",""),
       "membership_rules":p["rules"],
       "source_refs":sorted(SRC)}
    # The session date, and the citation that carries it. Absent when no session
    # was seen — the emitter's standing rule: an absent field means "not
    # measured", where `""` would claim "measured, and nothing there" (trap T4).
    if p.get("last_session_on"):
        e["last_session_on"]=p["last_session_on"]
        e["source_refs"]=sorted(set(e["source_refs"])|{"SRC-0012"})
    cp=("vault:"+p["vault"]["overview"]) if p["vault"] else None
    if cp and cp.startswith("../"): cp="vault:"+cp[3:]                                             
    if cp and (paths.VAULT/cp.split(":",1)[1]).exists(): e["canonical_page"]=cp
    if p["vault"]: e["vault_notes"]=p["vault"]["notes"]
    ov=OVERRIDES.get(i)
    if ov:
        for k,v in ov.items():
            if k=="why": continue
            e[k]=sorted(set(e.get(k) or [])|set(v)) if k=="source_refs" else v
        e["curated_fields"]=sorted(k for k in ov if k!="why")
    # THE ORGANIZATION, after the curated overrides so an accepted proposal's
    # `organization` wins, and absent entirely when organizations.json is not
    # configured — a workspace that never set it up sees no new field.
    _org = ORGS.assign(e)
    if _org:
        e["organization"] = _org["organization"]
        e["organization_source"] = _org["source"]
        e["organization_why"] = _org["why"]
    if p["sites"]: e["sites"]=p["sites"]
    if p.get("local_only"): e["local_only"]=p["local_only"]
    # `prev` is now read for ONE purpose: to say out loud when measurement has
    # cleared a field that had a value. Silence is what made T11 expensive — a
    # curated `canonical_page` vanished and the only sign was a count falling
    # from 44 to 43. A loss that announces itself is a loss somebody can put back
    # into project_overrides.json.
    for field in ("lifecycle","description","canonical_page"):
        had, has = prev.get(field), e.get(field)
        if had and not has:
            cleared.append(f"{i}.{field} was {str(had)[:40]!r}, measurement says nothing")
    out_projs.append(e)
    # Relation ids name the PROJECT ID, not the merge key: with an
    # identity_overrides entry the id stays put across a rename while the key
    # moves, and an edge id built from the key moved with it.
    slug_id = i.split(":", 1)[1]
    for k in p["repos"]:
        _why = next((r.split(": ", 1)[1] for r in p.get("rules") or [] if r.startswith(f"{k}: ")),
                    f"{p.get('anchor')} anchor")
        add_rel(f"relation:{slug_id}:implemented-by:{k}".replace("/","-"),"implemented_by",i,"repository:"+k,SRC,_why)
    for s in p["sites"]:
        if s["confidence"]=="registry-confirmed":
            add_rel(f"relation:{s['owned_domain']}:public-domain-of:{slug_id}","public_domain_of",
                    "domain:"+s["owned_domain"],i,SRC)
# A WHOLESALE SWING IS REFUSED, and this is the second line rather than the
# first. `scan_filesystem` now refuses to write when `git` cannot run, because a
# missing `git` was measured to turn 156 projects into 206 and 172 repositories
# into 149 — but that preflight only guards ONE cause. Nothing anywhere compared
# an emit against the one before it, so any future collector failure of the same
# shape would have been written into the canonical registry and committed by the
# next tick.
#
# ±25% against the previous registry. Normal tick-to-tick movement is nought to
# two projects out of 156 — well under one percent — and the disaster this comes
# from was +32% and −13%. A threshold between them catches it and never fires on
# ordinary work.
#
# A first emit has no baseline and is never refused. And a legitimate bulk change
# is not blocked, only made deliberate: `OBSERVATORY_ALLOW_BULK=1` proceeds, and
# the refusal names it.
def _bulk_refusal() -> str:
    import os as _os
    if _os.environ.get("OBSERVATORY_ALLOW_BULK"):
        return ""
    limit = 0.25
    for name, key, new in (("projects.json", "projects", out_projs),
                           ("repositories.json", "repositories", out_repos)):
        f = INV / name
        if not f.is_file():
            continue
        try:
            was = len(json.loads(f.read_text(encoding="utf-8"))[key])
        except (ValueError, OSError, KeyError):
            continue
        if not was:
            continue
        change = (len(new) - was) / was
        if abs(change) > limit:
            return (f"{name} would go from {was} to {len(new)} "
                    f"({change:+.0%}, limit ±{limit:.0%}). A swing this size is a "
                    f"collector failure far more often than it is real work — a "
                    f"missing `git` alone accounts for +32% of projects. Nothing "
                    f"was written. If the change is genuine, re-run with "
                    f"OBSERVATORY_ALLOW_BULK=1.")
    return ""

_refusal = _bulk_refusal()
if _refusal:
    print(f"emit REFUSED: {_refusal}", file=sys.stderr)
    raise SystemExit(1)

_stamped("projects.json", {"schema_version":2,"updated_on":OBS,"projects":out_projs,"degraded":M.get("degraded",[])})
_stamped("identity.json", _idmap, stamps=())
_stamped("repositories.json", {"schema_version":2,"updated_on":OBS,"repositories":out_repos,"degraded":M.get("degraded",[])})
# ---- Heroku ----------------------------------------------------------------
# The registry knew what a project IS and never where it RUNS. This is the
# second half, and it is a SEPARATE document for the same reason domain
# liveness is: `projects.json` is what the estate is made of, this is what a
# provider says about it today, and folding one into the other would make a
# provider outage look like a project changing shape.
# TWO INPUTS, and the second is named here because the derivation that builds
# the pipeline graph reads THIS script's source, not the module it delegates to:
# `heroku_registry.records` opens `collectors/heroku_links.json` for the
# hand-verified links, so this step's output depends on that file even though
# the path never appears in a call below.
HEROKU_SRC = paths.SCRATCH / "heroku.json"
heroku_apps: list = []
if HEROKU_SRC.is_file():
    import heroku_registry
    _scan = json.loads(HEROKU_SRC.read_text(encoding="utf-8"))
    heroku_apps, _edges = heroku_registry.records(_scan, out_projs, out_repos, relations)
    for e in _edges:
        add_rel(e["id"], e["type"], e["from"], e["to"], e["source_refs"], e.get("rule"))
    _doc = heroku_registry.document(_scan, heroku_apps, OBS)
    _stamped("heroku-apps.json", _doc)
    _t = _doc["totals"]
    print(f"heroku-apps.json: {_t['apps']} app(s), {_t['linked_to_a_project']} linked, "
          f"{_t['unlinked']} unlinked, ${_t['monthly_cost']}/month")
    # THE HEROKU CHAIN, applied to the projects' sites: a custom
    # domain Heroku accepts for an app that a named rule tied to a project is
    # that project's surface. Ranked with GitHub evidence by estate_surfaces.
    import estate_surfaces as _es
    _owned_now = {d["name"] for d in json.load(open(INV/"domains.json"))["domains"]}
    _by_id = {p["id"]: p for p in out_projs}
    _added = 0
    for _pid, _hosts in _es.heroku_site_hints(_scan.get("apps") or [], heroku_apps).items():
        _proj = _by_id.get(_pid)
        if not _proj:
            continue
        for _h, _ev in _hosts.items():
            _hit = next((s for s in _proj.setdefault("sites", []) if s["host"] == _h), None)
            if _hit:
                if _ev not in _hit.get("evidence", []):
                    _hit.setdefault("evidence", []).append(_ev)
                continue
            _reg = _es.registrable(_h, _owned_now)
            _proj["sites"].append({"host": _h, "owned_domain": _reg,
                                   "confidence": "registry-confirmed" if _reg else "declared-not-in-registry",
                                   "evidence": [_ev]})
            if _reg:
                add_rel(f"relation:{_reg}:public-domain-of:{_pid.split(':',1)[1]}",
                        "public_domain_of", "domain:"+_reg, _pid, SRC)
            _added += 1
    if _added:
        print(f"  heroku domains: {_added} host(s) joined to projects through their apps")

# WHAT THE ESTATE HOLDS AND HOW IT GROUPS. `estate_surfaces` reads
# collectors/host_boundary.json for each zone's standing. Zones need a scan; the
# product document is built on every emit because its curated half is a file
# in this repository and its suggested half is derived from the sites above.
import estate_surfaces
_domains_now = json.load(open(INV/"domains.json"))["domains"]
# THE MCP INVENTORY: what the agents are told to reach, tracked here
# now that the gateway is off.
MCP_SRC = paths.SCRATCH / "mcp.json"
if MCP_SRC.is_file():
    _mscan = json.loads(MCP_SRC.read_text(encoding="utf-8"))
    _mrows = estate_surfaces.mcp_rows(_mscan)
    _mdoc = estate_surfaces.mcp_document(_mscan, _mrows, OBS)
    _stamped("mcp-servers.json", _mdoc)
    _mt = _mdoc["totals"]
    print(f"mcp-servers.json: {_mt['declarations']} declaration(s), {_mt['distinct_servers']} "
          f"server(s), " + ", ".join(f"{k} {v}" for k, v in sorted(_mt['by_liveness'].items()))
          + (f", {_mt['key_in_url']} with a key in the URL" if _mt['key_in_url'] else ""))
_pdoc, _pedges, _perrors = estate_surfaces.products_document(out_projs, _domains_now, OBS)
if _perrors:
    # A curated file with a wrong project id or an unknown role is refused
    # whole: emitting half of an operator's grouping is a grouping nobody made.
    sys.exit("collectors/products.json refused:\n  " + "\n  ".join(_perrors))
for e in _pedges:
    add_rel(e["id"], e["type"], e["from"], e["to"], e["source_refs"], e.get("rule"))
    relations[-1]["role"] = e["role"]
_stamped("products.json", _pdoc)
products_doc = _pdoc
print(f"products.json: {_pdoc['totals']['curated']} curated, "
      f"{_pdoc['totals']['suggested']} suggested, "
      f"{_pdoc['totals']['projects_grouped']} project(s) grouped")
for _dg in _pdoc.get("degraded") or []:
    print(f"  degraded products.json: {_dg['reason'][:110]}")
ZONES_SRC = paths.SCRATCH / "cloudflare_zones.json"
zone_rows: list = []
if ZONES_SRC.is_file():
    _zscan = json.loads(ZONES_SRC.read_text(encoding="utf-8"))
    zone_rows = estate_surfaces.zone_rows(_zscan, _domains_now, out_projs, _pdoc["products"])
    _zdoc = estate_surfaces.zones_document(_zscan, zone_rows, OBS)
    _stamped("cloudflare-zones.json", _zdoc)
    _zt = _zdoc["totals"]
    print(f"cloudflare-zones.json: {_zt['zones']} zone(s), {_zt['linked']} linked to a "
          f"project, {_zt['in_domain_registry']} in the domain registry, "
          + ", ".join(f"{k} {v}" for k, v in sorted(_zt['by_standing'].items())))

# ---- accounts (docs/design/DEPLOYMENTS.md, PB-127) ------------------------
import accounts as _accounts
_acc_doc, _acc_edges = _accounts.build(
    (_scan.get("apps") or []) if HEROKU_SRC.is_file() else [], zone_rows, OBS,
    HEROKU_SRC.is_file(), ZONES_SRC.is_file())
for e in _acc_edges:
    add_rel(e["id"], e["type"], e["from"], e["to"], e["source_refs"], e.get("rule"))
_stamped("accounts.json", _acc_doc)
print(f"accounts.json: {_acc_doc['totals']['accounts']} account(s), "
      f"{_acc_doc['totals']['attributed']} resource(s) attributed, "
      f"{_acc_doc['totals']['unattributed']} unattributed")
for _dg in _acc_doc["degraded"]:
    print(f"  degraded accounts.json: {_dg['reason']}")

# ---- credentials ---------------------------------------------------------
# WHAT EXISTS AND WHO MAY USE IT, never a value. `tools/vault.py` holds the
# values; this is the estate's side of the same question, and the reason it is
# a registry document is that "which projects share this account" is a fact
# about what exists rather than a measurement at an instant.
# TWO INPUTS NAMED HERE for the derivation that reads this script's source:
# `credentials_registry.records` opens `collectors/credential_owners.json`, and
# the scan it reads is `store/raw/openrouter.json` — same reason the Heroku
# block names its curated file by hand.
CRED_SRC = paths.SCRATCH / "openrouter.json"
credentials: list = []
if CRED_SRC.is_file():
    import credentials_registry
    _cscan = json.loads(CRED_SRC.read_text(encoding="utf-8"))
    _vault = pathlib.Path(os.environ.get(
        "OBSERVATORY_VAULT_DIR",
        paths.source_path("secret_store", paths.SECRETS) / 'projects'))
    credentials, _cedges = credentials_registry.records(_cscan, _vault, out_projs)
    for e in _cedges:
        add_rel(e["id"], e["type"], e["from"], e["to"], e["source_refs"], e.get("rule"), e)
    _cdoc = credentials_registry.document(credentials, _cscan, OBS)
    _stamped("credentials.json", _cdoc)
    _ct = _cdoc["totals"]
    print(f"credentials.json: {_ct['credentials']} credential(s), "
          f"{_ct['claimed_by_a_project']} claimed, {_ct['unclaimed']} unclaimed, "
          f"{_ct['leaked_unrotated']} leaked and unrotated")

# ---- env files -----------------------------------------------------------
# WHAT EACH PROJECT HOLDS ON THIS DISK, as names. The credential document above
# answers "what accounts exist"; this answers "what is actually sitting in the
# working copy", and until it existed the answer was a grep. The two are kept
# apart deliberately: an account is a fact about a provider and an env file is a
# fact about a folder, and folding them together would make a rotated key look
# like a moved file.
# ONE INPUT, named here for the derivation that reads this script's source:
# `store/raw/env.json`, written by the `env` step.
ENV_SRC = paths.SCRATCH / "env.json"
if ENV_SRC.is_file():
    import env_registry
    _escan = json.loads(ENV_SRC.read_text(encoding="utf-8"))
    _edoc = env_registry.document(_escan, OBS)
    _stamped("env-inventory.json", _edoc)
    _et = _edoc["totals"]
    print(f"env-inventory.json: {_et['env_files']} env file(s) + {_et['templates']} "
          f"template(s), {_et['secrets']} secret-class variable(s), "
          f"{_et['shared_across_projects']} shared across projects, "
          f"{_et['tracked_in_git']} tracked in git")

# WHAT GOOGLE SEES. The scan is cached and may be older than this
# emit; the document carries its own `scanned_on` so the page can say how old.
GOOGLE_SRC = paths.SCRATCH / "google.json"
if GOOGLE_SRC.is_file():
    import google_registry
    _gscan = json.loads(GOOGLE_SRC.read_text(encoding="utf-8"))
    _grows = google_registry.rows(_gscan, out_projs,
                                  paths.config_file("ga4_properties.json"))
    _gdoc = google_registry.document(_gscan, _grows, OBS)
    _stamped("google-properties.json", _gdoc)
    _gt = _gdoc["totals"]
    _gusers = "unknown" if _gt["users_30d"] is None else f"{_gt['users_30d']:,}"
    _gloose = "unknown" if _gt["users_30d_unclaimed"] is None else f"{_gt['users_30d_unclaimed']:,}"
    print(f"google-properties.json: {_gt['properties']} property(ies) in "
          f"{_gt['accounts']} account(s), {_gt['linked_to_a_project']} linked, "
          f"{_gt['outside_the_estate']} outside, {_gt['unclaimed']} unclaimed "
          f"({_gloose} users/30d), "
          f"{_gusers} summed users/30d; {_gt['measured_properties']} measured, "
          f"{_gt['unknown_properties']} unknown")

# WHAT PRODUCTION HOLDS, as verdicts. Two inputs, both gitignored: the remote
# scan's fingerprints and the local env scan's. The document written from them
# carries neither, only the four words.
REMOTE_SRC = paths.SCRATCH / "remote-env.json"
if REMOTE_SRC.is_file() and ENV_SRC.is_file():
    import remote_registry
    _rscan = json.loads(REMOTE_SRC.read_text(encoding="utf-8"))
    _rdoc = remote_registry.document(_rscan, json.loads(ENV_SRC.read_text(encoding="utf-8")), OBS)
    _stamped("remote-env.json", _rdoc)
    _rt = _rdoc["totals"]
    print(f"remote-env.json: {_rt['apps']} app(s), {_rt['apps_with_a_checkout']} with a "
          f"checkout here, {_rt['same_as_local']} production value(s) equal to a local "
          f"one, {_rt['differs']} differing, {_rt['remote_only']} only in production, "
          f"{_rt['retired_still_deployed']} retired and still deployed")

# ---- environments (docs/design/DEPLOYMENTS.md, PB-128) --------------------
# ONE CURATED INPUT, named for the derivation that reads this script's source:
# `config/environments.json`, the operator's override of which environment a
# deployment serves.
import environments as _environments
_env_over, _env_problems = _environments.load_overrides(paths.config_file("environments.json"))
_env_doc, _env_edges = _environments.build(
    heroku_apps, (_scan.get("apps") or []) if HEROKU_SRC.is_file() else [], credentials,
    _edoc["files"] if ENV_SRC.is_file() else [], out_projs, _env_over, _env_problems, OBS)
for e in _env_edges:
    add_rel(e["id"], e["type"], e["from"], e["to"], e["source_refs"], e.get("rule"))
_stamped("environments.json", _env_doc)
_nt = _env_doc["totals"]
print(f"environments.json: {_nt['environments']} environment(s), {_nt['deployments_assigned']} deployment(s) "
      f"assigned, {_nt['deployments_unassigned']} unassigned")
for _dg in _env_doc["degraded"]:
    print(f"  degraded environments.json: {_dg['reason']}")

# ---- run bindings (DEPLOYMENTS.md rule 5, PB-129) -------------------------
# A production config var whose salted fingerprint equals a vault slot's current
# value: that slot is read at run time by that deployment. Needs both scans.
if REMOTE_SRC.is_file() and ENV_SRC.is_file() and CRED_SRC.is_file():
    import credentials_registry as _cr
    _serves = {e["from"]: e["to"] for e in _env_edges}
    _run = _cr.run_edges(_rdoc, heroku_apps, {c["id"] for c in credentials}, _serves)
    for e in _run:
        add_rel(e["id"], e["type"], e["from"], e["to"], e["source_refs"], e.get("rule"), e)
    print(f"run bindings: {len(_run)} vault slot(s) read at run time by a production app")

endpoints = ({p["id"] for p in out_projs} | {r["id"] for r in out_repos}
             | {a["id"] for a in heroku_apps} | {c["id"] for c in credentials}
             | {z["id"] for z in zone_rows} | {a["id"] for a in _acc_doc["accounts"]}
             | {e["id"] for e in _env_doc["environments"]}
             | {pr["id"] for pr in products_doc["products"]}
             | {"domain:" + d["name"] for d in json.load(open(INV/"domains.json"))["domains"]})
kept, dropped = [], []
for r in relations:
    if r["from"] in endpoints and r["to"] in endpoints:
        kept.append(r)
    else:
        dropped.append(r)
if dropped:
    print(f"dropped {len(dropped)} relation(s) whose endpoint no longer exists:")
    for r in dropped[:8]:
        missing = [s for s in (r["from"], r["to"]) if s not in endpoints]
        print(f"  {r['id']} -> {', '.join(missing)}")
relations = kept
rel_doc.setdefault("relation_types", {})
rel_doc["relation_types"]["implemented_by"] = "A project is implemented by a repository."
rel_doc["relation_types"]["public_domain_of"] = "A domain is a public address of a project."
rel_doc["relation_types"]["credential_used_by"] = ("A project may use a credential. "
    "Many-to-many on purpose: one account serves several projects and one project uses "
    "several accounts, so the edge repeats rather than folding into a field.")
rel_doc["relation_types"]["part_of"] = ("A project is one part of a product — a site, its admin, "
    "its app, its wiki. Curated only: the edge carries the member's `role`, and a "
    "suggested grouping in registry/products.json never becomes an edge.")
rel_doc["relation_types"]["deployed_to"] = ("A project is deployed to a hosting application. "
    "The edge carries the rule that made it in registry/heroku-apps.json; a name was never enough.")
rel_doc["relation_types"]["in_account"] = ("A provider resource (a Heroku app, a Cloudflare zone) lives in a "
    "provider account. Only when the provider stated the account; registry/accounts.json lists the rest "
    "as unattributed (docs/design/DEPLOYMENTS.md).")
rel_doc["relation_types"]["serves"] = ("A deployment serves one environment of its project. Only on an "
    "override in config/environments.json or the provider's own statement (a Heroku pipeline stage); "
    "registry/environments.json lists the rest as unassigned (docs/design/DEPLOYMENTS.md).")
rel_doc["schema_version"]=2; rel_doc["updated_on"]=OBS; rel_doc["relations"]=relations
_stamped("relations.json", rel_doc)
src_doc["sources"]=sources
_stamped("sources.json", src_doc, stamps=())                                      
_stamped("duplicate-repo-names.json", {"schema_version":1,"updated_on":OBS,"note":"Repository names that occur under more than one owner. Not defects by themselves; each pair needs a human decision.","source_refs":["SRC-0008"],"groups":M["duplicate_repo_names"]})
# A CLONE POINTING AT AN ADDRESS THAT MOVED. The merge follows the transfer on
# every tick so the registry never gains a phantom repository, and until this
# line the record of having done so was written into `model.json` and read by
# nobody. The fix is one command per clone and the operator cannot run it
# without being told which checkout to run it in, so the folder travels with
# the pair. Tracked, unlike the model: it belongs in the diff an operator reads.
_stamped("stale-remotes.json", {"schema_version":1,"updated_on":OBS,"note":"Local checkouts whose `origin` names an address that has been transferred. GitHub keeps the old path working as a redirect, so nothing breaks and nothing says so; the merge follows the transfer to keep the phantom out of the registry. Remedy per row: git -C <path> remote set-url origin git@github.com:<now>.git","source_refs":["SRC-0008"],"clones":M.get("stale_remotes",[])})
print(f"projects={len(out_projs)} repositories={len(out_repos)} relations={len(relations)} sources={len(sources)}")
print("with canonical_page:",sum(1 for p in out_projs if "canonical_page" in p))

# ---- domain liveness -------------------------------------------------------
# A SEPARATE artefact, deliberately. `registry/domains.json` is a curated
# transcription of two documents the operator supplied; this is what DNS and
# RDAP say today. Merging them would destroy the only record of what was
# believed and when, and the first run showed why that record is worth keeping:
# registrar and expiry matched the transcription on every domain RDAP could
# answer for. The transcription was right. What it could not carry is liveness.
LIVE_SRC = paths.SCRATCH / "domains_live.json"
if LIVE_SRC.is_file():
    live = json.loads(LIVE_SRC.read_text(encoding="utf-8"))
    owned = {d["name"]: d for d in
             json.loads((paths.REGISTRY / "domains.json").read_text())["domains"]}
    # THE PREVIOUS ANSWER, so a host that was already dark keeps the day it was
    # FIRST SEEN dark. The file is rewritten whole on every emit, so without this
    # the estate can say a host is dark and never how long — which is the
    # difference between an incident and a decision to stop publishing it
    #.
    was_dark: dict[str, str] = {}
    try:
        for r in json.loads((paths.REGISTRY / "domain-liveness.json")
                            .read_text(encoding="utf-8"))["hosts"]:
            if r.get("dark_first_seen"):
                was_dark[r["host"]] = r["dark_first_seen"]
    except (OSError, ValueError, KeyError, TypeError):
        was_dark = {}
    rows, agree, differ = [], 0, []
    for name, h in sorted(live["hosts"].items()):
        w = live["rdap"].get(h.get("registrable", name), {})
        row = {"host": name, "resolves": h.get("resolves", False),
               "http_status": h.get("http", 0),
               "nameservers": h.get("nameservers", []),
               "checked_on": (h.get("checked_at") or "")[:10]}
        # THREE OUTCOMES, and only one of them starts a clock. `h.get("resolves")`
        # WITHOUT the default above: an absent key is "not measured", and the row
        # defaults it to False for the reader, which would have started the
        # clock on a malformed receipt. A probe that could not run (`None`)
        # neither starts nor clears it: reading "could not look" as "serves
        # nothing" once multiplied the dark domains several times over.
        _seen = h.get("resolves")
        if _seen is False:
            row["dark_first_seen"] = was_dark.get(name) or row["checked_on"]
        elif _seen is None and was_dark.get(name):
            row["dark_first_seen"] = was_dark[name]
        about = w.get("about") if w else None
        # A registry entry of its own + an RDAP record about a SHORTER name means
        # the name sits under a multi-label public suffix and rdap.org has no
        # record for it. The suffix's registrar and expiry are not this domain's.
        wrong_subject = bool(w) and name in owned and about != name
        if w and not wrong_subject:
            row["measured"] = {k: v for k, v in
                               (("about", about),
                                ("registrar", w.get("registrar")),
                                ("expires_on", w.get("expiration")),
                                ("registered_on", w.get("registration")),
                                ("statuses", w.get("statuses"))) if v}
        elif wrong_subject:
            row["unverifiable"] = (f"RDAP answered for {about}, a different name. "
                                   f"{name} sits under a multi-label public suffix and "
                                   f"has no RDAP record of its own; the suffix's "
                                   f"registrar and expiry are not this domain's.")
        d = owned.get(name)
        if d and w and not wrong_subject:
            said_r, said_e = d.get("registrar") or "", (d.get("namecheap") or {}).get("expires_on")
            got_r = (w.get("registrar") or "").lower()
            if (said_r and got_r and said_r not in got_r) or \
               (said_e and w.get("expiration") and said_e != w["expiration"]):
                differ.append({"domain": name, "registry_says": {"registrar": said_r,
                               "expires_on": said_e},
                               "rdap_says": {"registrar": w.get("registrar"),
                                             "expires_on": w.get("expiration")}})
            else:
                agree += 1
        rows.append(row)
    atomic.write_json(paths.REGISTRY / "domain-liveness.json", {
        "schema_version": 1,
        "note": ("Measured by collectors/scan_domains.py from public DNS and RDAP. "
                 "It NEVER overwrites registry/domains.json: that file records what "
                 "the operator transcribed, this one what the network says today, and "
                 "a disagreement is named rather than resolved."),
        # This file carried NO provenance at all until 2026-09-06, and the
        # validator never loaded it, so neither its claims nor their source were
        # checked by anything.
        "source_refs": ["SRC-0011"],
        # A date: nothing downstream reads the instant (searched 2026-09-07 across
        # dashboard/, mcp/, tools/ and survey.py — no reader), the per-host
        # `checked_on` beside it is already a date, and at second resolution this
        # one line guaranteed a diff on every domain scan even when no liveness
        # answer had changed. `store/raw/domains_live.json` keeps the instant.
        "scanned_on": (live.get("scanned_at") or "")[:10],
        "verification": {"agreed": agree, "disagreed": len(differ),
                         "unverifiable": len(live.get("degraded", [])),
                         "disagreements": differ},
        "hosts": rows,
        "degraded": live.get("degraded", []),
    }, indent=1)
    dark = sum(1 for r in rows if not r["resolves"])
    print(f"domain-liveness.json: {len(rows)} host(s), {dark} dark, "
          f"{agree} agree / {len(differ)} disagree with the transcription")


# Said out loud, never inferred from a count. T11 cost a curated `canonical_page`
# and the only sign was a project total falling from 44 to 43.
if cleared:
    print(f"CLEARED by measurement — {len(cleared)} field(s) that had a value now have none. "
          f"If one of these was curated, its home is collectors/project_overrides.json:")
    for line in cleared[:10]:
        print(f"  {line}")
if cleared_repos:
    print(f"CLEARED on repositories — {len(cleared_repos)} field(s); a curated one belongs in "
          f"collectors/repo_overrides.json:")
    for line in cleared_repos[:10]:
        print(f"  {line}")
