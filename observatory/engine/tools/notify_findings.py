#!/usr/bin/env python3
"""Tell someone, once, about a finding that is new or has got worse.

THE HARD PART IS NOT SENDING, IT IS NOT SENDING AGAIN. A notice that fires every
thirty minutes teaches the operator to ignore the channel, and then it is worth
less than silence. Delivery is therefore recorded in the event store under
`kind='finding.notified'` with `ref='<finding id>@<severity>'`, and the schema's
own `events_dedup` UNIQUE(kind, ref) enforces once only: the guarantee lives in
the database, not in this file's control flow. Encoding severity in the ref is
what makes a rise in severity speak again while a steady finding stays quiet.

CHANNEL. macOS notification centre, which needs no credential, no network and no
external service. Sending findings to a chat or a mailbox would be publishing
this machine's private inventory somewhere else, and that is the operator's
decision to make explicitly, not a default for a background tick to take.

`info` findings never notify. They carry no deadline; they belong in the
dashboard and in `observatory_findings`, where they are read on purpose rather
than pushed.

    notify_findings.py [--dry-run]    # `--help` lists the flag that adds info
"""
from __future__ import annotations
import hashlib, json, os, pathlib, sqlite3, subprocess, sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths              
import atomic              

FINDINGS = paths.REGISTRY / "findings.json"
KIND = "finding.notified"
#: The other half of once-only: the record that an episode ENDED.
CLEARED = "finding.cleared"


def episode(con: sqlite3.Connection, fid: str) -> int:
    """Which occurrence of this finding we are in: the count of endings so far.

    **Once only was once FOR EVER.** The dedup key was `<id>@<severity>`, and
    the event store keeps it, so a finding that closed and came back was
    announced the first time and never again. Not hypothetical: soon after the
    channel went live, a notified finding closed within a day. Had it come
    back, the operator would never have heard about it, and the tool whose
    entire purpose is telling a human would have been silent BY DESIGN.

    So the key carries the episode: `<id>@<severity>#<k>`, where k is how many
    times the finding has been recorded as cleared. Every part stays idempotent
    (`UNIQUE(kind, ref)` still does the enforcing, one layer down) and the
    audit trail grows rather than being rewritten.

    Retention prunes `events` by age, so a finding open past the horizon loses
    its `notified` row AND its `cleared` rows together: it notifies once more,
    which is the right answer for something that has been true for a year, and
    the counter cannot drift out of step with the notifications it counts.
    """
    return con.execute("select count(*) from events where kind = ? and ref like ?",
                       (CLEARED, f"{fid}#%")).fetchone()[0]


def close_episodes(con: sqlite3.Connection, open_ids: set[str]) -> list[str]:
    """Record an ending for every notified finding that is no longer present.

    Written before anything is sent, because the send decision depends on it.
    The ref is `<id>#<k>`, so recording the k-th ending twice is ignored and the
    counter only advances when an ending is actually new.
    """
    notified = [r[0] for r in con.execute(
        "select ref from events where kind = ?", (KIND,))]
    #: id -> the episodes it has been notified in. An ending needs a beginning:
    #: without this the loop below wrote `#0`, then `#1`, then `#2` for a
    #: finding that had simply stayed away — one ending per run for ever, and
    #: the counter running ahead of reality until a recurrence was filed under
    #: an episode nothing had announced. Found by the test below on the third
    #: cycle, which is the first place it becomes visible.
    started: dict[str, set[int]] = {}
    for ref in notified:
        base, _, ep = ref.rpartition("#")
        if base and ep.isdigit():
            started.setdefault(base.rsplit("@", 1)[0], set()).add(int(ep))
        else:
            # A legacy ref, which IS episode 0 (see `already` in main).
            started.setdefault(ref.rsplit("@", 1)[0], set()).add(0)
    closed = []
    for fid in sorted(set(started) - open_ids):
        k = episode(con, fid)
        if k not in started[fid]:
            continue                                                       
        cur = con.execute(
            "insert or ignore into events (id, kind, ref, actor, occurred_at,"
            " payload_json) values (?,?,?,?,?,?)",
            (f"ev:cleared:{hashlib.sha256(f'{fid}#{k}'.encode()).hexdigest()[:16]}",
             CLEARED, f"{fid}#{k}", "tool:notify_findings", now_z(),
             json.dumps({"episode": k}, ensure_ascii=False)))
        if cur.rowcount:
            closed.append(f"{fid}#{k}")
    if closed:
        con.commit()
    return closed


