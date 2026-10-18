#!/usr/bin/env python3
"""This month's provider spend across every account the doors hold — the estate total the L0 limit is about —
and each project's month and last 30 days.

WHY. The meter (meter.py, OBS-55) holds its own reservations, but most spend today happens on keys whose callers
never ask it. L0 — the estate's monthly total across every provider key, USD 1,000 by default (operator,
2026-10-11) — is only meaningful against what the providers actually charged. Each OpenRouter account the door
holds a provisioning key for (`tools/openrouter.py stash`) answers `GET /api/v1/activity` with its daily spend;
this sums the current UTC month per account (OBS-56).

PER PROJECT (J-11, OBS-93). Under contract DEC-0037 Fabric's launcher proposes a project's limit from its agents'
cost presets and its recent spend, and Observatory measures that spend. Every key of the door's ledger
(`openrouter-issued.json`) is read once by its hash (`openrouter.read_activity`: the last 30 completed UTC days,
by day and model) and summed under the ledger row's `project`. A key the door retired inside the windows keeps
its spend too: `revoke`, a completed `rotate` and `revoke --forget-pending` leave a tombstone (`retired`: name,
account, hash, project, `retired_on`, `deleted`) with a usage snapshot read just before the DELETE
(`openrouter.usage_snapshot`), because the provider keeps no history of a deleted key: on 2026-10-15 `GET
/activity?api_key_hash=` for a hash that never existed, and `GET /keys/{hash}` for a key revoked earlier, both
answered 404. A snapshot is trusted whole only when its days reconciled with the provider's own counters in the
last read before the DELETE (`reconciled: true`); the scan counts it and never asks for the deleted key, and
after it a deleted key spends nothing. Two windows, both of completed UTC days (the provider's report has no today): the current month so far
(what a monthly limit counts) and the last 30 days (what "recent spend" means when the month has just begun).

ONE PROJECT, ONE BUCKET. A ledger row's `project` is resolved by the rule every door uses
(`vault_project.resolve`): a string the registry claims is grouped under that project's registry id, so a
project's folder, its id and its name, and a project with two vault folders, all sum into one bucket (its
`folders` say which were seen). A string the registry does not claim is its own bucket, as typed. When the
registry cannot be read nobody can say which names are one project, so every project's figures are null.

WHAT NO PROJECT CLAIMS IS ITS OWN TOTAL, NEVER A GUESS. A ledger key whose row names no project (a key claimed
from a file, `openrouter.py claim`) and spend on hashes no ledger row or tombstone holds (the account's figure
minus every hash read in it) go to `unattributed`. Nothing is attributed from a key's name, a consumer's name or
a destination's folder: the ledger's `project` field is the only claim.

A WINDOW NOT READ WHOLE IS NULL, NEVER A LOWER FIGURE. A project's window is `null`, with the key named in
`unread`, when any of its keys could not be read for it:
  - the provider would not answer for the key, or answered something that is not an activity report (a row
    that is not an object, a usage that is not a number, a cut-off body);
  - a pending row has no hash yet;
  - a tombstone has no usage snapshot (its read failed, or a forgotten pending row had no hash): every window
    starting on or before its day; or its snapshot did not reconcile: the windows holding any day after its last
    activity day through the retirement day;
  - a tombstone not known to be deleted whose key is gone at the provider (404): every window, since when it
    stopped is unknown. Such a tombstone is otherwise read by its hash like a live key while the door keeps it,
    whatever its `retired_on`, and its spend is attributed to its project (the door writes none today: `issue`
    over a key still live under another name is refused);
  - a tombstone that carries its row's untracked rotation (`untracked_through`) or a pending predecessor
    (`predecessor_hash`): the windows starting on or before that day, as for the live row;
  - two ledger entries hold the same hash, so neither can be told apart;
  - a key was rotated before tombstones existed: the row's `rotations` count more rotations than
    `tombstoned_rotations` (a pending rotation not counted), and the untracked ones happened on or before
    `tombstones_since` (or on `rotated_on` when none was tracked), so every window starting on or before that day
    is null (`openrouter.untracked_through`, the rule `_retire` uses for tombstones too);
  - the step's deadline (`OBSERVATORY_STEP_DEADLINE`) arrived, or the provider became unreachable: the scan
    stops reading, writes what it read, and names every key it did not reach.
An account's unattributed residual is null too while any of its keys is unread, since the two cannot be told
apart. A pending rotation still holds `predecessor_hash` and is read like any key. A ledger entry that is not an
object may be any project's key, so it nulls every project and the unattributed total.

WHAT IT RECORDS. Per account: the door's label, the month's USD and the last 30 days', the number of activity
rows and the last day they cover (the provider's activity lags about a day). Per project: the two windows, its
live and retired key counts, its folders, `through` (the earliest last-covered day among its accounts: the
figures cover the provider's report up to that day) and the names (ledger names, never values) of keys it could
not read. Never a key, never a hash, never a model's prompt, never a per-key figure. Top-level `partial` is
about the estate total only (an account not read); `attribution_partial` says some project or the unattributed
total is not whole, and each `degraded` entry carries its `scope` (`account` or `attribution`).

    scan_spend.py store/raw/spend.json
"""
from __future__ import annotations

