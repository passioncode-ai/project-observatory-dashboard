#!/usr/bin/env python3
"""`GET /fabric/v1/events`: the event store, read as sentences a person follows.

fabric-service/0.1 asks a service to publish what it did as an append-only feed
with a cursor, and to build that feed as a VIEW over the log it already keeps
rather than a second store that can drift from the first. The observatory
already keeps one: the `events` table of `store/observatory.db`, where
`collectors/scan_events.py` writes commits, `collectors/scan_sessions.py` agent
sessions and `tools/notify_findings.py` every finding it announced
(`finding.notified`) and every one that went away (`finding.cleared`). Nothing
here writes; the store is opened read-only and never migrated.

IDS AND THE CURSOR. The table's key is text (`commit:<sha>`), which does not
order. Its SQLite `rowid` does: rows are numbered in the order they were
inserted, so "everything after the last row I showed you" is `rowid > n` and a
host polling with its cursor sees each insert once. An event's id is its
highest rowid, zero-padded to twelve digits so the ids also increase as
strings. One limit is stated rather than hidden: SQLite reuses the highest
rowid when the newest row is deleted before the next insert (the only deleter
of fresh rows is a session withdrawn as ambiguous), and a host already past
that id misses the one row that reuses it.

GROUPING. A collector inserts one repository's commits as one run of rows, so
consecutive rows of the same kind, project and author are one event —
"3 commits in Fabric by A. Author" — instead of three. A page read forward
always starts right after an id this feed issued, which is the last row of a
group, so paging never splits a group. A run that grows after it was shown
(the same author's next commits, inserted before any other row) arrives as a
second event after the host's cursor; only the cursor-less "newest" view, which
a host reads once to start, would show the two halves as one. Finding rows are
never grouped: each is a decision for the operator. A page reads at most
`SCAN_LIMIT` rows, so the first-ever read of a 40,000-commit history stays
bounded; a group cut there ends at the cut and the next page continues after it.

NOTIFY. Only a newly opened finding asks a host to notify — the same findings,
at the same severities, that the observatory's own notification step already
decided a person must hear about. Commits, sessions and cleared findings are
routine.

RETENTION is the store's: `config/retention.json` `events_days` (365 by
default), far above the protocol's floor of seven days or 1000 events. A cursor
older than what is retained returns the oldest page that remains.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Iterable
from urllib.parse import quote

import fabric_service as fs

ID_WIDTH = 12
SCAN_LIMIT = 20000
CHUNK = 1000
SUBJECT_LIMIT = 160
#: Kinds whose consecutive rows fold into one event.
GROUPED = ("commit", "session")
SEVERITY_LEVEL = {"critical": "error", "warning": "warning", "info": "notice"}
_CURSOR = re.compile(r"^[0-9]{1,18}$")


class FeedError(Exception):
    """The feed cannot be read; the message is one sentence for the operator."""


def event_id(rowid: int) -> str:
    return "%0*d" % (ID_WIDTH, rowid)


def parse_cursor(after: str | None) -> int | None:
    if after is None or after == "":
        return None
    if not _CURSOR.match(after):
        raise fs.ServiceError("The cursor is not one this service issued.")
    return int(after)


def _utc(value: str | None) -> str:
    """Any ISO spelling the store holds -> `YYYY-MM-DDTHH:MM:SSZ`."""
    if value:
        text = value.strip().replace("Z", "+00:00")
        try:
            moment = datetime.fromisoformat(text)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass
    return fs.now_iso()


def _payload(raw: str | None) -> dict:
    try:
        doc = json.loads(raw or "{}")
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _sentence(text: str) -> str:
    text = " ".join(str(text).split()).rstrip(" .;,")
    return text + "."


def _quote(text: str) -> str:
    text = " ".join(str(text).split())
    if len(text) > SUBJECT_LIMIT:
        text = text[: SUBJECT_LIMIT - 1] + "…"
    return "“" + text + "”"


def _project_label(project_id: str | None, payload: dict, labels: dict[str, str]) -> str:
    if project_id and project_id in labels:
        return labels[project_id]
    if payload.get("repo"):
        return str(payload["repo"])
    if project_id:
        return project_id.split(":", 1)[-1]
    return "an unregistered project"


def project_link(project_id: str) -> str:
    """The Projects page with that project's panel open (`#project:<id>`)."""
    return "/dashboard/projects.html#" + quote(project_id, safe=":._-~")


