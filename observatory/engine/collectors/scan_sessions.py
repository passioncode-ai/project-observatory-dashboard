#!/usr/bin/env python3
"""Where work was DONE, not only where it was committed.

**The defect this closes.** `last_activity_on` is the max of a repository's
GitHub `pushed_at`, its local `last_commit`, and a folder's mtime
(`collectors/merge.py`): every source a git fact. So a project worked on
without a commit is invisible, and `activity.py` then calls it `cooling` or
`dormant` on that silence. The companion session store on one machine held
thousands of session summaries across dozens of project names, and a fair share
of the names that matched a project had sessions NEWER than the registry's last
recorded activity.

**That total is not this collector's own `counts.sessions`, and the difference is
the window.** `window_days` is 365, so the report reads fewer sessions than the
store holds. Two right answers to "how much work", and the README names which is
which. Projects were labelled `cooling` while work went on in them for months.

**It reads somebody else's store, read-only, and says so when it cannot.**
The companion database belongs to the claude-mem plugin. This opens it
`mode=ro`, never writes, and when it is absent or unreadable the result carries
the reason in `degraded` rather than an empty list of sessions: an empty list
would read as "no work happened anywhere", which is the opposite claim.

**Attribution is a declared rule, not a guess.** claude-mem stores a plain
`project` string, derived from the working directory, not a `project:<slug>` id.
The rules below are tried in order and the one that matched is recorded on every
session, so a wrong attribution can be argued with. Names that match nothing are
counted and listed in `degraded`: a sizeable share usually matches nothing, and
that is a fact about the estate worth seeing rather than a silence.

**Sessions are recorded for EVERY matched project, including third-party
clones**, and that is a deliberate departure from `estate.records_events()`.
That rule excludes a third-party clone because *"a third-party clone's commits
are somebody else's history"*: true of a commit, whose author is external, and
false of a session. A session in that clone happened because the operator sat
down and worked in it. Sessions in a third-party checkout are this operator's
attention, and "where was work done" is the question this project exists to
answer.

    python3 collectors/scan_sessions.py store/raw/sessions.json
"""
from __future__ import annotations
import json, pathlib, re, sqlite3, sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import paths                                                                      
import atomic                                                                     

#: The store this reads. One path, and it is the plugin's own.
#: Resolved through `paths`, like every other location this repository
#: reads — see `paths.COMPANION_DB` for why it is redirectable.
STORE = paths.COMPANION_DB

#: Sessions older than this are not read. The event window in
#: `store/retention.json` is 365 days, so anything older would be written and
#: then tombstoned on the next retention pass — work for nothing, and a
#: misleading "events grew" in the meantime.
WINDOW_DAYS = 365


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_index(projects: list[dict]) -> tuple[dict, dict]:
    """(index, conflicts): lowercased name -> (project id, rule), and the ambiguous names.

    Each project contributes its folders, id slug, name and repositories under a
    ranked rule; the strongest rule wins a name, and a tie between projects leaves
    the name ambiguous rather than picking one.
    """
    strengths = {'local folder name': 0, 'project id slug': 1, 'project name': 2,
                 'repository name': 3, 'repository checkout folder': 4}
    best: dict[str, tuple[int, str, set[str]]] = {}

    def put(key: str, pid: str, rule: str) -> None:
        key = (key or '').strip().lower()
        if not key:
            return
        rank = strengths[rule]
        previous = best.get(key)
        if previous is None or rank < previous[0]:
            best[key] = (rank, rule, {pid})
        elif rank == previous[0]:
            previous[2].add(pid)

    valid = {p['id'] for p in projects}
    for p in projects:
        for folder in p.get('local_folders') or []:
            put(folder, p['id'], 'local folder name')
        put(p['id'].split(':', 1)[1], p['id'], 'project id slug')
        put(p.get('name') or '', p['id'], 'project name')
    try:
        repos = json.loads((paths.REGISTRY / 'repositories.json').read_text(encoding='utf-8'))['repositories']
        rel = json.loads((paths.REGISTRY / 'relations.json').read_text(encoding='utf-8'))['relations']
    except (OSError, json.JSONDecodeError, KeyError):
        repos, rel = [], []
    owners: dict[str, set[str]] = {}
    for r in rel:
        if r['type'] == 'implemented_by' and r['from'] in valid:
            owners.setdefault(r['to'], set()).add(r['from'])
    for r in repos:
        for pid in owners.get(r['id'], set()):
            put(r['name_with_owner'].split('/')[-1], pid, 'repository name')
            put((r.get('local') or {}).get('folder'), pid, 'repository checkout folder')
    index, conflicts = {}, {}
    for key, (_, rule, candidates) in sorted(best.items()):
        ordered = sorted(candidates)
        if len(ordered) == 1:
            index[key] = (ordered[0], rule)
        else:
            conflicts[key] = ordered
            index[key] = (None, f"ambiguous {rule}: {', '.join(ordered)}")
    return index, conflicts