import http.client
import json
import math
import pathlib
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import atomic  # noqa: E402
import paths  # noqa: E402
import step_budget  # noqa: E402

API = "https://openrouter.ai/api/v1"
SCHEMA_VERSION = 2
#: The provider answers `/activity` for the last 30 completed UTC days.
LAST_DAYS = 30
#: Below this, an account's residual (its figure minus its keys') is float noise, not spend.
RESIDUAL_NOISE_USD = 1e-6
#: Seconds kept back from the step's deadline: one key read (the door's 30 s timeout) and writing the file.
DEADLINE_RESERVE_S = 45.0
WINDOWS = ("month", "last30")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: The account read's own ceiling; near the step's deadline it is cut to what is left less a margin to write.
ACCOUNT_TIMEOUT_S = 60.0
WRITE_MARGIN_S = 15.0


def activity(key: str, timeout: float = ACCOUNT_TIMEOUT_S) -> list:
    req = urllib.request.Request(f"{API}/activity", headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        doc = json.loads(r.read())
    if not isinstance(doc, dict):
        raise ValueError("the activity report is not an object")
    return doc.get("data", []) or []


def key_activity(admin: str, key_hash: str) -> list:
    """One key's activity, last 30 completed UTC days; raises when the provider will not say."""
    import openrouter
    return openrouter.read_activity(admin, key_hash)


def clean(rows) -> tuple[list[tuple[str, float]], int]:
    """(day, USD) per well-formed row, and how many rows were not well formed."""
    if not isinstance(rows, list):
        raise ValueError("the activity report's data is not a list")
    good, bad = [], 0
    for x in rows:
        if not isinstance(x, dict):
            bad += 1
            continue
        try:
            usd = float(x.get("usage") or 0)
        except (TypeError, ValueError):
            bad += 1
            continue
        if not math.isfinite(usd):
            bad += 1
            continue
        good.append((str(x.get("date") or "")[:10], usd))
    return good, bad


def month_of(rows: list[tuple[str, float]], month: str) -> dict:
    this = [(d, u) for d, u in rows if d.startswith(month)]
    days = sorted({d for d, _u in this})
    return {"usd": round(sum(u for _d, u in this), 6), "rows": len(this), "through": days[-1] if days else None}


def window_of(now: datetime) -> tuple[str, str, str]:
    """(month, first day of the last 30 completed UTC days, the last one)."""
    today = now.date()
    return now.strftime("%Y-%m"), (today - timedelta(days=LAST_DAYS)).isoformat(), \
        (today - timedelta(days=1)).isoformat()


def sums(rows: list[tuple[str, float]], month: str, start: str, end: str) -> dict[str, float]:
    """USD per window. The last-30 window is bounded here too: a row outside it is not counted."""
    out = {"month": 0.0, "last30": 0.0}
    for day, usd in rows:
        if day.startswith(month) and day <= end:    # completed days only, as the provider reports them
            out["month"] += usd
        if start <= day <= end:
            out["last30"] += usd
    return out


def snapshot_sums(tomb: dict, month: str, start: str, end: str) -> tuple[dict[str, float], list[str]] | None:
    """A retired key's spend per window from its tombstone's usage snapshot, and the windows the snapshot cannot
    vouch for; None when there is no usable snapshot.

    The snapshot holds the key's spend by UTC day, read just before the DELETE (`openrouter.usage_snapshot`).
    After it the key is deleted and spends nothing more, so a later day counts 0. A snapshot is whole only when
    its days reconciled with the provider's own counters in that same read (`reconciled: true`). Otherwise every
    day after the last one the activity report held (`activity_through`), through the retirement day, is
    unknown, and so is each window holding such a day; with no activity day at all, every window up to the
    retirement day. A day that does not parse makes every window unknown."""
    if tomb.get("usage") != "read" or not isinstance(tomb.get("usage_by_day"), dict):
        return None
    rows = []
    for day, usd in tomb["usage_by_day"].items():
        if isinstance(usd, bool) or not isinstance(usd, (int, float)) or not math.isfinite(usd):
            return None
        rows.append((str(day)[:10], float(usd)))
    retired = str(tomb.get("retired_on") or "")[:10]
    try:
        date.fromisoformat(retired)
    except ValueError:
        return sums(rows, month, start, end), list(WINDOWS)
    unknown: list[str] = []
    if tomb.get("reconciled") is not True:
        last = str(tomb.get("activity_through") or "")[:10]
        try:
            gap_first = (date.fromisoformat(last) + timedelta(days=1)).isoformat()
        except ValueError:
            gap_first = "0000-00-00"            # no activity day known: anything up to the retirement day
        unknown = [w for w, first in (("month", f"{month}-01"), ("last30", start))
                   if gap_first <= end and retired >= first and gap_first <= retired]
    return sums(rows, month, start, end), unknown


def windows_from(day: str, month: str, start: str) -> list[str]:
    """The windows that start on or before `day` — those a key retired or rotated on `day` may have spent in.
    A day that does not parse could be any day: both."""
    try:
        date.fromisoformat(day)
    except ValueError:
        return list(WINDOWS)
    return [w for w, first in (("month", f"{month}-01"), ("last30", start)) if day >= first]


def row_hashes(rec: dict) -> list[str]:
    """Every key a live ledger row holds, each read by its hash: its own, a pending rotation's predecessor, and a
    successor minted whose delivery was never recorded (`successor_pending`, `stale_successors`: live, maybe in
    the slot, deleted by the next rotation; round 6, R6-6)."""
    out = [rec.get("hash"), rec.get("predecessor_hash"), rec.get("successor_pending")]
    out += [e.get("hash") for e in rec.get("stale_successors") or [] if isinstance(e, dict)]
    seen: list[str] = []
    for h in out:
        if h and isinstance(h, str) and h not in seen:
            seen.append(h)
    return seen


def untracked_rotation(rec: dict) -> str | None:
    """The latest day an untracked rotation of this live key can have happened on, or None: the door's
    `openrouter.untracked_through`, the one rule for live rows and tombstones alike (a pending rotation,
    whose predecessor is read by its hash, is not untracked)."""
    import openrouter
    return openrouter.untracked_through(rec)


class Bucket:
    """One project's (or the unattributed) figures: a running sum per window, nulled by any unread part."""

    def __init__(self) -> None:
        self.usd = {w: 0.0 for w in WINDOWS}
        self.blind = {w: False for w in WINDOWS}
        self.keys = 0
        self.retired = 0
        self.folders: set[str] = set()
        self.accounts: set[str] = set()
        self.unread: list[dict] = []
        self.note: str | None = None

    def add(self, got: dict[str, float]) -> None:
        for w in WINDOWS:
            self.usd[w] += got.get(w, 0.0)

    def miss(self, key: str | None, reason: str, windows: list[str]) -> None:
        for w in windows:
            self.blind[w] = True
        self.unread.append({"key": key, "reason": reason, "windows": list(windows)})

    def figure(self, w: str):
        return None if self.blind[w] else round(self.usd[w], 6)

    def doc(self, latest: dict[str, str | None]) -> dict:
        days = [latest.get(a) for a in sorted(self.accounts)]
        out = {"month_usd": self.figure("month"), "last30_usd": self.figure("last30"), "keys": self.keys,
               "retired_keys": self.retired, "folders": sorted(self.folders),
               "through": min(days) if days and all(days) else None,
               "partial": any(self.blind.values()), "unread": self.unread}
        if self.note:
            out["note"] = self.note
        return out


def project_key(text, projects) -> tuple[str | None, str | None]:
    """(the bucket a ledger row's `project` names, the folder it names). The bucket is the registry id of the one
    project that claims the string, else the string as typed. None (or empty) is no claim."""
    text = str(text or "").strip()
    if not text:
        return None, None
    if projects is None:
        return text, text
    import vault_project
    r = vault_project.resolve(text, projects, loaded=True)
    if r.project_id:
        return r.project_id, r.folder or text
    return text, text


class Stop(Exception):
    """The scan stops reading keys: the step's deadline, or the provider is unreachable."""


def per_project(seen: dict, month: str, start: str, end: str, degraded: list) -> tuple[dict, dict]:
    """Every live and retired ledger key read by hash and summed under its project; the rest unattributed."""
    import openrouter
    import vault_project

    def attribution(source: str, reason: str) -> None:
        degraded.append({"source": source, "reason": reason, "scope": "attribution"})

    unattributed = Bucket()
    latest = {label: v.get("latest") for label, v in seen.items()}
    try:
        doc = openrouter.ledger()
    except (OSError, RuntimeError, ValueError) as exc:
        attribution("spend:openrouter:ledger", f"the door's ledger: {exc}")
        for w in WINDOWS:
            unattributed.blind[w] = True
        return {}, {**unattributed.doc(latest), "unledgered_month_usd": None, "unledgered_last30_usd": None}
    projects = vault_project.load_projects()
    if projects is None:
        attribution("spend:registry", "the registry would not read, so which names are one project is unknown: "
                                      "every project's figures are null")
    # The work: (ledger name, its row or tombstone, the hashes to read, the bucket, what it is).
    work: list[tuple[str, dict, list[str], Bucket]] = []
    buckets: dict[str, Bucket] = {}

    def bucket_for(project) -> Bucket:
        key, folder = project_key(project, projects)
        if key is None:
            return unattributed
        b = buckets.setdefault(key, Bucket())
        b.folders.add(folder)
        return b

    issued = doc.get("issued") or {}
    retired = doc.get("retired", [])
    malformed: list[str] = []
    if not isinstance(retired, list):           # `ledger()` refuses this; an older door or a hand edit may not
        malformed.append("retired")
        retired = []
    rows = []
    for name, rec in sorted(issued.items()):
        if isinstance(rec, dict):
            rows.append((name, rec))
        else:
            malformed.append(f"issued:{name}")
    stones = []
    for i, tomb in enumerate(retired):
        if not isinstance(tomb, dict):
            malformed.append(f"retired[{i}]")
            continue
        if windows_from(str(tomb.get("retired_on") or "")[:10], month, start):
            stones.append(tomb)                 # retired before either window began: no spend to find there
        elif tomb.get("deleted", True) is not True:
            stones.append(tomb)                 # may still be spending: followed while the door keeps it
    # A hash two entries hold would be counted once and read as 0 for the other: neither is trusted.
    holders: dict[str, int] = {}
    for _name, rec in rows:
        for h in set(row_hashes(rec)):
            holders[h] = holders.get(h, 0) + 1
    for tomb in stones:
        if tomb.get("hash"):
            holders[tomb["hash"]] = holders.get(tomb["hash"], 0) + 1
    shared = {h for h, n in holders.items() if n > 1}
    ledgered = {label: {w: 0.0 for w in WINDOWS} for label in seen}

    def not_whole(account) -> None:
        if account in seen:
            seen[account]["whole"] = False

    def doubled(name: str, bucket: Bucket, account) -> None:
        bucket.miss(name, "its provider hash is held by another ledger entry too, so its spend cannot be "
                          "told apart", list(WINDOWS))
        attribution(f"spend:openrouter:key:{name}", "a provider hash held by two ledger entries")
        not_whole(account)

    for name, rec in rows:
        bucket = bucket_for(rec.get("project"))
        bucket.keys += 1
        bucket.accounts.add(str(rec.get("account")))
        if not rec.get("hash"):
            bucket.miss(name, "no provider hash in the ledger (pending: minted or not, unknown)", list(WINDOWS))
            attribution(f"spend:openrouter:key:{name}", "no provider hash to read by")
            not_whole(rec.get("account"))
            continue
        hashes = row_hashes(rec)
        if any(h in shared for h in hashes):
            doubled(name, bucket, rec.get("account"))
            continue
        untracked = untracked_rotation(rec)
        if untracked:
            hidden = windows_from(untracked, month, start)
            if hidden:
                bucket.miss(name, f"rotated on or before {untracked} without a tombstone: the predecessor's spend "
                                  "cannot be read by hash", hidden)
                attribution(f"spend:openrouter:key:{name}", f"rotated on or before {untracked} before tombstones "
                                                            f"existed: {', '.join(hidden)} not attributable")
        work.append((name, rec, hashes, bucket))
    for tomb in stones:
        day = str(tomb.get("retired_on") or "")[:10]
        bucket = bucket_for(tomb.get("project"))
        bucket.retired += 1
        account = tomb.get("account")
        bucket.accounts.add(str(account))
        name = str(tomb.get("name") or "?")
        if tomb.get("hash") in shared:
            doubled(name, bucket, account)
            continue
        horizon = str(tomb.get("untracked_through") or "")[:10]
        if horizon:
            hidden = windows_from(horizon, month, start)
            if hidden:
                bucket.miss(name, f"rotated on or before {horizon} without a tombstone, before it was retired",
                            hidden)
                attribution(f"spend:openrouter:key:{name}", f"retired with a rotation untracked through {horizon}")
        if tomb.get("predecessor_hash"):
            hidden = windows_from(day, month, start)
            bucket.miss(name, "retired while a rotation was pending: its predecessor's spend was not read", hidden)
            attribution(f"spend:openrouter:key:{name}", "retired with a pending predecessor not read")
            not_whole(account)
        if tomb.get("deleted", True) is not True:
            # Not known to be deleted: it may still be spending, so it is read by its hash like a live key, and any
            # snapshot it carries is not used (the activity report holds those days too). Gone (404): when it
            # stopped is unknown, so every window reads null (round 5, R5-1).
            if not tomb.get("hash"):
                bucket.miss(name, f"retired on {day} ({tomb.get('reason')}), not known to be deleted, with no "
                                  "hash to read by", list(WINDOWS))
                attribution(f"spend:openrouter:key:{name}", "a retired key not known to be deleted, no hash")
                not_whole(account)
            else:
                work.append((name, tomb, [tomb["hash"]], bucket))
            continue
        got = snapshot_sums(tomb, month, start, end)
        if got is None:
            # No snapshot of what it spent, and the provider keeps no history of a deleted key (404, measured
            # 2026-10-15): every window it may have spent in is unknown.
            bucket.miss(name, f"retired on {day} ({tomb.get('reason')}) with no usage snapshot: "
                              f"{tomb.get('usage_error') or 'none recorded'}", windows_from(day, month, start))
            attribution(f"spend:openrouter:key:{name}", "a retired key with no usage snapshot")
            not_whole(account)
            continue
        usd, unknown = got
        bucket.add(usd)
        if account in ledgered:
            for w in WINDOWS:
                ledgered[account][w] += usd[w]
        if unknown:
            bucket.miss(name, f"its usage snapshot did not reconcile with the provider's counters: the days after "
                              f"{tomb.get('activity_through') or 'its first'} through {day} are unknown", unknown)
            attribution(f"spend:openrouter:key:{name}", "a usage snapshot that did not reconcile")
            not_whole(account)

    # Read every hash once, under the step's deadline.
    admins: dict[str, str | None] = {}
    stopped: str | None = None
    for name, rec, hashes, bucket in work:
        account = rec.get("account")
        acct = seen.get(account)
        if stopped:
            bucket.miss(name, stopped, list(WINDOWS))
            if acct is not None:
                acct["whole"] = False
            continue
        if step_budget.remaining() < DEADLINE_RESERVE_S:
            stopped = "not read: the step's deadline arrived before this key"
            attribution("spend:deadline", "the step's deadline arrived; the keys not reached are named as unread")
            bucket.miss(name, stopped, list(WINDOWS))
            if acct is not None:
                acct["whole"] = False
            continue
        if account not in admins:
            try:
                admins[account] = openrouter.read_admin(account)[1]
            except (OSError, RuntimeError, ValueError) as exc:
                admins[account] = None
                attribution(f"spend:openrouter:{account}",
                            f"no provisioning key to read its keys: {type(exc).__name__}")
        admin = admins[account]
        if admin is None:
            bucket.miss(name, f"its account {account!r} has no readable provisioning key", list(WINDOWS))
            if acct is not None:
                acct["whole"] = False
            continue
        got = {w: 0.0 for w in WINDOWS}
        try:
            for h in hashes:
                got_rows, bad = clean(key_activity(admin, h))
                if bad:
                    raise ValueError(f"{bad} activity row(s) not well formed")
                for w, usd in sums(got_rows, month, start, end).items():
                    got[w] += usd
        except openrouter.Unreachable as exc:
            stopped = f"not read: the provider became unreachable ({exc})"
            attribution("spend:openrouter:unreachable", "the provider became unreachable; the scan stopped and "
                                                       "the keys not reached are named as unread")
            bucket.miss(name, stopped, list(WINDOWS))
            if acct is not None:
                acct["whole"] = False
            continue
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError, http.client.HTTPException) as exc:
            reason = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__
            if "retired_on" in rec and "HTTP 404" in reason:
                reason = "its key, not known to be deleted, is gone at the provider; when it stopped is unknown"
            bucket.miss(name, f"GET /activity: {reason}", list(WINDOWS))
            attribution(f"spend:openrouter:key:{name}", f"GET /activity: {reason}")
            if acct is not None:
                acct["whole"] = False
            continue
        bucket.add(got)
        if account in ledgered:
            for w in WINDOWS:
                ledgered[account][w] += got[w]

    # The residual: what each account spent on hashes no ledger row or tombstone holds.
    residual = {w: 0.0 for w in WINDOWS}
    # An account the estate scan could not read has an unknown residual, so the unattributed total is unknown too.
    accounts_unread = any(d.get("scope") == "account" for d in degraded)
    blind = {w: accounts_unread for w in WINDOWS}
    for label, acct in seen.items():
        if not acct.get("whole", True):
            for w in WINDOWS:
                blind[w] = True
            continue
        for w in WINDOWS:
            left = acct[w] - ledgered[label][w]
            if left < -RESIDUAL_NOISE_USD:
                blind[w] = True
                attribution(f"spend:openrouter:{label}", f"its keys add up to more than the account's {w} figure")
                continue
            residual[w] += max(left, 0.0)
    for w in WINDOWS:
        if blind[w]:
            unattributed.blind[w] = True
        else:
            unattributed.usd[w] += residual[w]
    if projects is None:
        for b in buckets.values():
            for w in WINDOWS:
                b.blind[w] = True
            b.note = "the registry would not read: this name may be one of several for one project"
    if malformed:
        # An entry that is not an object may be any project's key: no project's figure can be shown whole.
        attribution("spend:openrouter:ledger", f"{len(malformed)} ledger entr{'y is' if len(malformed) == 1 else 'ies are'}"
                                               f" not readable: {', '.join(malformed[:5])}")
        for b in [*buckets.values(), unattributed]:
            for w in WINDOWS:
                b.blind[w] = True
            b.note = "the door's ledger holds an entry that cannot be read; it may be this project's key"
    un = unattributed.doc(latest)
    un["unledgered_month_usd"] = None if blind["month"] else round(residual["month"], 6)
    un["unledgered_last30_usd"] = None if blind["last30"] else round(residual["last30"], 6)
    if any(blind.values()):
        un["partial"] = True
    return {k: b.doc(latest) for k, b in sorted(buckets.items())}, un


