#!/usr/bin/env python3
"""Render the project dashboard from the typed inventory. Standard library only.

Reads registry/{projects,repositories,relations,domains}.json and writes a
self-contained HTML page that opens over file:// with no server.
"""
from __future__ import annotations
import html, json, os, re, sys
from datetime import datetime, timedelta, timezone
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import atomic
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))                       
import paths                                                                    
import i18n
import finding_types
import subject_links
# `paths`, not `ROOT / "registry"`. The third tool found with the path inlined —
# after the validator — and the consequence is the same: it is the
# one file `OBSERVATORY_REGISTRY` cannot redirect, so a test that points it at a
# copy silently renders the live registry and passes for the wrong reason.
INV = paths.REGISTRY
OUT = paths.DASHBOARD_HTML


def load(name):
    with (INV / name).open(encoding="utf-8") as handle:
        return json.load(handle)


#: The rolling windows the work tiles report over. Rolling rather than calendar
#: weeks, because every one of the 852 rows in `project_week` is still `open` —
#: the freeze rule fires at `week_start < today - 365d` and the oldest week is
#: exactly one year old — so a "this week" tile would be comparing a partial week
#: against a whole one.
WORK_WINDOWS = (7, 28)



#: The clone states that assert work on this disk and possibly nowhere else.
#: Named here rather than repeated: `collectors/scan_remotes.AT_RISK_STATES` is
#: the same list at the measuring end, and the two must not drift.
AT_RISK_SYNC = ("ahead", "local-only-branch", "unpushed-and-remote-moved",
                "diverged")


def at_risk_total(repos: dict) -> dict:
    """One tile: how many commits across all checkouts are on no remote.

    Three outcomes, and the middle one is the reason this is a function. No
    at-risk checkout means there is nothing to say. At-risk checkouts that all
    carry a count give their sum. At-risk checkouts with NO count mean the
    remotes probe has not measured them yet, and printing `0` there would be a
    reassurance about work nobody has looked at.
    """
    risky = [r for r in repos.values()
             if (r.get("local") or {}).get("sync") in AT_RISK_SYNC]
    if not risky:
        return {}
    counted = [r for r in risky if isinstance((r.get("local") or {}).get("unpushed"), int)]
    # MEASURED AS NOTHING is its own group, not part of the unmeasured. Two
    # `local-only-branch` checkouts hold no commit any remote lacks, and calling
    # them "not counted" overstated the uncertainty of the estate's own figure.
    nothing = [r for r in risky if (r.get("local") or {}).get("nothing_exclusive")]
    unmeasured = len(risky) - len(counted) - len(nothing)
    if not counted:
        return {"unpushed_commits":
                "nothing to lose" if nothing and not unmeasured else "not measured"}
    total = sum(r["local"]["unpushed"] for r in counted)
    if unmeasured:
        return {"unpushed_commits": f"{total}+", "unpushed_unmeasured": unmeasured}
    return {"unpushed_commits": total}


def work_stats() -> dict:
    """What HAPPENED across all projects, as tiles.

    The other tiles count what exists — projects, repositories, sites, notes —
    and none of them counts work, although the store holds every commit event
    and watching where the work went is part of this system's purpose.

    COUNTED FROM `events`, never by summing a rollup column. `active_days`,
    `authors` and `worked_days` are set sizes folded at write time, and adding
    them across projects would count one Tuesday once per project. A distinct
    count over the event rows is the one place the question can be answered
    without that error.

    ABSENT, NOT ZERO, without a store. A fresh clone has none — it is gitignored
    — and `0 commits` where the truth is "there is no store" would be an
    inversion of the facts. So an unreadable store yields `{}` and the tiles
    simply do not appear.
    """
    import sqlite3
    if not paths.DB.exists():
        return {}
    out: dict[str, object] = {}
    try:
        conn = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return {}
    try:
        for days in WORK_WINDOWS:
            r = conn.execute(
                "SELECT count(*) c, count(DISTINCT project_id) p FROM events"
                " WHERE kind='commit' AND occurred_at >= date('now', ?)",
                (f"-{days} days",)).fetchone()
            if r is None:
                continue
            # The window is IN THE LABEL. "2222" means nothing on its own, and a
            # tile is one number beside one caption.
            out[f"commits_{days}d"] = r["c"]
            out[f"projects_active_{days}d"] = r["p"]
        # WHERE the work went, not only how much. A name rather than a number,
        # which the tile renderer prints identically.
        top = conn.execute(
            "SELECT project_id, count(*) c FROM events WHERE kind='commit'"
            "   AND project_id IS NOT NULL"
            f"  AND occurred_at >= date('now', '-{max(WORK_WINDOWS)} days')"
            " GROUP BY project_id ORDER BY c DESC LIMIT 1").fetchone()
        if top is not None:
            out[f"busiest_{max(WORK_WINDOWS)}d"] = (
                top["project_id"].split(":", 1)[-1])
    except sqlite3.Error as exc:
        # NAMED. A silent handler here would drop the tiles and leave the page
        # looking exactly as it did before this existed.
        print(f"  work tiles unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return {}
    finally:
        conn.close()
    return out


def clip_words(text: str, limit: int) -> str:
    """`text` cut to at most `limit` characters at a word boundary, with an
    ellipsis when anything was cut. A statement sliced mid-word with no mark
    read as complete and as misspelt at once (the health page's queue)."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    space = cut.rfind(" ")
    # A single unbroken token longer than most of the limit (a path, a hash)
    # is cut where it is: dropping it whole would hide what the row is about.
    if space >= limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:—-") + "…"


def fold_notes(rows, limit: int = 5) -> dict[str, list[dict]]:
    """The observatory's conclusions per project, newest first, with identical
    statements folded into one line that counts them.

    The ledger keeps a conclusion each time it is drawn, so the project panel
    printed the same sentence five times and showed nothing else. One line per
    distinct statement, its newest date and state, and `n` repeats — up to
    `limit` distinct statements, the repeats of those counted beyond it."""
    out: dict[str, list[dict]] = {}
    for r in rows:
        bucket = out.setdefault(r["project_id"], [])
        text = clip_words(r["statement"] or "", 400)
        same = next((n for n in bucket if n["text"] == text), None)
        if same is not None:
            same["n"] += 1
            continue
        if len(bucket) >= limit:
            continue
        bucket.append({"at": (r["created_at"] or "")[:10], "text": text,
                       "state": r["state"] or "", "conf": r["confidence"], "n": 1})
    return out


def _queue(conn, limit: int = 12) -> list[dict]:
    """The newest proposals awaiting a human, newest first.

    Newest rather than oldest, deliberately: the oldest are closest to expiry
    and saying so as a NUMBER is `ledger.review_backlog`'s job. What a person
    scanning the page wants is what the agent has just concluded, which is the
    half a count cannot carry.
    """
    # THE CURRENT REVISION ONLY, and the join is not decoration: counting every
    # `proposed` ROW overcounts against the health panel's `proposed`, because
    # a superseded revision keeps its state. Two numbers for "waiting" on one
    # page is worse than one number that is harder to compute — so this uses
    # the panel's own definition, character for character.
    CURRENT = ("ledger l JOIN (SELECT memory_id, MAX(revision) r FROM ledger"
               " GROUP BY memory_id) m ON m.memory_id = l.memory_id"
               " AND m.r = l.revision WHERE l.state = 'proposed'")
    return [{"id": r["memory_id"], "rev": r["revision"], "kind": r["kind"],
             "project": r["project_id"], "at": (r["created_at"] or "")[:16].replace("T", " "),
             "statement": clip_words(r["statement"] or "", 200)}
            for r in conn.execute(
                "SELECT l.memory_id, l.revision, l.kind, l.project_id, l.created_at,"
                f" l.statement FROM {CURRENT}"
                " ORDER BY l.created_at DESC LIMIT ?", (limit,))]


def _digest(conn) -> dict:
    """The review queue in one line: how many rows wait, of which kinds, and
    what retention erases first — the same arithmetic `tools/review.py digest`
    prints, read here so the health page can say it above the rows instead of
    leaving the operator to count. Retention's horizon is the store's own
    (`retention.json → ledger.proposed_days`, 90 when unstated); the exempt
    owners are `review.py`'s to know, so this counts every waiting row and
    says "erases" of the ones whose day has come, not of the exempt.
    """
    CURRENT = ("ledger l JOIN (SELECT memory_id, MAX(revision) r FROM ledger"
               " GROUP BY memory_id) m ON m.memory_id = l.memory_id"
               " AND m.r = l.revision WHERE l.state = 'proposed'")
    horizon = 90
    try:
        cfg = json.loads((paths.config_file("retention.json")).read_text(encoding="utf-8"))
        horizon = int(cfg["ledger"]["proposed_days"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    rows = [dict(r) for r in conn.execute(
        f"SELECT l.kind, l.created_at FROM {CURRENT}")]
    by_kind = Counter((r.get("kind") or "?") for r in rows)
    now = datetime.now(timezone.utc)
    soon_n, first = 0, None
    for r in rows:
        try:
            made = datetime.strptime(r.get("created_at") or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        gone = made + timedelta(days=horizon)
        if gone <= now + timedelta(days=7):
            soon_n += 1
            first = gone if first is None or gone < first else first
    return {"waiting": len(rows), "by_kind": dict(by_kind.most_common()),
            "horizon_days": horizon, "erases_within_7d": soon_n,
            "first_erase_on": first.strftime("%Y-%m-%d") if first else None}


def heartbeat_age_s(stamp: str, now: datetime | None = None) -> int:
    """Whole seconds since a `%Y-%m-%dT%H:%M:%SZ` stamp, from an aware UTC clock.

    Replaces the naive `datetime.utcnow` clock, deprecated since Python 3.12;
    the number is the same. A stamp in any other shape raises ValueError, as
    strptime always did.
    """
    at = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return int(((now or datetime.now(timezone.utc)) - at).total_seconds())


def from_store() -> dict:
    """What the STORE knows, or an honest emptiness.

    Without this the page would read `registry/*.json` and nothing else, and
    the events, weekly rollups, plugin measurements, the wallet and the
    provider health would all be invisible on the one screen that exists to
    show them. The registry says what EXISTS; only the store says what
    HAPPENED.

    It degrades rather than failing: a fresh clone has no store — it is
    gitignored — and a dashboard that cannot be built without one would make
    the gate unrunnable there.
    """
    import sqlite3
    out = {"weeks": {}, "metrics": {}, "timeline": {}, "notes": {},
       "health": {}, "degraded": "", "queue": []}
    if not paths.DB.exists():
        out["degraded"] = {"text": "no local store — history and metrics are unavailable"}
        return out
    try:
        conn = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        out["degraded"] = {"text": "the store did not open: {error}", "args": {"error": type(exc).__name__}}
        return out
    try:
        out["queue"] = _queue(conn)
        out["digest"] = _digest(conn)
    except sqlite3.Error as exc:
        # The queue is the least of what this function returns; a store that
        # cannot answer for it must not cost the caller the weeks, metrics and
        # health beside it.
        out["queue"] = []
        # One sentence carries both faults, so the page still has one line to translate.
        prior = out["degraded"]
        out["degraded"] = ({"text": "{prior}; the queue could not be read: {error}",
                            "args": {"prior": prior["text"], "error": type(exc).__name__}} if prior else
                           {"text": "the queue could not be read: {error}", "args": {"error": type(exc).__name__}})
    try:
        # Twenty-six weeks: half a year is the longest span a sparkline this
        # size can show without becoming a smear, and the rollup keeps the rest.
        for r in conn.execute(
                "SELECT project_id, week_start, commits, sessions, worked_days"
                " FROM project_week"
                " WHERE week_start >= date('now', '-182 days') ORDER BY week_start"):
            # `s` and `d` may be NULL: a row computed before the session columns
            # existed. Carried through as null rather than 0 so the
            # page can tell "no work" from "not measured" — the reason those
            # columns are nullable at all.
            out["weeks"].setdefault(r["project_id"], []).append(
                {"w": r["week_start"], "c": r["commits"],
                 "s": r["sessions"], "d": r["worked_days"]})
        # EVERY metric, by name and unit, never a name this file knows. The
        # first version read `disk.bytes` explicitly and the plugin suite caught
        # it: a core file naming a plugin's metric is the six-file problem
        # returning one row at a time. The manifest declares the unit; this
        # renders whatever arrives.
        # THE CAPTIONS, from the manifests, through the plugin layer's own
        # reader. Five numbers sat on a project's row — `70 packages · 92 MB ·
        # 2.6 GB · 8 days · 6 tags` — and three were a guess without a mouse:
        # the two byte figures are what the project COSTS and what deleting its
        # package directories GIVES BACK, opposite meanings told apart only by
        # a `title` attribute. A map from metric name to caption may not live
        # here (the six-file problem), so the plugin declares it and this reads
        # it.
        labels: dict[str, str] = {}
        try:
            sys.path.insert(0, str(paths.ROOT / "collectors"))
            import run_plugins
            for man in run_plugins.manifests():
                for met in man.get("metrics", []) or []:
                    lab = str(met.get("label") or "").strip()
                    if lab and met.get("name"):
                        labels[met["name"]] = lab
        except Exception as exc:                                                    
            # NAMED, not swallowed. Without captions the page still renders —
            # the script falls back to the metric name — so this degrades rather
            # than fails, and says which.
            print(f"  metric captions unavailable: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
        # THE PREVIOUS VALUE, because retention keeps it for exactly this and
        # nothing was showing it. `store/retention.json` holds
        # `metrics_keep_per_series: 2` with the reason written beside it — "the
        # newest, plus one so a reader can see whether it moved" — and the page
        # rendered only the newest, so the second row was kept for a reader who
        # had no way to read it. Measured 2026-09-09: 911 metric rows, seven
        # series, two or three samples each, and no movement visible anywhere.
        #
        # ABSENT, never zero: a series with one sample carries no `p` at all,
        # and the script renders nothing rather than "+0" — a first measurement
        # and an unchanged one are different facts.
        rows = list(conn.execute(
            "SELECT project_id, metric, unit, value, at FROM metrics"
            " ORDER BY project_id, metric, at DESC"))
        seen: dict[tuple[str, str], int] = {}
        for r in rows:
            key = (r["project_id"], r["metric"])
            rank = seen.get(key, 0)
            seen[key] = rank + 1
            if rank == 0:
                out["metrics"].setdefault(r["project_id"], []).append(
                    {"n": r["metric"], "v": r["value"], "u": r["unit"],
                     "l": labels.get(r["metric"], ""), "at": r["at"]})
            elif rank == 1:
                for m in out["metrics"][r["project_id"]]:
                    if m["n"] == r["metric"]:
                        m["p"], m["pat"] = r["value"], r["at"]
                        break
        # S3: the story of ONE project. Ten commits is what a panel can show
        # without becoming a log; the rest is what `observatory_timeline` is for.
        for r in conn.execute(
                "SELECT project_id, occurred_at, actor, payload_json FROM events"
                " WHERE kind='commit' AND project_id IS NOT NULL"
                " ORDER BY occurred_at DESC"):
            bucket = out["timeline"].setdefault(r["project_id"], [])
            if len(bucket) >= 10:
                continue
            try:
                pay = json.loads(r["payload_json"] or "{}")
            except json.JSONDecodeError:
                pay = {}
            bucket.append({"at": r["occurred_at"][:10], "who": r["actor"] or "",
                           "what": clip_words(pay.get("subject") or "", 110),
                           "repo": pay.get("repo") or ""})
        # What the observatory CONCLUDED, with its confidence and its state — the
        # agent proposes and never asserts (a model may only propose), so a reader must
        # see `proposed` beside the sentence or the caveat is lost.
        # THE TOMBSTONE JOIN, and this query had none. Every other
        # read of the ledger is record-wide — `live()`, the search, the review
        # queue, `build_findings` — so an erased conclusion was hidden
        # everywhere EXCEPT the one surface a person looks at. The fragment is
        # shared with `survey.project_detail` rather than spelled out again.
        import survey as survey_mod
        out["notes"] = fold_notes(conn.execute(
                "SELECT l.project_id, l.created_at, l.statement, l.state, l.confidence"
                " FROM ledger l" + survey_mod.LIVE_LEDGER_JOIN +
                " WHERE l.project_id IS NOT NULL AND" + survey_mod.LIVE_LEDGER_WHERE +
                " ORDER BY l.created_at DESC"))
        last = conn.execute(
            "SELECT started_at, finished_at, degraded_json FROM scans"
            " ORDER BY started_at DESC LIMIT 1").fetchone()
        deg = json.loads(last["degraded_json"] or "[]") if last else []
        out["health"] = {
            "last_scan": last["finished_at"] if last else "",
            "degraded_sources": len(deg),
            "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "weeks": conn.execute("SELECT COUNT(*) FROM project_week").fetchone()[0],
            "metrics": conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0],
            "proposed": conn.execute(
                "SELECT COUNT(*) FROM ledger l JOIN (SELECT memory_id, MAX(revision) r"
                " FROM ledger GROUP BY memory_id) m ON m.memory_id=l.memory_id"
                " AND m.r=l.revision WHERE l.state='proposed'").fetchone()[0],
            # Registry proposals are a DIFFERENT queue from ledger memories, and
            # it had no reader anywhere: `observatory_propose` is declared MCP
            # surface, it writes here, and neither this page nor any CLI looked.
            # A caller told its patch landed, in a table nobody reads, is the
            # dead-data shape this pass exists to close.
            "registry_proposals": conn.execute(
                "SELECT COUNT(*) FROM proposals WHERE status='proposed'").fetchone()[0],
        }
        # EMBEDDING POLICY (PB-137 N-003, OBS-33): whether any memory text may leave
        # this machine for a remote embedding model, read-only, the same answer
        # `full doctor` gives — a person sees it here without running a command.
        try:
            import embedding_policy
            out["health"]["embedding_policy"] = embedding_policy.status(
                paths.CONFIG, paths.STATE, embedding_policy.configured_model())
        except Exception as exc:                                                     # noqa: BLE001
            out["health"]["embedding_policy"] = {"state": "unknown",
                                                 "reason": f"{type(exc).__name__}"}
        # AGENT WORKFLOWS, so the operator sees what agents are carrying between
        # sessions: how many are open, and how many handoffs wait for a session
        # to accept them (an offer nobody takes lapses, and the work stalls
        # quietly). A store from before migration 0008 has neither table.
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {"workflows", "workflow_leases"} <= tables:
            out["health"]["workflows_open"] = conn.execute(
                "SELECT COUNT(*) FROM workflows WHERE status = 'open'").fetchone()[0]
            out["health"]["handoffs_waiting"] = conn.execute(
                "SELECT COUNT(*) FROM workflow_leases WHERE state = 'offered'"
                " AND expires_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now')").fetchone()[0]
        # The projection's lag. Both search indexes are fed by the outbox, so a
        # pending queue means the answer any search gives is missing these
        # revisions — and this page is where "the observer is still watching" is
        # supposed to be legible.
        lag = conn.execute(
            "SELECT count(*) AS n, min(l.created_at) AS oldest FROM outbox o"
            " JOIN ledger l ON l.memory_id = o.memory_id AND l.revision = o.revision"
            " WHERE o.consumed_at IS NULL").fetchone()
        if lag and lag["n"]:
            out["health"]["projection_lag"] = lag["n"]
            out["health"]["projection_oldest"] = lag["oldest"]
        # THE ALWAYS-ON SERVER, from its own heartbeat. Three states, spelled
        # apart: beating (age and what it watches), silent (installed and not
        # reporting — `server.silent` on the board says the same louder), and
        # absent (never ran here / turned off) — the page says which, because
        # "no row" would read as "no server exists" on a machine where one is
        # supposed to be up.
        sd = paths.SCRATCH / "serverd.json"
        if sd.is_file():
            try:
                beat = json.loads(sd.read_text(encoding="utf-8"))
                age_s = heartbeat_age_s(beat.get("at", ""))
                out["health"]["server_age_s"] = age_s
                # The receipt is written on change, so "silent" is the age it names
                # itself (serverd SILENT_AFTER_SECONDS); 90 s for an older server.
                out["health"]["server_silent_after_s"] = beat.get("silent_after_s") or 90
                out["health"]["server_port"] = beat.get("port")
                out["health"]["server_uptime_s"] = beat.get("uptime_s")
                r = beat.get("remote") or {}
                out["health"]["server_at_risk"] = r.get("at_risk_total")
                l = beat.get("leaks") or {}
                out["health"]["leaks_open"] = l.get("open")
            except (ValueError, OSError):
                out["health"]["server_age_s"] = None
        else:
            out["health"]["server_age_s"] = -1                           
    except sqlite3.Error as exc:
        out["degraded"] = {"text": "the store is unreadable: {error}", "args": {"error": str(exc)[:60]}}
    finally:
        conn.close()

    # The wallet and the provider's health are FILES, not tables, and they are
    # the half `docs/ARCHITECTURE.md:114` promised would be on this page: "the
    # agent degrades to collectors-only and SAYS SO IN THE DASHBOARD".
    for key, name in (("wallet", "wallet.json"), ("provider", "provider-health.json")):
        f = paths.STATE / name
        if f.is_file():
            try:
                out["health"][key] = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

    # PREPARED HERE, because the page must not read a field the file does not
    # have. `store/wallet.json` keeps per-month and per-day maps with no scalar
    # "this month" field, so a page testing for one would find it false on
    # every render and the spend row would never appear. A branch whose true
    # side is unreachable is found only by EXECUTING the page, not by checking
    # the data behind it — so the scalars are computed once, here.
    #
    # And it is THIS PROJECT's own spend, from its own journal, labelled as
    # such. A provider-side counter can measure a key shared with other
    # consumers, and showing that number here would report somebody else's
    # spending as this installation's. The date arithmetic stays in Python: a
    # browser deriving "which month is it" is a second place for the answer to
    # be wrong.
    w = out["health"].get("wallet") or {}
    if w:
        today = datetime.now(timezone.utc)
        out["health"]["spend_today"] = round(
            (w.get("days") or {}).get(today.strftime("%Y-%m-%d"), 0.0), 6)
        out["health"]["spend_month"] = round(
            (w.get("months") or {}).get(today.strftime("%Y-%m"), 0.0), 6)
        out["health"]["spend_denomination"] = w.get("denomination") or "credits"
    return out


#: How many findings the page carries BEFORE every remaining type is given a
#: row. It was a bare `[:40]` in the middle of a dict literal — a policy nobody
#: could find and nobody was told about — and the sentence that
#: justified it said what falls off the end is "the tail of `info`, the right
#: end to drop". Measured 2026-09-09, on 64 open findings: the tail held **ten
#: whole classes with no row on the page at all** — `store.faults_recurring`,
#: `project.unobservable`, `work.unwitnessed` and seven more. Dropping more of a
#: kind the reader can already see is a cap. Dropping the only instance of a
#: kind is a silence, and it is the one this repository refuses everywhere else.
def scanned(doc):
    """A registry document that a scan wrote, or None. `init` writes empty
    documents with `scanned_on: null` so every reader finds a file; the pages
    must read those as "not scanned" — never as "measured, and nothing there"."""
    return doc if isinstance(doc, dict) and doc.get("scanned_on") else None


FINDINGS_ON_PAGE = 8                                                                                     


def _findings_panel(FINDINGS: dict, links: dict | None = None) -> dict:
    """The findings the page carries, grouped so none is omitted.

    The header prints the counts, so a list shorter than the count beside it
    would tell the reader two different things without saying which was the
    whole.

    The arithmetic happens HERE because both filters are known here: `counts`
    in `registry/findings.json` already excludes acknowledged findings, and so
    does the list below, so the two agree exactly. Recomputing a total in the
    page's script would duplicate what the payload already holds.

    **Every open row is carried.** Rows are kept in the builder's own order —
    severity, then deadline, then id — and each type's rows beyond
    `FINDINGS_ON_PAGE` are marked `folded`, so the page shows them under a
    per-type control that names their count instead of dropping them.

    The findings surface is the ONLY one a row of severity `info` reaches: the
    notifier sends `critical` and `warning` by default, and the review digest
    is about the ledger's queue rather than the board. So an info row cut from
    this page would reach nobody at all.
    """
    # NO OMISSION. A cap that dropped rows of a kind already shown, however
    # honestly it counted them, still left rows the operator could not reach
    # and so could not act on. Every open row is CARRIED; what a type has
    # beyond `FINDINGS_ON_PAGE` rows is FOLDED under a per-type control that
    # names its count, so the page grows by disclosure and never by omission.
    open_ = [f for f in FINDINGS["findings"] if not f.get("acked")]
    per_type: dict[str, int] = {}
    items = []
    for f in open_:
        n = per_type.get(f["type"], 0)
        per_type[f["type"]] = n + 1
        # THE SUBJECT'S ROW, resolved here against the rows the pages render
        # (`subject_links`): a subject with no row stays text, never a link
        # that lands on nothing.
        link = subject_links.resolve(f.get("subject"), links) if links else None
        extra = ({"href": link["href"], "href_label": link["label"], "href_page": bool(link.get("page"))}
                 if link else {})
        items.append({**f, **extra, "folded": n >= FINDINGS_ON_PAGE})
    folded_by_type = {k: v - FINDINGS_ON_PAGE for k, v in per_type.items() if v > FINDINGS_ON_PAGE}
    # WHAT THE OPERATOR SILENCED, beside what speaks (S5). Withheld from the
    # list and the counts, shown here with the reason — a silence nobody can
    # see later is the silence this file refuses.
    # The title's message id and arguments travel with it, so a silenced row
    # reads in the reader's language like an open one.
    silenced = [{"id": f["id"], "type": f["type"], "severity": f["severity"],
                 "title": f["title"], "acked": f["acked"],
                 **{k: f[k] for k in ("title_id", "title_args") if k in f}}
                for f in FINDINGS["findings"] if f.get("acked")]
    return {"counts": FINDINGS["counts"],
            "built_at": FINDINGS.get("built_at"),
            "items": items,
            # The reader's name for each type present; an unnamed type keeps
            # its id on the page (`finding_types`).
            "type_labels": finding_types.labels_for([f["type"] for f in FINDINGS["findings"]]),
            "folded_by_type": folded_by_type,
            "silenced": silenced,
            "omitted": 0}


def hosting_context(relations: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    """({deployment id: environment name}, {resource id: account label}).

    From the `serves` and `in_account` edges and the documents they point at
    (docs/design/DEPLOYMENTS.md). A deployment with no `serves` edge has no
    environment here, and the page shows it as not specified, never as a guess
    (PB-130). Either document may be absent in an older registry.
    """
    envs = {e["id"]: e["name"] for e in (load("environments.json")["environments"]
                                         if (INV / "environments.json").is_file() else [])}
    labels = {a["id"]: a["label"] for a in (load("accounts.json")["accounts"]
                                           if (INV / "accounts.json").is_file() else [])}
    env_of = {r["from"]: envs[r["to"]] for r in relations if r["type"] == "serves" and r["to"] in envs}
    account_of = {r["from"]: labels[r["to"]] for r in relations
                  if r["type"] == "in_account" and r["to"] in labels}
    return env_of, account_of


def history_gap(project: dict, repos: list[dict], horizon_days: int | None,
                today: str | None = None) -> dict | None:
    """Why a project with measured commits has no recorded history, or None.

    The panel's "What happened" list is the event store, and an empty list read
    "no commits in the window" — a statement about the SUBJECT. For a project
    the registry measured commits in, inside the window, that sentence is false:
    the history was not RECORDED. Two causes are known here: the project's
    ownership is one the event scan refuses by rule (`estate.records_events`),
    or the scan has not read the checkout (never run since it appeared, or git
    could not answer). The page names which, with the newest measured commit,
    instead of asserting an absence nobody measured.
    """
    import estate
    lo = project.get("local_only") or {}
    commits = int(lo.get("commits") or 0)
    newest = lo.get("last_commit") or ""
    for r in repos:
        local = r.get("local") or {}
        commits += int(local.get("commits") or 0)
        newest = max(newest, local.get("last_commit_on") or "")
    if not commits or not newest:
        return None
    if horizon_days:
        now = datetime.strptime(today, "%Y-%m-%d") if today else datetime.now(timezone.utc).replace(tzinfo=None)
        if newest[:10] < (now - timedelta(days=horizon_days)).strftime("%Y-%m-%d"):
            return None
    ownership = project.get("ownership")
    if not estate.records_events(ownership):
        # One literal per message: the catalog check reads `"text": "…"` whole.
        return {"text": "history not recorded: this project is classed {ownership}, and only your own projects' commits are recorded (newest measured commit {date}). If it is yours, list its owner in {file}.",
                "args": {"ownership": ownership, "date": newest[:10], "file": "config/ownership.json"}}
    return {"text": "history not recorded: the checkout's newest commit is {date}, but no commit reached the event store. The events scan has not read it yet, or git could not answer — run {command}.",
            "args": {"date": newest[:10], "command": "project-observatory full local"}}


def runtime_paths() -> dict:
    """The paths the page's copy-a-command buttons are built from.

    A DEMO BUILD CARRIES NONE. `tools/demo_estate.py` renders the real pages
    over a fictional estate for screenshots, and these paths are the builder's
    machine: its home in the MCP empty state and its workspace in every
    `OBSERVATORY_HOME=…` command, visible text in a picture meant for strangers.
    With `OBSERVATORY_DEMO=1` the block is empty and the page falls back to the
    neutral spellings it already has: `project-observatory full …` with no
    workspace, `python3`, paths relative to the engine, and `"$HOME"`.
    """
    configured = bool((__import__("configuration").load().get("sources") or {}).get("projects"))
    if os.environ.get("OBSERVATORY_DEMO") == "1":
        return {"projects_configured": configured}
    return {"projects": str(paths.DATA),
            "secrets": str(paths.source_path("secret_store", paths.SECRETS)),
            "engine": str(paths.ROOT), "home": str(paths.HOME), "python": sys.executable,
            "scratch": str(paths.SCRATCH), "user_home": str(Path.home()),
            "projects_configured": configured}


def build():
    pdoc, rdoc = load("projects.json"), load("repositories.json")
    projects, repos = pdoc["projects"], {r["id"]: r for r in rdoc["repositories"]}
    relations = load("relations.json")["relations"]
    dups = load("duplicate-repo-names.json")["groups"]

    _fd = INV / "findings.json"
    FINDINGS = json.loads(_fd.read_text(encoding="utf-8")) if _fd.is_file() else None
    # ABSENT IS NOT EMPTY. A fresh clone has no scan and `heroku-apps.json` will
    # not exist; the tab then says the estate was never asked, rather than
    # showing nothing and reading as "you host nothing".
    _hd = paths.REGISTRY / "heroku-apps.json"
    HEROKU = scanned(json.loads(_hd.read_text(encoding="utf-8"))) if _hd.is_file() else None
    # NEVER A VALUE, and the document is built so it cannot hold one: a `label`
    # is what the provider calls a key. `tools/check_secrets.py` reads it on
    # every gate run, so the page inherits that guarantee rather than restating it.
    _cd = paths.REGISTRY / "credentials.json"
    CREDS = scanned(json.loads(_cd.read_text(encoding="utf-8"))) if _cd.is_file() else None
    # WHICH KEYS A PROJECT USES, for its panel. The credential document is
    # heavy and rides only on the keys page; this is the small inverse of its
    # `used_by` edges — a name, its kind, where it sits and the anchor of its
    # row — so the project panel can answer "what does this authenticate with"
    # and hand the reader to the keys page by row. Names and places only, as
    # everywhere: the document never held a value to leak.
    KEYS: dict[str, list] = {}
    for _c in (CREDS or {}).get("credentials") or []:
        _slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(_c.get("id", "")).replace("credential:", "", 1))
        for _pid in _c.get("used_by") or []:
            KEYS.setdefault(_pid, []).append({
                "name": _c.get("name"), "kind": _c.get("kind"), "slug": _slug,
                "env": _c.get("env"), "leaked": bool(_c.get("leaked")),
                "signed": bool((_c.get("signature") or {}).get("purpose")),
                "where": (f"vault:{_c['vault_project']}" if _c.get("vault_project")
                          else f"{_c['in_project']}/{_c.get('path') or ''}" if _c.get("in_project")
                          else _c.get("source") or ""),
            })
    for _pid in KEYS:
        KEYS[_pid].sort(key=lambda k: (k["kind"] or "", (k["name"] or "").lower()))
    # THE MOVEMENTS JOURNAL, for the keys page — read by the same module the
    # board's `secret.moved_unrecorded` finding reads it with, so the page and
    # the finding cannot disagree. The journal sits beside the leak register in
    # the vault's store; names, dates and places, never a value.
    if CREDS is not None:
        sys.path.insert(0, str(ROOT / "tools"))
        import movements as _movements
        _leaks = Path(os.environ.get("OBSERVATORY_VAULT_DIR",
                                     paths.source_path("secret_store", paths.SECRETS) / "projects")) / "leaks.jsonl"
        _hk_trail: dict[str, list] = {}
        _hk_project: dict[str, str] = {}
        _hkf = paths.SCRATCH / "heroku.json"
        if _hkf.is_file():
            try:
                for _app in json.loads(_hkf.read_text(encoding="utf-8")).get("apps") or []:
                    if _app.get("config_releases"):
                        _hk_trail[_app["name"]] = _app["config_releases"]
            except (OSError, ValueError):
                _hk_trail = {}
        for _a in (HEROKU or {}).get("apps") or []:
            if _a.get("project"):
                _hk_project[_a["name"]] = str(_a["project"]).split(":", 1)[-1]
        CREDS["movements"] = _movements.journal_tail(_leaks, 30)
        CREDS["unrecorded"] = [{**u, "project": _hk_project.get(u["app"])}
                               for u in _movements.unrecorded(_hk_trail, _movements.read_moves(_leaks))]

    # WHAT IS ON THIS DISK, as names. The credential document above answers
    # "which accounts exist"; this answers "what is sitting in the working copy",
    # and the whole document is sent rather than a trimmed one: the trim saved
    # 156KB of a 1.1MB page and cost a paragraph explaining what had been left
    # out, which is a bad trade for a file opened locally.
    _ed = paths.REGISTRY / "env-inventory.json"
    ENVD = scanned(json.loads(_ed.read_text(encoding="utf-8"))) if _ed.is_file() else None
    # THE KEYS A PROJECT HOLDS IN ITS OWN `.env` FILES join the panel's key
    # list too. The credential document knows the vault, the machine store and
    # the shared secrets directory — not the variables sitting in a checkout,
    # which is where most of a project's keys actually are, so without this a
    # panel could claim no credential is linked when that is false.
    # Names, file, class and git state — the value never leaves the file. Each
    # row links to its own env row (`#e-<file>:<NAME>`).
    # `project` in the inventory is the FOLDER name, joined to the project id
    # through the same folder index the credential collector uses — a match by
    # project name alone would miss a project whose folder is named differently.
    _folder_pid = {f: p["id"] for p in pdoc["projects"] for f in (p.get("local_folders") or [])}
    _name_pid = {p["name"]: p["id"] for p in pdoc["projects"]}
    for _f in (ENVD or {}).get("files") or []:
        _pid = _folder_pid.get(_f.get("project")) or _name_pid.get(_f.get("project"))
        if not _pid:
            continue
        for _v in _f.get("variables") or []:
            if _v.get("class") not in ("secret",):
                continue
            KEYS.setdefault(_pid, []).append({
                "name": _v.get("name"), "kind": "env-file", "slug": None,
                "env": None, "leaked": False, "signed": True,                                          
                "where": _f.get("path"), "git": _f.get("git"),
                "href": "env.html#e-" + re.sub(r"[^A-Za-z0-9_.:/-]+", "-", f"{_f.get('path')}:{_v.get('name')}"),
            })
    for _pid in KEYS:
        KEYS[_pid].sort(key=lambda k: (k["kind"] or "", (k["name"] or "").lower()))
    # THE ENV PAGE'S PROJECT LINKS, resolved here. The inventory names a
    # project by its FOLDER, and the page built `#project:<folder>` from it —
    # a link to a panel that does not exist for every folder whose project id
    # differs from its name. Only folders the registry knows get an id; the
    # page prints the rest as text and says why.
    if ENVD is not None:
        _named = set()
        for _f in ENVD.get("files") or []:
            _named.add(_f.get("project"))
            for _v in _f.get("variables") or []:
                _named.update(_v.get("shared_with") or [])
                _named.update(_v.get("available_in") or [])
        ENVD["project_ids"] = {n: _folder_pid.get(n) or _name_pid.get(n)
                               for n in sorted(x for x in _named if x)
                               if _folder_pid.get(n) or _name_pid.get(n)}

    # WHAT PRODUCTION HOLDS, as the four verdicts and never a value. Two pages
    # read it: the Heroku row says how its configuration compares in one line,
    # and the ENV row says it per variable.
    # WHAT GOOGLE SEES. The whole inventory goes to its own page; the
    # per-project summary rides everywhere, because "how many people came" is a
    # fact about a project and belongs beside the project.
    _gf = paths.REGISTRY / "google-properties.json"
    GOOGLE = scanned(json.loads(_gf.read_text(encoding="utf-8"))) if _gf.is_file() else None
    from collectors.google_registry import normalize_document, project_traffic
    if GOOGLE is not None:
        GOOGLE = normalize_document(GOOGLE)
    TRAFFIC = project_traffic((GOOGLE or {}).get("properties", []))
    # SEARCH CONSOLE, JOINED THE SAME WAY: a site is a host, and a host already
    # resolves to a project through the registry — so the console link lands on
    # the project rather than on a list nobody opens.
    import sys as _sys
    _sys.path.insert(0, str(paths.ROOT / "plugins"))
    import hostmap as _hostmap
    HOSTMAP = _hostmap.build()
    _sc = {}
    for _s in (GOOGLE or {}).get("search_console", []):
        _host = (_s.get("site") or "").replace("sc-domain:", "").replace("https://", "").replace("http://", "").strip("/")
        _owner = HOSTMAP.get(_host) or HOSTMAP.get(_host.replace("www.", ""))
        if _owner:
            _sc.setdefault(_owner, []).append({"site": _s.get("site"), "url": _s.get("console_url")})
    for _pid, _sites in _sc.items():
        TRAFFIC.setdefault(_pid, {"users_30d": None, "properties": []})["search_console"] = _sites

    _rf = paths.REGISTRY / "remote-env.json"
    REMOTE = json.loads(_rf.read_text(encoding="utf-8")) if _rf.is_file() else None

    # THE WHOLE DOMAIN LIST, not just the ones a project claims. 53 registered,
    # 9 on a project row: the rest were a tile with a number and no way to see
    # WHICH. The same omission Heroku had until it got a tab (audit 2026-09-09).
    DOMAINS = load("domains.json")["domains"]
    # WHAT THE ESTATE HOLDS AND HOW IT GROUPS. Both are projections;
    # a fresh clone without a Cloudflare scan simply has no zones, and the page
    # says so rather than showing an empty table.
    _zf = paths.REGISTRY / "cloudflare-zones.json"
    ZONES = (scanned(load("cloudflare-zones.json")) or {}).get("zones") if _zf.is_file() else None
    _mf = paths.REGISTRY / "mcp-servers.json"
    MCP = scanned(load("mcp-servers.json")) if _mf.is_file() else None
    _pf = paths.REGISTRY / "products.json"
    PRODUCTS = load("products.json")["products"] if _pf.is_file() else []
    PRODUCT_OF = {}
    for pr in PRODUCTS:
        for mbr in pr["members"]:
            PRODUCT_OF.setdefault(mbr["project"], []).append(
                {"id": pr["id"], "name": pr["name"], "kind": pr["kind"], "role": mbr["role"]})
    LIVE = {}
    _lv = INV / "domain-liveness.json"
    if _lv.is_file():
        for h in json.loads(_lv.read_text(encoding="utf-8"))["hosts"]:
            LIVE[h["host"]] = {"resolves": h["resolves"], "http": h["http_status"],
                               "on": h["checked_on"]}
    members, sites, domain_owner = {}, {}, {}
    for rel in relations:
        if rel["type"] == "implemented_by":
            members.setdefault(rel["from"], []).append(rel["to"])
        elif rel["type"] == "public_domain_of":
            domain_owner.setdefault(rel["from"].split(":", 1)[1], []).append(rel["to"])
            sites.setdefault(rel["to"], []).append(rel["from"].split(":", 1)[1])

    rows = []
    for project in projects:
        ids = sorted(members.get(project["id"], []))
        rows.append({
            "id": project["id"],
            "name": project["name"],
            "owner": (project["owners"] or ["—"])[0] if len(project["owners"]) == 1
                     else ("multi" if project["owners"] else "—"),
            "owners": project["owners"],
            "ownership": project["ownership"],
            "anchor": project["anchor"],
            "lifecycle": project["lifecycle"],
            "description": project.get("description", ""),
            "stack": project.get("stack", []),
            "folders": project.get("local_folders", []),
            # A local-only project that IS a git repository: the row counted
            # "0 repos · 1 folder" for a checkout the findings call "Repository
            # with no remote", so it never said it was a repository at all.
            "local_git": ("no-remote" if (project.get("local_only") or {}).get("unpublished")
                          else "elsewhere" if int((project.get("local_only") or {}).get("commits") or 0) > 0
                          else None),
            "last": project.get("last_activity_on", ""),
            "tier": project.get("activity_tier", ""),
            "note": project.get("canonical_page", "") if project.get("has_vault_note") else "",
            "rules": project.get("membership_rules", []),
            # `vault_notes`, and it is called `wiki_notes` here for one reason:
            # the store's own conclusions were also written to `r["notes"]`
            # twenty lines below, so the second assignment replaced this COUNT
            # with a LIST and the wiki chip rendered
            # "[object Object],[object Object] notes" on every row that had a
            # note. One word for two populations, and the page said so out loud
            # (2026-09-09). Seen by opening the page, which is why it lived.
            "wiki_notes": project.get("vault_notes", 0),
            # Measured liveness, folded in per host. A homepage that does not
            # resolve is the one fact about a site an operator must not have to
            # discover by clicking it.
            "sites": [{**x, "live": LIVE.get(x["host"])} for x in project.get("sites", [])],
            "products": PRODUCT_OF.get(project["id"], []),
            "repos": [{
                "nwo": repos[i]["name_with_owner"],
                "url": repos[i]["url"],
                "host": repos[i]["host"],
                "visibility": repos[i]["visibility"],
                "archived": repos[i]["archived"],
                "fork": repos[i]["fork"],
                "branch": repos[i].get("default_branch", ""),
                "path": (repos[i].get("local") or {}).get("path", ""),
                "checked_out": (repos[i].get("local") or {}).get("checked_out_branch", ""),
                "dirty": (repos[i].get("local") or {}).get("uncommitted_files", 0),
                "sync": (repos[i].get("local") or {}).get("sync", ""),
                # HOW MUCH IS AT STAKE. The count reached the registry and the
                # findings and stopped short of the page — the same enumerated
                # key list that lost it in `emit_registry.py`, one layer further
                # on. `None` when the probe could not count, never 0.
                "unpushed": (repos[i].get("local") or {}).get("unpushed"),
                "unpushedOn": (repos[i].get("local") or {}).get("unpushed_newest_on"),
                "nothing_exclusive": (repos[i].get("local") or {}).get("nothing_exclusive"),
                "pushed": repos[i].get("last_pushed_on", ""),
                "status": repos[i].get("status"),
                "supersededBy": repos[i].get("superseded_by"),
                "movedTo": repos[i].get("moved_to"),
                "statusEvidence": repos[i].get("status_evidence"),
                "language": repos[i].get("language", ""),
            } for i in ids if i in repos],
        })
    rows.sort(key=lambda r: (r["ownership"] != "owned", r["owner"].lower(), -len(r["repos"]), r["name"].lower()))
    # THE DOMAIN ROWS the domains page draws, built once: its tile, its badge
    # and its table count the same list (registrar names joined with the
    # Cloudflare zones no registrar lists), so the overview cannot say one
    # number while the page it links to shows another.
    DOMAIN_ROWS = [{
        "name": d["name"],
        "registrar": d.get("registrar"),
        "expires_on": (d.get("namecheap") or {}).get("expires_on"),
        "auto_renew": (d.get("namecheap") or {}).get("auto_renew"),
        "status": (d.get("namecheap") or {}).get("status"),
        "live": LIVE.get(d["name"]),
        "projects": sorted(domain_owner.get(d["name"], [])),
    } for d in DOMAINS]
    import shell as _shell_counts
    _dom_total, _dom_unowned = _shell_counts.domain_totals(DOMAIN_ROWS, ZONES or [])

    stats = {
        # WHAT HAPPENED, first, because the estate's inventory answers a
        # different question and answered it alone until now.
        **work_stats(),
        # WHAT IS AT RISK, beside what happened. Registry-derived rather than
        # store-derived, which is why it is not inside `work_stats()`.
        **at_risk_total(repos),
        "projects": len(rows),
        "repositories": len(repos),
        "with_site": sum(1 for r in rows if r["sites"]),
        "with_note": sum(1 for r in rows if r["note"]),
        "with_folder": sum(1 for r in rows if r["folders"]),
        "no_note": sum(1 for r in rows if not r["note"]),
        "dirty": sum(1 for r in rows for x in r["repos"] if x["dirty"]),
        "unsynced": sum(1 for r in rows for x in r["repos"]
                        if x["sync"] and x["sync"] != "current"),
        # Counted although it is shown nowhere. An inactive repository anchors no
        # project, so it cannot appear in a row — and a registry that hides a
        # thing without saying how many it hid is lying by omission.
        "inactive_repos": sum(1 for r in repos.values()
                              if r.get("status") == "inactive"),
        "dead_site": sum(1 for r in rows
                         if any(s.get("live") and not s["live"]["resolves"] for s in r["sites"])),
        # PROJECTS whose lifecycle is archived — not archived repositories, of
        # which there are more. One word for two populations reads as a
        # contradiction the moment both are on screen.
        "archived": sum(1 for r in rows if r["lifecycle"] == "archived"),
        "archived_repos": sum(1 for r in repos.values() if r.get("archived")),
        "domains": _dom_total,
        # THE SAME OMISSION AS `inactive_repos`, one subject over, and it went
        # unnoticed until the page was asked what it hides. A domain no project
        # claims has no row to sit in, so of 53 registered domains the page
        # showed nine and printed "53" beside them — the operator's assets, most
        # of them costing a renewal a year, counted and not listed. Measured
        # 2026-09-09. The count is the honest half of what a page can do here;
        # which of them matter is `domain.dark`, `domain.expiring` and
        # `domain.hold` on the board.
        # Counted from the same rows as the domains page's own "no project"
        # filter, so the tile and the page cannot disagree.
        "domains_no_row": _dom_unowned,
        "owners": len({r["owner"] for r in rows}),
        # THE COUNT OF WHAT IS RUNNING, beside the count of what exists. An
        # application nothing claims is the same omission as a domain nothing
        # claims, one provider over, and it is the reason this tile is a pair.
        "heroku_apps": len(HEROKU["apps"]) if HEROKU else 0,
        "heroku_unlinked": (HEROKU.get("totals", {}).get("unlinked", 0) if HEROKU else 0),
        "heroku_cost": (HEROKU.get("totals", {}).get("monthly_cost", 0) if HEROKU else 0),
        "creds": len(CREDS["credentials"]) if CREDS else 0,
        "creds_leaked": (CREDS.get("totals", {}).get("leaked_unrotated", 0) if CREDS else 0),
        "creds_unclaimed": (CREDS.get("totals", {}).get("unclaimed", 0) if CREDS else 0),
        "heroku_down": (sum(1 for a in HEROKU["apps"]
                            if a["state"] in ("down", "suspended") or a["crashed"])
                        if HEROKU else 0),
    }
    # A project's own applications, folded onto its row. `state` and `cost`
    # travel with it because the two questions an operator asks of a hosted
    # project — is it up, what does it cost — must be answerable without
    # leaving the row.
    if HEROKU:
        env_of, account_of = hosting_context(relations)
        by_project = {}
        for a in HEROKU["apps"]:
            if a.get("project"):
                by_project.setdefault(a["project"], []).append(
                    {"name": a["name"], "state": a["state"], "cost": a["monthly_cost"],
                     "rule": a["link_rule"], "env": env_of.get(a["id"]),
                     "account": account_of.get(a["id"])})
        for r in rows:
            r["heroku"] = sorted(by_project.get(r["id"], []), key=lambda x: x["name"])
    else:
        for r in rows:
            r["heroku"] = []

    owners = [o for o, _ in Counter(r["owner"] for r in rows).most_common()]
    store = from_store()
    try:
        from collectors.scan_events import retention_horizon_days
        _horizon = retention_horizon_days()
    except Exception as exc:                                                    
        # The window only narrows the explanation below; without it every
        # measured commit counts as inside it, which errs towards explaining.
        print(f"  retention window unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        _horizon = None
    _by_id = {p["id"]: p for p in projects}
    for r in rows:
        r["weeks"] = store["weeks"].get(r["id"], [])
        r["metrics"] = store["metrics"].get(r["id"], [])
        r["timeline"] = store["timeline"].get(r["id"], [])
        r["notes"] = store["notes"].get(r["id"], [])
        if not r["timeline"]:
            _gap = history_gap(_by_id[r["id"]],
                               [repos[i] for i in members.get(r["id"], []) if i in repos], _horizon)
            if _gap:
                r["history_gap"] = _gap
    # THE QUEUE, READ-ONLY. 401 revisions sit `proposed` and the default outcome
    # for every one of them is expiry at 90 days; the only door was a terminal
    # command, so the operator could not even SEE what was waiting without
    # opening one. Deciding still needs the terminal — `tools/review.py` refuses
    # a write without one on purpose — but a queue nobody can look at is a queue
    # nobody works (audit 2026-09-09).
    PAYLOAD = {"runtime": runtime_paths(), "rows": rows, "stats": stats, "owners": owners, "dups": dups,
                          "queue": store.get("queue") or [],
                          "digest": store.get("digest"),
                          "health": store["health"], "store_degraded": store["degraded"],
                          # Acknowledged findings are withheld from the page on
                          # purpose: the operator silenced them, and a panel that
                          # shows them anyway teaches that silencing does nothing.
                          "findings": (_findings_panel(FINDINGS, subject_links.build_index(
                                           projects=projects, relations=relations,
                                           domains=DOMAIN_ROWS, zones=ZONES or [],
                                           apps=(HEROKU or {}).get("apps") or [],
                                           credentials=(CREDS or {}).get("credentials") or [],
                                           google_credentials=(GOOGLE or {}).get("credentials") or [],
                                           env_files=(ENVD or {}).get("files") or [],
                                           mcp_servers=(MCP or {}).get("servers") or []))
                                       if FINDINGS else None),
                          # TWO DIFFERENT FACTS, and the page showed the weaker
                          # one as its headline. `updated_on` is when the
                          # registry's CONTENT last changed; `last_scan` is when
                          # the estate was last MEASURED. A reader asking "is
                          # this current?" wants the second, and until
                          # 2026-09-07 the first was a hardcoded literal
                          # (`OBS="2026-09-03"`) so the headline was a constant
                          #. Both are shown now, each labelled.
                          "heroku": HEROKU,
                          "creds": CREDS,
                          "env": ENVD,
                          "mcp": MCP,
                          "remote": REMOTE,
                          "google": GOOGLE,
                          "traffic": TRAFFIC,
                          "keys": KEYS,
                          "zones": ZONES,
                          "products": PRODUCTS,
                          "domains": DOMAIN_ROWS,
                          "updated": pdoc.get("updated_on", ""),
                          "measured": store["health"].get("last_scan", "")}
    # THE MACHINE, read from what the tick's machine/git-hygiene/cleanup steps
    # wrote; only the Machine page carries it (shell.slice_for).
    try:
        import machine_view
        PAYLOAD["machine"] = machine_view.summary()
    except Exception as exc:  # noqa: BLE001 — a page, not a crash
        PAYLOAD["machine"] = {"degraded": [{"source": "machine", "reason": f"{type(exc).__name__}: {exc}"[:200]}]}
    # THE AGENTS: workflows, handoffs and sessions, read-only from the store;
    # only the Agents page carries it (shell.slice_for), and the overview card
    # reads its counters.
    try:
        import agents_view
        PAYLOAD["agents"] = agents_view.summary()
    except Exception as exc:  # noqa: BLE001 — a page, not a crash
        PAYLOAD["agents"] = {"degraded": [{"source": "agents", "reason": f"{type(exc).__name__}: {exc}"[:200]}]}
    build.last_payload = PAYLOAD                                                  
    payload = json.dumps(PAYLOAD, ensure_ascii=False)
    locale = build_locale()
    t = i18n.Translator(locale)
    title = "Projects — the operator's registry"
    return (template_for(locale).replace("__PAGE__", "").replace("__NAV__", "").replace("__CARDS__", "").replace("__MACHINE__", "").replace("__AGENTS__", "")
            .replace("__TITLE__", html.escape(t(title)))
            .replace("__PAGE_TITLE__", "")
            .replace("__H1__", t.mark(title, tag="h1"))
            .replace("__SUB__", t.mark("A registry built from the connected sources: project folders, "
                                       "repositories and notes. Every project↔repository link carries "
                                       "the rule that made it."))
            .replace("__DATA__", payload.replace("</", "<\\/")))


def build_locale() -> str:
    """The language the pages are built in: `OBSERVATORY_LOCALE` when set (the
    checks and the screenshot use it), else the workspace's `interface.locale`,
    else English. An unsupported value stops the build rather than guessing."""
    value = os.environ.get("OBSERVATORY_LOCALE")
    if value:
        return i18n.check_locale(value)
    import configuration
    return i18n.check_locale(configuration.interface_locale())


def template_for(locale: str) -> str:
    """The template in one language, with the catalogs the script needs."""
    catalogs = json.dumps(i18n.catalogs(), ensure_ascii=False, sort_keys=True)
    return (i18n.localize_markup(TEMPLATE, locale)
            .replace("__LOCALE__", locale)
            .replace("__I18N__", catalogs.replace("</", "<\\/")))


def build_pages(payload: dict) -> dict[str, int]:
    """docs/dashboard/<page>.html for every page in `shell.PAGES`, from the
    same template and the same data, each carrying only what it renders
. Returns bytes written per page."""
    import shell
    locale = build_locale()
    paths.DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    page_tpl, css, js = shell.split_template(template_for(locale))
    atomic.write_text(paths.DASHBOARD_DIR / shell.ASSET_CSS, css)
    atomic.write_text(paths.DASHBOARD_DIR / shell.ASSET_JS, js)
    sizes = {}
    for name, _title, _kind in shell.PAGES:
        page = shell.page_html(page_tpl, name, payload, locale)
        atomic.write_text(paths.DASHBOARD_DIR / f"{name}.html", page)
        sizes[name] = len(page.encode("utf-8"))
    return sizes


TEMPLATE = r"""<!doctype html>
<html lang="__LOCALE__" data-theme="dark" data-build-locale="__LOCALE__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>__TITLE__</title>
<link rel="icon" type="image/svg+xml" href="__ICON__">
<script>
/* THE READER'S LANGUAGE, BEFORE FIRST PAINT. The page is built in the
   workspace's language (`interface.locale`); a reader who picked the other one
   with the EN/RU switch keeps it in localStorage, and `lang` is set here so
   that the first paint, hyphenation and screen readers already agree with it.
   Guarded: the smoke harness has no localStorage. */
(function () {
  var root = document.documentElement, locale = root.getAttribute("data-build-locale");
  try {
    var chosen = localStorage.getItem("observatory.locale");
    if (chosen === "en" || chosen === "ru") locale = chosen;
  } catch (e) {}
  root.lang = locale;
})();
</script>
<style>
__PC_TOKENS__
/* DESIGN TOKENS. The PassionCode design system above owns every value; the
 * dashboard's own role names are aliases of its semantic `--pc-*` roles, so a
 * rule below never names a colour. One fixed dark theme, as in every
 * PassionCode product surface: gold is action, selection and focus; green,
 * peach, pink-red and blue are positive, warning, negative and information,
 * and each is always paired with words. */
:root {
  --bg: var(--pc-bg);
  --panel: var(--pc-panel);
  --panel-2: var(--pc-panel-raised);
  --ink: var(--pc-text);
  --muted: var(--pc-text-muted);
  --border: var(--pc-border);
  --border-strong: var(--pc-border-strong);
  --accent: var(--pc-accent);
  --accent-hover: var(--pc-accent-hover);
  --accent-weak: var(--pc-accent-soft);
  --accent-ink: var(--pc-on-accent);
  --ok: var(--pc-positive);
  --ok-weak: var(--pc-positive-soft);
  --warn: var(--pc-warning);
  --warn-weak: var(--pc-warning-soft);
  --danger: var(--pc-negative);
  --danger-weak: var(--pc-negative-soft);
  --info: var(--pc-info);
  --info-weak: var(--pc-info-soft);

  --r-control: var(--pc-radius-control);
  --r-card: var(--pc-radius-panel);
  --r-pill: var(--pc-radius-pill);

  --motion-ease: var(--pc-ease);
  --dur-hover: var(--pc-duration-hover);

  --font-ui: var(--pc-font);
  --font-data: var(--pc-font-data);

/* TYPE SCALE: the system's desktop sizes — metadata 12px, body 14px. */
  --t-chip: var(--pc-type-meta);
  --t-label: var(--pc-type-meta);
  --t-body: var(--pc-type-body);
  --t-section: var(--pc-type-section);
  --t-page: var(--pc-type-page);

/* SPACING, on the system's 4px grid. */
  --space-1: var(--pc-space-1);
  --space-2: var(--pc-space-2);
  --space-3: var(--pc-space-3);
  --space-4: var(--pc-space-4);
  --space-5: var(--pc-space-5);
  --space-6: var(--pc-space-6);

  background-color: var(--bg);
  color: var(--ink);
  color-scheme: dark;
}

/* Reduced motion: hover transitions collapse to instant. */

@media (prefers-reduced-motion: reduce) {
  :root {
    --dur-hover: 0s;
  }
}

/* BASE */
*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 400 var(--t-body)/1.5 var(--font-ui);
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--r-control); }
.mono { font-family: var(--font-data); font-variant-numeric: tabular-nums; }