def notify(title: str, body: str) -> tuple[bool, str]:
    """osascript takes AppleScript source, so a quote in a title is code.

    Returns (delivered, detail). The detail is what a finding needs: "it
    failed" is not actionable, and the usual reason here is specific: a
    launchd job is not attached to an Aqua session, so the window server
    refuses it.
    """
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    try:
        r = subprocess.run(
            ["osascript", "-e",
             f'display notification "{esc(body)}" with title "{esc(title)}"'],
            capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return True, "osascript accepted it"
        return False, f"osascript exited {r.returncode}: {(r.stderr or '').strip()[:160]}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def host_channel() -> str | None:
    """Who delivers instead of us, or None.

    ONE CHANNEL. When this service's Fabric descriptor is installed and Fabric
    Dashboards is on the machine, the host reads `finding.opened` from the
    events feed (with `notify: true`) and raises the banner itself, titled with
    the agent and what it wants. A banner from here as well is the same finding
    twice, from a sender that is not the host (Fabric Dashboards ADR-0010).
    The descriptor alone is not enough: without the app nobody would deliver.
    """
    override = os.environ.get("FABRIC_SERVICES_DIR")
    services = pathlib.Path(override).expanduser() if override else \
        pathlib.Path.home() / "Library/Application Support/ai.passioncode.fabric/services"
    if not (services / "project-observatory.default.json").is_file():
        return None
    apps = [pathlib.Path(os.environ["FABRIC_DASHBOARDS_APP"])] if os.environ.get("FABRIC_DASHBOARDS_APP") else \
        [pathlib.Path("/Applications/Fabric Dashboards.app"),
         pathlib.Path.home() / "Applications/Fabric Dashboards.app"]
    for app in apps:
        if app.is_dir():
            return f"delivered by Fabric Dashboards from the events feed ({app})"
    return None


def now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _report(when: str, delivered: bool, detail: str, undelivered: list[dict]) -> None:
    """What `tools/build_findings.py` reads to know whether the channel works."""
    atomic.write_json(paths.SCRATCH / "notify.json", {
        "attempted_at": when, "delivered": delivered, "detail": detail,
        "undelivered": undelivered})


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    levels = {"critical", "warning"} | ({"info"} if "--include-info" in argv else set())
    if not FINDINGS.is_file():
        print("notify_findings: no findings.json — run tools/build_findings.py")
        return 0
    try:
        doc = json.loads(FINDINGS.read_text(encoding="utf-8"))
        if not isinstance(doc.get("findings"), list):
            raise ValueError("no `findings` list")
    except (ValueError, OSError) as exc:
        # A REFUSAL WITH A RECEIPT. The document is written atomically, so this
        # is unlikely — and a traceback out of the tick's notify step would be a
        # channel that stopped working with no record of having stopped, which
        # is the failure this whole file is arranged against.
        print(f"notify_findings: findings.json is unreadable: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        _report(now_z(), False, f"findings.json unreadable: {type(exc).__name__}", [])
        return 1
    due = [f for f in doc["findings"]
           if f["severity"] in levels and not f.get("acked")]

    # `paths.DB`, not `paths.STORE / "observatory.db"`. The literal bypassed the
    # `OBSERVATORY_DB` override, so nothing could redirect this tool — and its
    # own first test wrote a fixture notification into the LIVE event store
    # before the line was noticed (2026-09-07; the row was removed by hand).
    # `tools/run_probes.py` keeps the literal on purpose and says why.
    db = paths.DB
    if not db.is_file():
        print("notify_findings: no event store; refusing to send without a way to "
              "record that it was sent")
        return 0
    con = sqlite3.connect(db)
    # ENDINGS FIRST. A finding that has gone is what makes the NEXT occurrence
    # of it new, so the record of its ending has to exist before the send
    # decision is taken.
    # NOT ON A DRY RUN. `close_episodes` WRITES — it is the record that an
    # episode ended — and a command whose whole promise is "this changes
    # nothing" must not advance the counter that decides what the next real run
    # says. Caught by running `--dry-run` against the live store one minute
    # after the function was added.
    closed = [] if dry else close_episodes(con, {f["id"] for f in doc["findings"]})
    seen = {r[0] for r in con.execute(
        "select ref from events where kind = ?", (KIND,))}

    def ref_of(f: dict) -> str:
        return f"{f['id']}@{f['severity']}#{episode(con, f['id'])}"

    # LEGACY REFS. The fifteen rows written before 2026-09-07 carry
    # `<id>@<severity>` with no episode, which IS episode 0 — read any other way
    # they would all re-notify at once, which is the noise this file exists to
    # prevent.
    def already(f: dict) -> bool:
        r = ref_of(f)
        return r in seen or (r.endswith("#0") and r[:-2] in seen)

    fresh = [f for f in due if not already(f)]
    if not fresh:
        print(f"notify_findings: {len(due)} open, nothing new since the last run"
              + (f"; {len(closed)} finding(s) closed: {', '.join(closed)}" if closed else ""))
        con.close()
        # WRITTEN HERE TOO. The report used to be produced only after a send
        # attempt, so a quiet run left the previous run's file in place, and a
        # stale "delivered: false" would have kept the channel finding lit after
        # the channel recovered. The same early return shape as the review
        # digest: a writer placed after a return that the common case takes
        # never runs in the common case.
        _report(now_z(), True, "nothing new to deliver", [])
        return 0

    crit = [f for f in fresh if f["severity"] == "critical"]
    head = (f"{len(crit)} critical" if crit else f"{len(fresh)} new") + " finding" \
           + ("s" if (len(crit) if crit else len(fresh)) != 1 else "")
    body = "; ".join(f["title"] for f in (crit or fresh)[:3])
    if len(fresh) > 3:
        body += f"; +{len(fresh) - 3} more"

    host = host_channel()
    if host:
        con.close()
        if not dry:
            _report(now_z(), True, host, [])
        print(f"notify_findings: {len(fresh)} new/escalated, {host}; no banner of our own")
        return 0

    if dry:
        print(f"[dry-run] would notify: {head} — {body}")
        for f in fresh:
            print(f"  {f['severity']:8s} {f['title']}")
        con.close()
        return 0

    sent, detail = notify(f"Observatory — {head}", body)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # RECORDED ONLY IF IT WAS DELIVERED. This inserted the event either way and
    # said so out loud — "FAILED — recorded anyway" — which meant one failed
    # `osascript` marked a critical finding as notified FOR EVER: the dedup key
    # is `(kind, ref)`, so the next run sees it in `seen` and stays silent. In
    # the one tool whose entire purpose is telling a human, a failure to tell
    # them was written down as having told them.
    #
    # This has not bitten on this machine — all twelve recorded notifications
    # carry `delivered: true` (measured 2026-09-07) — but the path is reachable
    # from launchd, where a job not attached to an Aqua session cannot reach the
    # window server at all.
    if sent:
        for f in fresh:
            ref = ref_of(f)
            con.execute(
                "insert or ignore into events (id, kind, ref, actor, occurred_at, "
                "payload_json) values (?,?,?,?,?,?)",
                # A STABLE id. `abs(hash(ref))` is randomised per process by
                # PYTHONHASHSEED, so the same fact got a different id on every
                # run; the UNIQUE(kind, ref) index hid that, and a re-run after
                # a delete would have written the same fact under a new name.
                (f"ev:notify:{hashlib.sha256(ref.encode()).hexdigest()[:16]}",
                 KIND, ref, "tool:notify_findings", now,
                 json.dumps({"title": f["title"], "severity": f["severity"],
                             "delivered": True}, ensure_ascii=False)))
        con.commit()
    con.close()

    # A CHANNEL THAT CANNOT DELIVER IS ITSELF A FINDING. Without this, not
    # recording a failure would trade a permanent silence for a retry every
    # thirty minutes that nobody hears either — the operator would learn
    # nothing from either shape. `build_findings.py` reads this file.
    _report(now, sent, detail,
            [] if sent else [{"id": f["id"], "severity": f["severity"],
                              "title": f["title"]} for f in fresh])
    print(f"notify_findings: {len(fresh)} new/escalated, "
          f"notification {'delivered' if sent else 'NOT DELIVERED — kept fresh so a later run can try, and raised as a finding'}")
    for f in fresh:
        print(f"  {f['severity']:8s} {f['title']}")
    return 0


if __name__ == "__main__":
    import configuration
    if (True) and not configuration.enabled("notifications", "features"):
        print("Not configured: enable features.notifications explicitly")
        raise SystemExit(0)

    raise SystemExit(main(sys.argv))