def exclusions() -> tuple[list[tuple[str, str]], dict[str, str]]:
    """The curated "not a project of this estate" list: shapes and names.

    A name reported as unattributed work and never resolvable turns a finding
    list into noise, and noise is how a list of real problems stops being read.
    Every row carries WHY, so a wrong exclusion can be argued with rather than
    discovered.
    """
    f = paths.config_file('session_name_exclusions.json')
    if not f.is_file():
        return [], {}
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], {}
    shapes = [(r["pattern"], r["id"]) for r in doc.get("shape_rules", [])]
    names = {r["name"].strip().lower(): r["why"] for r in doc.get("names", [])}
    return shapes, names


def excluded(name: str, shapes: list[tuple[str, str]], names: dict[str, str]) -> str | None:
    """The reason this name is not a project here, or None."""
    key = (name or "").strip().lower()
    if key in names:
        return f"curated: {names[key][:80]}"
    for pattern, rule_id in shapes:
        if re.match(pattern, key):
            return f"shape {rule_id}"
    return None


def attribute(name: str, index: dict) -> tuple[str | None, str]:
    """A project id and the rule, or (None, why not).

    claude-mem writes a path-like name for a session started in a subdirectory:
    `<checkout>/<folder>` for work inside that checkout. The FIRST segment is the
    project; the last is a folder inside it, and matching on the last segment
    would attribute a project's own subdirectory to whatever else happens to
    share that word.
    """
    key = (name or "").strip().lower()
    if not key:
        return None, "the session carries no project name"
    if key in index:
        pid, rule = index[key]
        return pid, rule
    if "/" in key:
        head = key.split("/", 1)[0]
        if head in index:
            pid, rule = index[head]
            return pid, f"{rule} (first path segment of {name!r})"
    return None, f"{name!r} matches no project folder, id or name"


#: WHY THERE IS NO PATH-BASED ATTRIBUTION, written down because it was built,
#: driven against live data, and removed the same hour.
#:
#: The rule was: a name matching no project, whose work touched exactly one
#: folder the registry holds, belongs to that project. It looked like a
#: measurement rather than a guess about a name. It is not. Contact is not
#: ownership, and the live data said so:
#:
#:   * the sessions of a LOST project were credited to a notes vault, because
#:     that work read a reference note in it;
#:   * a skill's sessions were credited to the family repository the work
#:     happened to sit beside, while the registry held the skill's own project.
#:
#: Without the measurement, the rule would have silently given one project the
#: sessions of another's work: the misattribution this collector exists to
#: prevent, one source over. The classification below is evidence and stays; the
#: decision it informs is the operator's.


def estate_paths(*blobs: str | None) -> set[str]:
    """Every path under the estate root that these JSON blobs name.

    claude-mem stores `files_read`/`files_edited` as JSON arrays, and
    `group_concat` joins several rows' arrays with `|`. Anything that does not
    parse is skipped rather than guessed at: a malformed blob is not evidence.
    """
    out: set[str] = set()
    root = str(paths.DATA)
    for blob in blobs:
        for chunk in (blob or "").split("|"):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                items = json.loads(chunk)
            except ValueError:
                items = [chunk]
            if isinstance(items, str):
                items = [items]
            for item in items if isinstance(items, list) else []:
                if isinstance(item, str) and item.startswith(root + "/"):
                    # The estate-relative FOLDER, not the file: the question is
                    # which project this work belonged to.
                    out.add(item[len(root) + 1:].split("/", 1)[0])
    return out