/* HEADER */
header {
  padding: var(--space-5) var(--space-5) var(--space-4);
  background: var(--panel); border-bottom: 1px solid var(--border);
}
h1 { margin: 0 0 var(--space-1); font: 700 var(--t-page)/1.2 var(--font-ui); letter-spacing: -.01em; }
.sub { color: var(--muted); max-width: 74ch; }

/* TILES and the review queue rows */
.tiles { display: flex; flex-wrap: wrap; gap: var(--space-2); margin-top: var(--space-4); }
.tile {
  border: 1px solid var(--border); border-radius: var(--r-card);
  padding: var(--space-2) var(--space-3); min-width: 104px; background: var(--panel);
}
.qrow { display: grid; grid-template-columns: 128px 1fr; gap: var(--space-3);
  padding: var(--space-2) 0; border-bottom: 1px solid var(--border);
  font-size: var(--t-body); }
.qrow:last-child { border-bottom: 0; }
.qrow .qacts { margin-top: var(--space-2); display: flex; gap: var(--space-2); }
/* Filter bar above a list: type selector, free-text query, shown count. */
.verbs { display: flex; flex-wrap: wrap; gap: 4px; }
.fbar { display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: center;
  margin: var(--space-2) 0 var(--space-3); }
.fbar .ftype, .fbar .fq { font: 400 var(--t-body) var(--font-ui); color: var(--ink);
  background: var(--panel); border: 1px solid var(--border); border-radius: var(--r-control);
  padding: 4px 8px; }
.fbar .fq { min-width: 220px; }
.fbar .fshown { font: 400 var(--t-chip) var(--font-data); color: var(--muted); margin-left: auto; }
.f.fhide { display: none !important; }
.f:target { background: var(--accent-weak); }
.fsubj { font-family: var(--font-data); font-size: var(--t-chip); margin-left: 6px; }
.fperma { color: var(--muted); text-decoration: none; margin-left: 6px; font-family: var(--font-data); }
.fperma:hover { color: var(--ink); }
.filters { font: 400 var(--t-chip) var(--font-data); color: var(--muted);
  margin: var(--space-3) var(--space-5) 0; }
.filters b { color: var(--ink); font-weight: 600; }
tr:target > td { background: var(--accent-weak); }
/* A folded group shows its first row and hides the rest behind a toggle. */
tbody.grp.folded tr + tr { display: none; }
.grp-fold { font: inherit; color: inherit; background: none; border: 0; padding: 0;
  cursor: pointer; text-align: left; width: 100%; }
.grp-fold::before { content: "▸ "; color: var(--muted); }
.grp-fold[aria-expanded="true"]::before { content: "▾ "; }
/* Sortable column headers: the header is a button, the arrow follows
 * aria-sort. */
th[data-sort] .sort { font: inherit; color: inherit; background: none; border: 0; padding: 0;
  cursor: pointer; text-align: inherit; }