def finding_link(finding_id: str | None) -> str:
    """The Findings page scrolled to the row; the anchor rule is the page's own."""
    if not finding_id:
        return "/dashboard/findings.html"
    return "/dashboard/findings.html#f-" + re.sub(r"[^A-Za-z0-9_.:-]+", "-", finding_id)


def _finding_id(ref: str | None) -> str | None:
    """`<id>@<severity>#<k>` (notified) or `<id>#<k>` (cleared) -> `<id>`."""
    if not ref:
        return None
    base = ref.rsplit("#", 1)[0] if "#" in ref else ref
    return base.rsplit("@", 1)[0] if "@" in base else base


class _Group:
    __slots__ = ("key", "rows")

    def __init__(self, key: tuple, row: sqlite3.Row) -> None:
        self.key = key
        self.rows = [row]


def _key(row: sqlite3.Row) -> tuple:
    kind = row["kind"]
    if kind == "commit":
        return ("commit", row["project_id"] or row["repo_id"], row["actor"])
    if kind == "session":
        return ("session", row["project_id"])
    return ("single", row["rowid"])


def _title_of(conn: sqlite3.Connection, fid: str | None) -> str | None:
    """The title the finding was announced under, from its own `finding.notified` row."""
    if not fid:
        return None
    # A range on the (kind, ref) unique index, not LIKE: LIKE is case-folding and
    # would not use the index. '@' + 1 is 'A'.
    row = conn.execute(
        "SELECT payload_json FROM events WHERE kind='finding.notified' AND ref >= ? AND ref < ?"
        " ORDER BY rowid DESC LIMIT 1", (fid + "@", fid + "A")).fetchone()
    title = _payload(row[0]).get("title") if row else None
    return str(title) if title else None


def _render(group: _Group, conn: sqlite3.Connection, labels: dict[str, str]) -> dict:
    rows = sorted(group.rows, key=lambda r: r["rowid"])
    newest = max(rows, key=lambda r: (_utc(r["occurred_at"]), r["rowid"]))
    at = _utc(newest["occurred_at"])
    ident = event_id(rows[-1]["rowid"])
    head = rows[0]
    kind = head["kind"]
    payload = _payload(newest["payload_json"])
    project_id = head["project_id"]
    subject = None
    link = None
    if project_id:
        label = _project_label(project_id, payload, labels)
        subject = {"type": "project", "id": project_id[:128], "label": label[:120]}
        link = project_link(project_id)

    if kind == "commit":
        where = _project_label(project_id, payload, labels)
        who = head["actor"] or "an unknown author"
        latest = payload.get("subject")
        if len(rows) == 1:
            text = f"Commit in {where} by {who}" + (f": {_quote(latest)}" if latest else "")
        else:
            text = (f"{len(rows)} commits in {where} by {who}"
                    + (f"; latest {_quote(latest)}" if latest else ""))
        return fs.make_event(ident, at, "project.commits", "info", _sentence(text),
                             subject=subject, link=link)
    if kind == "session":
        where = _project_label(project_id, payload, labels)
        prompts = sum(int(_payload(r["payload_json"]).get("prompts") or 0) for r in rows)
        count = f" ({prompts} prompt{'s' if prompts != 1 else ''})" if prompts else ""
        text = ("An agent session worked in " + where + count if len(rows) == 1
                else f"{len(rows)} agent sessions worked in {where}" + count)
        return fs.make_event(ident, at, "project.sessions", "info", _sentence(text),
                             subject=subject, link=link)
    if kind == "finding.notified":
        fid = _finding_id(head["ref"])
        severity = str(payload.get("severity") or "warning")
        title = str(payload.get("title") or "a finding on the dashboard needs a look")
        subject = {"type": "finding", "id": (fid or "finding")[:128], "label": title[:120]}
        return fs.make_event(ident, at, "finding.opened", SEVERITY_LEVEL.get(severity, "warning"),
                             _sentence(f"New {severity} finding: {title}"), subject=subject,
                             link=finding_link(fid), notify=True)
    if kind == "finding.cleared":
        fid = _finding_id(head["ref"])
        title = _title_of(conn, fid)
        text = f"Resolved: {title}" if title else "A finding the dashboard raised is resolved"
        if fid:
            subject = {"type": "finding", "id": fid[:128], "label": (title or "resolved finding")[:120]}
        return fs.make_event(ident, at, "finding.cleared", "info", _sentence(text),
                             subject=subject, link=finding_link(None))
    where = _project_label(project_id, payload, labels) if project_id or payload.get("repo") else None
    if kind == "deploy":
        what = payload.get("release") or payload.get("tag") or (head["ref"] or "")[:12]
        text = f"Deploy of {what} in {where}" if where else f"Deploy of {what}"
        return fs.make_event(ident, at, "project.deploy", "notice", _sentence(text),
                             subject=subject, link=link)
    if kind == "scan":
        text = f"A scan of {where} was recorded" if where else "A scan was recorded"
        return fs.make_event(ident, at, "project.scan", "info", _sentence(text),
                             subject=subject, link=link)
    # A kind this view does not know yet still reaches the feed, as a sentence
    # rather than a machine id: the words of its kind, never the kind itself.
    words = re.sub(r"[^a-z]+", " ", str(kind).lower()).strip() or "activity"
    text = f"New {words} recorded" + (f" for {where}" if where else "")
    return fs.make_event(ident, at, "project.activity", "info", _sentence(text),
                         subject=subject, link=link)


