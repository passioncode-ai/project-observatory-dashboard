#!/usr/bin/env python3
"""The daily learning review (OBS-331): once a UTC day, one local report of what agents learned and what failed.

WHAT IT READS, by arithmetic and with no model:

* **Lessons** (`observatory_lesson`, `store/lessons.py`): every revision of every unerased lesson record. A revision
  is placed on the UTC day of its `created_at`, so a lesson minted, revised, replaced, promoted or invalidated on day D
  is listed in D's report, each with the record and revision it came from (`ledger:<memoryId>@<revision>`).
* **The corroborator** (`tools/corroborate.py` `corroborate_lessons`), unchanged: it is the one rule that promotes a
  lesson, and the review never promotes by any other. When no tick that refreshed the registry ran it within a day
  (its receipt `corroboration.json` is absent or older than yesterday), the review runs it for real, so a lesson two
  distinct projects hold is promoted at least daily; otherwise it runs dry, for the waiting, not resolvable and
  contradicted lists only.
* **Products' daily operations digests** (`ops_digest_store.py`, OBS-330): the FIRST revision of each line (OD-6),
  read for the rises the product itself recorded in an hour (OD-4), a day-over-day rise and an operation that rose on
  several of the last seven days.
* **The registry**, read only, to name the project a lesson's pseudonymous key belongs to: the clones that carry the
  key, each clone's repository, the projects that repository implements. A key no registered clone carries keeps
  `project: null`.

WHAT IT WRITES: `<state>/learning-review/<YYYY-MM>/<YYYY-MM-DD>.json` (`observatory-learning-review/1`) and the same
report as one self-contained HTML page beside it, owner-only, plus its run record `state.json`. Never `registry/`
(committed and projected), never the wiki, the ledger, the board or Fabric: nothing leaves the machine.

THE DAY IS A UTC DAY, because a digest's `day` is one by contract (DEC-0153 OD-1) and the digest store is keyed by it;
a local boundary would split one digest line across two reports. The report of D is due at the first run at or after
D+1 at the settle hour (`settle_hour_utc`, 6 by default, the setting `learning_review.settle_hour_utc`), so an
overnight machine's catch-up arrives before it is written. A complete report is never rewritten by the tick; a
partial one (a source could not be read) is rewritten at each due run until it is complete or until D+2 at the settle
hour. `run --day D --again` writes a new revision and keeps the previous one beside it as `<day>.r<n>.json`.

RW-4, held here: the `ok` of a host's `retro-after-work` operation counts the retro requests answered by a retro the
host saw written, never the retros written. The report shows asked, answered, refused and not seen side by side and
never divides, subtracts or labels them as retros written.

What this module does not do yet (OBS-331 parts b and c): the MCP tool, the Health row, doctor's key, the two finding
types and the translated page (part b); the `insights/` reader and the counter of refused digest lines (part c). The
report says so in its notes rather than showing an empty count as a measured zero.
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA = "observatory-learning-review/1"
STATE_SCHEMA = "observatory-learning-review-state/1"
DIR = "learning-review"
#: The hour of D+1 (UTC) at which the report of D is due. The setting `learning_review.settle_hour_utc` overrides it.
SETTLE_HOURS = 6
#: Reports one run writes at most when days are missing; an older missing day is named, never written.
CATCHUP_DAYS = 7
#: The digests' horizon: a report older than this is pruned, and no day older than it is reviewed.
KEEP_DAYS = 400
#: A day-over-day rise reuses OD-4's numbers (`ops_digest.RISE_MIN_FAILED`, `ops_digest.RISE_FACTOR`); an
#: operation that rose on at least REPEAT_MIN of the REPEAT_DAYS days ending on D is rising repeatedly.
REPEAT_MIN = 3
REPEAT_DAYS = 7
#: How far back a digest line that arrived late is looked for: a catch-up after a machine was off for a month.
LATE_DAYS = 31
#: Bounds on what one report lists; the rest is counted in `truncated`.
MAX_LESSONS = 500
MAX_CLASSES = 200
MAX_FAILURES = 500
MAX_BYTES = 8 * 1024 * 1024
#: How long a second writer waits for the first before it answers `review-locked`.
LOCK_WAIT_SECONDS = 30.0
#: The run record keeps this many runs.
RUNS_KEPT = 30
#: Time the review gives git to name the projects of lesson keys (one root-commit read per registered clone).
KEYS_SECONDS = 60.0

DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
REPORT_NAME = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})\.json")
RETRO_OP = "retro-after-work"
LESSON_CORROBORATOR = "agent:lesson-corroborator"
RISE_CODES = ("risen-in-hour", "rose-since-yesterday", "rising-repeatedly")

#: Every refusal of the CLI, with what to do about it.
HINTS = {
    "day-invalid": "a day is written YYYY-MM-DD, a real calendar day",
    "day-not-closed": "a UTC day is reviewed once it has ended; yesterday is the newest day a review may cover",
    "day-outside-retention": f"reports and digests are kept {KEEP_DAYS} days; an older day is not reviewed",
    "no-report": "no review has been written for that day yet: `project-observatory full learning-review run` "
                 "writes what is due",
    "report-unreadable": "the report file is damaged; `project-observatory full learning-review run --again` with "
                         "its day writes a new revision and keeps the damaged one beside it",
    "review-locked": "another review is being written; run it again in a minute",
    "review-unwritable": "the review folder cannot be written (a full disk, or a folder that is not this "
                         "account's); nothing was written and the previous report stands",
}

#: Read by the report and its page so a person sees the rule each number came from.
NOTES = {
    "identical-repeats-invisible": "An agent that records the same lesson again, word for word, writes nothing, so "
                                   "a repeat like that cannot be counted.",
    "refusals-not-counted": "Digest lines a product sent and Observatory refused are not counted yet (OBS-331 "
                            "part c); a refused line is missing from these counts without a trace here.",
    "insights-not-read": "Insight lines (insights/*.jsonl, insight/1) are not read yet (OBS-331 part c), and the "
                         "contract has not decided their grammar; nothing about insights is measured here.",
    "retro-asks-are-not-retros": "retro-after-work counts retro requests, never retros written: one retro may answer "
                                 "several asks (contract DEC-0153 RW-4).",
}


class Refused(Exception):
    """A typed refusal of the CLI: an answer with its code and hint, never a traceback."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code, self.detail = code, detail

    def envelope(self) -> dict:
        return {"error": "learning review refused", "code": self.code, "detail": self.detail,
                "hint": HINTS.get(self.code, "")}


class Locked(Refused):
    def __init__(self) -> None:
        super().__init__("review-locked", f"another writer held the lock for {LOCK_WAIT_SECONDS:.0f} s")