th[data-sort] .sort:hover, th[data-sort] .sort:focus-visible { text-decoration: underline; }
th[aria-sort="ascending"] .sort::after { content: " ▴"; color: var(--muted); }
th[aria-sort="descending"] .sort::after { content: " ▾"; color: var(--muted); }
/* Long identifiers in cells wrap anywhere rather than widening the table. */
td .mono { overflow-wrap: anywhere; }
.qrow .qm { color: var(--muted); font-family: var(--font-data); font-size: var(--t-chip); }
.tile.more-tiles { cursor: pointer; border-style: dashed; font: inherit; text-align: left;
  color: var(--muted); }
.tile.more-tiles:hover { border-color: var(--accent); color: var(--ink); }
.tile b { display: block; font: 600 var(--t-section)/1.2 var(--font-data);
  font-variant-numeric: tabular-nums; }
.tile span { color: var(--muted); font-size: var(--t-label);
  text-transform: uppercase; letter-spacing: .1em; }

/* FINDINGS */
#findings { margin: var(--space-4) var(--space-5) 0; }
.fh { display: flex; align-items: baseline; gap: var(--space-3);
  font: 600 var(--t-label) var(--font-ui); text-transform: uppercase;
  letter-spacing: .1em; color: var(--muted); margin-bottom: var(--space-2); }
.fh .when { font-family: var(--font-data); text-transform: none; letter-spacing: 0; }
.flist { border: 1px solid var(--border); border-radius: var(--r-card);
  background: var(--panel); }
.f { display: flex; gap: var(--space-3); align-items: baseline; flex-wrap: wrap;
  padding: var(--space-2) var(--space-3); border-bottom: 1px solid var(--border); }
.f:last-child { border-bottom: 0; }
.f .t { font-weight: 600; }
.f .d { color: var(--muted); flex: 1 1 24ch; }
.f .due { font-family: var(--font-data); font-size: var(--t-chip); color: var(--muted);
  white-space: nowrap; }
.f .act { font-size: var(--t-chip); color: var(--muted); font-style: italic; }
.tier { font-size: var(--t-chip); color: var(--muted); }
/* Lower-severity findings hide while the list is folded. */
.flist.folded .f.finfo, .flist.folded .f.fwarning { display: none; }
button.fold { margin: var(--space-2) 0 0 var(--space-3); }
/* Deltas: a rise is a warning colour, a fall is calm. */
.delta { font-family: var(--font-data); }
.delta.up { color: var(--warn); }
.delta.down { color: var(--ok); }
.not-scanned p { margin: 0 0 var(--space-2); }
.not-scanned .chip-btn { text-transform: none; letter-spacing: 0; white-space: normal; text-align: left; overflow-wrap: anywhere; }
.empty-estate { flex: 1 1 100%; }
.spark { display: flex; align-items: center; gap: 4px; color: var(--muted);
         font-size: var(--t-chip); font-family: var(--font-data); }
.spark svg { display: block; }
/* The sessions count beside a sparkline: muted like the rest, never fainter (AA). */
.spark-sess { white-space: nowrap; }
.panel { padding: 16px; margin-bottom: 12px; }

/* TABS */
.tabs { display: flex; gap: var(--space-2); align-items: flex-end;
  border-bottom: 1px solid var(--border); margin: var(--space-4) 0 0; }
.tab { appearance: none; border: 1px solid transparent; border-bottom: 0;
  background: none; color: var(--muted); cursor: pointer;
  font: 500 var(--t-body)/1 var(--font-ui);
  padding: var(--space-2) var(--space-3); border-radius: var(--r-control) var(--r-control) 0 0;
  transition: color var(--dur-hover) var(--motion-ease); }
.tab:hover { color: var(--ink); }
.tab[aria-selected="true"] { background: var(--panel); border-color: var(--border);
  color: var(--ink); font-weight: 600; margin-bottom: -1px; }
.tab .n { font-family: var(--font-data); font-variant-numeric: tabular-nums;
  color: var(--muted); margin-left: var(--space-1); }
.seg[hidden] { display: none; }
/* STATUS DOT: a coloured marker beside a state word. */
.st { display: inline-flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.st > * { white-space: normal; }
.st i { width: 8px; height: 8px; border-radius: var(--r-pill); flex: none; }
.st-running i { background: var(--ok); }
.st-down i, .st-suspended i { background: var(--danger); }
.st-resources-only i { background: var(--warn); }
.st-idle i { background: var(--border-strong); }
.unlinked { color: var(--warn); }
.dhead { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }
.dmeta { font-size: var(--t-chip); color: var(--muted); margin: 0 0 8px; }
.dlist { margin: 4px 0 12px; padding-left: 18px; }
.dlist li { font-size: var(--t-chip); line-height: 1.5; }
.plink { color: inherit; text-decoration: none; border-bottom: 1px solid var(--border); }
.plink:hover, .plink:focus { border-bottom-color: var(--accent); }
.health { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
          gap: 4px 16px; margin: 8px 0; }
.hrow { display: flex; justify-content: space-between; gap: 8px;
        font-size: var(--t-chip); color: var(--muted); }
.hrow b { font-family: var(--font-data); color: var(--ink); font-weight: 400; }
@media (max-width: 1100px) { #findings { margin: var(--space-4) var(--space-3) 0; } }

/* CONTROLS: the sticky search and filter bar. */
.controls {
  position: sticky; top: var(--topbar, 0px); z-index: 20;
  display: flex; flex-wrap: wrap; gap: var(--space-2);
  padding: var(--space-3) var(--space-5);
  background: var(--panel); border-bottom: 1px solid var(--border);
}
input[type=search], select {
  font: 400 var(--t-body) var(--font-ui); color: var(--ink);
  background: var(--panel); border: 1px solid var(--border-strong);
  border-radius: var(--r-control); padding: 6px var(--space-2);
}
input[type=search] { flex: 1; min-width: 240px; }
.seg { display: flex; flex-wrap: wrap; gap: var(--space-1); }
/* View switch: pill buttons, pressed state is the active one. */
.view { display: flex; gap: var(--space-1); margin: var(--space-2) var(--space-5) 0; justify-content: flex-end; }
.chip-btn {
  font: 400 var(--t-chip) var(--font-data); color: var(--muted);
  background: var(--panel); border: 1px solid var(--border-strong);
  border-radius: var(--r-pill); padding: 3px 10px; cursor: pointer;
  transition: background var(--dur-hover) var(--motion-ease),
              border-color var(--dur-hover) var(--motion-ease),
              color var(--dur-hover) var(--motion-ease);
}
.chip-btn:hover { background: var(--panel-2); }
.chip-btn[aria-pressed="true"] {
  background: var(--accent-weak); border-color: var(--accent); color: var(--ink);
}

/* A value shown on request: monospace, selectable in one click, wrapping
 * anywhere. */
code.val {
  display: block; font: 400 var(--t-chip) var(--font-data);
  color: var(--ink); background: var(--danger-weak, var(--panel-2));
  border: 1px solid var(--border-strong); border-radius: var(--r-control);
  padding: 4px 6px; margin-bottom: 4px;
  word-break: break-all; user-select: all;
}

/* Transient confirmation toast at the bottom of the viewport. */
.toast {
  position: fixed; left: 50%; bottom: var(--space-5, 24px);
  transform: translateX(-50%); z-index: 40;
  font: 400 var(--t-chip) var(--font-data); color: var(--ink);
  background: var(--panel-2); border: 1px solid var(--accent);
  border-radius: var(--r-pill); padding: 8px 16px; max-width: 80vw;
}

main { padding: 0 var(--space-5) var(--space-6); }

/* MAIN */

/* CARD: the panel that holds a table. */
.card { background: var(--panel); border: 1px solid var(--border);
  border-radius: var(--r-card); margin-top: var(--space-4); }

/* TABLE */
table { border-collapse: separate; border-spacing: 0; width: 100%;
  table-layout: fixed; }
/* Fixed layout with explicit column widths, so a long cell cannot push the
 * other columns around. */
col.c-name { width: 11%; } col.c-site { width:  8%; }
col.c-dir  { width:  9%; } col.c-repo { width: 16%; }
col.c-desc { width: 12%; } col.c-stack{ width:  8%; }
col.c-when { width: 13%; } col.c-host { width:  9%; }
col.c-users{ width:  8%; } col.c-note { width:  6%; }
thead th {
  position: sticky; top: var(--stick, 56px); z-index: 10;
  background: var(--panel-2); color: var(--muted);
  font: 600 var(--t-label) var(--font-ui);
  text-transform: uppercase; letter-spacing: .1em;
  text-align: left; padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--border-strong); white-space: nowrap;
}
thead th:first-child { border-top-left-radius: var(--r-card); }
thead th:last-child  { border-top-right-radius: var(--r-card); }

/* Group header rows */
tbody.grp th {
  text-align: left; padding: var(--space-3) var(--space-3) var(--space-2);
  font: 600 var(--t-label) var(--font-ui);
  text-transform: uppercase; letter-spacing: .1em; color: var(--muted);
  border-bottom: 1px solid var(--border-strong);
  border-top: 1px solid var(--border);
}
tbody.grp:first-of-type th { border-top: 0; }
tbody.grp th .n { font-family: var(--font-data); font-variant-numeric: tabular-nums;
  color: var(--muted); }
tbody td {
  padding: var(--space-2) var(--space-3); vertical-align: top;
  border-bottom: 1px solid var(--border);
}
tbody.grp tr:last-child td { border-bottom: 0; }
tbody tr { transition: background var(--dur-hover) var(--motion-ease); }
tbody tr:hover { background: var(--panel-2); }
td.num { text-align: right; font-family: var(--font-data);
  font-variant-numeric: tabular-nums; white-space: nowrap; }
/* Numeric cells align right and do not wrap; their captions may. */
td.num .tier, td.num .anchor { white-space: normal; }

.name { font-weight: 600; overflow-wrap: anywhere; }
.nwo, .folder { overflow-wrap: anywhere; }
/* The "more" control that unfolds a long list inside a cell. */
.more {
  font: 400 var(--t-chip) var(--font-data); color: var(--muted);
  background: transparent; border: 1px dashed var(--border-strong);
  border-radius: var(--r-pill); padding: 1px 8px; margin-top: 2px; cursor: pointer;
  transition: background var(--dur-hover) var(--motion-ease),
              color var(--dur-hover) var(--motion-ease);
}
.more:hover { background: var(--panel-2); color: var(--ink); }
.repos.folded .item:nth-child(n+4) { display: none; }
.anchor { color: var(--muted); font-size: var(--t-chip); font-family: var(--font-data); }
.desc { color: var(--muted); max-width: 52ch;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.none { color: var(--muted); opacity: .55; }

/* CHIPS */
.chip {
  display: inline-flex; align-items: center; gap: 5px;
  font: 400 var(--t-chip) var(--font-data); color: var(--ink);
  background: var(--panel); border: 1px solid var(--border-strong);
  border-radius: var(--r-pill); padding: 1px 8px; margin: 1px 3px 1px 0;
  /* A chip may wrap its own text rather than overflow its cell. */
  white-space: normal;
}
.chip .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); flex: none; }
.chip.ok { background: var(--ok-weak); border-color: var(--ok); }
.chip.ok .dot { background: var(--ok); }
.chip.warn { background: var(--warn-weak); border-color: var(--warn); }
.chip.warn .dot { background: var(--warn); }
.chip.danger { background: var(--danger-weak); border-color: var(--danger); }
.chip.danger .dot { background: var(--danger); }

/* REPOSITORIES inside a row */
.repos { display: flex; flex-direction: column; gap: 3px; }
.repo { display: flex; align-items: baseline; gap: var(--space-2); flex-wrap: wrap; }
.repo .nwo { font-family: var(--font-data); font-size: var(--t-chip); }
/* A superseded repository is shown struck through and faded. */
.repo.sub { opacity: .62; }
.repo.sub .nwo { text-decoration: line-through; text-decoration-thickness: 1px; }
.rule {
  font: 400 var(--t-chip) var(--font-data); color: var(--muted);
  padding-left: var(--space-3); border-left: 1px solid var(--border);
  margin-left: 2px; max-width: 60ch;
}
/* Site links: monospace, one per line, wrapping anywhere. */
.sites a { font-family: var(--font-data); font-size: var(--t-chip); display: block;
  overflow-wrap: anywhere; }
/* TOP BAR: brand, page links and the language switch, sticky above
 * everything else. */
.topbar { position: sticky; top: 0; z-index: 30; display: flex; flex-wrap: wrap;
  align-items: center; gap: var(--space-2) var(--space-4);
  padding: var(--space-2) var(--space-5); background: var(--panel);
  border-bottom: 1px solid var(--border); }
/* THE PRODUCT GLYPH on its dark tile, as every PassionCode product carries it,
 * beside the name and the family it belongs to. */
.brand { display: flex; align-items: center; gap: var(--space-2); color: var(--ink);
  text-decoration: none; padding: var(--space-1) 0; }
.brand-mark { display: block; width: 32px; height: 32px; flex-shrink: 0; }
.brand-name { display: grid; gap: 2px; min-width: 0; }
.brand-name strong { font-size: var(--t-body); font-weight: 700; letter-spacing: -.01em; }
.brand-family { color: var(--muted); font-size: var(--t-chip); }
/* EN/RU: two equal choices; the pressed one is the reader's language. */
.locale-switch { display: inline-flex; margin-left: auto; border: 1px solid var(--border-strong);
  border-radius: var(--r-control); overflow: hidden; }
.locale-switch button { font: 600 var(--t-chip) var(--font-data); letter-spacing: .06em;
  color: var(--muted); background: transparent; border: 0; padding: var(--space-1) var(--space-3);
  min-height: 28px; cursor: pointer;
  transition: background var(--dur-hover) var(--motion-ease), color var(--dur-hover) var(--motion-ease); }
.locale-switch button + button { border-left: 1px solid var(--border-strong); }
.locale-switch button[aria-pressed="true"] { background: var(--accent); color: var(--accent-ink); }
.locale-switch button:hover:not([aria-pressed="true"]) { background: var(--panel-2); color: var(--ink); }
.family-link { color: var(--muted); font-size: var(--t-chip); text-decoration: underline;
  text-underline-offset: .2em; }
.family-link:hover { color: var(--ink); }
.made-by { margin: var(--space-4) 0 0; color: var(--muted); }
.made-by a { color: var(--muted); text-decoration: underline; text-underline-offset: .2em; }
.pages { display: flex; flex-wrap: wrap; gap: var(--space-2); margin: 0; }
.pages a.pg { text-decoration: none; padding: 6px 12px; border: 1px solid var(--border);
  border-radius: var(--r-pill); background: var(--panel); color: var(--ink); font-size: var(--t-chip); }
.pages a.pg[aria-current="page"] { background: var(--ink); color: var(--panel); border-color: var(--ink); }
.pages a.pg .n { font-family: var(--font-data); font-variant-numeric: tabular-nums; margin-left: 4px; opacity: .7; }
.mods { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: var(--space-3); margin: var(--space-3) 0; }
.mods a.mod { display: block; text-decoration: none; color: inherit; padding: var(--space-3); }
.mods a.mod b { display: block; margin-bottom: 4px; }
body[data-page] .tabs, body[data-page] .controls, body[data-page] .seg, body[data-page] .view { display: none; }
body[data-page="projects"] .controls, body[data-page="projects"] #seg-projects, body[data-page="projects"] #view-projects,
body[data-page="heroku"] .controls, body[data-page="heroku"] #seg-heroku,
body[data-page="domains"] .controls, body[data-page="domains"] #seg-domains,
body[data-page="creds"] .controls, body[data-page="creds"] #seg-creds,
body[data-page="env"] .controls, body[data-page="env"] #seg-env,
body[data-page="mcp"] .controls, body[data-page="mcp"] #seg-mcp,
body[data-page="traffic"] .controls, body[data-page="traffic"] #seg-traffic { display: flex; }
body[data-page] .controls.no-rows { display: none; }
/* SPLIT PAGES. Each page shows only its own controls and sections; the
 * rules below hide what belongs to another page. */
body[data-page]:not([data-page="index"]):not([data-page="findings"]) #findings { display: none; }
body[data-page]:not([data-page="index"]):not([data-page="health"]) #tiles,
body[data-page]:not([data-page="index"]):not([data-page="health"]) #more-tiles { display: none; }
/* Health-only and projects-only sections. */
body[data-page]:not([data-page="health"]) #observer,
body[data-page]:not([data-page="health"]) #queue-s { display: none; }
body[data-page]:not([data-page="projects"]) #reading,
body[data-page]:not([data-page="projects"]) #dups-s { display: none; }
body[data-page="index"] #out, body[data-page="findings"] #out, body[data-page="health"] #out,
body[data-page="machine"] #out, body[data-page="machine"] #list-tools,
body[data-page="agents"] #out, body[data-page="agents"] #list-tools,
body[data-page="index"] #panel, body[data-page="findings"] #panel, body[data-page="health"] #panel,
body[data-page="machine"] #panel, body[data-page="agents"] #panel { display: none; }
/* The Agents page: server-rendered cards (dashboard/agents_page.py). */
.agents-wf { margin-top: var(--space-2); }
.agents-wf > summary { cursor: pointer; display: flex; flex-wrap: wrap; align-items: center;
  gap: var(--space-2); list-style: none; }
.agents-wf > summary::-webkit-details-marker { display: none; }
.agents-wf > summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.agents-meta { flex-basis: 100%; color: var(--muted); font-size: var(--t-label); }
.agents-wf h3 { font: 600 var(--t-label) var(--font-ui); text-transform: uppercase;
  letter-spacing: .06em; color: var(--muted); margin: var(--space-3) 0 var(--space-1); }
.agents-project { margin: var(--space-4) 0 0; font-size: var(--t-label); color: var(--muted); }
.agents-lane { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2);
  margin-top: var(--space-3); }
.agents-seg { display: inline-flex; flex-direction: column; gap: var(--space-1);
  border: 1px solid var(--border-strong); border-radius: var(--r-card);
  padding: var(--space-2) var(--space-3); background: var(--panel-2);
  font: 400 var(--t-chip) var(--font-data); }
.agents-seg.offered { border-style: dashed; border-color: var(--warn); }
.agents-seg.lapsed { text-decoration: line-through; color: var(--muted); }
.agents-dots { display: flex; flex-wrap: wrap; gap: 4px; }
.agents-dot { border-radius: var(--r-pill); padding: 0 6px; border: 1px solid var(--border-strong);
  background: var(--panel); }
.agents-dot.st-done { border-color: var(--ok); }
.agents-dot.st-blocked { border-color: var(--danger); }
.agents-dot.st-in_progress { border-color: var(--accent); }
.agents-arrow { color: var(--muted); font: 400 var(--t-chip) var(--font-data); }
.agents-ended { color: var(--muted); }
.agents-needs { list-style: none; padding: 0; margin: 0; }
.agents-need { padding: var(--space-2) 0; border-bottom: 1px solid var(--border); }
.agents-need:last-child { border-bottom: 0; }
.agents-cmd { display: block; margin-top: var(--space-1); white-space: pre-wrap;
  word-break: break-all; }
.agents-constraints li { color: var(--warn); }
.visually-hidden-list { position: absolute; width: 1px; height: 1px; overflow: hidden;
  clip: rect(0 0 0 0); white-space: nowrap; }
@media (max-width: 640px) { .agents-lane { flex-direction: column; align-items: stretch; } }
/* The Machine page: server-rendered cards (dashboard/machine_page.py). */
.machine-h { font: 600 var(--t-section) var(--font-ui); margin: 0 0 var(--space-3); }
#machine thead th.num { text-align: right; }
.machine-at, .machine-hint { color: var(--muted); font-size: var(--t-label); margin-top: var(--space-3); }
body[data-page="findings"] .flist.folded .finfo, body[data-page="findings"] .flist.folded .fwarning { display: block; }
/* The index page puts the inventory tiles first and the work tiles after
 * them. */
body[data-page="index"] header { display: flex; flex-direction: column; }
body[data-page="index"] header > #tiles { order: 1; }
body[data-page="index"] header > #work-h, body[data-page="index"] header > #work { order: 2; }
body[data-page]:not([data-page="index"]):not([data-page="health"]) #work,
body[data-page]:not([data-page="index"]):not([data-page="health"]) #work-h { display: none; }
#work-h { font-size: var(--t-chip); letter-spacing: .08em; text-transform: uppercase;
  color: var(--muted); margin: var(--space-3) 0 var(--space-2); }
.tile.name { text-decoration: none; color: inherit; }
.tile.name b { font-size: var(--t-body); }
.f.ftype-folded:not(.ftype-open) { display: none; }
.silenced { margin: 8px 0; } .silenced summary { cursor: pointer; font-size: var(--t-chip); color: var(--muted); }
.fsilenced { opacity: .75; }
.folder { font-family: var(--font-data); font-size: var(--t-chip); color: var(--muted); display: block; }

footer {
  margin-top: var(--space-6); padding: var(--space-4) var(--space-5);
  border-top: 1px solid var(--border); color: var(--muted); font-size: var(--t-chip);
}
footer h3 { margin: 0 0 var(--space-1); font: 600 var(--t-label) var(--font-ui);
  text-transform: uppercase; letter-spacing: .1em; }
footer ul { margin: 0 0 var(--space-3); padding-left: var(--space-4); }
.empty { padding: var(--space-6); text-align: center; color: var(--muted); }

/* NARROW SCREENS: a table row becomes a stacked card, each cell labelled
 * from its data-label attribute. */
@media (max-width: 1100px) {
  table, tbody, tr, td, tbody.grp th { display: block; width: 100%; }
  thead { display: none; }
  tbody.grp th { border-top: 0; }
  tbody tr { border-bottom: 1px solid var(--border-strong); padding: var(--space-2) 0; }
  tbody td { border: 0; padding: 2px var(--space-3); }
  tbody td::before {
    content: attr(data-label); display: block;
    font: 600 var(--t-chip) var(--font-ui); text-transform: uppercase;
    letter-spacing: .1em; color: var(--muted); margin-top: var(--space-2);
  }
  td.num { text-align: left; }
  td.e { display: none; }                                             
  .desc { max-width: none; }
}
</style>
</head>
<body>
__NAV__
<header>
  __H1__
  <p class="sub">__SUB__ <span id="upd-label" data-t>Measured</span><span class="mono" id="upd"></span><span data-t>; registry content last changed</span>
  <span class="mono" id="content-stamp"></span>.</p>
<div class="tiles" id="tiles"></div>
  <h3 id="work-h" data-t>Activity</h3>
  <div class="tiles work" id="work"></div>
  <section id="findings"></section>
</header>

<nav class="tabs" role="tablist" aria-label="What to show">
  <button class="tab" type="button" id="tab-projects" role="tab" aria-selected="true"
          aria-controls="out"><span data-t>Projects</span> <span class="n" id="n-projects"></span></button>
  <button class="tab" type="button" id="tab-heroku" role="tab" aria-selected="false"
          aria-controls="out">Heroku <span class="n" id="n-heroku"></span></button>
  <button class="tab" type="button" id="tab-domains" role="tab" aria-selected="false"
          aria-controls="out"><span data-t>Domains</span> <span class="n" id="n-domains"></span></button>
  <button class="tab" type="button" id="tab-creds" role="tab" aria-selected="false"
          aria-controls="out"><span data-t>Keys</span> <span class="n" id="n-creds"></span></button>
  <button class="tab" type="button" id="tab-env" role="tab" aria-selected="false"
          aria-controls="out">ENV <span class="n" id="n-env"></span></button>
  <button class="tab" type="button" id="tab-mcp" role="tab" aria-selected="false"
          aria-controls="out">MCP <span class="n" id="n-mcp"></span></button>
</nav>

<div class="controls">
  <input type="search" id="q" placeholder="Search: name, description, repository, folder, domain, stack"
         aria-label="Search the registry">
  <select id="owner" aria-label="Owner"></select>
  <div class="seg" id="seg-projects" role="group" aria-label="Project filters">
    <button class="chip-btn" data-f="heroku" aria-pressed="false" data-t>has Heroku</button>
    <button class="chip-btn" data-f="noheroku" aria-pressed="false" data-t>no Heroku</button>
    <button class="chip-btn" data-f="site" aria-pressed="false" data-t>has a site</button>
    <button class="chip-btn" data-f="note" aria-pressed="false" data-t>has a note</button>
    <button class="chip-btn" data-f="nonote" aria-pressed="false" data-t>no note</button>
    <button class="chip-btn" data-f="folder" aria-pressed="false" data-t>has a folder</button>
    <button class="chip-btn" data-f="dirty" aria-pressed="false" data-t>uncommitted</button>
    <button class="chip-btn" data-f="unsynced" aria-pressed="false" data-t>clone out of sync</button>
    <button class="chip-btn" data-f="dead" aria-pressed="false" data-t>site does not resolve</button>
    <button class="chip-btn" data-f="drift" aria-pressed="false" data-t>declared active, measured dormant</button>
    <button class="chip-btn" data-f="owned" aria-pressed="false" data-t>mine only</button>
  </div>
  <!-- D-21: a VIEW switch, not a filter. "rules" changes what each
       row shows, never which rows show — so it sits outside the filter group,
       and `narrowing()`, which reads chips inside `.seg` only, does not count
       it among the filters. -->
  <div class="view" id="view-projects" role="group" aria-label="Table view">
    <button class="chip-btn" data-f="rules" aria-pressed="false" title="show the rule behind each project↔repository link" data-t>rules: show</button>
  </div>
  <div class="seg" id="seg-heroku" role="group" aria-label="App filters" hidden>
    <button class="chip-btn" data-f="noproject" aria-pressed="false" data-t>no project</button>
    <button class="chip-btn" data-f="nofolder" aria-pressed="false" data-t>no folder</button>
    <button class="chip-btn" data-f="norepo" aria-pressed="false" data-t>no repository</button>
    <button class="chip-btn" data-f="down" aria-pressed="false" data-t>down</button>
    <button class="chip-btn" data-f="waste" aria-pressed="false" data-t>paying for nothing</button>
    <button class="chip-btn" data-f="stale" aria-pressed="false" data-t>no deploy for a year</button>
    <button class="chip-btn" data-f="oldstack" aria-pressed="false" data-t>outdated stack</button>
  </div>
  <div class="seg" id="seg-domains" role="group" aria-label="Domain filters" hidden>
    <button class="chip-btn" data-f="d-noproject" aria-pressed="false" data-t>no project</button>
    <button class="chip-btn" data-f="d-dark" aria-pressed="false" data-t>does not resolve</button>
    <button class="chip-btn" data-f="d-http" aria-pressed="false" data-t>HTTP error</button>
    <button class="chip-btn" data-f="d-expiring" aria-pressed="false" data-t>expires in ≤90 days</button>
    <button class="chip-btn" data-f="d-norenew" aria-pressed="false" data-t>no auto-renewal</button>
    <button class="chip-btn" data-f="d-unmeasured" aria-pressed="false" data-t>domain@@not measured</button>
  </div>
  <div class="seg" id="seg-creds" role="group" aria-label="Key filters" hidden>
    <button class="chip-btn" data-f="c-leaked" aria-pressed="false" data-t>open leaks</button>
    <button class="chip-btn" data-f="c-unclaimed" aria-pressed="false" data-t>unowned</button>
    <button class="chip-btn" data-f="c-shared" aria-pressed="false" data-t>shared: 2+ projects</button>
    <button class="chip-btn" data-f="c-norotate" aria-pressed="false" data-t>never rotated</button>
    <button class="chip-btn" data-f="c-disabled" aria-pressed="false" data-t>disabled</button>
    <button class="chip-btn" data-f="c-untracked" aria-pressed="false" data-t>not in the store</button>
  </div>
  <div class="seg" id="seg-traffic" role="group" aria-label="Traffic filters" hidden>
    <button class="chip-btn" data-f="t-unclaimed" aria-pressed="false" data-t>no project</button>
    <button class="chip-btn" data-f="t-linked" aria-pressed="false" data-t>linked</button>
    <button class="chip-btn" data-f="t-quiet" aria-pressed="false" data-t>no users</button>
    <button class="chip-btn" data-f="t-app" aria-pressed="false" data-t>app only</button>
  </div>
  <div class="seg" id="seg-mcp" role="group" aria-label="MCP filters" hidden>
    <button class="chip-btn" data-f="m-url" aria-pressed="false" data-t>key in URL</button>
    <button class="chip-btn" data-f="m-down" aria-pressed="false" data-t>not answering</button>
    <button class="chip-btn" data-f="m-auth" aria-pressed="false" data-t>sign-in needed</button>
    <button class="chip-btn" data-f="m-alone" aria-pressed="false" data-t>in one agent only</button>
  </div>
  <div class="seg" id="seg-env" role="group" aria-label="ENV filters" hidden>
    <button class="chip-btn" data-f="e-shared" aria-pressed="false" data-t>shared with another project</button>
    <button class="chip-btn" data-f="e-reuse" aria-pressed="false" data-t>empty, set in another project</button>
    <button class="chip-btn" data-f="e-git" aria-pressed="false" data-t>in git</button>
    <button class="chip-btn" data-f="e-open" aria-pressed="false" data-t>readable beyond the owner</button>
    <button class="chip-btn" data-f="e-all" aria-pressed="false" data-t>+ config and empty</button>
    <button class="chip-btn" data-f="e-tpl" aria-pressed="false" data-t>+ templates</button>
  </div>
</div>

<div id="toast" class="toast" role="status" aria-live="polite" hidden></div>
<section id="panel" class="card panel" role="dialog" aria-labelledby="panel-title" hidden></section>
__CARDS__
__MACHINE__
__AGENTS__
<div id="list-tools" class="list-tools"></div>
<main id="out" aria-live="polite"></main>

<footer>
  <section id="reading">
    <h3 data-t>How to read this</h3>
    <p data-t>The rule under a repository is why it is linked to the project: the folder name,
    a mention in a note, the organization, or a link checked by hand. A repository
    superseded by another, or an empty one, is dimmed and struck through and names its
    successor — it is not the equal of what replaced it.</p>
  </section>
  <section id="observer">
    <h3 data-t>Observer state</h3>
    <p data-t>What the observatory knows about itself: when it last looked, how much
    it has collected and how many conclusions wait for a person's decision. Empty here
    means there is no local store — the registry still reads as before.</p>
    <div id="health" class="health"></div>
  </section>
  <section id="queue-s">
    <h3 id="queue-h" data-t>Awaiting a person's decision</h3>
    <div id="queue"></div>
  </section>
  <section id="dups-s">
    <h3 id="dups-h" data-t>Repository names under several owners</h3>
    <ul id="dups"></ul>
  </section>
  <p class="made-by"><a href="https://passioncode.ai/" rel="noopener" data-t>Project Observatory is part of the PassionCode.ai toolkit</a></p>