def verdict_for(folders: set[str]) -> tuple[str, list[str]]:
    """What an unmatched name IS, from the paths its work touched.

    Three answers where the finding used to offer two, and the third is the one
    worth raising: a project this estate had and has lost. The evidence is
    already in the session store; nothing here guesses.
    """
    gone = sorted(f for f in folders if not (paths.DATA / f).is_dir())
    if gone:
        return "estate-folder-gone", gone
    if folders:
        # The folders exist, so the name is an alias for work in a folder the
        # matcher did not connect — a rule to add, not a project to invent.
        return "folder-exists-unmatched", sorted(folders)
    return "no-path-recorded", []


def scan() -> dict:
    """Sessions from BOTH sources: the Stop hook's own `session` records in the
    ledger, and — where it is still installed — the companion's store.

    The hook's records come first because they are this engine's own: one per
    Claude Code session, already attributed to a project id by the hook, and
    present on every machine with the plugin. The companion was retired on
    machines that used it, and a history read only from it was degraded for
    ever. A session in both is one session; its earliest start and its larger
    turn count are kept."""
    companion = _scan_companion()
    own, own_degraded = _from_ledger()
    merged: dict[tuple[str, str], dict] = {}
    for row in [*companion["sessions"], *own]:
        key = (row["session_id"], row["project_id"])
        prev = merged.get(key)
        if prev is None:
            merged[key] = dict(row)
            continue
        prev["prompts"] = max(prev["prompts"], row["prompts"])
        prev["started_at"] = min(x for x in (prev["started_at"], row["started_at"]) if x) \
            if (prev["started_at"] or row["started_at"]) else None
        prev["started_on"] = (prev["started_at"] or prev["started_on"] or "")[:10]
        prev["ended_on"] = max(prev["ended_on"] or "", row["ended_on"] or "")
    out = dict(companion)
    out["sessions"] = sorted(merged.values(), key=lambda s: (s["project_id"], s["session_id"]))
    out["sources"] = ["ledger:session", str(STORE)]
    out["counts"] = dict(companion["counts"], sessions=len(out["sessions"]),
                         projects=len({s["project_id"] for s in out["sessions"]}),
                         from_stop_hook=len(own))
    degraded = list(companion.get("degraded") or [])
    not_applicable = list(companion.get("not_applicable") or [])
    if own:
        # THE COMPANION'S ABSENCE NO LONGER COSTS THE HISTORY. Its messages
        # said "activity falls back to commits alone", which stops being true
        # once the hook's records answer; they are kept, as not-applicable.
        moved = [d for d in degraded if d.get("source") == "claude-mem"
                 and "falls back to commits alone" in d.get("reason", "")]
        degraded = [d for d in degraded if d not in moved]
        not_applicable += [dict(d, reason=d["reason"].replace(
            "activity falls back to commits alone",
            "the Stop hook's own session records are the history")) for d in moved]
        for d in not_applicable:
            d["reason"] = d["reason"].replace(
                "activity rests on commits alone",
                "the Stop hook's own session records are the history")
    out["degraded"] = degraded + own_degraded
    if not_applicable:
        out["not_applicable"] = not_applicable
    return out


def _from_ledger() -> tuple[list[dict], list[dict]]:
    """The Stop hook's `session` records: one memory per session, a revision per
    turn that moved files. Read-only; attribution is the hook's own."""
    cutoff = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp()
                                    - WINDOW_DAYS * 86400, timezone.utc
                                    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not paths.DB.is_file():
        # No store yet is a workspace where nothing has been recorded: a
        # measured empty, not a failure to read one.
        return [], []
    try:
        conn = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT l.session_id AS sid, l.project_id AS pid, min(l.created_at) AS started,"
            "       max(l.created_at) AS ended, count(*) AS turns"
            " FROM ledger l LEFT JOIN tombstones t ON t.memory_id = l.memory_id"
            " WHERE l.kind = 'session' AND l.session_id IS NOT NULL"
            "   AND l.project_id IS NOT NULL AND t.memory_id IS NULL AND l.created_at >= ?"
            " GROUP BY l.session_id, l.project_id", (cutoff,)).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        return [], [{"source": "ledger", "reason": f"the Stop hook's session records could "
                                                   f"not be read ({type(exc).__name__})"}]
    return [{"session_id": r["sid"], "project_id": r["pid"], "claude_mem_project": "",
             "matched_by": "stop-hook", "started_on": (r["started"] or "")[:10],
             "ended_on": (r["ended"] or "")[:10], "started_at": r["started"],
             "prompts": r["turns"]} for r in rows], []