# ── time ────────────────────────────────────────────────────────────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_day(value: Any) -> date | None:
    if not isinstance(value, str) or not DAY.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def settle_hour() -> int:
    """`learning_review.settle_hour_utc` from the workspace settings, else SETTLE_HOURS. A setting that cannot be
    read is not a reason to stop the review: the default is used, and `configuration.load` names the fault to the
    commands that read settings."""
    try:
        import configuration
        value = (configuration.load().get("learning_review") or {}).get("settle_hour_utc", SETTLE_HOURS)
    except Exception:  # noqa: BLE001
        return SETTLE_HOURS
    return value if type(value) is int and 0 <= value <= 23 else SETTLE_HOURS


def due_at(day: date, hour: int) -> datetime:
    """When the report of `day` becomes due: D+1 at the settle hour, UTC."""
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1, hours=hour)


def newest_due_day(now: datetime, hour: int) -> date:
    """The newest day whose report is due at `now`."""
    today = now.astimezone(timezone.utc).date()
    return today - timedelta(days=1) if now >= due_at(today - timedelta(days=1), hour) else today - timedelta(days=2)


def local_offset(at: datetime | None = None) -> str:
    """The machine's offset from UTC when the report is written, `+01:00`."""
    offset = (at or datetime.now()).astimezone().utcoffset() or timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


# ── where reports live ──────────────────────────────────────────────────────────────────────────────────────

def root() -> Path:
    import paths
    return Path(paths.STATE) / DIR


def report_path(day: date, suffix: str = ".json") -> Path:
    return root() / day.isoformat()[:7] / f"{day.isoformat()}{suffix}"


def state_path() -> Path:
    return root() / "state.json"