</footer>

<script>
// THE DATA. The placeholder below is replaced by the builder with the JSON
// payload (naming it here put a second copy of the payload in this comment); the
// runtime block carries the paths commands on this page are built from.
const PAGE = "__PAGE__";
const PAGE_TITLE = "__PAGE_TITLE__";
const TABLE_PAGES = ["projects", "domains", "heroku", "creds", "env", "mcp", "traffic"];
const D = __DATA__;
const RUNTIME = D.runtime || {};
const shellArg = value => "'" + String(value).replace(/'/g, "'\\''") + "'";
const projectFile = value => String(RUNTIME.projects || ".").replace(/\/$/, "") + "/" + value;
const secretFile = value => String(RUNTIME.secrets || ".").replace(/\/$/, "") + "/" + value;
const engineCommand = (file, args = []) =>
  (RUNTIME.home ? "env " + shellArg("OBSERVATORY_HOME=" + RUNTIME.home) + " " : "") +
  shellArg(RUNTIME.python || "python3") + " " +
  shellArg(String(RUNTIME.engine || ".").replace(/\/$/, "") + "/" + file) +
  args.map(value => " " + shellArg(value)).join("");
const toolCommand = (name, args = []) => engineCommand("tools/" + name, args);
const cliCommand = (...args) => engineCommand("observatory.py", args);
const privateInput = command => command + " < " + shellArg("/absolute/path/to/private-input");
// The command a person types, as the docs spell it: `project-observatory full …`,
// with this workspace named when the page knows it.
const fullCommand = (...args) =>
  (RUNTIME.home ? "OBSERVATORY_HOME=" + shellArg(RUNTIME.home) + " " : "") +
  "project-observatory full " + args.join(" ");
// "Not scanned" with the commands that scan it, each a copy button: the reader
// learns what is missing and gets the exact next step, never an empty table.
const notScanned = (said, commands) => `<div class="empty not-scanned"><p>${said}</p>` +
  commands.map(c => `<p><button class="chip-btn mono" type="button" data-copy="${E(c)}" title="${T("copy the command")}">${E(c)}</button></p>`).join("") +
  `</div>`;

// __SHARED_BELOW__ — the split pages keep everything above this line inline
// Everything below is shared by every page and may move to a separate
// script file. The rows are normalised first, so that every renderer can
// assume the list fields exist rather than guarding each access.
for (const r of D.rows || []) {
  r.folders = r.folders || [];
  r.stack = r.stack || [];
  r.repos = r.repos || [];
  r.sites = r.sites || [];
}
const E = s => String(s == null ? "" : s).replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// THE LANGUAGE. English message ids in the code, one catalog per language
// (dashboard/locales/*.json — the builder reads the same files). The page is
// built in the workspace's language; the EN/RU switch keeps the reader's own
// choice in localStorage and reloads, so every renderer below simply runs again
// in it. Static text carries its English id in `data-t` and is re-translated by
// `localizeStatic` when the two languages differ.
const I18N = __I18N__;
const LOCALE_KEY = "observatory.locale";
const BUILD_LOCALE = document.documentElement.getAttribute("data-build-locale") || "en";
const LOCALE = (() => {
  try {
    const chosen = localStorage.getItem(LOCALE_KEY);
    if (chosen && Object.prototype.hasOwnProperty.call(I18N, chosen)) return chosen;
  } catch (e) {}
  return Object.prototype.hasOwnProperty.call(I18N, BUILD_LOCALE) ? BUILD_LOCALE : "en";
})();
const PLURAL = new Intl.PluralRules(LOCALE);
// Numbers are grouped the way the reader's language groups them.
const NUM = v => Number(v).toLocaleString(LOCALE);
// T("{n} projects", {n: 3}): the translation, its plural form chosen by `n`,
// its placeholders filled; an id with no translation reads as English, never
// as an empty string.
function T(id, args) {
  let entry = (I18N[LOCALE] || {})[id];
  if (entry == null && LOCALE !== "en") entry = (I18N.en || {})[id];
  if (entry && typeof entry === "object") {
    const n = args && typeof args.n === "number" ? args.n : NaN;
    entry = (isNaN(n) ? entry.other : entry[PLURAL.select(n)]) || entry.other;
  }
  // `context@@text` ids show only their text when untranslated (i18n.py).
  const text = typeof entry === "string" ? entry : id.split("@@").pop();
  if (!args) return text;
  return text.replace(/\{([a-z_][a-z0-9_]*)\}/g, (m, k) => !(k in args) ? m
    : (typeof args[k] === "number" && Number.isInteger(args[k]) ? NUM(args[k]) : String(args[k])));
}
// A finding's title in the reader's language: its message id and arguments
// (`finding_types.titled` in the rule modules). A findings.json written before
// titles carried an id has only the English `title`, and shows it.
// A title is a sentence. Some rule ids start lowercase; one that starts with a
// placeholder ({project}) begins with a name, which keeps its own case.
const sentence = (id, text) => /^[a-z]/.test(id || "") ? text.charAt(0).toLocaleUpperCase(LOCALE) + text.slice(1) : text;
const findingTitle = f => f.title_id ? sentence(f.title_id, T(f.title_id, f.title_args || {})) : sentence(f.title, String(f.title || ""));
function localizeStatic(root) {
  root.querySelectorAll("[data-t]").forEach(el => {
    let args;
    try { args = el.dataset.tArgs ? JSON.parse(el.dataset.tArgs) : undefined; } catch (e) {}
    el.textContent = T(el.dataset.t, args);
  });
  for (const name of ["aria-label", "placeholder", "title", "data-label"])
    root.querySelectorAll(`[data-t-${name}]`).forEach(el =>
      el.setAttribute(name, T(el.getAttribute(`data-t-${name}`))));
}
if (LOCALE !== BUILD_LOCALE) {
  localizeStatic(document);
  if (PAGE_TITLE) document.title = T(PAGE_TITLE) + " — Project Observatory";
}
document.querySelectorAll(".locale-switch button[data-locale]").forEach(b => {
  b.setAttribute("aria-pressed", String(b.dataset.locale === LOCALE));
  b.addEventListener("click", () => {
    if (b.dataset.locale === LOCALE) return;
    try {
      // Choosing the workspace's own language forgets the override, so a
      // later change of `interface.locale` reaches this reader too.
      if (b.dataset.locale === BUILD_LOCALE) localStorage.removeItem(LOCALE_KEY);
      else localStorage.setItem(LOCALE_KEY, b.dataset.locale);
    } catch (e) {
      // Storage refused (a private window): the choice cannot outlive the
      // page, so say so instead of pretending the switch worked.
      return toast(T("Your browser keeps no settings here; set interface.locale in the workspace instead."));
    }
    location.reload();
  });
});
// `/` focuses the search box, unless the reader is already typing.
document.addEventListener("keydown", ev => {
  if (ev.key !== "/" || ev.metaKey || ev.ctrlKey || ev.altKey) return;
  const tag = (ev.target && ev.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select") return;
  const q = PAGE === "findings" ? document.querySelector(".fq") : document.getElementById("q");
  if (q && q.offsetParent) { ev.preventDefault(); q.focus(); q.select(); }
});

// Two timestamps, each labelled: when the estate was last MEASURED, and when
// the registry's content last changed.
// Never "Measured not measured": with no scan yet the label itself says so.
if (D.measured) {
  // The space lives with the value, so "Not measured yet" meets the semicolon directly.
  document.getElementById("upd").textContent = " " + D.measured.replace("T", " ").replace("Z", " UTC");
} else {
  document.getElementById("upd-label").textContent = T("Not measured yet");
  document.getElementById("upd").textContent = "";
}
const cs = document.getElementById("content-stamp");
if (cs) cs.textContent = D.updated || "—";
const S = D.stats;
// A TILE'S CAPTION AGREES WITH ITS NUMBER. A counted noun is a plural id
// (`tile@@{n} …`): the catalog picks the form for the number ("1 inactive
// repo", and the Russian few/many forms) and the number itself goes in the tile's bold
// slot, so the caption is the chosen form without its `{n}`. A value that is
// not a number ("12+", "not measured") takes the form a count of many takes.
function pluralCaption(id, value) {
  let entry = (I18N[LOCALE] || {})[id];
  if (entry == null && LOCALE !== "en") entry = (I18N.en || {})[id];
  const n = typeof value === "number" ? value : parseFloat(value);
  let text;
  if (entry && typeof entry === "object")
    text = entry[Number.isFinite(n) ? PLURAL.select(n) : (entry.many ? "many" : "other")] || entry.other;
  else text = typeof entry === "string" ? entry : id.split("@@").pop();
  return String(text).replace("{n}", "").replace(/\s+/g, " ").trim();
}
// [caption, value]: an id carrying `{n}` is a counted noun, any other is a
// caption that needs no agreement ("down", "with a site").
const tile = (id, value) => [id.includes("{n}") ? pluralCaption(id, value) : T(id), value];
// INVENTORY TILES. The primary set is always visible; the rest sit behind a
// "more" control so the header stays short.
const TILES_PRIMARY = [
  tile("tile@@{n} projects", S.projects), tile("tile@@{n} repositories", S.repositories),
  tile("tile@@{n} Heroku apps", S.heroku_apps), tile("tile@@{n} apps down", S.heroku_down),
  tile("tile@@{n} domains", S.domains), tile("tile@@{n} sites do not resolve", S.dead_site),
  tile("tile@@{n} clones out of sync", S.unsynced), tile("Heroku $/mo", Math.round(S.heroku_cost)),
];
const TILES_REST = [
  tile("tile@@{n} owners", S.owners), tile("with a site", S.with_site), tile("with a folder", S.with_folder),
  tile("with a note", S.with_note), tile("without a note", S.no_note),
  tile("tile@@{n} archived projects", S.archived), tile("tile@@{n} archived repos", S.archived_repos),
  tile("tile@@{n} inactive repos", S.inactive_repos), tile("tile@@{n} domains without a project", S.domains_no_row),
  tile("tile@@{n} Heroku apps without a project", S.heroku_unlinked),
  // Local state of the working copies, and the credential counts.
  tile("tile@@{n} dirty working copies", S.dirty), tile("tile@@{n} keys in the registry", S.creds),
  tile("tile@@{n} keys leaked", S.creds_leaked), tile("tile@@{n} keys without a project", S.creds_unclaimed),
];
// DRIFT: projects declared active whose measured activity says dormant or
// cold — a claim and a measurement that disagree.
const DRIFT = D.rows.filter(r => r.lifecycle === "active"
                            && (r.tier === "dormant" || r.tier === "cold")).length;
// WORK TILES: what happened, over the rolling windows the builder computes.
// A key absent from the stats (no store) is simply not shown.
// Stable ids in the data, words from the catalog: the label is the reader's
// language, the key is the builder's contract.
const WORK_KEYS = [["commits_7d", "tile@@{n} commits, 7 d"], ["projects_active_7d", "tile@@{n} projects in progress, 7 d"],
                   ["commits_28d", "tile@@{n} commits, 28 d"], ["projects_active_28d", "tile@@{n} projects in progress, 28 d"],
                   ["unpushed_commits", "tile@@{n} commits on no remote"]];
const WORK_VALUE = k => k === "unpushed_commits" && typeof S[k] === "string" && !/^\d/.test(S[k])
  ? T(S[k])
  : k === "unpushed_commits" && S.unpushed_unmeasured
    ? `${S[k]} (${T("{n} checkouts not counted", {n: S.unpushed_unmeasured})})` : S[k];
// The caption agrees with the counted number, not with the decorated value.
const TILES_WORK = WORK_KEYS.filter(([k]) => S[k] != null)
  .map(([k, id]) => [pluralCaption(id, S[k]), WORK_VALUE(k)]);
const tileHTML = ts => ts.map(([k, v]) =>
  `<div class="tile"><b>${v}</b><span>${k}</span></div>`).join("");
{
  const host = document.getElementById("work");
  const top = S.busiest_28d;
  // The busiest project is a NAME, so it is a link to that project rather
  // than a number in a tile.
  // The store keys work by project id; the tile prints the project's NAME,
  // as every other surface does (`local-alpha-web` read as a different thing
  // from the row called alpha-web).
  const topRow = top && (D.rows || []).find(r => r.id === "project:" + top);
  if (host) host.innerHTML = TILES_WORK.length
    ? tileHTML(TILES_WORK) + (top
        ? `<a class="tile name" href="projects.html#project:${E(top)}"><b>${E(topRow && topRow.name || top)}</b>` +
          `<span>${T("most work, 28 d")}</span></a>` : "")
    : `<div class="tile"><b>—</b><span>${T("no store: activity not measured")}</span></div>`;
}
  // An empty registry says how to fill it, instead of showing a row of zeros
  // that would read as "measured, and nothing there".
document.getElementById("tiles").innerHTML = !D.rows.length
  ? `<div class="empty-estate">${notScanned(
       RUNTIME.projects_configured === false
         ? T("No project measured yet. Name the folder that holds your project checkouts, then measure it:")
         : T("No project measured yet. Measure this machine's projects folder:"),
       (RUNTIME.projects_configured === false ? [fullCommand("configure sources projects", "PATH")] : [])
         .concat([fullCommand("local")]))}</div>`
  : tileHTML(TILES_PRIMARY) +
  (DRIFT ? `<a class="tile" href="projects.html?f=drift"><b>${DRIFT}</b>` +
           `<span>${pluralCaption("tile@@{n} declared active, measured dormant", DRIFT)}</span></a>` : "") +
  `<button class="tile more-tiles" id="more-tiles" type="button" aria-expanded="false"
     ><b>+${TILES_REST.length}</b><span>${T("more counters")}</span></button>`;
const MORE_TILES = document.getElementById("more-tiles");                                      
if (MORE_TILES) MORE_TILES.onclick = e => {
  const b = e.currentTarget, open = b.getAttribute("aria-expanded") === "true";
  b.setAttribute("aria-expanded", String(!open));
  // "+N" counts what is hidden; once the counters are open nothing is, so
  // the number gives way to the collapse sign.
  const count = b.querySelector("b");
  if (open) { document.querySelectorAll(".tile.extra").forEach(t => t.remove());
              if (count) count.textContent = "+" + TILES_REST.length;
              b.querySelector("span").textContent = T("more counters"); return; }
  b.insertAdjacentHTML("beforebegin",
    tileHTML(TILES_REST).replaceAll('class="tile"', 'class="tile extra"'));
  if (count) count.textContent = "−";
  b.querySelector("span").textContent = T("collapse");
};


// OBSERVER HEALTH: what the observatory knows about itself, row by row. A
// missing field is left out rather than shown as zero.
const H = D.health || {};
const hb = [];
if (D.store_degraded) hb.push([T("store"), T(D.store_degraded.text || D.store_degraded, D.store_degraded.args)]);
if (H.last_scan) hb.push([T("last scan"), H.last_scan.replace("T", " ").replace("Z", " UTC")]);
if (H.events != null) hb.push([T("events"), NUM(H.events)]);
if (H.weeks != null) hb.push([T("weekly snapshots"), NUM(H.weeks)]);
if (H.metrics != null) hb.push([T("plugin measurements"), NUM(H.metrics)]);
// The local server, from its heartbeat: not running, alive, or silent — three
// states, spelled apart.
if (H.server_age_s != null) {
  if (H.server_age_s === -1) hb.push([T("local server"), T("not running")]);
  else if (H.server_age_s < (H.server_silent_after_s || 90)) {
    const up = H.server_uptime_s >= 3600
      ? T("{n} h", {n: Math.floor(H.server_uptime_s / 3600)}) : T("{n} min", {n: Math.floor(H.server_uptime_s / 60)});
    hb.push([T("local server"), T("answered when measured · port {port} · uptime {up}", {port: String(H.server_port), up})]);
    if (H.server_at_risk != null)
      hb.push([T("remote at risk"), T("{n} checkouts with work only on this disk", {n: H.server_at_risk})]);
  } else hb.push([T("local server"), T("SILENT for {n} min — store/logs/serverd.err", {n: Math.floor(H.server_age_s / 60)})]);
}
// When the server is down or silent, the row carries the command that
// starts or inspects it, ready to copy.
const SERVERD_FIX = {
  down: [T("the observer is not running — start it in a terminal"), toolCommand("serverd.py", ["--run"])],
  silent: [T("the observer is silent — check it"), toolCommand("serverd.py", ["--status"])],
};
if (H.leaks_open > 0) hb.push([T("secret leaks"), T("{n} not rotated — vault.py leaks", {n: H.leaks_open})]);
if (H.proposed != null) hb.push([T("awaiting the operator's decision"), H.proposed]);
if (H.registry_proposals) hb.push([T("registry edits proposed"), H.registry_proposals]);
// Whether memory text may leave this machine (OBS-33): the state in words and the
// command that shows the consents, ready to copy.
if (H.embedding_policy) {
  const EP = H.embedding_policy, show = fullCommand("embedding-policy show");
  if (EP.state === "remote-for-consented")
    hb.push([T("memory embeddings"), T("texts of {n} projects may go to {model}; confidential, workflow text and agents' queries never leave",
      {n: (EP.inForce || []).length, model: ((EP.configured || {}).provider || "") + "/" + ((EP.configured || {}).model || "")}), show]);
  else if (EP.state === "local-only")
    hb.push([T("memory embeddings"), T("nothing leaves this machine"), show]);
  else
    hb.push([T("memory embeddings"), T("policy refused, nothing leaves this machine: {reason}", {reason: EP.reason || "?"}), show]);
}
if (H.workflows_open) hb.push([T("agent workflows open"), H.workflows_open]);
if (H.handoffs_waiting) hb.push([T("handoffs waiting for a session"), H.handoffs_waiting]);
if (H.projection_lag) hb.push([T("conclusions not indexed"),
  T("{n}, oldest from {date}", {n: H.projection_lag, date: (H.projection_oldest || "").slice(0, 10)})]);
if (H.degraded_sources) hb.push([T("sources degraded"), H.degraded_sources]);
// This project's own spend, prepared by the builder from its own journal —
// the page does no date arithmetic of its own.
// Both figures carry the wallet's unit: a bare "0.0051" beside a month in
// credits read as a different currency, or as none.
const DENOMINATION = {credits: T("unit@@credits"), usd: "USD", USD: "USD"};
const spendUnit = DENOMINATION[H.spend_denomination] || String(H.spend_denomination || "");
if (H.spend_month != null)
  hb.push([T("spent by this project this month"), `${(+H.spend_month).toFixed(4)} ${spendUnit}`.trim()]);
if (H.spend_today != null)
  hb.push([T("of which today"), `${(+H.spend_today).toFixed(4)} ${spendUnit}`.trim()]);
// Models the provider-health file holds in quarantine, named up to three.
{
  const q = Object.keys(H.provider || {});
  if (q.length)
    hb.push([T("models in quarantine: {n}", {n: q.length}), q.slice(0, 3).join(", ")
             + (q.length > 3 ? " " + T("and {n} more", {n: q.length - 3}) : "")]);
}
{
  const state = H.server_age_s == null ? null
    : H.server_age_s === -1 ? "down" : H.server_age_s < (H.server_silent_after_s || 90) ? "up" : "silent";
  const fix = SERVERD_FIX[state];
  document.getElementById("health").innerHTML = (hb.length
    ? hb.map(([k, v, c]) => `<div class="hrow"><span>${E(k)}</span><b>${E(String(v))}` +
        (c ? ` <span class="mono">${E(c)}</span> <button class="chip-btn" type="button" data-copy="${E(c)}">${T("copy")}</button>` : "") +
        `</b></div>`).join("")
    : `<div class="hrow"><span>${T("observer")}</span><b>${T("no data")}</b></div>`)
    + (fix ? `<div class="hrow"><span>${E(fix[0])}</span><b><span class="mono">${E(fix[1])}</span>` +
             ` <button class="chip-btn" type="button" data-copy="${E(fix[1])}">${T("copy")}</button></b></div>` : "");
}

document.getElementById("dups").innerHTML = D.dups.map(g =>
  `<li class="mono">${g.map(E).join("  ·  ")}</li>`).join("")
  || `<li class="none">${T("none")}</li>`;

// TABS AND FILTERS. One table area, one selector and one chip group per tab;
// the current tab decides which of them apply.
let tab = "projects";

const sel = document.getElementById("owner");
// The selector's meaning changes with the tab: owner, team, registrar,
// section, project, agent or account.
const SEL_BY_TAB = {
  projects: [T("all owners"), T("Owner"), () => D.owners],
  heroku:   [T("all teams"), T("Team"),
             () => [...new Set(((D.heroku && D.heroku.apps) || []).map(a => a.team))].sort()],
  // Every registrar a row carries, and "—" for the names that carry none —
  // the same rows the table groups, so each option selects something.
  domains:  [T("all registrars"), T("Registrar"),
             () => { const rows = domainRows();
                     return [...new Set(rows.map(d => d.registrar).filter(Boolean))].sort()
                       .concat(rows.some(d => !d.registrar) ? [[NO_REGISTRAR, "—"]] : []); }],
  creds:    [T("all sections"), T("Section"),
             () => CRED_SECTIONS.map(s => s[1])],
  // Only the projects the current view can show: by default ENV lists secrets
  // in live files, and a project holding only templates or config would be an
  // option that empties the table.
  env:      [T("all projects"), T("Project"),
             () => [...new Set(ENVV.filter(e => keepEnv(e, currentQuery(), "")).map(e => e.project)
                                  .filter(Boolean))].sort()],
  mcp:      [T("all agents"), T("Agent"),
             () => [...new Set(((D.mcp && D.mcp.servers) || []).map(s => s.agent).filter(Boolean))].sort()],
  traffic:  [T("all accounts"), T("Account"),
             () => [...new Set(((D.google && D.google.properties) || []).map(p => p.account_name).filter(Boolean))].sort()],
};
// The value of the "—" registrar option: no registrar name can be empty
// brackets, so it cannot collide with one.
const NO_REGISTRAR = "[none]";
const currentQuery = () => ((document.getElementById("q") || {}).value || "").trim().toLowerCase();
// An option is a value or a [value, label] pair. The current choice is kept
// even when the view no longer offers it, so the selector never silently
// narrows to something the reader did not pick; the list is rewritten only
// when it changed, so an open selector is not rebuilt under the pointer.
function fillOwners() {
  const [all, label, vals] = SEL_BY_TAB[tab];
  const chosen = sel.value || "";
  const opts = vals().map(o => Array.isArray(o) ? o : [o, o]);
  if (chosen && !opts.some(([v]) => v === chosen)) opts.push([chosen, chosen === NO_REGISTRAR ? "—" : chosen]);
  const markup = `<option value="">${all}</option>` +
    opts.map(([v, l]) => `<option value="${E(v)}">${E(l)}</option>`).join("");
  if (sel.dataset.filled !== markup) { sel.innerHTML = markup; sel.dataset.filled = markup; }
  sel.value = chosen;
  sel.setAttribute("aria-label", label);
}
fillOwners();

// Active filter chips, kept per tab, so switching tabs never carries one
// tab's filters into another.
const activeBy = { projects: new Set(), heroku: new Set(), domains: new Set(),
                   creds: new Set(), env: new Set(), mcp: new Set(),
                   traffic: new Set() };
let active = activeBy[PAGE] || activeBy.projects;
document.querySelectorAll(".chip-btn[data-f]").forEach(c => c.onclick = () => {
  const on = c.getAttribute("aria-pressed") === "true";
  c.setAttribute("aria-pressed", String(!on));
  on ? active.delete(c.dataset.f) : active.add(c.dataset.f);
  const opposite = {heroku:"noheroku", noheroku:"heroku", note:"nonote", nonote:"note",
                    "t-linked":"t-unclaimed", "t-unclaimed":"t-linked"}[c.dataset.f];
  if (!on && opposite) {
    active.delete(opposite);
    document.querySelectorAll('.chip-btn[data-f="' + opposite + '"]').forEach(b => b.setAttribute("aria-pressed", "false"));
  }
  render();
});
// Filters can arrive in the URL (`?f=drift`), so a tile can link to an
// already-filtered view; such a chip is marked as switched on by a link.
{
  const wanted = new Set(new URLSearchParams(location.search || "").getAll("f"));
  document.querySelectorAll(".chip-btn[data-f]").forEach(c => {
    if (!wanted.has(c.dataset.f)) return;
    c.setAttribute("aria-pressed", "true");
    c.dataset.fromUrl = "1";
    c.title = T("switched on by a link");
    active.add(c.dataset.f);
  });
  // A search can arrive the same way (`?q=NAME`): a finding about a variable
  // name lands on the rows that carry it. The box shows it, so the narrowing
  // is visible and `reset` clears it like any typed search.
  const q = new URLSearchParams(location.search || "").get("q");
  const box = document.getElementById("q");
  if (q && box) box.value = q;
}

const APPS = (D.heroku && D.heroku.apps) || [];
const ENV_ORDER = ["production", "staging", "review", "development", "test", "local"];
const ENV_LABEL = {production: T("production"), staging: T("staging"), review: T("review"),
                   development: T("development"), test: T("test"), local: T("local")};
function hostingGroups(apps) {
  const rank = h => h.env ? ENV_ORDER.indexOf(h.env) : ENV_ORDER.length;
  const groups = new Map();
  [...apps].sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name))
    .forEach(h => { const k = h.env || ""; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(h); });
  // One group needs no heading when nothing is known about environments at all.
  const bare = groups.size === 1 && groups.has("");
  return [...groups].map(([env, list]) =>
    `<div class="envgroup" data-env="${E(env || "unassigned")}">` +
    (bare ? "" : `<div class="tier">${env ? chip(ENV_LABEL[env] || env, env === "production" ? "ok" : "")
                                         : chip(T("environment not stated"), "warn")}</div>`) +
    list.map(h =>
      `<div class="st st-${E(h.state)}" title="${E(h.rule)}${h.account ? " · " + T("account") + " " + E(h.account) : ""}"><i></i>` +
      `<span class="mono">${E(h.name)}</span>` +
      (h.cost ? `<span class="anchor"> $${Math.round(h.cost)}</span>` : "") +
      `</div>`).join("") + `</div>`).join("");
}
const DOMS = D.domains || [];
const LIVE_BY_HOST = h => { const d = DOMS.find(x => x.name === h); return d ? d.live : null; };
const CREDS = (D.creds && D.creds.credentials) || [];
const ENVF = (D.env && D.env.files) || [];
// One row per variable, flattened from the env files, with the file's own
// facts carried on every row.
const ENVV = ENVF.flatMap(f => f.variables.map(v => ({
  path: f.path, project: f.project, kind: f.kind, git: f.git, mode: f.mode,
  modified_on: f.modified_on, name: v.name, cls: v["class"],
  shared: v.shared_with || [], copies: v.copies || 1,
  available: v.available_in || [],
})));
const PROJ_NAME = new Map(D.rows.map(r => [r.id, r.name]));
function selectTab(next) {
  tab = next;
  active = activeBy[tab];
  for (const t of ["projects", "heroku", "domains", "creds", "env", "mcp"]) {
    const tb = document.getElementById("tab-" + t), sg = document.getElementById("seg-" + t);
    if (tb) tb.setAttribute("aria-selected", String(tab === t));
    if (sg) sg.hidden = tab !== t;
  }
  // The rules view switch belongs to the projects tab only.
  const vw = document.getElementById("view-projects");
  if (vw) vw.hidden = tab !== "projects";
  fillOwners();
  render();
}
for (const t of ["projects", "heroku", "domains", "creds", "env", "mcp"]) {
  const tb = document.getElementById("tab-" + t);
  if (tb) tb.onclick = () => selectTab(t);
}
document.getElementById("q").oninput = render;
sel.onchange = render;

const hay = r => [r.name, r.description, r.owner, r.last, r.folders.join(" "),
  r.stack.join(" "), r.sites.map(s => s.host).join(" "),

  // Heroku app names are searchable from the project row.
  (r.heroku || []).map(h => h.name).join(" "),
  r.repos.map(x => x.nwo + " " + x.path).join(" ")].join(" ").toLowerCase();

function keep(r, q, owner) {
  if (q && !hay(r).includes(q)) return false;
  if (owner && r.owner !== owner) return false;
  if (active.has("site") && !r.sites.length) return false;
  if (active.has("note") && !r.note) return false;
  if (active.has("nonote") && r.note) return false;
  if (active.has("folder") && !r.folders.length) return false;
  if (active.has("dirty") && !r.repos.some(x => x.dirty)) return false;
  if (active.has("dead") &&
      !r.sites.some(s => s.live && !s.live.resolves)) return false;
  if (active.has("unsynced") &&
      !r.repos.some(x => x.sync && x.sync !== "current")) return false;
  if (active.has("owned") && r.ownership !== "owned") return false;
  // Drift: declared active, measured dormant or cold.
  if (active.has("drift") &&
      !(r.lifecycle === "active" && (r.tier === "dormant" || r.tier === "cold"))) return false;
  if (active.has("heroku") && !(r.heroku || []).length) return false;
  if (active.has("noheroku") && (r.heroku || []).length) return false;
  return true;
}

// HEROKU FILTERS. "Stale" means no deploy in a year.
const YEAR_AGO = new Date(Date.now() - 365 * 864e5).toISOString().slice(0, 10);
const hayApp = a => [a.name, a.team, a.state, a.stack, a.region, a.github || "",
  a.project ? (PROJ_NAME.get(a.project) || a.project) : "",
  (a.local_folders || []).join(" "),
  (a.addons || []).map(x => x.plan).join(" ")].join(" ").toLowerCase();

function keepApp(a, q, team) {
  if (q && !hayApp(a).includes(q)) return false;
  if (team && a.team !== team) return false;
  if (active.has("noproject") && a.project) return false;
  if (active.has("nofolder") && (a.local_folders || []).length) return false;
  // No repository means neither a linked GitHub repo nor a project.
  if (active.has("norepo") && (a.github || a.project)) return false;
  if (active.has("down") &&
      !(a.state === "down" || a.state === "suspended" || (a.crashed || []).length)) return false;
  if (active.has("waste") && a.state !== "resources-only") return false;
  // `resources-only`: add-ons are billed while no dyno runs.
  if (active.has("stale") && !(a.last_deploy_on && a.last_deploy_on < YEAR_AGO)) return false;
  if (active.has("oldstack") && !a.stack_superseded) return false;
  return true;
}

const chip = (text, kind) =>
  `<span class="chip${kind ? " " + kind : ""}"><span class="dot"></span>${E(text)}</span>`;