def _scan_companion() -> dict:
    projects = json.loads((paths.REGISTRY / "projects.json")
                          .read_text(encoding="utf-8"))["projects"]
    index, conflicts = build_index(projects)

    degraded: list[dict] = []
    # NOT INSTALLED IS NOT BROKEN. The store's own directory is the companion's
    # home: when that is absent the tool is not on this machine at all, which is
    # a fact about the machine and asks nobody to repair anything. It is still
    # named, on `not_applicable`, because activity then rests on commits alone.
    # A home WITHOUT its database is the companion installed and failing, and
    # stays a degradation below.
    if STORE.parent.name == "unconfigured":
        # `configuration.source_path`'s placeholder: the source was never set, so
        # nothing was looked for — a configuration gap, not an absent tool.
        return {"scanned_on": now()[:10], "source": str(STORE), "sessions": [],
                "counts": {"sessions": 0, "projects": 0, "unmatched_names": 0},
                "degraded": [{"source": "claude-mem",
                              "reason": "the sessions integration is on and its `companion_db` "
                                        "source is not configured; activity falls back to "
                                        "commits alone"}]}
    if not STORE.parent.is_dir():
        return {"scanned_on": now()[:10], "source": str(STORE), "sessions": [],
                "counts": {"sessions": 0, "projects": 0, "unmatched_names": 0},
                "degraded": [],
                "not_applicable": [{"source": "claude-mem",
                                    "reason": f"claude-mem is not installed on this machine: "
                                              f"{STORE.parent} does not exist, so no session is "
                                              f"known and activity rests on commits alone"}]}
    if not STORE.is_file():
        return {"scanned_on": now()[:10], "source": str(STORE), "sessions": [],
                "counts": {"sessions": 0, "projects": 0, "unmatched_names": 0},
                "degraded": [{"source": "claude-mem",
                              "reason": f"{STORE} does not exist, so no session is known; "
                                        f"activity falls back to commits alone"}]}
    try:
        conn = sqlite3.connect(f"file:{STORE}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        return {"scanned_on": now()[:10], "source": str(STORE), "sessions": [],
                "counts": {"sessions": 0, "projects": 0, "unmatched_names": 0},
                "degraded": [{"source": "claude-mem",
                              "reason": f"the store is unreadable ({exc}); activity falls "
                                        f"back to commits alone"}]}

    cutoff = (datetime.now(timezone.utc).timestamp() - WINDOW_DAYS * 86400) * 1000
    sessions, unmatched = [], {}
    try:
        rows = conn.execute(
            "SELECT memory_session_id AS sid, project, min(created_at) AS started,"
            "       max(created_at) AS ended, count(*) AS prompts"
            " FROM session_summaries"
            " WHERE project IS NOT NULL AND project != ''"
            "   AND created_at_epoch >= ?"
            " GROUP BY memory_session_id, project", (cutoff,)).fetchall()
    except sqlite3.Error as exc:
        conn.close()
        return {"scanned_on": now()[:10], "source": str(STORE), "sessions": [],
                "counts": {"sessions": 0, "projects": 0, "unmatched_names": 0},
                "degraded": [{"source": "claude-mem",
                              "reason": f"the store's shape has moved ({exc}); this collector "
                                        f"reads `session_summaries(memory_session_id, project, "
                                        f"created_at, created_at_epoch)`"}]}
    # A WINDOW THAT MATCHES NOTHING IS NOT AN EMPTY ESTATE. `created_at_epoch`
    # is claude-mem's field in claude-mem's unit, MILLISECONDS: read as seconds
    # its values would land tens of thousands of years ahead. The cutoff above
    # multiplies by 1000 on that basis, so a switch to seconds upstream would put
    # every row BELOW it: the query would succeed, return nothing, and this
    # collector would report `sessions: 0` with no degradation at all, while
    # `activity.py` went on calling projects `cooling` and `dormant` on that
    # silence, which is the state this file was written to prevent.
    #
    # The guard is structural, not a unit check: if the table holds rows and the
    # WINDOW holds none, the filter has stopped matching whatever the reason
    # (a renamed column, a changed unit, a clock skew). Three outcomes again, and
    # the third is what was missing.
    if not rows:
        try:
            total = ev_total = conn.execute(
                "SELECT count(*) FROM session_summaries").fetchone()[0]
        except sqlite3.Error:
            total = ev_total = 0
        if total:
            conn.close()
            return {"scanned_on": now()[:10], "source": str(STORE), "sessions": [],
                    "counts": {"sessions": 0, "projects": 0, "unmatched_names": 0},
                    "window_days": WINDOW_DAYS,
                    "degraded": [{"source": "claude-mem",
                                  "reason": f"the store holds {total} session "
                                            f"summaries and none fell inside the "
                                            f"{WINDOW_DAYS}-day window; the filter has "
                                            f"stopped matching — `created_at_epoch` is "
                                            f"read as milliseconds, and a renamed column "
                                            f"or a changed unit looks exactly like this. "
                                            f"Activity falls back to commits alone"}]}
        del ev_total

    # HELD OPEN for the verdict query below, and closed on the way out. The
    # connection was closed here, so reading the evidence needed either a second
    # open or this line moved; one connection for one read-only pass is the
    # smaller change and the store is opened `mode=ro` either way.
    ev_conn = conn

    # Aggregated by (session, PROJECT ID), not by (session, claude-mem name).
    # Two names can resolve to one project (a checkout and a subfolder path
    # inside it both mean the same project), so grouping on the name produced
    # two rows for one fact, and the UNIQUE index then collapsed them at insert
    # time and reported "already present". Collapsing the same fact is correct;
    # letting the DATABASE do it made a second run and a duplicate spelling
    # indistinguishable in the output.
    merged: dict[tuple[str, str], dict] = {}
    shapes, names = exclusions()
    skipped: dict[str, str] = {}
    for r in rows:
        pid, rule = attribute(r["project"], index)
        if pid is None:
            key = r['project'].strip().lower()
            # A name more than one project can claim is AMBIGUOUS, not unknown:
            # it is kept with its candidates so the operator can settle it.
            candidates = conflicts.get(key) or conflicts.get(key.split('/', 1)[0])
            if candidates and rule.startswith('ambiguous '):
                u = unmatched.setdefault(r['project'], {'sessions': 0,
                    'candidates': candidates, 'reason': rule, 'session_ids': set()})
                u['sessions'] += 1
                u['session_ids'].add(r['sid'])
                continue
            # TWO outcomes, not one. A name the operator has classified as "not
            # a project here" is skipped with its reason on record; anything
            # else is UNKNOWN WORK and becomes a finding. Reporting both as
            # "unmatched" is what makes a list of eighteen names — thirty of
            # whose sessions are a plugin's own version folders — read as noise,
            # and a noisy list is one nobody opens.
            why = excluded(r["project"], shapes, names)
            if why:
                skipped[r["project"]] = why
                continue
            u = unmatched.setdefault(r["project"], {"sessions": 0})
            u["sessions"] += 1
            continue
        key = (r["sid"], pid)
        prev = merged.get(key)
        if prev is not None:
            prev["prompts"] += r["prompts"]
            prev["started_at"] = min(prev["started_at"] or "", r["started"] or "") or None
            prev["started_on"] = (prev["started_at"] or "")[:10]
            prev["ended_on"] = max(prev["ended_on"], (r["ended"] or "")[:10])
            # `spellings`, not `names`: this used to rebind `names`, which is the
            # EXCLUSION dictionary passed to `excluded()` below. After the first
            # session that spanned two claude-mem names, every later lookup ran
            # against a set of project strings instead — so the curated
            # exclusions silently stopped applying and the home folder landed in BOTH
            # buckets, excluded once and reported twelve times. A pure function
            # returning two answers for one argument is how the shadowing
            # showed itself; the invariant that catches it is that the two
            # buckets can never share a name (tests/test_sessions.py).
            spellings = set(prev["claude_mem_project"].split(" + ")) | {r["project"]}
            prev["claude_mem_project"] = " + ".join(sorted(spellings))
            continue
        merged[key] = {
            "session_id": r["sid"],
            "project_id": pid,
            "claude_mem_project": r["project"],
            "matched_by": rule,
            # DATE, not an instant: this lands in a committed projection through
            # `merge.py`, and that is where the boundary sits; the store keeps
            # the precise time on the event row.
            "started_on": (r["started"] or "")[:10],
            "ended_on": (r["ended"] or "")[:10],
            "started_at": r["started"],
            "prompts": r["prompts"],
        }
    sessions = list(merged.values())

    # THE EVIDENCE LIVES IN `observations`, NOT IN `session_summaries`. The first
    # version of this read `session_summaries.files_read`, NULL in every row for
    # a lost project, while `observations.files_read` named paths under the
    # estate root. Measuring the instrument before doubting the thing is what
    # found it.
    #
    # Queried once per UNMATCHED name only: the matched ones need no verdict, and
    # `observations` can hold thousands of rows for a single name.
    for name, u in unmatched.items():
        u["paths"] = set()
        try:
            for row in ev_conn.execute(
                    "SELECT files_read, files_modified FROM observations"
                    " WHERE project = ? AND (files_read LIKE ? OR files_modified LIKE ?)"
                    " LIMIT 200", (name, f"%{paths.DATA}/%", f"%{paths.DATA}/%")):
                u["paths"] |= estate_paths(row["files_read"], row["files_modified"])
        except sqlite3.Error as exc:
            # NAMED, never silently empty: a shape change here would make every
            # lost project read as "no path recorded", which is the answer this
            # whole classification exists to stop being the default.
            degraded.append({
                "source": "claude-mem",
                "reason": f"the paths behind {name!r} could not be read "
                          f"({type(exc).__name__}: {str(exc)[:60]}), so its verdict "
                          f"is `no-path-recorded` for want of evidence rather than "
                          f"because there is none"})
    if unmatched:
        top = sorted(unmatched.items(), key=lambda kv: -kv[1]["sessions"])[:8]
        degraded.append({
            "source": "claude-mem",
            "reason": f"{len(unmatched)} claude-mem project name(s) cannot be attributed to one project here, "
                      f"so {sum(u['sessions'] for u in unmatched.values())} session(s) are "
                      f"unattributed. Largest: "
                      + ", ".join(f"{n} ({u['sessions']})" for n, u in top)})

    ev_conn.close()
    touched = {s["project_id"] for s in sessions}
    return {
        "scanned_on": now()[:10],
        "source": str(STORE),
        "window_days": WINDOW_DAYS,
        "counts": {"sessions": len(sessions), "projects": len(touched),
                   "unmatched_names": len(unmatched),
                   "excluded_names": len(skipped)},
        # The unknown names and their weight, so `tools/build_findings.py` can
        # raise them without re-reading claude-mem. Sorted by sessions: the
        # question a finding answers is "how much work is unaccounted for".
        # THE VERDICT TRAVELS WITH THE COUNT. A name that matches nothing is
        # three different facts — a project this estate lost, a folder the
        # matcher failed to connect, or a name with no path evidence at all —
        # and the finding could not tell them apart because the collector did
        # not carry what distinguishes them.
        "unattributed": [
            {"name": n, "sessions": u["sessions"],
             "verdict": 'ambiguous-project' if u.get('candidates') else verdict_for(u["paths"])[0],
             "folders": verdict_for(u["paths"])[1][:6],
             **({'candidates': u['candidates'], 'reason': u['reason'],
                 'session_ids': sorted(u['session_ids'])} if u.get('candidates') else {})}
            for n, u in sorted(unmatched.items(), key=lambda kv: -kv[1]["sessions"])],
        "excluded": [{"name": n, "why": w} for n, w in sorted(skipped.items())],
        "sessions": sorted(sessions, key=lambda s: (s["project_id"], s["session_id"])),
        "degraded": degraded,
    }