def _read_json(path: Path, limit: int = MAX_BYTES + 1024 * 1024) -> dict | None:
    """A document of this module, or None when absent. A file that is there and is not one raises ValueError."""
    import osprivacy
    try:
        raw = osprivacy.read_regular(path, limit)
    except FileNotFoundError:
        return None
    doc = json.loads(raw.decode("utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"{path.name} is not an object")
    return doc


def read_report(day: date) -> dict | None:
    """The report of `day`, None when there is none; raises Refused(report-unreadable) for a damaged file."""
    try:
        doc = _read_json(report_path(day))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise Refused("report-unreadable", f"{day.isoformat()}: {type(exc).__name__}") from None
    if doc is not None and doc.get("schema") != SCHEMA:
        raise Refused("report-unreadable", f"{day.isoformat()} is not an {SCHEMA} document")
    return doc


def _report_or_none(day: date) -> dict | None:
    """For the due rule: a damaged report counts as none, so the next run writes it again."""
    try:
        return read_report(day)
    except Refused:
        return None


def report_days() -> list[date]:
    """Every day with a report on disk, oldest first."""
    out = []
    base = root()
    if not base.is_dir():
        return out
    for folder in sorted(p for p in base.iterdir() if p.is_dir() and not p.is_symlink()):
        for path in folder.iterdir():
            m = REPORT_NAME.fullmatch(path.name)
            day = parse_day(m.group(1)) if m else None
            if day is not None:
                out.append(day)
    return sorted(set(out))


def read_state() -> dict:
    try:
        doc = _read_json(state_path())
    except (OSError, ValueError, UnicodeDecodeError):
        doc = None
    if not isinstance(doc, dict) or doc.get("schema") != STATE_SCHEMA:
        return {"schema": STATE_SCHEMA, "lastWritten": None, "runs": [], "insights": {}}
    doc.setdefault("runs", [])
    doc.setdefault("insights", {})
    return doc


class _Lock:
    """One writer at a time across processes (the tick and a person's `run`), as the digest store's lock: a NOFOLLOW
    open, so a link planted as the lock file is refused. The second writer waits up to LOCK_WAIT_SECONDS."""

    def __init__(self, wait: float | None = None) -> None:
        self.wait = LOCK_WAIT_SECONDS if wait is None else wait

    def __enter__(self):
        import oslocks
        import osprivacy
        base = root()
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        osprivacy.private_folder(base)
        self.fd = osprivacy.open(base / ".lock", os.O_CREAT | os.O_RDWR | osprivacy.NOFOLLOW | osprivacy.NONBLOCK,
                                 0o600)
        deadline = time.monotonic() + self.wait
        while True:
            try:
                oslocks.flock(self.fd, oslocks.LOCK_EX | oslocks.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    raise Locked() from None
                time.sleep(0.2)

    def __exit__(self, *exc):
        os.close(self.fd)
        return False


# ── the due rule ────────────────────────────────────────────────────────────────────────────────────────────

def plan(now: datetime, hour: int | None = None) -> dict:
    """What a run at `now` writes: `days` (oldest first, at most CATCHUP_DAYS), and `notReviewed`, the missing days
    older than that window and newer than the last report written (a first run names none: there was no review to
    miss). Pure over the report files on disk."""
    hour = settle_hour() if hour is None else hour
    newest = newest_due_day(now, hour)
    today = now.astimezone(timezone.utc).date()
    oldest_kept = today - timedelta(days=KEEP_DAYS)
    window = [newest - timedelta(days=n) for n in range(CATCHUP_DAYS - 1, -1, -1)]
    window = [d for d in window if d >= oldest_kept]
    days = []
    for day in window:
        doc = _report_or_none(day)
        if doc is None:
            days.append(day)
        elif doc.get("state") == "partial" and now < due_at(day + timedelta(days=1), hour):
            days.append(day)
    on_disk = [d for d in report_days() if window and d < window[0]]
    not_reviewed = []
    if on_disk and window:
        last = on_disk[-1]
        gap = (window[0] - last).days - 1
        not_reviewed = [last + timedelta(days=n) for n in range(1, gap + 1) if last + timedelta(days=n) >= oldest_kept]
    return {"days": days, "notReviewed": not_reviewed, "newest": newest, "settleHourUtc": hour}


def is_due(now: datetime | None = None) -> bool:
    return bool(plan(now or utc_now())["days"])


# ── the registry: which project a lesson key belongs to ────────────────────────────────────────────────────

def _registry_doc(name: str, key: str) -> list:
    import paths
    try:
        doc = json.loads((Path(paths.REGISTRY) / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = doc.get(key) if isinstance(doc, dict) else None
    return rows if isinstance(rows, list) else []


def registry_projects() -> set[str]:
    return {p["id"] for p in _registry_doc("projects.json", "projects")
            if isinstance(p, dict) and isinstance(p.get("id"), str)}


def key_projects(keys: dict[str, list[Path]]) -> dict[str, str | None]:
    """Pseudonymous key -> the registry project its clones implement, or None. Measured links only: a clone's
    repository by its recorded `local.path`, the project by an `implemented_by` relation (registry rule 2: never a
    match by a name's look). Several projects: the first by id, so the answer is the same each day."""
    by_path: dict[str, str] = {}
    for r in _registry_doc("repositories.json", "repositories"):
        local = r.get("local") if isinstance(r, dict) else None
        p = local.get("path") if isinstance(local, dict) else None
        if isinstance(p, str) and isinstance(r.get("id"), str):
            by_path[os.path.realpath(p)] = r["id"]
    of_repo: dict[str, set[str]] = {}
    for rel in _registry_doc("relations.json", "relations"):
        if isinstance(rel, dict) and rel.get("type") == "implemented_by" \
                and isinstance(rel.get("from"), str) and isinstance(rel.get("to"), str):
            of_repo.setdefault(rel["to"], set()).add(rel["from"])
    out: dict[str, str | None] = {}
    for key, clones in keys.items():
        projects = set()
        for clone in clones:
            repo = by_path.get(os.path.realpath(str(clone)))
            projects |= of_repo.get(repo, set()) if repo else set()
        out[key] = sorted(projects)[0] if projects else None
    return out


# ── lessons ─────────────────────────────────────────────────────────────────────────────────────────────────

def _redactor():
    """One redactor per run: the known-value list grows, so a lesson redacted when it was written is scrubbed again
    before it enters a report (`retrieval_audit.scrub_text` does the same per call)."""
    import memory_redact
    return memory_redact.Redactor()


class Scrubber:
    def __init__(self) -> None:
        self.red = _redactor()

    def __call__(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return self.red.scrub(value)[0]


def lesson_rows(conn) -> list[dict]:
    """Every revision of every unerased lesson record, oldest first, its body parsed."""
    rows = conn.execute(
        "SELECT l.* FROM ledger l LEFT JOIN tombstones t ON t.memory_id = l.memory_id"
        " WHERE t.memory_id IS NULL AND l.kind = 'lesson' ORDER BY l.memory_id, l.revision").fetchall()
    out = []
    for r in rows:
        row = dict(r)
        for name in ("body_json", "provenance_json"):
            try:
                value = json.loads(row.get(name) or ("{}" if name == "body_json" else "[]"))
            except ValueError:
                value = {} if name == "body_json" else []
            row[name[:-5]] = value
        if not isinstance(row["body"], dict):
            row["body"] = {}
        if not isinstance(row["provenance"], list):
            row["provenance"] = []
        out.append(row)
    return out


def _replaces(row: dict) -> str | None:
    for p in row["provenance"]:
        if isinstance(p, dict) and isinstance(p.get("replaces"), str):
            return p["replaces"]
    return None


def classify(rows: list[dict]) -> list[dict]:
    """Every revision's code, or none: lesson-new, lesson-revised, lesson-replaced, lesson-promoted,
    observation-invalidated. An archived revision is the closing half of a replacement, told by its successor."""
    by_id: dict[str, list[dict]] = {}
    for r in rows:
        by_id.setdefault(r["memory_id"], []).append(r)
    revisions: dict[str, dict] = {f"{r['memory_id']}@{r['revision']}": r for r in rows}
    out = []
    for mid, history in by_id.items():
        history.sort(key=lambda r: r["revision"])
        for i, r in enumerate(history):
            prev = history[i - 1] if i else None
            code = None
            if r["state"] == "archived":
                code = None
            elif prev is None:
                replaced = _replaces(r)
                if replaced:
                    old = revisions.get(replaced)
                    old_commit = ((old or {}).get("body", {}).get("origin") or {}).get("commit")
                    new_commit = (r["body"].get("origin") or {}).get("commit")
                    revalidated = any(isinstance(p, dict) and p.get("source") == "lesson-evidence-revalidation"
                                      for p in r["provenance"])
                    code = "observation-invalidated" if revalidated or (old and old_commit == new_commit) \
                        else "lesson-replaced"
                else:
                    code = "lesson-new"
            elif r["state"] == "observed" and prev["state"] == "proposed" and any(
                    isinstance(p, dict) and p.get("source") == "corroboration" and p.get("by") == LESSON_CORROBORATOR
                    for p in r["provenance"]):
                code = "lesson-promoted"
            elif r["state"] in ("proposed", "observed", "supported"):
                code = "lesson-revised"
            if code:
                out.append({**r, "code": code, "replaces": _replaces(r) if code in (
                    "lesson-replaced", "observation-invalidated") else None})
    return out


def corroborator_receipt_age(now: datetime) -> tuple[str | None, int | None]:
    """The corroborator receipt's `checked_on` and its file age in seconds, or (None, None) when absent."""
    import paths
    path = Path(paths.SCRATCH) / "corroboration.json"
    try:
        age = int(max(0.0, now.timestamp() - path.stat().st_mtime))
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    checked = doc.get("checked_on") if isinstance(doc, dict) else None
    return (checked if isinstance(checked, str) else None), age


def run_corroborator(conn, now: datetime) -> tuple[dict | None, dict]:
    """The corroborator's lesson outcomes, and the source block. For real when no refreshing tick ran it within a
    day (its receipt is absent or older than yesterday), else dry."""
    checked, age = corroborator_receipt_age(now)
    yesterday = (now.astimezone(timezone.utc).date() - timedelta(days=1)).isoformat()
    dry = checked is not None and checked >= yesterday
    try:
        from tools import corroborate as C
        result = C.corroborate_lessons(conn, dry)
        if not dry:
            conn.commit()
    except Exception as exc:  # noqa: BLE001: a failed corroborator makes the report partial, never the run
        return None, {"state": "corroborator-failed", "receiptAge": age, "reason": type(exc).__name__}
    return result, {"state": "corroborator-dry" if dry else "corroborator-ran", "receiptAge": age}


# ── digests ─────────────────────────────────────────────────────────────────────────────────────────────────

class Digests:
    """The first revision of every digest line a report reads, cached per (project, month) for one run. A damaged
    month is named once, and every other month and project is still read."""

    def __init__(self) -> None:
        self.cache: dict[tuple[str, str], list[dict] | None] = {}
        self.damaged: set[tuple[str, str]] = set()
        self.unreadable: str | None = None
        self.slugs: list[str] = []
        try:
            import ops_digest_store as S
            base = S.store_root()
            if base.exists():
                self.slugs = sorted(p.name for p in base.iterdir() if p.is_dir() and not p.is_symlink()
                                    and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", p.name))
        except OSError as exc:
            self.unreadable = type(exc).__name__

    def month(self, slug: str, month: str) -> list[dict]:
        key = (slug, month)
        if key not in self.cache:
            import ops_digest_store as S
            try:
                self.cache[key] = S.first_lines_of(f"project:{slug}", month)
            except OSError:
                self.cache[key] = None
                self.damaged.add(key)
        return self.cache[key] or []

    def lines(self, slug: str, day: date) -> list[dict]:
        iso = day.isoformat()
        return [x for x in self.month(slug, iso[:7]) if x.get("day") == iso]


def _line_source(slug: str, line: dict) -> str:
    return f"ops-digests:{slug}/{line['day'][:7]}#{line['day']}/{line.get('source')}/r1"


def _ops(line: dict) -> dict[str, dict]:
    digest = line.get("digest") or {}
    return {c["op"]: c for c in digest.get("ops") or [] if isinstance(c, dict) and isinstance(c.get("op"), str)}


def _line_key(line: dict) -> str:
    return json.dumps([line.get("service"), line.get("source")], sort_keys=True)


def day_rises(digests: Digests, slug: str, day: date) -> dict[str, list[dict]]:
    """Per line key (service, source), the rises of `day`: the product's own hourly rises and the day-over-day ones."""
    import ops_digest as OD
    before = {_line_key(x): _ops(x) for x in digests.lines(slug, day - timedelta(days=1))}
    out: dict[str, list[dict]] = {}
    for line in digests.lines(slug, day):
        key, items = _line_key(line), []
        digest = line.get("digest") or {}
        for r in digest.get("risen") or []:
            if isinstance(r, dict) and isinstance(r.get("op"), str):
                items.append({"code": "risen-in-hour", "op": r["op"], "failed": r.get("failed"),
                              "hours": r.get("hours"), "previous": None})
        prior = before.get(key)
        for op, c in _ops(line).items():
            failed = c.get("failed") if isinstance(c.get("failed"), int) else 0
            previous = (prior or {}).get(op, {}).get("failed") if prior is not None and op in prior else None
            if OD.is_rise(failed, previous if isinstance(previous, int) else 0):
                items.append({"code": "rose-since-yesterday", "op": op, "failed": failed, "hours": None,
                              "previous": previous if isinstance(previous, int) else None})
        out[key] = items
    return out


def failures_for(digests: Digests, slug: str, project: str, day: date) -> tuple[list[dict], list[dict], dict]:
    """The failure items and the line rows of one project's day, and its retro-after-work counts."""
    import ops_digest as OD
    lines = digests.lines(slug, day)
    history = {d: day_rises(digests, slug, day - timedelta(days=d)) for d in range(REPEAT_DAYS)}
    items, rows = [], []
    retro = {"asked": 0, "answeredSeen": 0, "refused": 0, "notSeen": 0}
    seen_retro = False
    for line in lines:
        key, digest = _line_key(line), line.get("digest") or {}
        source = _line_source(slug, line)
        base = {"project": project, "service": line.get("service"), "source": line.get("source"), "line": source}
        ops = _ops(line)
        at_cap = len(digest.get("ops") or []) >= OD.OPS_MAX
        rows.append({"project": project, "service": line.get("service"), "source": line.get("source"),
                     "day": line["day"], "hours": digest.get("hours"), "total": digest.get("total"),
                     "ops": len(digest.get("ops") or []), "namesAtCap": at_cap, "revisions": line.get("revisions", 1),
                     "line": source})
        for item in history[0].get(key, []):
            items.append({**base, **item, "op": OD.op_shown(item["op"]), "days": None})
        rose_on: dict[str, list[str]] = {}
        for d, rises in history.items():
            for item in rises.get(key, []):
                rose_on.setdefault(item["op"], [])
                when = (day - timedelta(days=d)).isoformat()
                if when not in rose_on[item["op"]]:
                    rose_on[item["op"]].append(when)
        for op, days in sorted(rose_on.items()):
            if len(days) >= REPEAT_MIN:
                failed = ops.get(op, {}).get("failed", 0)
                items.append({**base, "code": "rising-repeatedly", "op": OD.op_shown(op), "failed": failed,
                              "hours": None, "previous": None, "days": sorted(days)})
        if at_cap:
            items.append({**base, "code": "names-at-cap", "op": None, "failed": None, "hours": None,
                          "previous": None, "days": None})
        if line.get("revisions", 1) > 1:
            items.append({**base, "code": "later-revisions-ignored", "op": None, "failed": None, "hours": None,
                          "previous": None, "days": None, "revisions": line["revisions"]})
        if RETRO_OP in ops:
            seen_retro = True
            c = ops[RETRO_OP]
            retro["answeredSeen"] += c.get("ok", 0)
            retro["refused"] += c.get("failed", 0)
            retro["notSeen"] += c.get("unknown", 0)
    retro["asked"] = retro["answeredSeen"] + retro["refused"] + retro["notSeen"]
    return items, rows, retro if seen_retro else None


def _failure_order(item: dict) -> tuple:
    rise = item["code"] in RISE_CODES
    return (0 if rise else 1, -(item.get("failed") or 0) if rise else 0, str(item.get("op") or ""),
            RISE_CODES.index(item["code"]) if rise else 0, item.get("project") or "", item.get("line") or "")


# ── one report ──────────────────────────────────────────────────────────────────────────────────────────────

class Run:
    """What one run reads once and every report of it shares: the ledger, the corroborator's outcomes, the lesson
    keys' projects and the digest store."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.scrub = Scrubber()
        self.degraded: list[dict] = []
        self.lessons_source: dict = {"state": "lessons-read", "records": 0}
        self.corroborator: dict | None = None
        self.corroborator_source: dict = {"state": "corroborator-failed", "receiptAge": None}
        self.rows: list[dict] = []
        self.classified: list[dict] = []
        self.current: list[dict] = []
        self.key_project: dict[str, str | None] = {}
        self.resolvable: set[str] = set()
        self.registry = registry_projects()
        self.digests = Digests()
        self._read_ledger()

    def _read_ledger(self) -> None:
        try:
            from store import db as store_db
            from store import lessons as LS
            import sqlite3
            conn = store_db.connect()
            conn.row_factory = sqlite3.Row
        except Exception as exc:  # noqa: BLE001
            self.lessons_source = {"state": "ledger-unreadable", "records": 0}
            self.degraded.append({"source": "lessons", "code": "ledger-unreadable", "reason": type(exc).__name__})
            return
        try:
            self.corroborator, self.corroborator_source = run_corroborator(conn, self.now)
            if self.corroborator is None:
                self.degraded.append({"source": "corroborator", "code": "corroborator-failed",
                                      "reason": self.corroborator_source.pop("reason", "")})
            self.rows = lesson_rows(conn)
            self.current = LS.current_lessons(conn)
        except Exception as exc:  # noqa: BLE001
            self.lessons_source = {"state": "ledger-unreadable", "records": 0}
            self.degraded.append({"source": "lessons", "code": "ledger-unreadable", "reason": type(exc).__name__})
            self.rows, self.current = [], []
        finally:
            conn.close()
        self.lessons_source["records"] = len({r["memory_id"] for r in self.rows})
        self.classified = classify(self.rows)
        keys = {((r["body"].get("origin") or {}).get("projectId")) for r in self.rows}
        keys.discard(None)
        if keys:
            try:
                from tools import corroborate as C
                found = C.project_keys(deadline=time.monotonic() + KEYS_SECONDS)
            except Exception:  # noqa: BLE001: unnamed projects, never a failed review
                found = {}
            self.resolvable = set(found)
            self.key_project = key_projects(found)

    def project_of(self, key: Any) -> str | None:
        return self.key_project.get(key) if isinstance(key, str) else None

    # ── lessons and classes of a day ──

    def lessons_of(self, day: date) -> list[dict]:
        iso = day.isoformat()
        latest = {}
        for r in self.rows:
            latest[r["memory_id"]] = r
        out = []
        for r in self.classified:
            if str(r.get("created_at") or "")[:10] != iso:
                continue
            origin = r["body"].get("origin") or {}
            item = {"code": r["code"], "memoryId": r["memory_id"], "revision": r["revision"],
                    "classKey": r["body"].get("classKey"), "title": self.scrub(r["body"].get("title")),
                    "origin": {"projectId": self.scrub(origin.get("projectId")),
                               "commit": self.scrub(origin.get("commit"))},
                    "project": self.project_of(origin.get("projectId")),
                    "state": latest.get(r["memory_id"], r)["state"],
                    "source": f"ledger:{r['memory_id']}@{r['revision']}"}
            if r.get("replaces"):
                item["replaces"] = f"ledger:{r['replaces']}"
            out.append(item)
        order = ("lesson-promoted", "observation-invalidated", "lesson-new", "lesson-replaced", "lesson-revised")
        out.sort(key=lambda i: (order.index(i["code"]), i["classKey"] or "", i["source"]))
        return out

    def outcomes(self) -> dict[str, dict[str, list[dict]]]:
        """The corroborator's lists by classKey."""
        out: dict[str, dict[str, list[dict]]] = {}
        for bucket in ("promoted", "waiting", "unresolvable", "contradicted", "invalidated"):
            for entry in (self.corroborator or {}).get(bucket) or []:
                ck = entry.get("classKey")
                if ck is None:
                    ck = next((r["body"].get("classKey") for r in self.current
                               if r["memory_id"] == entry.get("memory_id")), None)
                out.setdefault(ck, {}).setdefault(bucket, []).append(entry)
        return out

    def classes_of(self, day: date, lessons: list[dict]) -> tuple[list[dict], int]:
        iso = day.isoformat()
        by_class: dict[str, list[dict]] = {}
        for r in self.current:
            ck = r["body"].get("classKey")
            if isinstance(ck, str):
                by_class.setdefault(ck, []).append(r)
        touched = {i["classKey"] for i in lessons}
        outcomes = self.outcomes()
        listed, others = [], 0
        for ck, members in sorted(by_class.items()):
            keys = {(m["body"].get("origin") or {}).get("projectId") for m in members}
            keys.discard(None)
            if len(keys) < 2:
                continue
            if ck not in touched:
                others += 1
                continue
            got = outcomes.get(ck, {})
            promoted_today = any(i["classKey"] == ck and i["code"] == "lesson-promoted" for i in lessons)
            if got.get("promoted") and self.corroborator_source.get("state") == "corroborator-dry" \
                    and not promoted_today:
                # A dry run's "promoted" is what the corroborator WOULD promote: the tick's next run does it.
                outcome, why = None, "both projects' commits resolve here; the corroborator promotes it at its next run"
            elif promoted_today or got.get("promoted"):
                # Promoted on D, or by this run's corroborator (its revision then belongs to the day it ran on and is
                # listed in that day's report as `lesson-promoted`).
                outcome, why = "lesson-promoted", next((e.get("how") for e in got.get("promoted", [])),
                                                       "promoted by the corroborator on this day")
            elif got.get("contradicted"):
                outcome, why = "contradicted", got["contradicted"][0].get("why")
            elif got.get("unresolvable"):
                outcome, why = "not-resolvable-here", got["unresolvable"][0].get("why")
            elif got.get("waiting"):
                outcome, why = "waiting-second-project", "no second project's commit of this class resolves here yet"
            elif self.corroborator is None:
                outcome, why = None, "the corroborator did not run, so its outcome is unknown"
            else:
                outcome, why = None, "no member of this class waits for the corroborator"
            listed.append({"code": "class-repeated", "classKey": ck, "keys": len(keys),
                           "resolved": len(keys & self.resolvable), "outcome": outcome,
                           "why": self.scrub(why) if why else why,
                           "members": sorted(f"ledger:{m['memory_id']}@{m['revision']}" for m in members),
                           "changedOn": iso})
        return listed, others

    # ── digests of a day ──

    def failures_of(self, day: date) -> tuple[list[dict], list[dict], dict[str, dict], list[dict]]:
        failures, lines, retros, damaged = [], [], {}, []
        for slug in self.digests.slugs:
            project = f"project:{slug}"
            items, rows, retro = failures_for(self.digests, slug, project, day)
            if project not in self.registry and (items or rows):
                for row in rows:
                    row["projectNotInRegistry"] = True
                failures.append({"code": "project-not-in-registry", "project": project, "service": None,
                                 "source": None, "op": None, "failed": None, "hours": None, "previous": None,
                                 "days": None, "line": f"ops-digests:{slug}"})
            failures += items
            lines += rows
            if retro is not None:
                retros[project] = retro
        failures.sort(key=_failure_order)
        return failures, lines, retros, damaged

    def late_of(self, day: date, previous_written: datetime | None) -> list[dict]:
        """Digest lines of an earlier day that arrived after that day's report was written and after the previous
        report of this run: each is reported once, in the first report written after it arrived."""
        out = []
        for back in range(1, LATE_DAYS + 1):
            d = day - timedelta(days=back)
            report = _report_or_none(d)
            written = parse_stamp((report or {}).get("writtenAt"))
            if written is None:
                continue
            for slug in self.digests.slugs:
                for line in self.digests.lines(slug, d):
                    received = parse_stamp(line.get("receivedAt"))
                    if received is None or received <= written or received > self.now:
                        continue
                    if previous_written is not None and received <= previous_written:
                        continue
                    rises = day_rises(self.digests, slug, d).get(_line_key(line), [])
                    import ops_digest as OD
                    out.append({"code": "arrived-late", "day": d.isoformat(), "project": f"project:{slug}",
                                "service": line.get("service"), "source": line.get("source"),
                                "line": _line_source(slug, line), "receivedAt": line.get("receivedAt"),
                                "failures": [{**r, "op": OD.op_shown(r["op"])} for r in rises]})
        return out


def _cut(doc: dict, name: str, limit: int) -> None:
    items = doc[name]
    if len(items) > limit:
        doc["truncated"][name] += len(items) - limit
        doc[name] = items[:limit]


def build(run: Run, day: date, *, revision: int, previous_written: datetime | None,
          not_reviewed: list[date]) -> dict:
    """The report of `day` as a document (nothing written)."""
    lessons = run.lessons_of(day)
    classes, others = run.classes_of(day, lessons)
    failures, lines, retros_by_project, _ = run.failures_of(day)
    late = run.late_of(day, previous_written)
    degraded = [dict(d) for d in run.degraded]
    months = {day.isoformat()[:7], (day - timedelta(days=REPEAT_DAYS)).isoformat()[:7],
              (day - timedelta(days=LATE_DAYS)).isoformat()[:7]}
    damaged = sorted((s, m) for s, m in run.digests.damaged
                     if min(months) <= m <= max(months))
    for slug, month in damaged:
        degraded.append({"source": "digests", "code": "digest-month-damaged",
                         "reason": f"project:{slug} {month}: the month file is not readable; it is kept as it is"})
    if run.digests.unreadable:
        degraded.append({"source": "digests", "code": "digest-store-unreadable", "reason": run.digests.unreadable})
    digest_state = ("digest-store-unreadable" if run.digests.unreadable
                    else "digests-read" if lines else "no-digests")
    recorded: dict[str, int] = {}
    for i in lessons:
        if i["project"] and i["code"] in ("lesson-new", "lesson-revised", "lesson-replaced"):
            recorded[i["project"]] = recorded.get(i["project"], 0) + 1
    retros = [{"project": p, "lessonsRecorded": recorded.get(p, 0), "insightLinesAdded": None,
               "retroAfterWork": retros_by_project.get(p)}
              for p in sorted(set(recorded) | set(retros_by_project))]
    rises = [f for f in failures if f["code"] in ("risen-in-hour", "rose-since-yesterday")]
    summary = {
        "lessonsNew": sum(i["code"] == "lesson-new" for i in lessons),
        "lessonsRevised": sum(i["code"] == "lesson-revised" for i in lessons),
        "lessonsReplaced": sum(i["code"] == "lesson-replaced" for i in lessons),
        "promoted": sum(i["code"] == "lesson-promoted" for i in lessons),
        "invalidated": sum(i["code"] == "observation-invalidated" for i in lessons),
        "classesRepeated": len(classes), "classesRepeatedUnchanged": others,
        "rises": len(rises), "risingRepeatedly": sum(f["code"] == "rising-repeatedly" for f in failures),
        "arrivedLate": len(late),
    }
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    doc = {
        "schema": SCHEMA, "day": day.isoformat(), "boundary": "utc",
        "window": {"from": stamp(start), "to": stamp(start + timedelta(days=1)), "localOffset": local_offset()},
        "writtenAt": stamp(run.now), "revision": revision,
        "state": "partial" if degraded else "complete",
        "engine": __import__("configuration").VERSION,
        "rules": {"settleHourUtc": settle_hour(), "riseMinFailed": _od().RISE_MIN_FAILED,
                  "riseFactor": _od().RISE_FACTOR, "repeatMin": REPEAT_MIN, "repeatDays": REPEAT_DAYS,
                  "lateDays": LATE_DAYS},
        "sources": {
            "lessons": dict(run.lessons_source),
            "corroborator": dict(run.corroborator_source),
            "digests": {"state": digest_state, "projects": len({r["project"] for r in lines}), "lines": len(lines),
                        "damaged": [{"project": f"project:{s}", "month": m} for s, m in damaged]},
            "insights": {"state": "insights-not-read", "grammar": "insight-grammar-undecided"},
        },
        "summary": summary,
        "lessons": lessons, "classes": classes, "failures": failures, "lines": lines, "retros": retros,
        "insights": [], "arrivedLate": late,
        "daysNotReviewed": [d.isoformat() for d in not_reviewed],
        "truncated": {"lessons": 0, "classes": 0, "failures": 0},
        "notes": sorted(NOTES),
        "degraded": [{**d, "reason": run.scrub(d.get("reason") or "")} for d in degraded],
    }
    _cut(doc, "lessons", MAX_LESSONS)
    _cut(doc, "classes", MAX_CLASSES)
    _cut(doc, "failures", MAX_FAILURES)
    while len(json.dumps(doc, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
        longest = max(("lessons", "classes", "failures"), key=lambda k: len(doc[k]))
        if not doc[longest]:
            break
        _cut(doc, longest, len(doc[longest]) // 2)
    if any(doc["truncated"].values()):
        doc["degraded"].append({"source": "report", "code": "items-truncated",
                                "reason": "the lists are cut at their bounds; truncated counts the rest"})
    return doc


def _od():
    import ops_digest
    return ops_digest


# ── the page ────────────────────────────────────────────────────────────────────────────────────────────────

CSS = ("body{font:14px/1.5 -apple-system,system-ui,sans-serif;margin:2em auto;max-width:72em;padding:0 1em;"
       "background:#14161a;color:#e6e6e6}h1,h2{font-weight:600}h2{margin-top:2em;border-bottom:1px solid #333}"
       "table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #2a2a2a;padding:.3em .5em;"
       "text-align:left;vertical-align:top}code{font-size:12px;color:#c9c9c9}.note{color:#a8a8a8}"
       ".state-partial{color:#e0b050}.state-complete{color:#8fc98f}")


def _e(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, (list, tuple)):
        return ", ".join(_e(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{_e(k)} {_e(v)}" for k, v in value.items())
    return html.escape(str(value), quote=True)


def _table(head: list[str], rows: list[list[Any]], empty: str) -> str:
    if not rows:
        return f'<p class="note">{_e(empty)}</p>'
    out = ["<table><thead><tr>", *(f"<th>{_e(h)}</th>" for h in head), "</tr></thead><tbody>"]
    for row in rows:
        out.append("<tr>" + "".join(f"<td>{c if isinstance(c, _Raw) else _e(c)}</td>" for c in row) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


class _Raw(str):
    """Markup this module built from escaped parts."""


def _code(value: Any) -> _Raw:
    return _Raw(f"<code>{_e(value)}</code>")


def _service(value: Any) -> str:
    return f"{value.get('id')}.{value.get('instance')}" if isinstance(value, dict) else "none"


def render_html(doc: dict) -> str:
    """The report for a person, rendered from the JSON only: one page, inline CSS, no script, no external resource,
    every value escaped. English in this part; the translated page is OBS-331 part b."""
    s, w = doc["summary"], doc["window"]
    offset = w.get("localOffset") or "+00:00"
    sign = 1 if offset.startswith("+") else -1
    hours, minutes = int(offset[1:3]), int(offset[4:6])
    local = (datetime(2000, 1, 1, tzinfo=timezone.utc) + sign * timedelta(hours=hours, minutes=minutes))
    first = [f for f in doc["failures"] if f["code"] == "rising-repeatedly"]
    first_lessons = [i for i in doc["lessons"] if i["code"] == "lesson-promoted"]
    parts = [
        "<!doctype html>", '<html lang="en"><head><meta charset="utf-8">',
        f"<title>Learning review {_e(doc['day'])}</title><style>{CSS}</style></head><body>",
        f"<h1>Learning review of {_e(doc['day'])}</h1>",
        f'<p>The UTC day {_e(w["from"])} to {_e(w["to"])}; at this machine\'s offset {_e(offset)} that is '
        f'{local:%H:%M} to {local:%H:%M} the next day. Written {_e(doc["writtenAt"])}, revision '
        f'{_e(doc["revision"])}, <span class="state-{_e(doc["state"])}">{_e(doc["state"])}</span>.</p>',
        f"<p>{_e(s['lessonsNew'])} new lessons, {_e(s['lessonsRevised'])} revised, {_e(s['lessonsReplaced'])} "
        f"replaced, {_e(s['promoted'])} promoted, {_e(s['invalidated'])} invalidated; {_e(s['classesRepeated'])} "
        f"repeated classes changed ({_e(s['classesRepeatedUnchanged'])} unchanged); {_e(s['rises'])} rises, "
        f"{_e(s['risingRepeatedly'])} rising repeatedly; {_e(s['arrivedLate'])} lines arrived late.</p>",
        "<h2>What to look at first</h2>",
        _table(["Project", "Operation", "Days", "Failed on the day", "Source"],
               [[f["project"], _code(f["op"]), f["days"], f["failed"], _code(f["line"])] for f in first]
               + [[i["project"], _code(i["classKey"]), "promoted", "", _code(i["source"])] for i in first_lessons]
               + [[None, _code(c["classKey"]), f"{c['keys']} projects", c["outcome"], _code(c["members"])]
                  for c in doc["classes"] if c["outcome"] != "lesson-promoted"],
               "Nothing rose repeatedly, no lesson was promoted and no repeated class changed on this day."),
        "<h2>Rising failures</h2>",
        _table(["Code", "Project", "Service", "Operation", "Failed", "Hours it rose in", "Day before", "Source"],
               [[f["code"], f["project"], _service(f["service"]), _code(f["op"]), f["failed"], f["hours"],
                 f["previous"], _code(f["line"])] for f in doc["failures"]],
               "No product sent a digest for this day." if doc["sources"]["digests"]["state"] != "digests-read"
               else "No operation rose on this day."),
        "<h2>Lessons</h2>",
        _table(["Code", "Class", "Title", "Project", "Origin", "State", "Source"],
               [[i["code"], _code(i["classKey"]), i["title"], i["project"],
                 _code(f"{i['origin']['projectId']} {i['origin']['commit']}"), i["state"],
                 _code(i["source"] + (f" (replaces {i['replaces']})" if i.get("replaces") else ""))]
                for i in doc["lessons"]],
               "No lesson was recorded, revised or promoted on this day."),
        "<h2>Repeated classes</h2>",
        _table(["Class", "Projects", "Resolved here", "Outcome", "Why", "Members"],
               [[_code(c["classKey"]), c["keys"], c["resolved"], c["outcome"], c["why"], _code(c["members"])]
                for c in doc["classes"]],
               "No class held by two or more projects changed on this day."),
        "<h2>Retros per project</h2>",
        _table(["Project", "Lessons recorded", "retro-after-work asked", "answered by a retro the host saw written",
                "refused", "not seen"],
               [[r["project"], r["lessonsRecorded"]] + ([r["retroAfterWork"][k] for k in
                                                         ("asked", "answeredSeen", "refused", "notSeen")]
                                                        if r["retroAfterWork"] else ["no digest"] * 4)
                for r in doc["retros"]],
               "No project recorded a lesson or sent retro counts for this day."),
        "<h2>Digest lines read</h2>",
        _table(["Project", "Service", "Source", "Hours folded", "Total", "Operations", "Revisions", "Source"],
               [[r["project"], _service(r["service"]), r["source"], r["hours"], r["total"], r["ops"],
                 r["revisions"], _code(r["line"])] for r in doc["lines"]],
               "No product sent a digest for this day."),
        "<h2>Arrived late</h2>",
        _table(["Day", "Project", "Received", "Rises", "Source"],
               [[a["day"], a["project"], a["receivedAt"],
                 _code([f"{f['code']} {f['op']} {f['failed']}" for f in a["failures"]]), _code(a["line"])]
                for a in doc["arrivedLate"]],
               "No digest line of an earlier day arrived after its report."),
        "<h2>Sources</h2>",
        _table(["Source", "State"], [[k, v] for k, v in doc["sources"].items()], ""),
        _table(["Rule", "Value"], [[k, v] for k, v in doc["rules"].items()], ""),
    ]
    if doc["daysNotReviewed"]:
        parts.append(f"<p>Days not reviewed (older than {CATCHUP_DAYS} days when the review caught up): "
                     f"{_e(doc['daysNotReviewed'])}.</p>")
    if doc["degraded"]:
        parts.append(_table(["Source", "Code", "Reason"],
                            [[d.get("source"), d.get("code"), d.get("reason")] for d in doc["degraded"]], ""))
    if any(doc["truncated"].values()):
        parts.append(f"<p>Lists cut at their bounds; not shown: {_e(doc['truncated'])}.</p>")
    parts.append("<ul>" + "".join(f'<li class="note">{_e(NOTES.get(n, n))}</li>' for n in doc["notes"]) + "</ul>")
    parts.append("</body></html>\n")
    return "".join(parts)


# ── writing ─────────────────────────────────────────────────────────────────────────────────────────────────

def write_report(doc: dict, *, keep_previous: bool) -> None:
    import atomic
    import osprivacy
    day = date.fromisoformat(doc["day"])
    folder = report_path(day).parent
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    osprivacy.private_folder(folder)
    if keep_previous:
        old = _report_or_none(day)
        if old is not None:
            n = old.get("revision") if isinstance(old.get("revision"), int) else doc["revision"] - 1
            atomic.write_json(folder / f"{day.isoformat()}.r{n}.json", old)
    # The page first and the report last: the JSON is what says a day was reviewed, so a write that fails before it
    # leaves the previous report standing and the day due again.
    atomic.write_text(report_path(day, ".html"), render_html(doc))
    atomic.write_json(report_path(day), doc)


def prune(today: date) -> int:
    """Reports of days older than KEEP_DAYS, every revision and page, and the month folders they leave empty."""
    oldest, dropped = today - timedelta(days=KEEP_DAYS), 0
    base = root()
    if not base.is_dir():
        return 0
    for folder in sorted(p for p in base.iterdir() if p.is_dir() and not p.is_symlink()):
        for path in sorted(folder.iterdir()):
            day = parse_day(path.name[:10])
            if day is not None and day < oldest and path.is_file() and not path.is_symlink():
                path.unlink()
                dropped += 1
        if not any(folder.iterdir()):
            folder.rmdir()
    return dropped


def line_of(doc: dict) -> str:
    s = doc["summary"]
    return (f"{doc['day']} {doc['state']} r{doc['revision']} ({s['lessonsNew']} new, {s['lessonsRevised']} revised, "
            f"{s['promoted']} promoted, {s['classesRepeated']} repeated classes, {s['rises']} rises, "
            f"{s['arrivedLate']} late)")


def run(now: datetime | None = None, *, day: date | None = None, again: bool = False,
        wait: float | None = None) -> dict:
    """Write what is due at `now`, or the closed day `day`. Answers {outcome, written: [lines], ...}; raises
    Refused for a day the CLI refuses and for `review-locked`. `review-unwritable` is an answer with outcome
    `review-unwritable`, recorded in the run record when that record can still be written."""
    now = now or utc_now()
    today = now.astimezone(timezone.utc).date()
    if day is not None:
        if day >= today:
            raise Refused("day-not-closed", f"{day.isoformat()} has not ended in UTC")
        if day < today - timedelta(days=KEEP_DAYS):
            raise Refused("day-outside-retention", f"{day.isoformat()} is older than {KEEP_DAYS} days")
    started = time.monotonic()
    with _Lock(wait):
        state = read_state()
        if day is not None:
            existing = _report_or_none(day)
            if existing is not None and existing.get("state") == "complete" and not again:
                answer = {"outcome": "already-complete", "day": day.isoformat(), "written": [],
                          "report": str(report_path(day))}
                _record(state, now, answer, started)
                return answer
            # `--again` writes the next revision and keeps the current one beside it; a partial report run again
            # without it is rewritten in place, as the tick does.
            revision = (existing or {}).get("revision", 1) if existing else 1
            todo = [(day, revision + 1 if (existing and again) else revision, bool(existing and again))]
            not_reviewed: list[date] = []
        else:
            p = plan(now)
            todo = []
            for d in p["days"]:
                old = _report_or_none(d)
                todo.append((d, (old or {}).get("revision", 1) if old else 1, False))
            not_reviewed = p["notReviewed"]
        if not todo:
            answer = {"outcome": "not-due", "written": [], "next": stamp(due_at(newest_due_day(now, settle_hour())
                                                                               + timedelta(days=1), settle_hour()))}
            _record(state, now, answer, started)
            return answer
        review = Run(now)
        previous = parse_stamp((state.get("lastWritten") or {}).get("writtenAt"))
        written, docs = [], []
        try:
            for i, (d, revision, keep) in enumerate(todo):
                doc = build(review, d, revision=revision, previous_written=previous,
                            not_reviewed=not_reviewed if i == len(todo) - 1 else [])
                write_report(doc, keep_previous=keep)
                previous = parse_stamp(doc["writtenAt"])
                written.append(line_of(doc))
                docs.append(doc)
            prune(today)
        except (OSError, ValueError) as exc:
            answer = {"outcome": "review-unwritable", "written": written, "reason": type(exc).__name__}
            try:
                _record(state, now, answer, started, docs)
            except (OSError, ValueError):
                pass
            return answer
        answer = {"outcome": "written", "written": written, "report": str(report_path(date.fromisoformat(
            docs[-1]["day"])))}
        _record(state, now, answer, started, docs)
        return answer


def _record(state: dict, now: datetime, answer: dict, started: float, docs: list[dict] | None = None) -> None:
    """The run record: what the last report was, and the newest RUNS_KEPT runs (codes and counts only)."""
    import atomic
    for doc in docs or []:
        state["lastWritten"] = {"day": doc["day"], "revision": doc["revision"], "state": doc["state"],
                                "writtenAt": doc["writtenAt"]}
    last = (docs or [None])[-1]
    state["runs"] = (state.get("runs") or [])[-(RUNS_KEPT - 1):] + [{
        "at": stamp(now), "outcome": answer["outcome"], "day": last["day"] if last else answer.get("day"),
        "durationMs": int((time.monotonic() - started) * 1000),
        "codes": sorted({d["code"] for doc in docs or [] for d in doc["degraded"]}),
        "counts": last["summary"] if last else {}}]
    atomic.write_json(state_path(), state)


# ── reading ─────────────────────────────────────────────────────────────────────────────────────────────────

def newest_report() -> dict | None:
    for day in reversed(report_days()):
        doc = _report_or_none(day)
        if doc is not None:
            return doc
    return None


def listing(days: int = 14) -> list[dict]:
    out = []
    for day in reversed(report_days()[-days:] if days > 0 else []):
        try:
            doc = read_report(day)
        except Refused as exc:
            out.append({"day": day.isoformat(), "state": exc.code, "revision": None, "summary": None})
            continue
        if doc is not None:
            out.append({"day": doc["day"], "state": doc["state"], "revision": doc["revision"],
                        "writtenAt": doc.get("writtenAt"), "summary": doc["summary"]})
    return out


def main(argv: list[str]) -> int:
    """The tick's two calls: `--due` (exit 0 due, 3 not due, 2 an error) and `run` (one line per report written)."""
    if argv == ["--due"]:
        try:
            return 0 if is_due() else 3
        except Exception as exc:  # noqa: BLE001
            print(f"learning review: the due check failed ({type(exc).__name__})", file=sys.stderr)
            return 2
    if argv == ["run"]:
        try:
            answer = run()
        except Refused as exc:
            print(f"{exc.code}: {exc.detail}")
            return 2
        for line in answer["written"]:
            print(line)
        if answer["outcome"] == "review-unwritable":
            print(f"review-unwritable: {answer.get('reason')}; the previous report stands")
            return 2
        if not answer["written"]:
            print(answer["outcome"])
        return 0
    print("usage: learning_review.py --due | run", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