function repoLine(x, rules) {
  // A superseded, placeholder or moved repository is rendered faded and
  // struck through, and names its successor.
  const sub = ["superseded", "placeholder", "moved"].includes(x.status);
  const bits = [];
  if (x.visibility === "public") bits.push(chip("public"));
  if (x.archived) bits.push(chip(T("archived"), "warn"));
  if (x.fork) bits.push(chip(T("fork")));
  if (x.host === "bitbucket") bits.push(chip("bitbucket"));
  if (x.dirty) bits.push(chip(T("{n} unsaved", {n: x.dirty}), "danger"));
  // Sync states that put work at risk are danger; merely behind is a warning.
  const SYNC = {
    "behind":             [T("behind"), "warn"],
    "stale":              [T("behind"), "warn"],
    "behind-or-diverged": [T("behind or diverged"), "warn"],
    "diverged":           [T("diverged"), "danger"],
    "ahead":              [T("not pushed"), "danger"],
    "unpushed-and-remote-moved": [T("not pushed, remote moved on"), "danger"],
    "local-only-branch":  [T("branch only here"), "danger"],
    "unreachable":        [T("remote unreachable"), "danger"],
    "unknown":            [T("state unknown"), "warn"],
  };
  if (SYNC[x.sync]) {
    // The chip carries what is at stake: the unpushed count when measured.
    const [label, tone] = SYNC[x.sync];
    bits.push(chip(label + stakeText(x), tone));
  }
  if (x.checked_out && x.branch && x.checked_out !== x.branch)
    bits.push(chip(T("branch {name}", {name: x.checked_out}), "warn"));
  if (x.status === "superseded")
    bits.push(chip(T("superseded → {name}", {name: x.supersededBy}), "warn"));
  if (x.status === "placeholder") bits.push(chip(T("empty"), "warn"));
  if (x.status === "moved") bits.push(chip(T("moved → {name}", {name: x.movedTo}), "warn"));
  const rule = rules.find(s => s.startsWith(x.nwo + ":"));
  // The linking rule is shown only while the rules view is switched on.
  return `<div class="item"><div class="repo${sub ? " sub" : ""}">` +
    `<a class="nwo" href="${E(x.url)}" target="_blank" rel="noopener">${E(x.nwo)}</a>` +
    bits.join("") + `</div>` +
    (active.has("rules") && rule ? `<div class="rule">${E(rule.slice(x.nwo.length + 2))}</div>` : "") +
    `</div>`;
}

const NONE = '<span class="none">—</span>';
// Activity tiers, in the reader's language.
const TIER_LABEL = {active: T("active"), cooling: T("cooling"), dormant: T("dormant"),
                    cold: T("cold"), unknown: T("date unknown")};
// The registry's own enum words, in the reader's language; a value none of
// these names (a curated override, a newer collector) is shown as it is.
const LIFECYCLE_LABEL = {active: T("lifecycle@@active"), archived: T("lifecycle@@archived")};
// DECLARED AND MEASURED, said as two facts. The lifecycle is what the registry
// declares, the tier what the activity measured; printed bare side by side
// they read "active · active" (and the same stutter in Russian) when they agree
// and as noise when they do not. Agreement is said once; a disagreement
// labels both, since it is the fact worth seeing.
function stateLine(r) {
  const declared = labelOf(LIFECYCLE_LABEL, r.lifecycle) || "";
  const measured = TIER_LABEL[r.tier] || r.tier || "";
  if (declared && declared === measured) return T("{state} (declared and measured)", {state: declared});
  return [declared && T("declared {state}", {state: declared}),
          measured && T("measured {state}", {state: measured})].filter(Boolean).join(" · ");
}
// What a local-only folder is, where the row counts it: a git checkout with
// no remote, or with one on a host nothing here inventories.
const LOCAL_GIT_LABEL = {"no-remote": T("git, no remote"), elsewhere: T("git, remote not inventoried")};
const ANCHOR_LABEL = {repository: T("anchor@@repository"), "vault-folder": T("anchor@@notes-vault folder"),
                      "local-folder": T("anchor@@local folder"), organisation: T("anchor@@organisation")};
const ROLE_LABEL = {site: T("role@@site"), app: T("role@@app"), admin: T("role@@admin"), api: T("role@@API"),
                    backend: T("role@@backend"), wiki: T("role@@wiki"), docs: T("role@@docs"), bot: T("role@@bot"),
                    landing: T("role@@landing"), library: T("role@@library"), infra: T("role@@infrastructure"),
                    other: T("role@@other")};
const NOTE_STATE_LABEL = {proposed: T("conclusion@@proposed"), observed: T("conclusion@@observed"),
                          supported: T("conclusion@@supported"), contested: T("conclusion@@contested"),
                          stale: T("conclusion@@stale"), superseded: T("conclusion@@superseded"),
                          rejected: T("conclusion@@rejected"), archived: T("conclusion@@archived")};
const labelOf = (map, value) => map[value] || value || "";

// SPARKLINE: weekly commits as a solid line and sessions as a dashed one,
// on one shared scale. Weeks whose sessions were never measured are left out
// of the dashed line rather than drawn as zero, so "no work" and "not
// measured" stay apart.
function spark(weeks) {
  if (!weeks || !weeks.length) return "";
  const vals = weeks.map(w => w.c);
  const sess = weeks.map(w => (w.s == null ? null : w.s));
  const max = Math.max(...vals, ...sess.filter(v => v != null), 1);
  const total = vals.reduce((a, b) => a + b, 0);
  const stotal = sess.reduce((a, b) => a + (b || 0), 0);
  const measured = sess.some(v => v != null);
  const W = 72, H = 14, step = W / Math.max(vals.length - 1, 1);
  const line = (arr) => arr.map((v, i) => v == null ? null :
      `${(i * step).toFixed(1)},${(H - (v / max) * (H - 2)).toFixed(1)}`)
    .filter(Boolean).join(" ");
  const worked = weeks.filter(w => w.d != null && w.d > 0).length;
  const title = T("{weeks} wk, {commits} commits", {weeks: vals.length, commits: total}) +
    (measured ? ", " + T("{sessions} sessions, {worked} wk with work", {sessions: stotal, worked}) : "");
  return `<div class="spark" title="${title}">` +
    `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" aria-hidden="true">` +
    `<polyline points="${line(vals)}" fill="none" stroke="currentColor" stroke-width="1"/>` +
    (stotal ? `<polyline points="${line(sess)}" fill="none" stroke="currentColor"` +
              ` stroke-width="1" stroke-dasharray="2 2" opacity="0.55"/>` : "") +
    `</svg><span>${total}</span>` +
    (stotal ? `<span class="spark-sess">· ${T("{n} sessions", {n: stotal})}</span>` : "") +
    `</div>`;
}

// Plural forms come from the catalog: T("{n} projects", {n}) chooses the
// reader's language's form (Intl.PluralRules), so no renderer spells one.

// A plugin's caption is its manifest `label`, an English message id: the
// catalog translates the ones this distribution ships, and a third-party
// plugin's own label reads as its author wrote it. No label: the metric name.
const metricLabel = m => m.l ? T(String(m.l).trim()) : String(m.n || "").trim();

function metrics(list) {
  if (!list || !list.length) return "";
  // PLUGIN METRICS. The caption comes from the plugin's manifest and falls
  // back to the metric name; the unit is printed only when there is no
  // caption to carry it.
  return list.map(m => {
    const cap = metricLabel(m);
    const val = m.u === "bytes" ? bytes(m.v)
              : (m.l ? `${NUM(+m.v)}`
                     : `${NUM(+m.v)} ${E(m.u || "")}`.trim());
    // The change since the previous sample, when retention kept one. A
    // series with a single sample shows no delta at all, not "+0".
    let d = "";
    if (m.p != null && +m.p !== +m.v) {
      const up = +m.v > +m.p, diff = Math.abs(+m.v - +m.p);
      const shown = m.u === "bytes" ? bytes(diff) : NUM(diff);
      const was = m.u === "bytes" ? bytes(m.p) : NUM(+m.p);
      d = ` <span class="delta ${up ? "up" : "down"}" title="${T("was {value}", {value: was})}` +
          ` — ${E((m.pat || "").slice(0, 10))}">${up ? "↑" : "↓"}${shown}</span>`;
    }
    return `<div class="tier" title="${E(m.n)}">` +
           `<span class="cap">${E(cap)}</span> ${val}${d}</div>`;
  }).join("");
}

// WHAT IS AT STAKE in a checkout: the count of commits on no remote and the
// date of the newest, when the probe could count them.
function stakeText(x) {
  if (typeof x.unpushed === "number" && x.unpushed > 0)
    return ` · ${x.unpushed}` + (x.unpushedOn ? " " + T("from {date}", {date: x.unpushedOn}) : "");
  // Measured and found to hold nothing a remote lacks.
  if (x.nothing_exclusive) return " · " + T("nothing exclusive");
  return "";
}

function bytes(n) {
  if (!n) return "";
  const u = [T("B"), T("KB"), T("MB"), T("GB")]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toLocaleString(LOCALE, {maximumFractionDigits: n < 10 && i > 0 ? 1 : 0})} ${u[i]}`;
}

function row(r) {
  // THREE LINKS, ONE TAB STOP. The name, the counts and "Project details →"
  // all open the same panel; three stops per row made a small projects page 66
  // presses long. The name keeps the stop; the other two stay clickable.
  const link = `#${E(r.id)}`;
  const sites = r.sites.slice(0, 2).map(x =>
    `<a href="https://${E(x.host)}" target="_blank" rel="noopener">${E(x.host)}</a>` +
    (x.live && !x.live.resolves ? chip(T("not answering"), "danger") : "")).join(" ");
  const repos = r.repos.slice(0, 1).map(x => repoLine(x, r.rules || [])).join("");
  const dirty = r.repos.filter(x => x.dirty || ["ahead", "diverged", "unpushed-and-remote-moved", "local-only-branch"].includes(x.sync)).length;
  const traffic = (D.traffic || {})[r.id];
  return `<tr data-project="${E(r.id)}">
    <td data-label="${T("Project")}"><div class="name"><a class="plink" href="${link}">${E(r.name)}</a></div>
      <div class="desc">${E(r.description) || `<span class="none">${T("No description")}</span>`}</div>
      <div class="project-meta">${E(r.owner || T("Owner not stated"))} · ${E(r.lifecycle ? T("declared {state}", {state: labelOf(LIFECYCLE_LABEL, r.lifecycle)}) : T("status not stated"))}</div></td>
    <td data-label="${T("Activity")}"><div>${chip(TIER_LABEL[r.tier] || r.tier || T("activity@@not measured"))}</div>
      <div class="anchor mono">${E(r.last) || T("date not measured")}</div>${spark(r.weeks)}
      ${dirty ? chip(T("{n} repos need attention", {n: dirty}), "warn") : ""}</td>
    <td data-label="${T("Code and sites")}"><div class="resource-links">${repos}${sites}</div>
      <a class="plink anchor" href="${link}" tabindex="-1">${T("{n} repos", {n: r.repos.length})} · ${T("{n} sites", {n: r.sites.length})} · ${T("{n} folders", {n: r.folders.length})}${r.local_git ? " (" + LOCAL_GIT_LABEL[r.local_git] + ")" : ""}</a></td>
    <td data-label="${T("Hosting")}">${(r.heroku || []).length ? hostingGroups(r.heroku) : `<span class="none">${T("No linked apps")}</span>`}</td>
    <td data-label="${T("Audience / 30 days")}">${traffic && traffic.users_30d != null
      ? `<span class="mono">${NUM(traffic.users_30d)}</span><div class="anchor">${T("sum across properties")}${traffic.unknown_properties ? " · " + T("partial") : ""}</div>`
      : `<span class="none">${T("audience@@Not measured")}</span>`}
      <div class="anchor"><a class="plink" href="${link}" tabindex="-1">${T("Project details →")}</a></div></td>
  </tr>`;
}

// THE PROJECT PANEL: everything known about one project, opened from its row
// by the URL hash.
function detail(id) {
  const r = D.rows.find(x => x.id === id);
  const box = document.getElementById("panel");
  if (!id) { box.hidden = true; box.innerHTML = ""; return; }
  const closeHead = title => `
    <div class="dhead">
      <h2 id="panel-title">${E(title)}</h2>
      <button id="dclose" class="chip-btn" type="button" aria-label="${T("Close")}">${T("close")}</button>
    </div>`;
  const opened = () => {
    box.hidden = false;
    document.getElementById("dclose").onclick = () => { location.hash = ""; };
    box.scrollIntoView({block: "start"});
    // Focus moves into the panel, so a keyboard reader lands on what opened.
    const closeBtn = document.getElementById("dclose");
    if (closeBtn && closeBtn.focus) closeBtn.focus();
  };
  // A link to a project the registry does not hold says so, instead of
  // opening nothing: a blank panel reads as a broken page, not as a fact.
  if (!r) {
    box.innerHTML = closeHead(id.replace(/^project:/, "")) +
      `<p class="none" id="panel-missing">${T("This project is not in the registry: no row carries the id {id}. It may have been renamed or merged since the link was made.", {id: E(id)})}</p>`;
    opened();
    return;
  }
  const list = (items, empty, fn) => items && items.length
    ? `<ul class="dlist">${items.map(fn).join("")}</ul>`
    : `<p class="none">${empty}</p>`;
  // Without a store, the store-backed sections say so instead of "none".
  const gone = D.store_degraded
    ? `<p class="none">${E(T(D.store_degraded.text || D.store_degraded, D.store_degraded.args))}</p>` : "";
  box.innerHTML = `${closeHead(r.name)}
    <p class="dmeta">${E(labelOf(ANCHOR_LABEL, r.anchor))} · ${E(stateLine(r))}
      · ${T("last activity")} ${E(r.last) || "—"}</p>
    ${r.description ? `<p class="project-description">${E(r.description)}</p>` : ""}
    <h3>${T("What it is made of")}</h3>
    ${                                                                          
      list(r.repos, T("no repositories"), x => `<li>${repoLine(x, r.rules || [])}</li>`)}
    ${list(r.folders, T("no local folders"), f => `<li class="mono">${E(f)}</li>`)}
    ${list(r.sites, T("no sites declared"), s => `<li class="mono"><a href="https://${E(s.host)}" target="_blank" rel="noopener">${E(s.host)}</a>` +
        `<span class="anchor"> ${E((s.evidence || [])[0] || "")}</span></li>`)}
    <h3>${T("Hosting and environments")}</h3>
    ${(r.heroku || []).length ? hostingGroups(r.heroku) : `<p class="none">${T("No linked apps")}</p>`}
    <h3>${T("Technology and documentation")}</h3>
    <p>${(r.stack || []).map(x => chip(x)).join(" ") || T("Stack not measured")} · ${T("{n} notes", {n: r.wiki_notes || 0})}</p>
    <h3>${T("Product")}</h3>
    ${(r.products || []).length
      ? `<ul class="dlist">${r.products.map(p => `<li>${E(p.name)} — ${E(labelOf(ROLE_LABEL, p.role))}` +
          (p.kind === "suggested" ? ` <span class="anchor">${T("suggested by a shared domain, not a decision")}</span>` : "") +
          `</li>`).join("")}</ul>`
      : `<p class="none">${T("part of no product — group it in {file}", {file: "config/products.json"})}</p>`}
    <h3>${T("What happened")}</h3>
    ${gone || list(r.timeline, r.history_gap ? E(T(r.history_gap.text, r.history_gap.args)) : T("no commits in the window"),
      c => `<li><span class="mono">${E(c.at)}</span> ${E(c.what)}
            <span class="none">${E(c.who)}</span></li>`)}
    <h3>${T("What the observatory concluded")}</h3>
    ${gone || list(r.notes, T("no conclusions"),
      // Identical statements arrive folded by the builder (`fold_notes`):
      // one line, its newest date, and how many times it was drawn.
      n => `<li><span class="mono">${E(n.at)}</span> ${E(n.text)}` +
           (n.n > 1 ? ` <span class="anchor note-repeat" title="${T("{n} identical conclusions; the newest is shown", {n: n.n})}">×${n.n}</span>` : "") +
           `
            <span class="none">${E(labelOf(NOTE_STATE_LABEL, n.state))}${n.conf != null ? ", " + T("confidence {value}", {value: n.conf}) : ""}</span></li>`)}
    ${                                                                       
                                                          ""}
    <h3>${T("Analytics")}</h3>
    ${(() => {
      const tr = (D.traffic || {})[r.id];
      if (!tr) return `<p class="none">${T("no Google Analytics property is linked to this project — name it in {file} if there is one", {file: "config/ga4_properties.json"})}</p>`;
      return `<ul class="dlist">` + (tr.properties || []).map(g =>
        `<li><b>${g.users_30d == null ? T("not measured") : NUM(g.users_30d)}</b> — ${T("users summed across properties, 30 days")}${g.unknown_properties ? " · " + T("not measured: {n}", {n: g.unknown_properties}) : ""} · ` +
        `${T("{n} sessions", {n: Number(g.sessions_30d || 0)})} — ${E(g.name || "")}` +
        `<span class="none"> · ${E(RULE_LABEL[g.rule] || g.rule || "")}` +
        `${(g.hosts || []).length ? " · " + E(g.hosts.slice(0, 2).join(", ")) : ""}</span> ` +
        `<a href="${E(g.report_url)}" target="_blank" rel="noopener">GA4</a>` +
        (g.admin_url ? ` · <a href="${E(g.admin_url)}" target="_blank" rel="noopener">${T("admin")}</a>` : "") +
        `</li>`).join("") +
        (tr.search_console || []).map(s =>
          `<li>Search Console: <a href="${E(s.url)}" target="_blank" rel="noopener">${E(s.site)}</a></li>`).join("") +
        `</ul>`;
    })()}
    <h3>${T("Keys")}</h3>
    ${(() => {
      // Names, kinds and places only — the payload holds no value to show.
      const ks = (D.keys || {})[r.id] || [];
      if (!ks.length) return `<p class="none">${T("no credential in the registry is linked to this project — unowned ones are listed on the")} <a href="creds.html">${T("keys page")}</a></p>`;
      const KIND_LABEL = {"llm-api-key": T("LLM key"), "machine-secret": T("machine secret"),
                          "project-secret": T("store slot"), "project-secret-file": T("file beside the code"),
                          "leaked-untracked": T("known from a leak"), "env-file": T("in the project's .env")};
      // A key file tracked by git is danger; one merely not ignored is a
      // warning.
      const GIT_LABEL = {tracked: chip(T("file in git"), "danger"), loose: chip(T("not in .gitignore"), "warn"), ignored: "", "no-repo": ""};
      return `<ul class="dlist">` + ks.map(k =>
        `<li><a class="mono" href="${E(k.href || ("creds.html#c-" + k.slug))}">${E(k.name || "")}</a>` +
        `<span class="none"> · ${E(KIND_LABEL[k.kind] || k.kind || "")}${k.env ? " · " + E(k.env) : ""}` +
        `${k.where ? " · " + E(k.where) : ""}</span>` +
        (k.leaked ? ` ${chip(T("leak not closed"), "danger")}` : "") +
        (k.kind !== "env-file" && !k.signed ? ` ${chip(T("not signed"), "warn")}` : "") +
        (k.kind === "env-file" ? ` ${GIT_LABEL[k.git] || ""}` : "") +
        `</li>`).join("") + `</ul>`;
    })()}
    <h3>${T("What the plugins measured")}</h3>
    ${gone || (r.metrics && r.metrics.length
        ? `<ul class="dlist">${r.metrics.map(m => `<li><span class="mono">${E(metricLabel(m))}</span> ` +
            `${m.u === "bytes" ? bytes(m.v)
                : NUM(+m.v) + (m.l ? "" : " " + E(m.u || ""))}` +
            `${m.p != null && +m.p !== +m.v
               ? ` <span class="delta ${+m.v > +m.p ? "up" : "down"}">${+m.v > +m.p ? "↑" : "↓"}` +
                 `${m.u === "bytes" ? bytes(Math.abs(m.v - m.p)) : NUM(Math.abs(m.v - m.p))}</span>`
               : ""}</li>`).join("")}</ul>`
        : `<p class="none">${T("no measurements")}</p>`)}`;
  opened();
}

let PANEL_OPENER = null;
document.addEventListener("click", ev => {
  const a = ev.target && ev.target.closest && ev.target.closest('a.plink[href^="#project:"]');
  if (!a) return;
  // On another page the project panel lives on the projects page, so the
  // link navigates there with the same hash.
  if (PAGE && PAGE !== "projects") {
    ev.preventDefault();
    location.href = "projects.html" + a.getAttribute("href");
    return;
  }
  PANEL_OPENER = a;
});
window.addEventListener("hashchange", () => {
  if (!location.hash && PANEL_OPENER && PANEL_OPENER.focus) { PANEL_OPENER.focus(); PANEL_OPENER = null; }
});
function fromHash() {
  // The hash names either a tab or a project.
  const raw = typeof location !== "undefined" ? (location.hash || "") : "";
  const id = decodeURIComponent(raw.replace(/^#/, ""));
  if (["projects", "heroku", "domains", "creds", "env"].includes(id)) {
    detail("");
    if (id !== tab) selectTab(id);
    return;
  }
  detail(id.startsWith("project:") ? id : "");
}
addEventListener("hashchange", fromHash);
addEventListener("keydown", e => { if (e.key === "Escape" && location.hash) location.hash = ""; });

// WHAT NARROWS THE VIEW, stated beside the count. A table showing fewer rows
// than exist must say by what: the pressed chips of this tab (and which came
// from a link), the selector and the search text. `narrowing()` reads only
// the chips inside `.seg`, so a view switch is never counted as a filter.
function narrowing() {
  const seg = document.getElementById("seg-" + tab);
  const chips = seg ? [...seg.querySelectorAll('.chip-btn[data-f][aria-pressed="true"]')]
    .map(b => b.textContent.trim() + (b.dataset.fromUrl ? " " + T("(from a link)") : "")) : [];
  const q = (document.getElementById("q") || {}).value || "";
  const s = (sel && sel.value && sel.selectedIndex >= 0) ? sel.options[sel.selectedIndex].text : "";
  return { chips, q: q.trim(), sel: s };
}
function narrowingText(n) {
  const bits = [];
  if (n.chips.length) bits.push(T("filters: {list}", {list: n.chips.map(E).join(", ")}));
  if (n.sel) bits.push(T("selected: {value}", {value: E(n.sel)}));
  if (n.q) bits.push(T("search: “{text}”", {text: E(n.q)}));
  return bits.join(" · ");
}
function filterLine(shown, total, unit) {
  const what = narrowingText(narrowing());
  // `unit` is a catalog id with a plural form, chosen by the total. It is the
  // `of@@` form: the count follows "of", which in Russian takes the genitive
  // plural for 234 records, not the nominative the noun has on its own.
  return `<div class="filters" role="status">${T("Showing {shown} of {total}", {shown: `<b>${NUM(shown)}</b>`, total: unit ? T(unit, {n: total}) : NUM(total)})}` +
    (what ? ` · ${what} · <button class="chip-btn" type="button" data-clear>${T("reset")}</button>` : "") + `</div>`;
}
// GROUP FOLDING. Groups start folded, and open by themselves as soon as
// anything narrows the view — a search hit hidden inside a folded group would
// read as "not found".
function foldOpen(defaultOpen) {
  const n = narrowing();
  return !!(defaultOpen || n.q || n.chips.length || n.sel);
}
// ONE FOLD CONTROL FOR EVERY GROUPED TABLE. Only the ENV groups folded; the
// other five tables printed a heading that looked like the same control and
// did nothing, and `open` was accepted and ignored. The heading is the
// button now, everywhere, and the document-level `.grp-fold` handler toggles
// its group; `inner` must be phrasing content, as a button's is.
function grpHead(colspan, inner, open) {
  return `<tr class="group-heading"><th colspan="${colspan}" scope="colgroup">` +
    `<button class="grp-fold" type="button" aria-expanded="${open ? "true" : "false"}">${inner}</button></th></tr>`;
}
// A whole group: its body folded or open, its heading, its rows.
function grpBody(colspan, inner, open, rows, attrs = "") {
  return `<tbody class="grp${open ? "" : " folded"}"${attrs}>` + grpHead(colspan, inner, open) + rows + `</tbody>`;
}
// A hash naming a row reveals it: its folded group opens and the row
// scrolls into view. Finding and project hashes are handled elsewhere.
let REVEALED = "";
function revealHash() {
  const raw = typeof location !== "undefined" ? (location.hash || "") : "";
  if (!raw || raw.startsWith("#f-") || raw.startsWith("#project:")) return;
  // Once per hash, so a re-render does not keep scrolling the reader back.
  if (raw === REVEALED) return;
  const id = decodeURIComponent(raw.slice(1));
  let target = document.getElementById(id);
  // A link to a whole env FILE (`#e-<folder>/.env`) names no single row: every
  // variable row of that file is the target, and each is marked, so the
  // reader sees which rows the link meant rather than one row scrolled to.
  let rows = [];
  document.querySelectorAll("tr.hl").forEach(tr => tr.classList.remove("hl"));
  if (!target && id.startsWith("e-") && typeof CSS !== "undefined" && CSS.escape) {
    rows = [...document.querySelectorAll('tr[data-file="' + CSS.escape(id.slice(2)) + '"]')];
    target = rows[0] || null;
  }
  if (!target) return;
  for (const el of rows.length ? rows : [target]) {
    if (rows.length) el.classList.add("hl");
    const body = el.closest && el.closest("tbody.grp.folded");
    if (body) {
      body.classList.remove("folded");
      const b = body.querySelector(".grp-fold");
      if (b) b.setAttribute("aria-expanded", "true");
    }
  }
  // `start`, with the row's `scroll-margin-top` (workspace.css) clearing the
  // sticky table header: centring left a row near the end of a table under it.
  if (target.scrollIntoView) target.scrollIntoView({block: "start"});
  REVEALED = raw;
}
addEventListener("hashchange", () => { REVEALED = ""; revealHash(); });
// Once more when the page has finished loading: fonts and the stylesheet can
// move a row after the first render placed it, and the hash must still land.
addEventListener("load", () => { REVEALED = ""; revealHash(); });
// COLUMN SORTING. A header click cycles ascending, descending, none. The
// choice is kept per page for the session only, and empty values always sort
// last whichever the direction.
const SORT = (() => {
  try { return JSON.parse(sessionStorage.getItem("observatory.sort." + PAGE) || "{}") || {}; }
  catch (e) { return {}; }
})();
function sortTh(label, key) {
  const dir = SORT.key === key && SORT.dir ? SORT.dir : "none";
  return `<th aria-sort="${dir}" data-sort="${E(key)}"><button class="sort" type="button" title="${T("sort")}">${label}</button></th>`;
}
function sortInPlace(list, getters) {
  const g = SORT.key && getters[SORT.key];
  if (!g) return list;
  const m = SORT.dir === "descending" ? -1 : 1;
  const copy = [...list].sort((a, b) => {
    const x = g(a), y = g(b);
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    return (x < y ? -1 : x > y ? 1 : 0) * m;
  });
  list.splice(0, list.length, ...copy);
  return list;
}
document.addEventListener("click", ev => {
  const th = ev.target.closest && ev.target.closest("th[data-sort]");
  if (!th) return;
  const key = th.dataset.sort;
  const cur = SORT.key === key ? SORT.dir : "none";
  const next = cur === "none" ? "ascending" : cur === "ascending" ? "descending" : "none";
  SORT.key = next === "none" ? "" : key;
  SORT.dir = next === "none" ? "" : next;
  try { sessionStorage.setItem("observatory.sort." + PAGE, JSON.stringify(SORT)); } catch (e) {                      }
  render();
  // The render replaced the header that held focus, and focus fell to <body>:
  // a keyboard reader who pressed Enter was thrown to the top of the page.
  // Focus returns to the same column's new button, as the sort select does.
  const again = document.querySelector(`th[data-sort="${String(key).replace(/["\\]/g, "\\$&")}"] .sort`);
  if (again && again.focus) again.focus();
});
const lower = s => String(s || "").toLowerCase() || null;
const dateOr = s => (s ? String(s) : null);
const numOr = n => (typeof n === "number" ? n : (n == null || n === "" ? null : Number(n)));
// `empty` is [sentence, commands]: how this page's rows arrive. A page with no
// row at all says that and gives the command, like a page never scanned —
// "the registry holds no row of this kind" alone left the reader nowhere.
function nothingFound(total, note, empty) {
  if (!total && empty) return notScanned(empty[0], empty[1]);
  const what = narrowingText(narrowing());
  // An empty result says whether a filter caused it or the registry is empty.
  if (!what) return `<p class="empty"${total ? "" : " data-no-rows"}>${total ? T("Nothing found") : T("Empty here — the registry holds no row of this kind")}${note ? ". " + note : ""}</p>`;
  return `<p class="empty">${T("Nothing found with this narrowing — {what}.", {what})} ` +
    `<button class="chip-btn" type="button" data-clear>${T("reset")}</button>${note ? "<br>" + note : ""}</p>`;
}
document.addEventListener("click", ev => {
  const b = ev.target && ev.target.closest && ev.target.closest("[data-clear]");
  if (!b) return;
  const seg = document.getElementById("seg-" + tab);
  if (seg) seg.querySelectorAll('.chip-btn[data-f]').forEach(c => { c.setAttribute("aria-pressed", "false"); delete c.dataset.fromUrl; });
  if (active) active.clear();
  const q = document.getElementById("q"); if (q) q.value = "";
  if (sel) sel.value = "";
  render();
  // The reset button lived in the empty state the render just removed, so
  // focus would fall to <body>. It moves to the first control of the narrowing
  // it cleared: the search box, or the first filter chip where there is none.
  const next = q || (seg && seg.querySelector(".chip-btn[data-f]"));
  if (next && next.focus) next.focus();
});