_COLUMNS = "rowid, kind, project_id, repo_id, actor, ref, occurred_at, payload_json"


def _rows(conn: sqlite3.Connection, after: int | None) -> Iterable[sqlite3.Row]:
    """Forward from `after`, or backward from the newest row, in bounded chunks."""
    seen = 0
    if after is not None:
        floor = after
        while seen < SCAN_LIMIT:
            batch = conn.execute(f"SELECT {_COLUMNS} FROM events WHERE rowid > ? ORDER BY rowid LIMIT ?",
                                 (floor, CHUNK)).fetchall()
            if not batch:
                return
            for row in batch:
                seen += 1
                yield row
                if seen >= SCAN_LIMIT:
                    return
            floor = batch[-1]["rowid"]
    else:
        ceiling = None
        while seen < SCAN_LIMIT:
            if ceiling is None:
                batch = conn.execute(f"SELECT {_COLUMNS} FROM events ORDER BY rowid DESC LIMIT ?",
                                     (CHUNK,)).fetchall()
            else:
                batch = conn.execute(f"SELECT {_COLUMNS} FROM events WHERE rowid < ? ORDER BY rowid DESC LIMIT ?",
                                     (ceiling, CHUNK)).fetchall()
            if not batch:
                return
            for row in batch:
                seen += 1
                yield row
                if seen >= SCAN_LIMIT:
                    return
            ceiling = batch[-1]["rowid"]


def _groups(conn: sqlite3.Connection, after: int | None, limit: int) -> list[_Group]:
    """Up to `limit` complete groups: a group is complete once a row of another
    group follows it (in scan order), or the rows ran out."""
    groups: list[_Group] = []
    for row in _rows(conn, after):
        key = _key(row)
        if groups and key[0] in GROUPED and groups[-1].key == key:
            groups[-1].rows.append(row)
            continue
        if len(groups) == limit:
            break
        groups.append(_Group(key, row))
    if after is None:
        groups.reverse()
    return groups


def connect(db: Path) -> sqlite3.Connection | None:
    """A read-only connection, or None when the store does not exist yet."""
    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn
    except sqlite3.Error as exc:
        raise FeedError(f"The event store could not be opened ({type(exc).__name__}).") from None


def page(db: Path, after: str | None, limit: int, labels: dict[str, str]) -> dict:
    """The protocol's events page. Raises ServiceError for a bad cursor, FeedError
    when the store exists but cannot be read."""
    floor = parse_cursor(after)
    conn = connect(db)
    if conn is None:
        return {"events": [], "cursor": after or None}
    try:
        def fetch(_after, n):
            try:
                return [_render(g, conn, labels) for g in _groups(conn, floor, n)]
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return []
                raise FeedError(f"The event store could not be read ({type(exc).__name__}).") from None
            except sqlite3.Error as exc:
                raise FeedError(f"The event store could not be read ({type(exc).__name__}).") from None
        return fs.events_page(fetch, after or None, limit)
    finally:
        conn.close()


def latest_id(db: Path) -> str | None:
    """The newest event id, for the heartbeat; None when there is no store or no row."""
    try:
        conn = connect(db)
    except FeedError:
        return None
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT max(rowid) FROM events").fetchone()
        return event_id(row[0]) if row and row[0] is not None else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()