def to_events(out: dict) -> int:
    """Write the sessions into `events`, idempotently.

    `events` already anticipated this: its own schema comment lists the kinds as
    `commit | session | deploy | scan | ...`, and `events_dedup` is UNIQUE on
    `(kind, ref)`, so a session's `memory_session_id` as `ref` makes a second run
    free. `INSERT OR IGNORE` is the same discipline `collectors/scan_events.py`
    uses for commits.

    `actor` is `operator`, and that word carries NO authority here: `events` is
    a measured stream, not the ledger, and the field describes who did the work
    rather than who may approve anything. `project_week.authors` counts distinct
    actors for `kind='commit'` only (`store/rollup.py`), so this cannot inflate
    a rollup; verified before writing rather than after.
    """
    from store import db as store_db
    conn = store_db.connect()
    scan_id = store_db.scan_id("sessions", now())
    # Session events are a DERIVED stream, rebuildable from claude-mem, so a key
    # change is a rewrite rather than a migration. Rows written under the old
    # `ref = session_id` are removed here; without this the store would hold two
    # key shapes for one fact and every count of them would be wrong. Stated
    # rather than done quietly, because deleting measured rows is not a detail.
    stale = conn.execute(
        "DELETE FROM events WHERE kind='session' AND ref NOT LIKE '%:project:%'")
    if stale.rowcount:
        print(f"  removed {stale.rowcount} session event(s) written under the old key shape")
    inserted = skipped = withdrawn = 0
    try:
        # Withdraw events for a (session, project) pair that is now AMBIGUOUS and
        # no longer supported by a clean match: a name that became contested must
        # stop crediting whichever candidate it was credited to before.
        supported = {(s['session_id'], s['project_id']) for s in out['sessions']}
        ambiguous = {(sid, pid) for row in out.get('unattributed', [])
                     if row.get('verdict') == 'ambiguous-project'
                     for sid in row.get('session_ids', [])
                     for pid in row.get('candidates', [])}
        for sid, pid in sorted(ambiguous - supported):
            withdrawn += conn.execute("DELETE FROM events WHERE kind='session' AND ref=?",
                                      (f'{sid}:{pid}',)).rowcount
        conn.execute("INSERT INTO scans (id, started_at, collector_version) VALUES (?,?,?)",
                     (scan_id, now(), "scan_sessions/1"))
        for s in out["sessions"]:
            cur = conn.execute(
                "INSERT OR IGNORE INTO events (id, project_id, repo_id, kind, ref, actor,"
                " occurred_at, payload_json) VALUES (?,?,?,?,?,?,?,?)",
                # The subject is the session TIMES the project, not the session.
                # With `ref = session_id` alone, the UNIQUE `(kind, ref)` index
                # silently kept the first row and dropped the rest, and some
                # sessions genuinely span several projects, because the operator
                # moves between checkouts inside one session. Rows vanished as
                # "already present" on the very first run, which is the silent
                # loss shape rather than idempotency.
                (f"session:{s['session_id']}:{s['project_id']}", s["project_id"], None,
                 "session", f"{s['session_id']}:{s['project_id']}", "operator",
                 s["started_at"] or s["started_on"],
                 json.dumps({"prompts": s["prompts"], "matchedBy": s["matched_by"],
                             "claudeMemProject": s["claude_mem_project"]},
                            ensure_ascii=False)))
            inserted += cur.rowcount
            skipped += 1 - cur.rowcount
        conn.execute(
            "UPDATE scans SET finished_at = ?, counts_json = ?, degraded_json = ? WHERE id = ?",
            (now(), json.dumps({"sessions_inserted": inserted,
                                "sessions_already_present": skipped,
                                "sessions_ambiguous_withdrawn": withdrawn,
                                "projects": out["counts"]["projects"],
                                "unmatched_names": out["counts"]["unmatched_names"]}),
             json.dumps(out["degraded"], ensure_ascii=False), scan_id))
        conn.commit()
    finally:
        conn.close()
    print(f"  events: {inserted} new session event(s), {skipped} already present, "
          f"{withdrawn} ambiguous attribution(s) withdrawn")
    return inserted


def main(argv: list[str]) -> int:
    import configuration
    if not configuration.enabled("sessions"):
        print("sessions: not configured (integration disabled)")
        return 0
    dest = pathlib.Path(argv[1]) if len(argv) > 1 else paths.SCRATCH / "sessions.json"
    out = scan()
    atomic.write_json(dest, out)
    c = out["counts"]
    print(f"{c['sessions']} session(s) across {c['projects']} project(s) -> {dest}")
    if "--no-events" not in argv:
        to_events(out)
    for d in out["degraded"]:
        print(f"  DEGRADED {d['source']}: {d['reason']}", file=sys.stderr)
    for d in out.get("not_applicable") or []:
        print(f"  not applicable {d['source']}: {d['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