function render() {
  const search = document.getElementById("q");
  if (search) search.placeholder = T(({projects: "Search: project, description, repository, folder or domain",
    heroku: "Search: app, project or folder", domains: "Search: domain, registrar or project",
    creds: "Search: key, provider or project", env: "Search: variable, file or project",
    mcp: "Search: server, agent or address", traffic: "Search: property, site or project"})[tab] || "Search");
  const out = drawTab();
  // A FILTER BAR OVER NOTHING narrows nothing: while the page has no row at
  // all (never scanned, or a registry with none of this kind) it is hidden.
  const shown = document.getElementById("out");
  const controls = document.querySelector(".controls");
  if (controls && shown) controls.classList.toggle("no-rows",
    /class="empty not-scanned"|data-no-rows/.test(String(shown.innerHTML || "")));
  const tools = document.getElementById("list-tools");
  const table = document.getElementById("out");
  if (tools && table) {
    const fields = [...table.querySelectorAll("th[data-sort]")].map(h => [h.dataset.sort, h.textContent]);
    tools.innerHTML = fields.length ? `<label>${T("Sort")} <select id="list-sort" aria-label="${T("Sort the list")}">` +
      `<option value="">${T("Original order")}</option>` + fields.map(([key, label]) =>
        ["ascending", "descending"].map(dir => '<option value="' + E(key + ":" + dir) + '"' +
          (SORT.key === key && SORT.dir === dir ? ' selected' : '') + '>' + E(label) +
          (dir === "ascending" ? ' ↑' : ' ↓') + '</option>').join("")).join("") + '</select></label>' : '';
    const select = document.getElementById("list-sort");
    if (select) select.onchange = () => {
      [SORT.key, SORT.dir] = select.value ? select.value.split(":") : ["", ""];
      try { sessionStorage.setItem("observatory.sort." + PAGE, JSON.stringify(SORT)); } catch (e) {}
      render();
      document.getElementById("list-sort").focus();
    };
  }
  revealHash();                                                                  
  return out;
}
function drawTab() {
  if (PAGE && !TABLE_PAGES.includes(PAGE)) return;                                             
  // Tab counters are refreshed on every render.
  const setN = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  setN("n-projects", D.rows.length);
  // Each counter is the number its tab shows when it opens, in the unit of
  // that tab's own "Showing N of …" line (the split pages' badges:
  // `shell.counts_of`).
  setN("n-mcp", D.mcp ? (D.mcp.servers || []).length : "—");
  setN("n-domains", domainRows().length);
  setN("n-heroku", APPS.length || "—");
  setN("n-creds", CREDS.length || "—");
  setN("n-env", D.env ? ENVV.filter(e => envLive(e) && envNotable(e)).length : "—");
  if (tab === "heroku") return renderHeroku();
  if (tab === "domains") return renderDomains();
  if (tab === "mcp") return renderMcp();
  if (tab === "traffic") return renderTraffic();
  if (tab === "creds") return renderCreds();
  if (tab === "env") return renderEnv();
  const q = document.getElementById("q").value.trim().toLowerCase();
  const owner = sel.value;
  const rows = D.rows.filter(r => keep(r, q, owner));
  const out = document.getElementById("out");
  if (!rows.length) { out.innerHTML = nothingFound(D.rows.length); return; }
  sortInPlace(rows, {name: r => lower(r.name), activity: r => dateOr(r.last)});
  out.innerHTML = filterLine(rows.length, D.rows.length, "of@@{n} projects") + `<div class="card"><table class="project-summary">
    <colgroup><col style="width:26%"><col style="width:17%"><col style="width:23%"><col style="width:21%"><col style="width:13%"></colgroup>
    <thead><tr>${sortTh(T("Project"), "name")}${sortTh(T("Activity"), "activity")}
      <th>${T("Code and sites")}</th><th>${T("Hosting")}</th><th>${T("Audience / 30 days")}</th></tr></thead>
    <tbody>${rows.map(row).join("")}</tbody></table></div>`;
}

// ACTIONS. The page is read-only when opened as a file. Served by the local
// server, it carries a session token and can call that server's API; each
// action is then a button, and the same action is always available as a
// command to copy.

// The token comes from a meta tag the server writes into the page it serves;
// a page opened from disk has none.
const TOKEN = (document.querySelector('meta[name="observatory-token"]') || {}).content;
// LIVE only over http(s) with a token: every other way of opening the page
// stays copy-only.
const LIVE = typeof location !== "undefined"
  && String(location.protocol || "").startsWith("http") && !!TOKEN;

// A short confirmation at the bottom of the screen, gone after four seconds.
function toast(text) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 4000);
}

// Copy to the clipboard, with a textarea fallback where the async clipboard
// API is unavailable or refused (a file:// page, an older browser).
async function copyText(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_) {                                                                   }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.top = "-1000px";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
  document.body.removeChild(ta);
  return ok;
}

// WHAT WAS COPIED, in one line: the button's own label and the END of the
// command. A command starts with the interpreter and the engine's path, which
// are the same on every button, so its head told the reader nothing; the tool,
// its verb and its arguments are at the tail.
function copiedWhat(label, text) {
  const line = String(text || "").replace(/\s+/g, " ").trim();
  const tail = line.length > 56 ? "…" + line.slice(-56) : line;
  let name = String(label || "").replace(/\s+/g, " ").trim();
  // A GENERIC label says nothing about what was copied: the toast read
  // "copied: copy the command — …", repeating the button. Only a label that
  // names the thing ("Command: silence") is worth keeping beside the text.
  if ([T("copy the command"), T("copy")].includes(name)) name = "";
  return name && name !== line && tail ? name + " — " + tail : (name || tail);
}
// Any element with `data-copy` copies its command on click. A button marked
// `data-copy-quiet` copies a VALUE, and the toast names only its label: a
// secret printed in a toast is a secret on the screen.
document.addEventListener("click", ev => {
  const btn = ev.target && ev.target.closest && ev.target.closest("[data-copy]");
  if (!btn) return;
  const what = btn.dataset.copyQuiet ? String(btn.textContent || "").trim() : copiedWhat(btn.textContent, btn.dataset.copy);
  copyText(btn.dataset.copy).then(ok =>
    toast(ok ? T("copied: {what}", {what}) : T("clipboard unavailable — copy it from the hint")));
});
// Credential action buttons, handled in one place.
document.addEventListener("click", ev => {
  const btn = ev.target && ev.target.closest && ev.target.closest("[data-act][data-cred]");
  if (!btn || btn.disabled) return;
  const c = CREDS.find(x => x.id === btn.dataset.cred);
  if (!c) { toast(T("row not found — rescan")); return; }
  credAction(btn, c);
});

// THREE OUTCOMES, not two (PB-038). A refusal the server explained is
// definite: nothing happened, and trying again is safe. A timeout, a dropped
// connection, an unreadable answer to success, or a server fault after the
// action began is UNCERTAIN: the provider may already have revoked or minted,
// and "failed" would invite a second mint. Only the first may say "failed".
class Uncertain extends Error {}
const CALL_TIMEOUT_MS = 60000;
async function call(action, body, ms = CALL_TIMEOUT_MS) {
  const ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
  const timer = ctl ? setTimeout(() => ctl.abort(), ms) : null;
  let r;
  try {
    r = await fetch("/api/" + action, {
      method: "POST",
      // The token authorises the call; the caller header names which page made
      // it, for the server's own journal (a label, never an authority:
      // docs/design/ACCESS.md).
      headers: {"Content-Type": "application/json", "X-Observatory-Token": TOKEN,
                "X-Observatory-Caller": "page:" + (document.body.dataset.page || "index")},
      body: JSON.stringify(body),
      signal: ctl ? ctl.signal : undefined,
    });
  } catch (_) {
    throw new Uncertain(ctl && ctl.signal.aborted ? T("no answer within {n} s", {n: Math.round(ms / 1000)})
                                                  : T("the connection dropped"));
  } finally {
    if (timer) clearTimeout(timer);
  }
  let d = null;
  try { d = await r.json(); } catch (_) { d = null; }
  if (r.ok) {
    if (!d) throw new Uncertain(T("an answer arrived but cannot be read"));
    return d;
  }
  const said = (d && d.error) || ("HTTP " + r.status);
  // 502 is the provider's own refusal, relayed; any other 5xx is a fault that
  // may have struck after the action took effect.
  if (r.status >= 500 && r.status !== 502) throw new Uncertain(said);
  throw new Error(said);
}

// One credential action: confirm what cannot be undone, ask for what the
// action needs, call the server, and report the outcome.
function credAction(btn, c) {
  const act = btn.dataset.act;
  const ask = act === "revoke"
    ? T("Revoke key {name}? It stops working immediately and for good.", {name: c.label})
    : act === "limit"
      ? T("New monthly ceiling for {name}:", {name: c.name || c.label})
      : act === "leak"
        ? T("Where was this value exposed? One line — a transcript, a log, a screenshot:")
        : act === "rotate-key"
          ? T("Rotate {name}? The door mints a new key, delivers it to the same place and deletes the old one.", {name: c.name || c.label})
          : act === "disable"
            ? T("Disable {name}? It stops spending until “enable”.", {name: c.name || c.label})
            : null;
  if ((act === "revoke" || act === "rotate-key" || act === "disable") && !confirm(ask)) return;
  // Keys managed by name use `name`; the rest are addressed by label.
  let body = ["disable", "enable", "rotate-key"].includes(act)
    ? {name: c.name || c.label} : {label: c.label};
  if (act === "limit") {
    const v = prompt(ask, String(c.limit || 100));
    if (v === null) return;
    body = {label: c.label, limit: Number(v), limit_reset: "monthly"};
  }
  // SIGNING: a purpose and the evidence for it are required; the owner is
  // optional.
  if (act === "annotate") {
    const purpose = prompt(T("What is {name} for? One sentence:", {name: c.name || c.label}), "");
    if (!purpose || !purpose.trim()) return;
    const evidence = prompt(T("How is this known — a config, an email, a conversation, a file:"), "");
    if (!evidence || !evidence.trim()) return;
    const owner = prompt(T("Who is responsible for it (a person or a team, may be empty):"), "") || "";
    body = {id: c.id, purpose: purpose.trim(), evidence: evidence.trim(),
            owner: owner.trim()};
  }
  // MINTING a new key: its name at the provider, a monthly ceiling and
  // where it is delivered. The value is delivered by the server and never
  // reaches this page.
  if (act === "mint") {
    const name = prompt(T("The key's name at the provider (only the ledger sees it):"), "");
    if (!name || !name.trim()) return;
    const limit = prompt(T("Monthly ceiling for {name}, in dollars:", {name: name.trim()}), "10");
    if (limit === null || !(Number(limit) > 0)) return;
    const dest = prompt(T("Where to deliver it — observatory, claude-mem or vault:<project>/<env>/<NAME>:"),
                        T("vault:<project>/prod/OPENROUTER_API_KEY"));
    if (!dest || !dest.trim()) return;
    body = {name: name.trim(), limit: Number(limit), destination: dest.trim()};
  }
  // RECORDING A LEAK: where the value was seen, in one line.
  if (act === "leak") {
    const where = prompt(ask, "");
    if (!where || !where.trim()) return;
    body = {project: c.vault_project, env: c.env, name: c.name, where: where.trim()};
  }
  btn.disabled = true; btn.textContent = "…";
  call(act, body)
    .then(d => { toast(act === "annotate" ? T("signed — rescan")
                       : act === "revoke" ? T("revoked")
                       : act === "leak" ? T("marked as leaked — rotate it at the provider")
                       : act === "mint" ? T("minted and delivered to {place}", {place: d.destination})
                       : T("ceiling {value}/mo", {value: d.limit}));
                 btn.textContent = T("done · rescan"); })
    .catch(e => {
      if (e instanceof Uncertain) {
        // Kept disabled: repeating an action whose first attempt may have
        // landed is how one mint becomes two. A rescan says what happened.
        btn.textContent = T("outcome unknown · check");
        btn.title = T("Outcome unknown: {reason}", {reason: e.message});
        toast(T("outcome unknown ({reason}) — rescan and check before repeating", {reason: e.message}).slice(0, 160));
        return;
      }
      btn.disabled = false; btn.textContent = T("failed");
      toast(String(e.message).slice(0, 120));
    });
}

// A finding's subject becomes a link to the row it is about, on the page
// that holds that kind of row — resolved by the builder against the rows the
// pages render (dashboard/subject_links.py), so a subject with no row stays
// text. A population ("estate:…") links to its page, whose title is a message id.
function subjectHref(f) {
  if (!f || !f.href) return null;
  return [f.href, f.href_page ? T(f.href_label) : String(f.href_label || "")];
}
// The one slug rule shared by row ids and the links to them.
function anchorSlug(s) { return String(s).replace(/[^A-Za-z0-9_.:/-]+/g, "-"); }

// Credentials read by a provider tool have a "door": the tool that can
// issue, list and revoke them.
const doorOf = c => {
  const m = /^tools\/(openrouter|cloudflare)\.py$/.exec(c.read_by || "");
  return m ? m[1] : null;
};

// THE VERBS OF A CREDENTIAL: label, command, and the live action when the
// server can perform it (null means copy-only). Every verb is a command a
// reader can run in a terminal, so the page never becomes the only way to
// act. Placeholders in capitals are for the reader to fill in.
const ISSUE_CMD = {
  openrouter: toolCommand("openrouter.py", ["issue", "--name", "KEY_NAME", "--limit", "AMOUNT", "--to", "vault:PROJECT/prod/NAME"]),
  cloudflare: toolCommand("cloudflare.py", ["issue", "--account", "ACCOUNT_ID", "--preset", "analytics"]),
};
function credVerbs(c) {
  const door = doorOf(c);
  const n = c.name || c.label || "";
  const sign = [T("sign…"), toolCommand("sign_credential.py", ["set", c.id, "--purpose", "…", "--evidence", "…"]), "annotate"];
  if (c.kind === "llm-api-key")
    return [
      sign,
      [T("ceiling…"), toolCommand("openrouter.py", ["limit", n, "--set", "AMOUNT"]), "limit"],
      [c.disabled ? T("enable") : T("disable"), toolCommand("openrouter.py", [c.disabled ? "enable" : "disable", n]), c.disabled ? "enable" : "disable"],
      [T("rotate"), toolCommand("openrouter.py", ["rotate", n, ...(c.leaked ? ["--leaked"] : [])]), "rotate-key"],
      [T("revoke"), toolCommand("openrouter.py", ["revoke", n]), "revoke"],
    ];
  if (door)
    return [sign,
      [T("ping"), toolCommand(door + ".py", ["ping"]), null],
      [T("what was minted"), toolCommand(door + ".py", ["list"]), null],
      [T("mint…"), ISSUE_CMD[door], door === "openrouter" ? "mint" : null],
    ];
  if (c.vault_project) {
    const slot = [c.vault_project, c.env, c.name];
    const settle = [T("close the leak…"), toolCommand("vault.py", ["settle", ...slot, "--how", "…", "--revocation-evidence", "…", "--consumer-evidence", "…"]), null];
    const rotate = [T("rotate from a file…"), privateInput(toolCommand("vault.py", ["rotate", ...slot])), null];
    if (c.known_only_from_the_leak)
      return [sign, settle, [T("create the slot from a file…"), privateInput(toolCommand("vault.py", ["put", ...slot])), null]];
    return c.leaked ? [sign, settle, rotate]
      : [sign, [T("record a leak…"), toolCommand("vault.py", ["leak", ...slot, "--where", "…"]), "leak"], rotate];
  }
  if (c.kind === "project-secret-file") {
    const owner = (c.used_by || [])[0];
    const vaultProject = owner ? (PROJ_NAME.get(owner) || String(owner).split(":")[1]) : c.in_project;
    const slotName = String(c.name).replace(/^\.+/, "").replace(/\.[A-Za-z0-9]+$/, "")
      .replace(/[^A-Za-z0-9]+/g, "_").toUpperCase();
    const file = projectFile(c.in_project + "/" + c.path);
    return [sign,
      [T("into the store"), toolCommand("vault.py", ["put", vaultProject, "local", slotName]) + " < " + shellArg(file), null],
      [T("what is it"), "ls -l -- " + shellArg(file), null]];
  }
  return [sign, [T("what is it"), "ls -l -- " + shellArg(secretFile(n)), null]];
}

const CRED_SECTIONS = [
  ["door", T("Doors"), T("admin stashes: everything else is minted from them; the value is handed to nobody")],
  ["issued", T("Issued keys"), T("the door's ledger: name at the provider, ceiling, spend, date minted")],
  ["slot", T("Project secrets"), T("store slots — the value lives in the vault and travels only through stdin")],
  ["projfile", T("Secrets beside the code"),
   T("files in the project's own secrets/ folder; “into the store” moves the value into the vault through stdin, so any project can use it")],
  ["machine", T("Machine secrets"), T("files this machine's collectors and plugins authenticate with")],
];
const credSection = c => doorOf(c) ? "door"
  : c.kind === "llm-api-key" ? "issued"
  : c.kind === "project-secret-file" ? "projfile"
  : c.vault_project ? "slot" : "machine";

const credCmd = c => (credVerbs(c)[0] || ["", ""])[1];

const hayCred = c => [c.id, c.name || "", c.provider || "", c.kind, c.serves || "",
  (c.used_by || []).join(" "), c.label || ""].join(" ").toLowerCase();

function keepCred(c, q, section) {
  if (q && !hayCred(c).includes(q)) return false;
  if (section && (CRED_SECTIONS.find(s => s[1] === section) || [])[0] !== credSection(c))
    return false;
  if (active.has("c-leaked") && !c.leaked) return false;
  if (active.has("c-unclaimed") && (c.used_by || []).length) return false;
  // Shared: used by two or more projects, so a rotation touches all of them.
  if (active.has("c-shared") && (c.used_by || []).length < 2) return false;
  if (active.has("c-norotate") && c.rotated_on) return false;
  if (active.has("c-disabled") && !c.disabled) return false;
  if (active.has("c-untracked") && c.kind !== "leaked-untracked") return false;
  return true;
}

function renderCreds() {
  const out = document.getElementById("out");
  if (!D.creds) {
    out.innerHTML = notScanned(T("Credentials were not scanned. Add a key to this workspace's vault (the value on stdin), then rebuild:"),
      [privateInput(toolCommand("vault.py", ["put", "PROJECT", "local", "NAME"])), fullCommand("local")]);
    return;
  }
  const q = document.getElementById("q").value.trim().toLowerCase();
  const rows = CREDS.filter(c => keepCred(c, q, sel.value));
  if (!rows.length) {
    out.innerHTML = nothingFound(CREDS.length, "", [T("No key is tracked yet. Put one in this workspace's vault (the value on stdin), then rebuild:"),
      [privateInput(toolCommand("vault.py", ["put", "PROJECT", "ENV", "NAME"])), fullCommand("local")]]);
    return;
  }
  const cell = (label, html, cls) =>
    `<td data-label="${label}"${cls || html === NONE ? ` class="${
      [cls, html === NONE ? "e" : ""].filter(Boolean).join(" ")}"` : ""}>${html}</td>`;
  // A ceiling is dollars at the provider (the door mints it "in dollars"),
  // and so is its spend; the reset period is a word the reader's language has.
  const RESET_LABEL = {daily: T("reset@@daily"), weekly: T("reset@@weekly"), monthly: T("reset@@monthly")};
  const usd = v => "$" + Number(v).toLocaleString(LOCALE, {maximumFractionDigits: 3});
  const cap = c => c.limit == null ? NONE
    : `<span class="mono">${E(usd(c.limit))}</span>` +
      `<div class="tier">${E(c.limit_reset ? labelOf(RESET_LABEL, c.limit_reset) : T("no reset"))}` +
      (c.limit_reset ? "" : " " + chip(T("lifetime"), "warn")) +
      `</div><div class="anchor">${T("spent {value}", {value: "$" + (+c.usage || 0).toFixed(3)})}</div>`;
  const who = c => (c.used_by || []).length
    ? (c.used_by || []).map(p =>
        `<a class="plink" href="#${E(p)}">${E(p.split(":")[1])}</a>`).join(", ") +
      ((c.used_by || []).length > 1
        ? `<div class="tier">${chip(T("shared · a rotation touches every one"), "warn")}</div>` : "")
    // No project uses it: name the tool that reads it, or say it is nobody's.
    : c.read_by
      ? `<span class="mono">${E(c.read_by)}</span><div class="anchor">${T("reads it")}</div>`
      : `<span class="unlinked" title="${E(c.unclaimed_reason || "")}">${T("unowned")}</span>`;
  const state = c => {
    const bits = [];
    if (c.leaked) bits.push(chip(T("leak not closed"), "danger"));
    if (c.disabled) bits.push(chip(T("disabled"), "warn"));
    if (c.kind === "leaked-untracked") bits.push(chip(T("not in the store"), "warn"));
    // A key file sitting in a checkout: its git state and file mode matter.
    if (c.git === "tracked") bits.push(chip(T("in git"), "danger"));
    if (c.git === "loose") bits.push(chip(T("not ignored"), "warn"));
    if (c.kind === "project-secret-file" && String(c.mode).slice(-2) !== "00")
      bits.push(chip(T("mode {mode}", {mode: c.mode}), "warn"));
    if (!bits.length) bits.push(chip(T("in order"), "ok"));
    // Where a leaked value was seen, shortened, with the whole text on hover.
    const w = String(c.leaked_where || "");
    return bits.join(" ") + (c.leaked && w
      ? `<div class="anchor" title="${E(w)}">${E(w.slice(0, 110))}${w.length > 110 ? "…" : ""}</div>`
      : "");
  };
  sortInPlace(rows, {name: c => lower(c.name)});
  const groups = new Map();
  rows.forEach(c => { const k = SORT.key ? "sorted" : credSection(c);
    if (!groups.has(k)) groups.set(k, []); groups.get(k).push(c); });
  const row = c => `<tr id="c-${E(String(c.id).replace(/^credential:/, "").replace(/[^A-Za-z0-9_.-]+/g, "-"))}">
    <td data-label="${T("Credential")}"><div class="name">${E(c.name || c.id)}</div>
      ${                                                                   
                                                                            ""}
      <div class="anchor mono" title="${E(c.id)}">${E(c.label
        || [c.vault_project, c.env].filter(Boolean).join("/") || c.id)}</div>
      ${c.provider ? `<div class="anchor">${E(c.provider)}${c.serves ? " · " + E(c.serves) : ""}</div>` : ""}
      ${c.identity ? `<div class="anchor mono">${E(c.identity)}</div>` : ""}
      ${                                                                        
                                                                     ""}
      ${c.signature && c.signature.purpose
        ? `<div class="tier">${E(c.signature.purpose)}</div>` +
          `<div class="anchor">${E(c.signature.owner || T("no owner"))}` +
          `${c.signature.rotation_days ? " · " + T("rotated every {n} d", {n: c.signature.rotation_days}) : ""}` +
          ` · ${T("signed {date}", {date: E(c.signature.signed_on || "")})}</div>`
        : `<div class="anchor"><span class="unlinked">${T("not signed — nobody said what it is for")}</span></div>`}</td>
    ${cell(T("State"), state(c))}
    ${cell(T("Ceiling"), cap(c), "num")}
    ${cell(T("Projects"), who(c))}
    ${cell(T("Rotation"), c.rotated_on
      ? E(c.rotated_on) + `<div class="anchor">×${c.rotations || 1}</div>`
      // Never rotated: show when it was issued, or since when it has sat
      // here, before admitting "never".
      // A vault slot was STORED (`vault.py put`); only a provider's key is minted.
      : c.created_on
        ? `<span class="mono">${c.kind === "project-secret" ? T("stored {date}", {date: E(c.created_on)})
                                                           : T("minted {date}", {date: E(c.created_on)})}</span>`
        : c.installed_on
          ? `<span class="mono">${T("here since {date}", {date: E(c.installed_on)})}</span>`
          : `<span class="unlinked">${T("never")}</span>`)}
    ${cell(T("Action"), `<div class="verbs">` + credVerbs(c).map(([label, cmd, act]) =>
      LIVE && act
        ? `<button class="chip-btn" type="button" data-cred="${E(c.id)}" data-act="${E(act)}"
            >${E(label)}</button>`
        : `<button class="chip-btn" type="button" data-copy="${E(cmd)}"
            title="${E(cmd)}">${T("Command: {label}", {label: E(label)})}</button>`).join(" ") + `</div>`)}</tr>`;
  const leaked = rows.filter(c => c.leaked).length;
  const howto = LIVE
    ? T("the buttons act: this page is served by {server}", {server: `<span class="mono">tools/keyserver.py</span>`})
    : T("command mode: the buttons copy a command; run it in a terminal; before an import, replace /absolute/path/to/private-input with the path to the private file holding the value; start {server} to make them act",
        {server: `<span class="mono">${E(toolCommand("keyserver.py"))}</span>`});
  // Grouped by section, in a fixed order, and every section is shown even
  // when empty — an empty section is a fact, not a missing one.
  out.innerHTML = filterLine(rows.length, CREDS.length, "of@@{n} entries") + `<div class="action-mode"><b>${LIVE ? T("Actions connected") : T("Command mode")}</b> · ${LIVE ? T("The buttons perform the stated action.") : T("The buttons copy commands for a terminal.")}<details><summary>${T("How to use the actions")}</summary>${howto}</details></div><div class="card"><table>
    <colgroup><col style="width:23%"><col style="width:24%"><col style="width:10%">
      <col style="width:16%"><col style="width:11%"><col style="width:16%"></colgroup>
    <thead><tr>${sortTh(T("Credential"), "name")}<th>${T("State")}</th><th>${T("Ceiling")}</th>
      <th>${T("Projects")}</th><th>${T("Rotation")}</th><th>${T("Action")}</th></tr></thead>
    ${(SORT.key ? [["sorted", T("Selected entries"), T("One order by the chosen column")]] : CRED_SECTIONS).map(([key, title, says]) => {
      const cs = groups.get(key) || [];
      return `${grpBody(6, `${E(title)} <span class="n">${cs.length}</span><span class="anchor grp-says">${E(says)}</span>`, true,
        cs.length ? cs.map(row).join("")
        : `<tr><td colspan="6" class="e">${key === "issued"
            ? T("no key minted yet — “mint…” on the door's row")
            : T("empty here")}</td></tr>`)}`;
    }).join("")}</table></div>
    <p class="dmeta">${T("Showing {shown} of {total}", {shown: NUM(rows.length), total: NUM(CREDS.length)})} ·
      ${T("{n} open leaks", {n: leaked})} · ${T("measured {date}", {date: E(D.creds.scanned_on || "—")})} ·
      ${T("no values here: a label is what the provider itself calls the key;")}<br>
      ${T("writing and rotating a project secret from here is {refused} — the value travels only through stdin", {refused: `<b>${T("refused on purpose")}</b>`})}</p>
    ${movementsSection()}`;
}

// KEY MOVEMENTS. The journal records every time a value was put, rotated,
// moved or revoked. Beside it, the production configuration changes that
// have no journal row, each with the command that records it — the rule is
// that whoever moved a key records the movement in the same step.
function movementsSection() {
  const mv = (D.creds && D.creds.movements) || [];
  const un = (D.creds && D.creds.unrecorded) || [];
  const EVENT_LABEL = {put: T("put"), rotate: T("rotated"), moved: T("moved (by hand)"), settled: T("leak closed"),
                       issue: T("minted"), disable: T("disabled"), enable: T("enabled"), revoke: T("revoked"),
                       leak: T("leak recorded")};
  const unrows = un.map(u => {
    const proj = u.project || (u.app || "").replace(/-/g, "_");
    const cmds = (u.vars || []).map(v =>
      toolCommand("vault.py", ["moved", u.project || "PROJECT", "prod", v, "--at", "heroku", "--how", `set on Heroku app ${u.app}, release v${u.version}, ${String(u.at || "").slice(0, 16)}Z by ${u.by || "?"}`]));
    return `<li><b>${E(u.app)}</b> v${E(String(u.version || ""))} · ${E(String(u.at || "").slice(0, 16))}Z · ${E(u.by || "?")}:
      <span class="mono">${(u.vars || []).map(E).join(", ")}</span>
      ${cmds.map((c, i) => `<button class="chip-btn" type="button" data-copy="${E(c)}" title="${T("copy the movement record")}">${T("Command: record {name}", {name: E(u.vars[i])})}</button>`).join(" ")}</li>`;
  }).join("");
  const rows = mv.map(m => `<tr>
      <td class="num"><span class="mono">${E(String(m.at || "").slice(0, 16).replace("T", " "))}</span></td>
      <td>${E(EVENT_LABEL[m.event] || m.event || "")}</td>
      <td><span class="mono">${E(m.secret || m.of || "")}</span></td>
      <td>${E(m.by || "")}${m.tool ? ` <span class="none">· ${E(m.tool)}</span>` : ""}</td>
      <td>${E(m.at_provider ? T("at the provider: {name}", {name: m.at_provider}) : (m.to ? "→ " + m.to : ""))}${m.how ? `<div class="anchor">${E(String(m.how).slice(0, 160))}</div>` : ""}</td>
    </tr>`).join("");
  return `<h2 id="movements">${T("Key movements")}</h2>
    ${un.length ? `<div class="card"><p class="dmeta">${T("{n} Heroku changes this week are missing from the journal — the operator's rule: whoever moved it records it in the same step", {n: un.length})}</p>
      <ul class="dlist">${unrows}</ul></div>`
      // "Nothing unrecorded" is a comparison against Heroku's releases; with
      // Heroku never scanned there was no comparison, and saying everything
      // is recorded would read as a clean result nobody measured.
      : D.heroku ? `<p class="none">${T("every movement this week is recorded — Heroku has no change without a journal row")}</p>`
      : `<p class="none">${T("Heroku was not scanned, so its configuration changes were not compared with the journal")}</p>`}
    ${rows ? `<div class="card"><table><colgroup><col style="width:12%"><col style="width:12%"><col style="width:30%"><col style="width:14%"><col></colgroup>
      <thead><tr><th>${T("When")}</th><th>${T("What")}</th><th>${T("Key")}</th><th>${T("Who")}</th><th>${T("Where / how")}</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
      <p class="dmeta">${T("Latest {n} movements", {n: mv.length})} · ${T("full journal:")} <span class="mono">${E(toolCommand("vault.py", ["movements"]))}</span></p>`
      : `<p class="none">${T("the movement journal is empty")}</p>`}`;
}

// THE ENV PAGE: one row per variable in the projects' env files, by name,
// class and git state. Values never reach the payload; a value is shown only
// on request from the local server, and only for a short time.

const CLS_LABEL = {secret: T("secret"), config: T("config"),
                   placeholder: T("placeholder"), empty: T("empty@@value")};
const CLS_KIND = {secret: "warn", config: "", placeholder: "", empty: ""};

const hayEnv = e => [e.name, e.path, e.project, e.cls,
  e.shared.join(" "), e.available.join(" ")].join(" ").toLowerCase();

// By default only secrets are shown, plus any variable whose value exists
// in another project; the two extra chips widen the view to config, empty
// values and templates.
// The default view, named once: `shell.env_default_count` counts the ENV
// badge by the same two conditions.
const envLive = e => e.kind !== "template";
const envNotable = e => e.cls === "secret" || e.available.length > 0;
function keepEnv(e, q, project) {
  if (q && !hayEnv(e).includes(q)) return false;
  if (project && e.project !== project) return false;
  if (!active.has("e-tpl") && !envLive(e)) return false;
  if (!active.has("e-all") && !envNotable(e)) return false;
  if (active.has("e-shared") && !e.shared.length) return false;
  if (active.has("e-reuse") && !e.available.length) return false;
  if (active.has("e-git") && e.git !== "tracked") return false;
  if (active.has("e-open") && String(e.mode).slice(-2) === "00") return false;
  return true;
}