def main(argv: list[str]) -> int:
    import configuration
    if not configuration.enabled("openrouter"):
        print("spend: not configured (integration openrouter disabled)")
        return 0
    out_path = pathlib.Path(argv[1]) if len(argv) > 1 else paths.SCRATCH / "spend.json"
    now = utcnow()
    month, start, end = window_of(now)
    import openrouter
    accounts, degraded = [], []
    seen: dict[str, dict] = {}

    def account(source: str, reason: str) -> None:
        degraded.append({"source": source, "reason": reason, "scope": "account"})
    try:
        stashed = openrouter.admins()
    except (OSError, RuntimeError) as exc:
        stashed = []
        account("spend:openrouter", f"the door's account store: {exc}")
    for label, _path in stashed:
        if step_budget.remaining() < DEADLINE_RESERVE_S:
            account(f"spend:openrouter:{label}", "not read: the step's deadline arrived")
            continue
        try:
            _label, key = openrouter.read_admin(label)
        except (OSError, RuntimeError, ValueError) as exc:
            account(f"spend:openrouter:{label}", f"unreadable: {type(exc).__name__}")
            continue
        try:
            rows, bad = clean(activity(key, min(ACCOUNT_TIMEOUT_S, step_budget.remaining() - WRITE_MARGIN_S)))
        except urllib.error.HTTPError as exc:
            account(f"spend:openrouter:{label}", f"GET /activity HTTP {exc.code}")
            continue
        except OSError as exc:
            account(f"spend:openrouter:{label}", f"unreachable: {type(exc).__name__}")
            continue
        except (ValueError, TypeError, AttributeError, http.client.HTTPException) as exc:
            account(f"spend:openrouter:{label}", f"GET /activity: {type(exc).__name__}: not an activity report")
            continue
        finally:
            key = None
        if bad:
            account(f"spend:openrouter:{label}", f"{bad} activity row(s) not well formed and not counted")
        windows = sums(rows, month, start, end)
        days = sorted(d for d, _u in rows if d)
        seen[label] = {**windows, "latest": days[-1] if days else None, "whole": not bad}
        accounts.append({"provider": "openrouter", "account": label, "month": month, **month_of(rows, month),
                         "last30_usd": round(windows["last30"], 6)})
    projects, unattributed = per_project(seen, month, start, end, degraded)
    total = round(sum(a["usd"] for a in accounts), 6)
    estate_partial = any(d.get("scope") == "account" for d in degraded)
    attribution_partial = bool(unattributed.get("partial") or any(p.get("partial") for p in projects.values())
                               or any(d.get("scope") == "attribution" for d in degraded))
    atomic.write_json(out_path, {
        "schema_version": SCHEMA_VERSION, "scanned_at": now.isoformat(), "month": month,
        "window": {"last30_from": start, "last30_through": end}, "accounts": accounts,
        "total_usd": total, "projects": projects, "unattributed": unattributed,
        "partial": estate_partial, "attribution_partial": attribution_partial, "degraded": degraded,
    }, indent=1)
    print(f"spend.json: {month} ${total:.2f} across {len(accounts)} account(s), {len(projects)} project(s)"
          + (f", {len(degraded)} not read" if degraded else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