const envKey = e => e.path + " " + e.name;

// The rows currently on screen, by key, for the action buttons to find.
let ENV_ON_SCREEN = new Map();

// REVEAL: ask the local server for one value, then either copy it or show
// it for thirty seconds before it is hidden again.
function envReveal(btn, e, show) {
  const cell = btn.closest("td");
  btn.disabled = true;
  btn.textContent = "…";
  call("reveal", {path: e.path, name: e.name})
    .then(d => {
      if (!show) {
        return copyText(d.value).then(ok => {
          cell.innerHTML = envActions(e);
          toast(ok ? T("{name} copied", {name: e.name}) : T("clipboard unavailable"));
        });
      }
      cell.innerHTML =
        '<code class="val" tabindex="0">' + E(d.value) + '</code>' +
        '<div class="anchor"><button class="chip-btn" type="button" data-envhide="1"' +
        '>' + T("hide") + '</button> <button class="chip-btn" type="button" data-copy-quiet="1" data-copy="' +
        E(d.value) + '">' + T("copy") + '</button> · ' + T("hides in 30 s") + '</div>';
      const t = setTimeout(() => {
        if (cell.isConnected) cell.innerHTML = envActions(e);
      }, 30000);
      cell.dataset.timer = String(t);
    })
    .catch(err => {
      cell.innerHTML = envActions(e);
      toast(((err instanceof Uncertain ? T("outcome unknown:") + " " : "") + String(err.message)).slice(0, 140));
    });
}

function envActions(e) {
  if (LIVE) {
    return '<button class="chip-btn" type="button" data-env="' + E(envKey(e)) +
      '" data-show="1">' + T("show") + '</button> <button class="chip-btn" type="button" data-env="' +
      E(envKey(e)) + '">' + T("copy") + '</button>';
  }
  // Opened from a file: nothing can be revealed, so the button copies the
  // command that locates the value in a terminal.
  const project = e.project || (String(e.path || "").includes("/") ? String(e.path).split("/")[0] : "");
  if (!project || !e.name) return '<span class="unlinked">' + T("project or name not determined") + '</span>';
  return '<button class="chip-btn" type="button" data-copy="' +
    E(toolCommand("use_secret.py", ["where", project, e.name])) +
    '">' + T("copy the command") + '</button>';
}

document.addEventListener("click", ev => {
  const t = ev.target;
  if (!t || !t.closest) return;
  const hide = t.closest("[data-envhide]");
  if (hide) {
    const cell = hide.closest("td");
    clearTimeout(Number(cell.dataset.timer || 0));
    const e = ENV_ON_SCREEN.get(cell.dataset.key);
    if (e) cell.innerHTML = envActions(e);
    return;
  }
  const fold = t.closest(".grp-fold");
  if (fold) {
    const body = fold.closest("tbody");
    const open = fold.getAttribute("aria-expanded") === "true";
    fold.setAttribute("aria-expanded", String(!open));
    body.classList.toggle("folded", open);
    return;
  }
  const btn = t.closest("[data-env]");
  if (!btn) return;
  const e = ENV_ON_SCREEN.get(btn.dataset.env);
  if (e) envReveal(btn, e, btn.dataset.show === "1");
});

function renderEnv() {
  const out = document.getElementById("out");
  if (!D.env) {
    out.innerHTML = notScanned(T("Environment files were not scanned. Scan the projects folder, then rebuild:"),
      [fullCommand("env"), fullCommand("local")]);
    return;
  }
  const q = document.getElementById("q").value.trim().toLowerCase();
  const rows = ENVV.filter(e => keepEnv(e, q, sel.value));
  ENV_ON_SCREEN = new Map(rows.map(e => [envKey(e), e]));
  if (!rows.length) {
    out.innerHTML = nothingFound(ENVV.length, T("By default only secrets from live files are shown — “+ config and empty” and “+ templates” widen the selection."));
    return;
  }
  const state = e => {
    const bits = [chip(CLS_LABEL[e.cls] || e.cls, CLS_KIND[e.cls] || "")];
    if (e.kind === "template") bits.push(chip(T("template"), ""));
    if (e.git === "tracked") bits.push(chip(T("in git"), e.cls === "secret" ? "danger" : "warn"));
    if (e.git === "loose") bits.push(chip(T("not ignored"), "warn"));
    if (String(e.mode).slice(-2) !== "00") bits.push(chip(e.mode, "warn"));
    return bits.join(" ");
  };
  // The inventory names a project by its folder; the builder resolved each
  // folder the registry knows to its id (`env.project_ids`). A folder it does
  // not know is printed as text, never as a link to a panel that is not there.
  const PID = (D.env && D.env.project_ids) || {};
  const projectRef = p => PID[p]
    ? '<a class="plink" href="#' + E(PID[p]) + '">' + E(p) + '</a>'
    : '<span class="unlinked" title="' + T("no project in the registry has this folder") + '">' + E(p) + '</span>';
  const links = e => {
    if (e.shared.length) {
      return e.shared.map(projectRef).join(", ") +
        '<div class="tier">' + chip(T("the same value · a rotation touches every one"), "warn") + '</div>';
    }
    if (e.available.length) {
      return '<span class="anchor">' + T("the value is set in:") + '</span> ' +
        e.available.map(projectRef).join(", ");
    }
    return e.copies > 1
      ? '<span class="anchor">' + T("{n} copies in this same project", {n: e.copies}) + '</span>'
      : NONE;
  };
  sortInPlace(rows, {name: e => lower(e.name), modified: e => dateOr(e.modified_on)});
  const groups = new Map();
  rows.forEach(e => {
    const key = SORT.key ? T("Selected entries") : e.project;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(e);
  });
  const row = e => '<tr id="e-' + E(anchorSlug(e.path + ":" + e.name)) + '" data-file="' + E(anchorSlug(e.path)) + '">' +
    '<td data-label="' + T("Variable") + '"><div class="name mono">' + E(e.name) + '</div>' +
    '<div class="anchor mono" title="' + E(e.path) + '">' + E(e.path) + '</div></td>' +
    '<td data-label="' + T("What it is") + '">' + state(e) + '</td>' +
    '<td data-label="' + T("Links") + '">' + links(e) + '</td>' +
    '<td data-label="' + T("Changed") + '" class="num"><span class="mono">' + E(e.modified_on) + '</span></td>' +
    // Production: how the deployed configuration compares for this name.
    '<td data-label="' + T("Prod") + '">' + (() => {
      const m = REMOTE_BY_FOLDER.get(e.project);
      if (!D.remote) return '<span class="anchor">' + T("not scanned") + '</span>';
      if (!m) return NONE;
      const vs = (m.get(e.name) || []).slice().sort((a, b) =>
        VERDICT_ORDER.indexOf(a.verdict) - VERDICT_ORDER.indexOf(b.verdict) || String(a.app).localeCompare(String(b.app)));
      if (!vs.length) return '<span class="unlinked">' + T("not in production") + '</span>';
      return vs.map(v => {
        const [word, kind] = VERDICT_LABEL[v.verdict] || [v.verdict, ""];
        return chip(word, kind) + '<div class="anchor">' + E(v.app) + '</div>';
      }).join("");
    })() + '</td>' +
    '<td data-label="' + T("Value") + '" data-key="' + E(envKey(e)) + '">' + envActions(e) + '</td></tr>';
  const t = D.env.totals || {};
  const howto = LIVE
    ? T("“show” and “copy” act: this page is served by {server}, and every reveal is written to {log} before the file is read",
        {server: '<span class="mono">tools/keyserver.py</span>', log: '<span class="mono">store/logs/keyserver.jsonl</span>'})
    : T("command mode: the button copies a command to the clipboard; run it in a terminal. Start {server} to reveal and copy right here",
        {server: '<span class="mono">' + E(toolCommand('keyserver.py')) + '</span>'});
  out.innerHTML = filterLine(rows.length, ENVV.length, "of@@{n} variables") + '<div class="action-mode"><b>' + (LIVE ? T("Actions connected") : T("Command mode")) + '</b> · ' + (LIVE ? T("A value opens only on request and is hidden again.") : T("The buttons copy commands for a terminal; no values are shown here.")) + '<details><summary>' + T("How to use the actions") + '</summary>' + howto + '</details></div><div class="card"><table>' +
    '<colgroup><col style="width:26%"><col style="width:17%"><col style="width:20%">' +
    '<col style="width:9%"><col style="width:14%"><col style="width:14%"></colgroup>' +
    '<thead><tr>' + sortTh(T("Variable"), "name") + '<th>' + T("What it is") + '</th><th>' + T("Links") + '</th>' + sortTh(T("Changed"), "modified") +
    '<th>' + T("Prod") + '</th><th>' + T("Value") + '</th></tr></thead>' +
    [...groups].map(([p, es]) => {
      // Project headings provide context without hiding any variable metadata.
      const alarming = es.filter(e => e.cls === "secret" && e.git === "tracked");
      const open = true; // Metadata is visible; secret values still require explicit reveal.
      const secrets = es.filter(e => e.cls === "secret").length;
      return grpBody(6, E(p || T("outside any project")) +
        ' <span class="n">' + T("{n} variables", {n: es.length}) +
        (secrets ? ' · ' + T("{n} secrets", {n: secrets}) : '') + '</span>' +
        (alarming.length ? ' ' + chip(T("{n} secrets in git", {n: alarming.length}), "danger") : ''),
        open, es.map(row).join(""), ' data-envgroup="' + E(p || "-") + '"');
    }).join("") + '</table></div>' +
    '<p class="dmeta">' + T("Showing {shown} of {total}", {shown: NUM(rows.length), total: T("of@@{n} variables", {n: ENVV.length})}) + ' · ' +
    T("{files} and {templates} in {projects}", {files: T("{n} live files", {n: t.env_files || 0}),
      templates: T("{n} templates", {n: t.templates || 0}), projects: T("in@@{n} projects", {n: t.projects || 0})}) +
    ' · ' + T("{n} read as a secret", {n: t.secrets || 0}) + ' · ' +
    T("shared values: {n}", {n: t.shared_across_projects || 0}) + ' · ' + T("measured {date}", {date: E(D.env.scanned_on || "—")}) + '<br>' +
    T("No values on this page or in the registry: a “shared value” is established by a salted fingerprint, and the salt lives outside git and never leaves the machine.") + '</p>';
}

// DOMAINS: the registrar's list joined with the Cloudflare zones, with
// measured liveness and expiry.
const DAY = 864e5;
const daysUntil = iso => iso ? Math.round((Date.parse(iso) - Date.now()) / DAY) : null;
const hayDom = d => [d.name, d.registrar || "", d.status || "",
  (d.projects || []).join(" ")].join(" ").toLowerCase();

// THE ROWS OF THE DOMAINS TABLE: the registrar's list joined with the
// Cloudflare zones by name; a zone no registrar lists still gets a row,
// marked as known from Cloudflare. One function, so the table, its selector
// and its counter read the same list (`shell.domain_totals` counts it so).
function domainRows() {
  const zoneBy = new Map((D.zones || []).map(z => [z.name, z]));
  const rows = DOMS.map(d => ({ ...d, zone: zoneBy.get(d.name) || null, source: zoneBy.has(d.name) ? "both" : "registrar" }));
  (D.zones || []).forEach(z => { if (!DOMS.some(d => d.name === z.name))
    rows.push({ name: z.name, registrar: z.registrar ? z.registrar + " " + T("(according to Cloudflare)") : null,
      status: z.status, live: LIVE_BY_HOST(z.name), projects: z.project ? [z.project] : [],
      zone: z, source: "cloudflare" }); });
  return rows;
}
function keepDom(d, q, registrar) {
  if (q && !hayDom(d).includes(q)) return false;
  // "—" selects the names that carry no registrar at all.
  if (registrar === NO_REGISTRAR ? !!d.registrar : (registrar && d.registrar !== registrar)) return false;
  if (active.has("d-noproject") && (d.projects || []).length) return false;
  // Liveness filters: not resolving, not measured, or answering with an
  // HTTP error.
  if (active.has("d-dark") && !(d.live && d.live.resolves === false)) return false;
  if (active.has("d-unmeasured") && d.live) return false;
  if (active.has("d-http") && !(d.live && d.live.resolves && (d.live.http >= 400 || d.live.http === 0))) return false;
  if (active.has("d-expiring")) { const n = daysUntil(d.expires_on); if (n === null || n > 90) return false; }
  if (active.has("d-norenew") && d.auto_renew !== false) return false;
  return true;
}

// MCP SERVERS, as each agent declares them: where the key sits and whether
// the server answered the last probe.
function renderMcp() {
  const out = document.getElementById("out");
  if (!D.mcp) {
    out.innerHTML = notScanned(T("MCP was not scanned. Point it at your home, switch it on and scan:"),
      [fullCommand("configure sources mcp_config_root", RUNTIME.user_home ? shellArg(RUNTIME.user_home) : '"$HOME"'),
       fullCommand("configure integrations mcp true"), fullCommand("scan-mcp")]);
    return;
  }
  const q = document.getElementById("q").value.trim().toLowerCase();
  const ALL = D.mcp.servers || [];
  const rows = ALL.filter(s => (!q || [s.name, s.agent, s.scope, s.target || "", s.command || ""].join(" ").toLowerCase().includes(q))
    && (!sel.value || s.agent === sel.value)
    && (!active.has("m-url") || s.key_in_url)
    && (!active.has("m-down") || s.liveness === "failed")
    && (!active.has("m-auth") || s.liveness === "needs-auth")
    && (!active.has("m-alone") || ALL.filter(x => x.name === s.name).length === 1));
  if (!rows.length) { out.innerHTML = nothingFound(ALL.length); return; }
  const live = s => ({ connected: chip(T("answering"), "ok"), failed: chip(T("not answering"), "danger"),
    "needs-auth": chip(T("sign-in needed"), "warn"), "not-listed": chip(T("not listed"), "warn"),
    "not-probed": chip(T("not probed")) })[s.liveness] || chip(s.liveness || "—");
  // What the agent's own probe printed, in the reader's language when it is
  // one of the agent's fixed phrases; anything else (an error text) as said,
  // and the original always on hover.
  const PROBE_SAID = {"Connected": T("probe@@connected"), "Needs authentication": T("probe@@needs authentication"),
                      "Failed to connect": T("probe@@failed to connect")};
  const keyPlace = s => s.key_in_url ? chip(T("in the URL"), "danger") : s.key_in_header ? T("in a header") : s.key_in_env ? T("in env") : NONE;
  sortInPlace(rows, {name: s => lower(s.name)});
  const groups = new Map();
  rows.forEach(s => { const k = SORT.key ? T("Selected entries") : s.agent; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(s); });
  const row = s => `<tr id="m-${E(anchorSlug(s.agent + "/" + s.name))}">
    <td data-label="${T("Server")}"><div class="name mono">${E(s.name)}</div><div class="anchor">${E(s.agent)} · ${E(s.scope)}</div></td>
    <td data-label="${T("Transport")}">${E(s.transport || "—")}${s.command ? `<div class="anchor mono">${E(s.command)}${s.command_present === false ? " · " + T("not on disk") : ""}</div>` : ""}</td>
    <td data-label="${T("Target")}"><span class="mono">${E(s.target || "")}</span></td>
    <td data-label="${T("Key")}">${keyPlace(s)}</td>
    <td data-label="${T("Connection")}">${live(s)}${s.liveness_detail ? `<div class="anchor" title="${E(s.liveness_detail)}">${E(labelOf(PROBE_SAID, s.liveness_detail))}</div>` : ""}</td></tr>`;
  const t = D.mcp.totals || {};
  out.innerHTML = filterLine(rows.length, ALL.length, "of@@{n} declarations") + `<div class="card"><table>
    <colgroup><col style="width:22%"><col style="width:16%"><col style="width:26%"><col style="width:14%"><col style="width:22%"></colgroup>
    <thead><tr>${sortTh(T("Server"), "name")}<th>${T("Transport")}</th><th>${T("Target")}</th><th>${T("Key")}</th><th>${T("Connection")}</th></tr></thead>
    ${[...groups].map(([agent, ss]) =>
      `${grpBody(5, `${E(agent)} <span class="n">${ss.length}</span>`, true, ss.map(row).join(""))}`).join("")}</table></div>
    <p class="dmeta">${T("{n} declarations", {n: t.declarations || 0})} · ${T("{n} servers", {n: t.distinct_servers || 0})} ·
      ${T("{n} in one agent only", {n: t.in_one_agent_only || 0})} · ${T("{n} with the key in the URL", {n: t.key_in_url || 0})} ·
      ${D.mcp.own_declared ? T("the observatory's own server is declared") : `<b>${T("the observatory's own server is not declared")}</b>`} ·
      ${T("scan {date}", {date: E(D.mcp.scanned_on || "—")})}</p>`;
}

// ANALYTICS PROPERTIES, grouped by account, each linked to a project by a
// named rule. An unclaimed property is not a defect by itself — it may belong
// to someone else — but it has a row, so the decision can be made.
const TRAFFIC_STANDING = { linked: [T("linked@@property"), "ok"], outside: [T("outside the estate"), ""],
                           unclaimed: [T("unowned@@property"), "warn"] };
const RULE_LABEL = { declared: T("declared by the operator"), "declared-host": T("host from a file"),
                     "stream-host": T("by the stream's host"), "app-id": T("by app id"),
                     name: T("by property name") };
function renderTraffic() {
  const out = document.getElementById("out");
  if (!D.google) {
    out.innerHTML = notScanned(T("Analytics was not scanned. It needs a Google service account in the secret store; then switch it on and scan:"),
      [fullCommand("configure integrations google true"), fullCommand("google")]);
    return;
  }
  const ALL = D.google.properties || [];
  const q = document.getElementById("q").value.trim().toLowerCase();
  const rows = ALL.filter(p => (!q || [p.name, p.account_name, (p.hosts || []).join(" "),
      (p.app_ids || []).join(" "), p.project || ""].join(" ").toLowerCase().includes(q))
    && (!sel.value || p.account_name === sel.value)
    && (!active.has("t-unclaimed") || p.standing === "unclaimed")
    && (!active.has("t-linked") || p.standing === "linked")
    && (!active.has("t-quiet") || (p.users_30d === 0 && !p.error))
    && (!active.has("t-app") || ((p.app_ids || []).length && !(p.hosts || []).length)));
  if (!rows.length) { out.innerHTML = nothingFound(ALL.length); return; }
  const cell = (label, html, cls) =>
    `<td data-label="${label}"${cls || html === NONE ? ` class="${
      [cls, html === NONE ? "e" : ""].filter(Boolean).join(" ")}"` : ""}>${html}</td>`;
  const num = n => n == null ? NONE : `<span class="mono">${NUM(n)}</span>`;
  sortInPlace(rows, {name: p => lower(p.name), users: p => numOr(p.users_30d)});
  const groups = new Map();
  rows.forEach(p => { const k = SORT.key ? T("Selected entries") : p.account_name || "—";
    if (!groups.has(k)) groups.set(k, []); groups.get(k).push(p); });
  const row = p => `<tr id="g-${E(String(p.id).replace(/[^A-Za-z0-9_.:-]+/g, "-"))}">
    <td data-label="${T("Property")}"><div class="name">${E(p.name || p.property)}</div>
      <div class="anchor mono">${E(p.property || "")} · ${E(p.account_name || T("account not stated"))}</div>
      ${(p.hosts || []).length ? `<div class="anchor">${(p.hosts || []).slice(0, 3).map(E).join(" · ")}${p.hosts.length > 3 ? ` +${p.hosts.length - 3}` : ""}</div>` : ""}
      ${(p.app_ids || []).length ? `<div class="anchor mono">${(p.app_ids || []).slice(0, 2).map(E).join(" · ")}${p.app_ids.length > 2 ? ` +${p.app_ids.length - 2}` : ""}</div>` : ""}</td>
    ${cell(T("Users/30 d"), num(p.users_30d), "num")}
    ${cell(T("Sessions/30 d"), num(p.sessions_30d), "num")}
    ${cell(T("Views"), num(p.views_30d), "num")}
    ${cell(T("Project"), p.project
      ? `<a class="plink" href="#${E(p.project)}">${E(String(p.project).split(":")[1] || p.project)}</a>` +
        `<div class="anchor" title="${E(p.link_evidence || "")}">${E(RULE_LABEL[p.link_rule] || p.link_rule || "")}</div>`
      : `<span class="unlinked" title="${E(p.unlinked_reason || p.boundary_why || "")}">${
          p.standing === "outside" ? T("outside the estate") : T("no project")}</span>`)}
    ${cell(T("Link"), (() => { const [w, k] = TRAFFIC_STANDING[p.standing] || [p.standing, ""];
       return chip(w, k) + (p.error ? `<div class="tier">${chip(p.observation_conflicts ? T("measurements disagree") : T("did not answer"), "warn")}</div>` : ""); })())}
    ${cell(T("Where to look"), `<div class="verbs">` +
      `<a class="chip-btn" href="${E(p.report_url)}" target="_blank" rel="noopener" title="${T("GA4 report")}">GA4</a>` +
      (p.admin_url ? `<a class="chip-btn" href="${E(p.admin_url)}" target="_blank" rel="noopener" title="${T("property settings")}">${T("admin")}</a>` : "") +
      `</div>`)}</tr>`;
  const tt = D.google.totals || {};
  // Each service account is addressable by its client address: a finding
  // about one of its grants (`credential:… via <address>`) lands here.
  const creds = (D.google.credentials || []).map(c =>
    `<a class="chip-btn" id="gc-${E(anchorSlug(c.client_email || ""))}" href="${E(c.console_url)}" target="_blank" rel="noopener" title="${E(c.client_email || "")}">Cloud: ${E(c.cloud_project || "")}</a>`).join(" ");
  const sites = (D.google.search_console || []).map(s =>
    `<a class="chip-btn" href="${E(s.console_url)}" target="_blank" rel="noopener">Search Console: ${E(s.site)}</a>`).join(" ");
  out.innerHTML = filterLine(rows.length, ALL.length, "of@@{n} properties") +
    `<div class="list-summary"><b>${T("Last 30 days")}</b> · ${T("measured {date}", {date: E(D.google.scanned_on || "—")})} · ${T("a sum across properties does not mean unique people across them")}${tt.unknown_properties ? " · " + T("not measured: {n}", {n: tt.unknown_properties}) : ""}. <button class="chip-btn" type="button" data-copy="${E(engineCommand("collectors/scan_google.py", [String(RUNTIME.scratch || ".") + "/google.json", "--force"]) + " && " + cliCommand("merge") + " && " + cliCommand("emit") + " && " + cliCommand("dashboard"))}"
        title="${T("re-read from Google and rebuild the pages")}">${T("Copy the refresh command")}</button></div>` +
    `<div class="card"><table>
    <colgroup><col style="width:26%"><col style="width:11%"><col style="width:11%"><col style="width:10%"><col style="width:14%"><col style="width:12%"><col style="width:16%"></colgroup>
    <thead><tr>${sortTh(T("Property"), "name")}${sortTh(T("Users/30 d"), "users")}<th>${T("Sessions/30 d")}</th><th>${T("Views")}</th>
      <th>${T("Project")}</th><th>${T("Link")}</th><th>${T("Where to look")}</th></tr></thead>
    ${[...groups].map(([acc, ps]) =>
      `${grpBody(7, `${E(acc)} <span class="n">${ps.length}</span> <span class="n">${ps.some(p => p.users_30d != null) ? NUM(ps.reduce((n, p) => n + (p.users_30d || 0), 0)) + " — " + T("users summed / 30 d") : T("audience not measured")}${ps.some(p => p.users_30d == null) ? " · " + T("incomplete measurement") : ""}</span>`, true,
        ps.map(row).join(""))}`).join("")}</table></div>
    <div class="verbs" style="margin: var(--space-3) var(--space-5) 0">${creds}${sites}</div>
    <p class="dmeta">${T("Showing {shown} of {total}", {shown: NUM(rows.length), total: NUM(ALL.length)})} · ${T("{n} linked", {n: tt.linked_to_a_project || 0})},
      ${T("{n} unowned", {n: tt.unclaimed || 0})} (${tt.users_30d_unclaimed == null ? T("audience not measured") : T("{n} users across measured properties", {n: Number(tt.users_30d_unclaimed)})}) ·
      ${T("sum across measured properties: {value} users in 30 days", {value: tt.users_30d == null ? T("not measured") : NUM(tt.users_30d)})} ·
      ${T("measured {date}", {date: E(D.google.scanned_on || "—")})}
<br>
      ${T("The figures are cached for 12 hours; how long a refresh takes depends on the number of connected properties. Analytics can lag. “Unowned” is not a defect: by the operator's rule it is a product someone else looks after — but it has a row, so the decision can be made once.")}</p>`;
}

function renderDomains() {
  const out = document.getElementById("out");
  const q = document.getElementById("q").value.trim().toLowerCase();
  const rows0 = domainRows();
  const doms = rows0.filter(d => keepDom(d, q, sel.value));
  if (!doms.length) {
    out.innerHTML = nothingFound(rows0.length, "", [T("No domain is listed. Domains come from the zones a Cloudflare scan finds, and from {file}. Switch Cloudflare on and scan:", {file: "registry/domains.json"}),
      [fullCommand("configure integrations cloudflare true"), fullCommand("scan-cloudflare")]]);
    return;
  }
  const PROD_BY_PROJECT = new Map(D.rows.map(r => [r.id, r.products || []]));
  const standing = d => {
    if (!d.zone) return d.projects && d.projects.length ? chip(T("linked@@domain"), "ok") : chip(T("no project"));
    const s = d.zone.standing;
    if (s === "linked") return chip(T("linked@@domain"), "ok");
    if (s === "outside") return `<span title="${E(d.zone.boundary_why || "")}">${chip(T("outside the estate"))}</span>`;
    if (s === "pending") return chip(T("awaiting a word"), "warn");
    if (s === "dormant") return `<span title="${T("DNS read: the apex and www point nowhere")}">${chip(T("dormant@@domain"))}</span>`;
    if (s === "product") return chip(T("product"), "ok");
    return chip(T("no word"), "warn");
  };
  const cfCell = d => d.zone
    ? `<span class="mono">${E(d.zone.account_label || "")}</span><div class="anchor">${E(d.zone.status || "")}${d.zone.paused ? " · paused" : ""} · ${E(d.zone.plan || "")}</div>`
    : NONE;
  const PROD_NAME = new Map((D.products || []).map(p => [p.id, p.name]));
  const prodCell = d => { const ps = (d.projects || []).flatMap(p => PROD_BY_PROJECT.get(p) || []);
    if (!ps.length && d.zone && d.zone.product) return E(PROD_NAME.get(d.zone.product) || d.zone.product);
    return ps.length ? ps.map(p => `<span title="${E(p.kind)}">${E(p.name)}${p.kind === "suggested" ? "?" : ""}</span>`).join(", ") : NONE; };
  const cell = (label, html, cls) =>
    `<td data-label="${label}"${cls || html === NONE ? ` class="${
      [cls, html === NONE ? "e" : ""].filter(Boolean).join(" ")}"` : ""}>${html}</td>`;
  const liveCell = d => {
    if (!d.live) return chip(T("domain@@not measured"));
    if (d.live.resolves === false) return chip(T("does not resolve"), "danger");
  // Resolving but not answering is its own state, not an HTTP error.
    if (!d.live.http) return chip(T("resolves, not answering"), "warn");
    if (d.live.http >= 400) return chip("HTTP " + d.live.http, "warn");
    return chip("HTTP " + d.live.http, "ok");
  };
  const expiry = d => {
    const n = daysUntil(d.expires_on);
    if (n === null) return NONE;
    const word = n < 0 ? chip(T("expired"), "danger")
      : n <= 90 ? chip(T("{n} d", {n}), "warn") : T("{n} d", {n});
    return `${E(d.expires_on)}<div class="tier">${word}` +
      (d.auto_renew === false ? " " + chip(T("no auto-renewal"), "warn") : "") + `</div>`;
  };
  sortInPlace(doms, {name: d => lower(d.name), expiry: d => dateOr(d.expires_on)});
  const groups = new Map();
  doms.forEach(d => { const k = SORT.key ? T("Selected entries") : d.registrar || "—";
    if (!groups.has(k)) groups.set(k, []); groups.get(k).push(d); });
  const row = d => `<tr id="d-${E(d.name)}">
    <td data-label="${T("Domain")}"><div class="name"><a href="https://${E(d.name)}"
      target="_blank" rel="noopener">${E(d.name)}</a></div>
      <div class="anchor">${E(d.registrar || T("registrar not stated"))} · ${E(d.status || "")}</div></td>
    ${cell(T("Answers"), liveCell(d) + (d.live ? `<div class="anchor">${E(d.live.on)}</div>` : ""))}
    ${cell(T("Expires"), expiry(d), "num")}
    ${cell(T("Project"), (d.projects || []).length
      ? (d.projects || []).map(p => `<a class="plink" href="#${E(p)}">${E(p.split(":")[1])}</a>`).join(", ")
      : `<span class="unlinked">${T("no project")}</span>`)}
    ${cell(T("Link"), standing(d))}
    ${cell("Cloudflare", cfCell(d))}
    ${cell(T("Product"), prodCell(d))}
    ${                                                                       
                                                                               ""}
    ${cell(T("Where to look"), `<div class="verbs">` +
      `<a class="chip-btn" href="https://rdap.org/domain/${E(d.name)}"
         target="_blank" rel="noopener" title="${T("what the registrar says")}">RDAP</a>` +
      `<button class="chip-btn" type="button" data-copy="${E("dig +short " + shellArg(d.name) + " A AAAA CNAME")}"
         title="${T("copy the diagnostic command")}">${T("Command: {label}", {label: "DNS"})}</button>` +
      (d.zone ? `<a class="chip-btn" href="https://dash.cloudflare.com/?to=/:account/${E(d.name)}"
         target="_blank" rel="noopener" title="${T("the zone in Cloudflare")}">${T("zone")}</a>` : "") +
      `</div>`)}</tr>`;
  out.innerHTML = filterLine(doms.length, rows0.length, "of@@{n} names") + `<div class="card"><table>
    <colgroup><col style="width:18%"><col style="width:10%"><col style="width:11%"><col style="width:13%"><col style="width:11%"><col style="width:11%"><col style="width:12%"><col style="width:14%"></colgroup>
    <thead><tr>${sortTh(T("Domain"), "name")}<th>${T("Answers")}</th>${sortTh(T("Expires"), "expiry")}<th>${T("Project")}</th><th>${T("Link")}</th><th>Cloudflare</th><th>${T("Product")}</th><th>${T("Where to look")}</th></tr></thead>
    ${[...groups].map(([reg, ds]) =>
      `${grpBody(8, `${E(reg)} <span class="n">${ds.length}</span>`, true, ds.map(row).join(""))}`).join("")}</table></div>
    <p class="dmeta">${T("Showing {shown} of {total}", {shown: NUM(doms.length), total: NUM(rows0.length)})} ·
      ${T("{n} linked to no project", {n: rows0.filter(d => !(d.projects || []).length).length})} ·
      ${D.zones ? T("{zones} in {accounts}", {zones: T("{n} Cloudflare zones", {n: D.zones.length}), accounts: T("in@@{n} accounts", {n: new Set(D.zones.map(z => z.account_label)).size})}) : T("Cloudflare was not scanned")} ·
      ${T("the renewal price is not measured here")}</p>`;
}

const STATE_LABEL = { running: T("running"), down: T("crashed@@app"), suspended: T("suspended"),
                      "resources-only": T("resources only"), idle: T("idle") };

// PRODUCTION CONFIGURATION, compared with the local checkouts, indexed by app
// and by folder. Only verdicts arrive here, never a value.
const REMOTE_BY_APP = new Map(((D.remote && D.remote.apps) || []).map(a => [a.app, a]));
const REMOTE_BY_FOLDER = new Map();
for (const a of (D.remote && D.remote.apps) || [])
  for (const f of a.compared_with || []) {
    const key = String(f).replace(/\/+$/, "").split("/").pop();
    if (!REMOTE_BY_FOLDER.has(key)) REMOTE_BY_FOLDER.set(key, new Map());
    const m = REMOTE_BY_FOLDER.get(key);
    // EVERY APP, not the first: a prod and a staging app compared with one
    // folder each have their own verdict, and keeping only the first hid the other.
    for (const v of a.vars || []) {
      if (!m.has(v.name)) m.set(v.name, []);
      if (!m.get(v.name).some(x => x.app === a.app)) m.get(v.name).push({...v, app: a.app});
    }
  }
const VERDICT_ORDER = ["same_as_local", "local_only", "differs", "remote_only", "not_compared", "no_local_checkout"];
const VERDICT_LABEL = {
  same_as_local: [T("same as local"), "danger"],
  differs: [T("a different value"), "ok"],
  remote_only: [T("only in production"), ""],
  local_only: [T("only locally"), "warn"],
  not_compared: [T("not compared"), ""],
  no_local_checkout: [T("no code here"), ""],
};

function renderHeroku() {
  const out = document.getElementById("out");
  if (!D.heroku) {
    // Never scanned is said as such, with the command that scans.
    out.innerHTML = notScanned(T("Heroku was not scanned. Log in with the Heroku CLI, then switch it on and scan:"),
      [fullCommand("configure integrations heroku true"), fullCommand("heroku")]);
    return;
  }
  const q = document.getElementById("q").value.trim().toLowerCase();
  const team = sel.value;
  const apps = APPS.filter(a => keepApp(a, q, team));
  if (!apps.length) { out.innerHTML = nothingFound(APPS.length); return; }
  const money = n => n == null || !Number.isFinite(Number(n)) ? "—" : "$" + Number(n).toLocaleString("en", {maximumFractionDigits: 2});
  sortInPlace(apps, {name: a => lower(a.name), cost: a => numOr(a.monthly_cost)});
  const groups = new Map();
  apps.forEach(a => { const key = SORT.key ? T("Selected entries") : a.team; if (!groups.has(key)) groups.set(key, []); groups.get(key).push(a); });
  const cell = (label, html, cls) =>
    `<td data-label="${label}"${cls || html === NONE ? ` class="${
      [cls, html === NONE ? "e" : ""].filter(Boolean).join(" ")}"` : ""}>${html}</td>`;
  const appRow = a => {
    const dynos = (a.formation || []).filter(f => f.qty).map(f =>
      `<div class="st">${chip(`${E(f.type)}×${f.qty}`)}<span class="mono">${E(f.size)}</span></div>`).join("");
    const crashed = (a.crashed || []).length
      ? `<div class="tier">${chip("crashed: " + E(a.crashed.join(", ")), "danger")}</div>` : "";
    // Add-ons with their plan and monthly price.
    const planName = p => String(p || "").replace(/^heroku-/, "").replace(":", " ");
    const addons = (a.addons || []).length
      ? (a.addons || []).map(x => `<div class="st"><span>${E(planName(x.plan))}</span>` +
          (x.cents ? `<span class="mono">$${x.cents / 100}</span>` : "") + `</div>`).join("")
      : NONE;
    // An add-on attached from another app is shared, so changing it touches
    // that app too.
    const shared = (a.addons_attached || []).length
      ? `<div class="tier">${chip(T("shared with {apps}", {apps: E(a.addons_attached.map(x => x.owner).join(", "))}), "warn")}</div>` : "";
    const sha = a.deployed_commit && a.deployed_commit.sha
      ? `<div class="anchor mono" title="${T("the commit from the description of release v{n}", {n: E(a.deployed_commit.release)})}">${E(a.deployed_commit.sha.slice(0, 8))}</div>` : "";
    const deploy = a.last_deploy_on
      ? E(a.last_deploy_on) + sha + (a.last_deploy_on < YEAR_AGO ? `<div class="tier">${chip(T("over a year"), "warn")}</div>` : "")
      : `<span class="unlinked">${a.never_deployed ? T("never had code") : T("not found")}</span>`;
    // Why an app has no project: its source is outside, its folder is
    // unclaimed, or its source is unknown.
    const WHY_LABEL = { "external-repo": T("the source is outside our GitHub"),
                        "folder-unclaimed": T("no project claims the folder"),
                        "no-source": T("the source is unknown") };
    const project = a.project
      ? `<a class="plink" href="#${E(a.project)}">${E(PROJ_NAME.get(a.project) || a.project)}</a>` +
        `<div class="anchor">${E(a.link_rule)}</div>`
      : `<span class="unlinked" title="${E(a.unlinked_reason || "")}">${T("no project")}</span>` +
        `<div class="anchor">${E(WHY_LABEL[a.unlinked_kind] || a.unlinked_kind || "")}</div>`;
    const folders = (a.local_folders || []).length
      ? (a.local_folders || []).map(p =>
          `<span class="folder">${E(p.replace(/^\/Users\/[^/]+\//, "~/"))}</span>`).join("")
      : `<span class="unlinked">${T("no folder")}</span>`;
    return `<tr id="a-${E(a.name)}">
      <td data-label="${T("App")}"><div class="name">${a.web_url
        ? `<a href="${E(a.web_url)}" target="_blank" rel="noopener">${E(a.name)}</a>`
        : E(a.name)}</div>
        <div class="anchor">${E(a.team || T("account not stated"))} · ${E(a.region)} · ${E(a.stack)}${a.stack_superseded ? " · " + T("stack no longer supported") : ""}</div></td>
      ${cell(T("State"), `<span class="st st-${E(a.state)}"><i></i>${E(STATE_LABEL[a.state] || a.state)}</span>` +
        (a.maintenance ? `<div class="tier">${chip("maintenance", "warn")}</div>` : "") + crashed)}
      ${cell(T("$/mo"), money(a.monthly_cost), "num")}
      ${cell(T("Code deploy"), deploy, "num")}
      ${cell(T("Project"), project)}
      ${                                                                       
                                                                            ""}
      ${cell(T("Configuration"), (() => {
        const r = REMOTE_BY_APP.get(a.name);
        if (!D.remote) return `<span class="unlinked">${T("not scanned")}</span>`;
        if (!r) return `<span class="unlinked">${T("not in the scan")}</span>`;
        if (r.error) return chip(T("did not answer"), "warn");
        const c = r.counts || {};
        const n = (r.vars || []).length;
        if (!r.compared_with.length)
          return `<span class="mono">${n}</span><div class="anchor">${T("variables; no code here, nothing to compare with")}</div>`;
        return `<span class="mono">${n}</span><div class="anchor">${T("variables@@count")}</div>` +
          (c.same_as_local ? `<div class="tier">${chip(T("{n} as local", {n: c.same_as_local}), "danger")}</div>` : "") +
          ((r.retired_in_use || []).length
            ? `<div class="tier">${chip(T("{n} retired", {n: (r.retired_in_use || []).length}), "danger")}</div>` : "") +
          `<div class="anchor">${T("{n} differ", {n: c.differs || 0})} · ${T("{n} only here", {n: c.remote_only || 0})}</div>`;
      })() + shared + `<details class="resource-detail"><summary>${T("Resources and folders")}</summary><b>${T("Dynos")}</b>${dynos || NONE}<b>${T("Add-ons")}</b>${addons}<b>${T("Code on this machine")}</b>${folders}</details>`)}
      ${cell(T("Command"), `<div class="verbs">` +
        [[T("restart"), `heroku ps:restart -a ${a.name}`],
         [T("logs"), `heroku logs -t -a ${a.name}`],
         ...(a.state === "suspended" || a.state === "resources-only"
             ? [[T("what is it"), `heroku apps:info -a ${a.name}`]] : [])]
        .map(([label, cmd]) => `<button class="chip-btn" type="button"
              data-copy="${E(cmd)}" title="${E(cmd)}">${T("Command: {label}", {label: E(label)})}</button>`).join(" ")
        + `</div>`)}</tr>`;
  };
  const total = apps.reduce((n, a) => n + (Number(a.monthly_cost) || 0), 0);
  out.innerHTML = filterLine(apps.length, APPS.length, "of@@{n} apps") +
    `<div class="list-summary"><b>${T("{money}/mo", {money: money(total)})}</b> · ${T("an estimate for the selected apps from the price list, not a bill")} · ${T("measured {date}", {date: E(D.heroku.scanned_on || "—")})}</div><div class="card"><table>
    <colgroup><col style="width:20%"><col style="width:10%"><col style="width:8%"><col style="width:12%"><col style="width:16%"><col style="width:22%"><col style="width:12%"></colgroup>
    <thead><tr>
      ${sortTh(T("App"), "name")}<th>${T("State")}</th>
      ${sortTh(T("$/mo"), "cost")}<th>${T("Code deploy")}</th><th>${T("Project")}</th>
      <th>${T("Configuration")}</th><th>${T("Command")}</th>
    </tr></thead>${[...groups].map(([team, as]) =>
      `${grpBody(7, `${E(team)} <span class="n">${as.length}</span> <span class="n">${T("{money}/mo", {money: money(as.reduce((n, a) => n + a.monthly_cost, 0))})}</span>`, true,
        as.map(appRow).join(""))}`).join("")}</table></div>
    <p class="dmeta">${T("Showing {shown} of {total}", {shown: NUM(apps.length), total: NUM(APPS.length)})} · ${T("{money}/mo", {money: money(total)})} ·
      ${T("measured {date}", {date: E(D.heroku.scanned_on || "—")})} ·
      ${T("on-demand list prices, not the bill")}</p>`;
}

// A long repository list inside a cell folds after three; the button
// toggles it.
document.getElementById("out").addEventListener("click", e => {
  const b = e.target.closest(".more");
  if (!b) return;
  const list = b.previousElementSibling;
  const folded = list.classList.toggle("folded");
  b.textContent = folded ? T("+{n} more", {n: list.children.length - 3}) : T("collapse");
});

// STICKY HEADERS, on the single page only. There the filter bar sticks to the
// top and the table header sticks below it, so the bar's height — which
// changes as it wraps — is measured into `--stick`. A split page pins neither
// (workspace.css: the bar is static and the header sticks at 0), and the only
// other bar it has is the navigation RAIL beside the content, whose height is
// no offset of anything; measuring it set `--topbar` to the rail's height.
const bar = document.querySelector(".controls");
const stick = () => {
  if (PAGE || !bar) return;
  document.documentElement.style.setProperty("--stick", bar.offsetHeight + "px");
};
if (!PAGE && bar) new ResizeObserver(stick).observe(bar);
// THE REVIEW QUEUE, read-only: the newest proposals, each with the commands
// that accept or reject it. The decision itself is taken in a terminal.
(function renderQueue() {
  const host = document.getElementById("queue"), head = document.getElementById("queue-h");
  const q = D.queue || [];
  if (!q.length) {
    head.textContent = T("Awaiting a person's decision");
    host.innerHTML = `<p class="none">${D.store_degraded
      ? E(T(D.store_degraded.text || D.store_degraded, D.store_degraded.args)) : T("nothing proposed — the queue is empty")}</p>`;
    return;
  }
  // The header says how many are shown of how many wait in total.
  const total = (D.health && D.health.proposed) || q.length;
  head.textContent = T("Awaiting a person's decision — {shown} of {total}", {shown: q.length, total});
  // One line above the rows: what waits, of which kinds, and what
  // retention erases first.
  const dg = D.digest || null;
  const digestLine = dg ? `<p class="dmeta" id="queue-digest">${T("{n} waiting:", {n: dg.waiting})} ` +
    Object.entries(dg.by_kind || {}).map(([k, n]) => `${n} ${E(k)}`).join(", ") +
    " · " + T("retention erases a proposal after {n} d", {n: dg.horizon_days}) +
    (dg.erases_within_7d ? " — " + T("{n} go before {date}", {n: `<b>${dg.erases_within_7d}</b>`, date: E(dg.first_erase_on || "")}) : " — " + T("nothing goes this week")) +
    ` · <button class="chip-btn" type="button" data-copy="${E(toolCommand("review.py", ["digest"]))}" title="${T("copy the command")}">${T("Command: {label}", {label: T("queue digest")})}</button></p>` : "";
  host.innerHTML = digestLine + q.map(r => {
    const ok = toolCommand("review.py", ["promote", r.id, "--why", ""]);
    const no = toolCommand("review.py", ["reject", r.id, "--why", ""]);
    return `<div class="qrow" id="q-${E(r.id)}">
      <span class="qm">${E(r.at)}<br>${E(r.kind)}</span>
      <span>${E(r.statement)}${r.project
        ? ` <a class="plink" href="projects.html#${E(r.project)}">${E(String(r.project).split(":")[1])}</a>` : ""}
        <div class="anchor mono">${E(r.id)} r${E(r.rev)}</div>
        <div class="qacts"><button class="chip-btn" type="button" data-copy="${E(ok)}"
             title="${T("copy the accept command")}">${T("Command: {label}", {label: T("accept")})}</button>
          <button class="chip-btn" type="button" data-copy="${E(no)}"
             title="${T("copy the reject command")}">${T("Command: {label}", {label: T("reject")})}</button></div></span>
    </div>`;
  }).join("") +
    // The page carries the newest rows only; the rest are one command away,
    // and the command is handed over rather than left for the reader to find.
    (total > q.length ? (() => {
      const all = toolCommand("review.py", ["list"]);
      return `<p class="dmeta" id="queue-rest">${T("{n} more are waiting; the page shows the newest. The whole queue:", {n: total - q.length})} ` +
        `<span class="mono">${E(all)}</span> <button class="chip-btn" type="button" data-copy="${E(all)}" title="${T("copy the command")}">${T("Command: {label}", {label: T("the whole queue")})}</button></p>`;
    })() : "") +
    `<p class="none">${T("The button puts the command on the clipboard — the decision is taken in a terminal ({list} for the list): a write without a terminal is refused on purpose, and there is no {flag} flag.",
      {list: `<span class="mono">${E(cliCommand("review"))}</span>`, flag: '<span class="mono">--yes</span>'})}</p>`;
})();

(function renderFindings() {
  const F = D.findings, host = document.getElementById("findings");
  if (!F) {
    // Never built is said as such, with the command that builds it.
    host.innerHTML = `<div class="fh">${T("findings@@heading")} <span class="when">${T("not built")} — ` +
      `${E(cliCommand("findings"))}</span></div>`;
    return;
  }
  const c = F.counts, when = E((F.built_at || "").slice(0, 16).replace("T", " "));
  const SEV = { critical: [T("critical"), "danger"], warning: [T("warning"), "warn"],
               info: [T("info"), ""] };
  // A TYPE IS AN ID; THE READER GETS ITS NAME. The builder sends the label of
  // each type present (dashboard/finding_types.py), an English message id
  // the catalog translates; a type it does not name keeps its id, never blank.
  const TYPE_LABELS = F.type_labels || {};
  const typeLabel = type => TYPE_LABELS[type] ? T(TYPE_LABELS[type]) : String(type || "");
  const typeCounts = Object.entries(F.items.reduce((m, f) => (m[f.type] = (m[f.type] || 0) + 1, m), {}))
    .map(([k, n]) => [k, typeLabel(k), n])
    .sort((a, b) => a[1].localeCompare(b[1], LOCALE) || a[0].localeCompare(b[0]));
  const openCount = (c.critical || 0) + (c.warning || 0) + (c.info || 0);
  host.innerHTML = (openCount === 0 ? `<p class="empty">${T("No open findings.")}</p>` : '') +
    `<div class="fh">${T("Findings")} <span class="when">${c.critical || 0} ${T("critical")} · ` +
    `${c.warning || 0} ${T("warning")} · ${c.info || 0} ${T("info")}` +
    // Silenced findings are counted in the header too, so what was
    // acknowledged away is never invisible.
    `${(F.silenced || []).length ? " · " + T("{n} silenced", {n: F.silenced.length}) : ""}` +
    `${F.elsewhere ? ` · <a href="findings.html">${T("details and {n} more — on the findings page", {n: F.elsewhere})}</a>` : ""}` +
    (when ? " · " + T("unchanged since {date}", {date: when}) : "") + `</span></div>` +
    // The findings page gets its own filter bar: severity chips, a type
    // selector with per-type counts, and a search box. Elsewhere, anything
    // below critical sits behind one "show more" control.
    (PAGE === "findings"
      ? `<div class="fbar" role="group" aria-label="${T("Narrow the findings")}">` +
        ["critical", "warning", "info"].map(s => `<button class="chip-btn" type="button" data-sev="${s}" aria-pressed="false">${SEV[s][0]} <span class="n">${c[s] || 0}</span></button>`).join("") +
        `<select class="ftype" name="ftype" aria-label="${T("Finding type")}"><option value="">${T("all types")}</option>` +
        typeCounts.map(([k, label, n]) => `<option value="${E(k)}" title="${E(k)}">${E(label)} · ${n}</option>`).join("") +
        `</select><input type="search" class="fq" name="fq" placeholder="${T("Search the findings")}" aria-label="${T("Search the findings")}">` +
        `<span class="fshown" role="status"></span></div>`
      : "") +
    `<div class="flist">` + F.items.map(f => {
      const [word, kind] = SEV[f.severity] || [f.severity, ""];
      // Acknowledging is done in a terminal; the button copies that command.
      const cmd = toolCommand("ack.py", [f.id, "--why", ""]);
      const foldCls = ""; // Every selected finding is visible.
      // Each row has a stable anchor, so a link can point at one finding.
      const fid = "f-" + String(f.id).replace(/[^A-Za-z0-9_.:-]+/g, "-");
      const subj = subjectHref(f);
      return `<div class="f${f.severity === "critical" ? "" : " f" + f.severity}${foldCls}" id="${E(fid)}" data-type="${E(f.type)}" data-sev="${E(f.severity)}">${chip(word, kind)}` +
        `<span class="t"><span class="ftl" title="${E(f.type)}">${E(typeLabel(f.type))}</span>${E(findingTitle(f))}` +
        (subj ? ` <a class="plink fsubj" href="${E(subj[0])}" title="${T("open {name}", {name: E(subj[1])})}">→ ${E(subj[1])}</a>` : "") +
        // Named after its finding: its own text is "#", which is all a screen
        // reader announced for every row.
        (PAGE === "findings" ? ` <a class="fperma" href="#${E(fid)}" title="${T("link to this row")}" aria-label="${E(T("Link to: {title}", {title: findingTitle(f)}))}">#</a>` : "") +
        `</span>` +
        (f.deadline ? `<span class="due">${T("by {date}", {date: E(f.deadline)})}</span>` : "") +
        (PAGE === "index" ? `<a class="fdetail" href="findings.html#${E(fid)}">${T("Review →")}</a>` :
          `<details class="finding-body"><summary aria-label="${E(T("Evidence and action: {title}", {title: findingTitle(f)}))}">${T("Evidence and action")}</summary><p class="d">${E(f.detail)}</p>` +
          `<p class="act">${E(f.action)}</p>` +
          ` <button class="chip-btn ack" type="button" data-cmd="${E(cmd)}" title="${T("copy the silence command")}">${T("Command: {label}", {label: T("silence")})}</button></details>`) + '</div>';
    }).join("") +
    `</div>` +
    // Silenced findings are listed with who silenced them, when and why,
    // and the command that brings each back.
    ((F.silenced || []).length ? `<details class="silenced"><summary>${T("{n} silenced — not on the page or in the counters; here you see who, when and why", {n: F.silenced.length})}</summary>` +
      F.silenced.map(s => `<div class="f fsilenced">${chip(T("silenced"))}<span class="t">${E(findingTitle(s))}</span>` +
        `<span class="d">${E(s.acked.why || T("no reason"))} — ${E(s.acked.by || "?")}` +
        `${s.acked.until ? ", " + T("until {date}", {date: E(s.acked.until)}) : ""}</span>` +
        `<span class="act"><button class="chip-btn ack" type="button" data-cmd="${E(toolCommand("ack.py", ["--undo", s.id]))}">${T("Command: {label}", {label: T("restore")})}</button></span></div>`).join("") +
      `</details>` : "");
  // Copy buttons for acknowledge and undo, and per-type unfolding.
  host.querySelectorAll("button.ack").forEach(b => b.addEventListener("click", () => {
    const cmd = b.getAttribute("data-cmd"), label = b.textContent;
    copyText(cmd).then(ok => {
      toast(ok ? T("copied: {what}", {what: copiedWhat(label, cmd)}) : T("clipboard unavailable — copy it from the hint"));
      if (ok) { b.textContent = T("copied"); setTimeout(() => { b.textContent = label; }, 1500); }
    });
  }));
  host.querySelectorAll("button.ftype").forEach(b => b.addEventListener("click", () => {
    const type = b.getAttribute("data-type");
    const rows = host.querySelectorAll(`.f.ftype-folded[data-type="${type.replace(/"/g, '\\"')}"]`);
    const open = b.getAttribute("aria-expanded") !== "true";
    rows.forEach(r => r.classList.toggle("ftype-open", open));
    b.setAttribute("aria-expanded", String(open));
    b.textContent = open ? T("collapse {type}", {type: typeLabel(type)}) : T("{n} more of type {type}", {n: rows.length, type: typeLabel(type)});
  }));
  if (PAGE === "findings") {
    const bar = host.querySelector(".fbar");
    const sevOn = new Set();
    const rows = [...host.querySelectorAll(".flist > .f")];
    const apply = () => {
      const type = bar.querySelector(".ftype").value;
      const q = bar.querySelector(".fq").value.trim().toLowerCase();
      let shown = 0;
      rows.forEach(r => {
        const ok = (!sevOn.size || sevOn.has(r.dataset.sev)) && (!type || r.dataset.type === type)
          && (!q || r.textContent.toLowerCase().includes(q));
        r.classList.toggle("fhide", !ok);
        if (ok) shown++;
      });
      // While anything narrows the list, per-type folding is set aside.
      host.querySelectorAll("button.ftype").forEach(b => { b.hidden = !!(sevOn.size || type || q); });
      if (sevOn.size || type || q) host.querySelectorAll(".f.ftype-folded").forEach(r => r.classList.add("ftype-open"));
      const what = [];
      if (sevOn.size) what.push([...sevOn].map(s => SEV[s][0]).join(", "));
      if (type) what.push(T("type {type}", {type: typeLabel(type)}));
      if (q) what.push(T("search: “{text}”", {text: q}));
      bar.querySelector(".fshown").innerHTML = T("showing {shown} of {total}", {shown, total: rows.length}) +
        (what.length ? ` · ${E(what.join(" · "))} <button class="chip-btn" type="button" data-fclear>${T("reset")}</button>` : "");
      let none = host.querySelector(".fnone");
      if (!shown && what.length) {
        if (!none) { none = document.createElement("p"); none.className = "empty fnone"; host.querySelector(".flist").before(none); }
        none.innerHTML = `${T("Nothing found with this narrowing — {what}.", {what: E(what.join(" · "))})} <button class="chip-btn" type="button" data-fclear>${T("reset")}</button>`;
      } else if (none) none.remove();
    };
    bar.querySelectorAll("[data-sev]").forEach(b => b.addEventListener("click", () => {
      const on = b.getAttribute("aria-pressed") === "true";
      b.setAttribute("aria-pressed", String(!on));
      on ? sevOn.delete(b.dataset.sev) : sevOn.add(b.dataset.sev);
      apply();
    }));
    bar.querySelector(".ftype").addEventListener("change", apply);
    bar.querySelector(".fq").addEventListener("input", apply);
    host.addEventListener("click", ev => {
      if (!(ev.target.closest && ev.target.closest("[data-fclear]"))) return;
      sevOn.clear();
      bar.querySelectorAll("[data-sev]").forEach(b => b.setAttribute("aria-pressed", "false"));
      bar.querySelector(".ftype").value = ""; bar.querySelector(".fq").value = "";
      apply();
      // The reset button is removed by `apply()`; focus goes to the findings
      // search box rather than falling to <body>.
      bar.querySelector(".fq").focus();
    });
    // A finding named in the URL hash is unfolded and scrolled into view.
    const reveal = () => {
      const target = location.hash && location.hash.startsWith("#f-") && document.getElementById(location.hash.slice(1));
      if (target) {
        const body = target.querySelector(".finding-body"); if (body) body.open = true;
        sevOn.clear(); bar.querySelector(".ftype").value = ""; bar.querySelector(".fq").value = "";
        bar.querySelectorAll("[data-sev]").forEach(b => b.setAttribute("aria-pressed", "false"));
        apply(); target.scrollIntoView({block: "center"});
      }
    };
    reveal(); window.addEventListener("hashchange", reveal);
    apply();
  }
  const fold = document.getElementById("finfo");
  if (fold) fold.addEventListener("click", () => {
    const list = host.querySelector(".flist");
    const shown = list.classList.toggle("folded") === false;
    fold.setAttribute("aria-expanded", String(shown));
    const n = F.items.filter(f => f.severity !== "critical").length;
    fold.textContent = shown ? T("collapse") : T("show {n} more (warning and info)", {n});
  });
})();

if (PAGE) {
  // A split page: mark the body with its name, so the CSS shows only this
  // page's parts, and turn the tab buttons into links between pages.
  const bodyEl = document.body || document.documentElement;
  if (bodyEl && bodyEl.setAttribute) bodyEl.setAttribute("data-page", PAGE);
  if (TABLE_PAGES.includes(PAGE)) { tab = PAGE; active = activeBy[tab] || new Set(); }
  for (const t of TABLE_PAGES) {
    const b = document.getElementById("tab-" + t);
    if (b) { b.onclick = () => { location.href = t + ".html"; };
             b.setAttribute("aria-selected", String(t === tab)); }
    const s = document.getElementById("seg-" + t);
    if (s) s.hidden = t !== tab;
  }
  // The selector is filled for this page's tab.
  if (typeof fillOwners === "function" && TABLE_PAGES.includes(PAGE)
      && typeof SEL_BY_TAB !== "undefined" && SEL_BY_TAB[tab]) fillOwners();
}
stick();
render();
// Finally, open whatever the URL hash names.
fromHash();
// THE AGENTS PAGE STAYS CURRENT between ticks when the local server serves it:
// the server renders the same section again from the store (`/agents`, the same
// Python renderer as the built page) and the page swaps it in. Which workflows
// are expanded, and where focus is, survive the swap; a failed refresh keeps
// what is shown and says that live updates are paused. Opened as a file there
// is no server to ask, and the page says it is the snapshot of its build.
(function agentsLive() {
  if (PAGE !== "agents" || typeof fetch !== "function") return;
  const say = text => { const el = document.getElementById("agents-live"); if (el) el.textContent = text; };
  if (!String(location.protocol || "").startsWith("http")) {
    say(T("A snapshot of the last build. Open it through the local server for live updates."));
    return;
  }
  const keep = box => new Set([...box.querySelectorAll("details[open]")]
    .map(d => (d.querySelector("summary .mono") || {}).textContent));
  const refresh = () => fetch("/agents?locale=" + encodeURIComponent(LOCALE), {cache: "no-store"})
    .then(r => { if (!r.ok) throw new Error(String(r.status)); return r.text(); })
    .then(markup => {
      const box = document.getElementById("agents");
      if (!box) return;
      const open = keep(box);
      const within = document.activeElement && box.contains(document.activeElement)
        ? document.activeElement.closest("details") : null;
      const focused = within ? (within.querySelector("summary .mono") || {}).textContent : null;
      const holder = document.createElement("div");
      holder.innerHTML = markup;
      const fresh = holder.querySelector("#agents");
      if (!fresh) throw new Error("no section");
      fresh.querySelectorAll("details").forEach(d => {
        const id = (d.querySelector("summary .mono") || {}).textContent;
        if (open.has(id)) d.open = true;
      });
      box.replaceWith(fresh);
      if (focused) {
        const again = [...fresh.querySelectorAll("details summary")].find(
          s => (s.querySelector(".mono") || {}).textContent === focused);
        if (again) again.focus();
      }
      say(T("Live — refreshed {at}", {at: new Date().toLocaleTimeString(LOCALE)}));
    })
    .catch(() => say(T("Live updates are paused — this is the last data the page read.")));
  say(T("Live — refreshed {at}", {at: new Date().toLocaleTimeString(LOCALE)}));
  setInterval(refresh, 15000);
})();
</script>
</body>
</html>
"""

BRAND = Path(__file__).with_name("brand")
#: The PassionCode design system, vendored byte for byte (brand/manifest.json
#: pins its source commit and SHA-256), then the dashboard's own layout.
TEMPLATE = (TEMPLATE
            .replace("__PC_TOKENS__", (BRAND / "passioncode-tokens.css").read_text(encoding="utf-8"))
            .replace("</style>", (Path(__file__).with_name("workspace.css").read_text(encoding="utf-8")) + "\n</style>"))
import shell as _shell
TEMPLATE = TEMPLATE.replace("__ICON__", _shell.ICON)

if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(OUT, build())
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)")
    # The split pages, from the same payload: docs/dashboard/index.html is what
    # a person opens; the single page above is what the checks read.
    sizes = build_pages(build.last_payload)
    print(f"wrote {len(sizes)} page(s) under {paths.DASHBOARD_DIR}: "
          + ", ".join(f"{k} {v // 1024} KB" for k, v in sizes.items()))