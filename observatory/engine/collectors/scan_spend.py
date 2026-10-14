#!/usr/bin/env python3
"""This month's provider spend across every account the doors hold — the estate total the L0 limit is about —
and each project's month and last 30 days.

WHY. The meter (meter.py, OBS-55) holds its own reservations, but most spend today happens on keys whose callers
never ask it. L0 — the estate's monthly total across every provider key, USD 1,000 by default (operator,
2026-10-11) — is only meaningful against what the providers actually charged. Each OpenRouter account the door
holds a provisioning key for (`tools/openrouter.py stash`) answers `GET /api/v1/activity` with its daily spend;
this sums the current UTC month per account (OBS-56).

PER PROJECT (J-11, OBS-93). Under contract DEC-0037 Fabric's launcher proposes a project's limit from its agents'
cost presets and its recent spend, and Observatory measures that spend. Every key in the door's ledger
(`openrouter-issued.json`) is read once by its hash (`openrouter.read_activity`: the last 30 completed UTC days,
by day and model) and summed under the ledger row's `project`, normalised to the project's vault folder by the
rule every door uses (`vault_project.resolve`: a registry id and its folder are one project). Two windows: the
current UTC month (what a monthly limit counts) and the last 30 completed UTC days (what "recent spend" means
when the month has just begun).

WHAT NO PROJECT CLAIMS IS ITS OWN TOTAL, NEVER A GUESS. A ledger key whose row names no project (a key claimed
from a file, `openrouter.py claim`) and spend on hashes no ledger row holds at all (the account's figure minus
the ledger keys read in it) go to `unattributed`. Nothing is attributed from a key's name, a consumer's name or
a destination's folder: the ledger's `project` field is the only claim.

A WINDOW NOT READ WHOLE IS NULL, NEVER A LOWER FIGURE. A key the provider would not answer for, a pending row
with no hash yet, and a key rotated inside the window (`rotate` deletes its predecessor and the ledger keeps
only the successor's hash, so the predecessor's spend cannot be found by hash) each make that project's figure
for that window `null`, with the key named in `unread` and the source in `degraded`. A pending rotation still
holds `predecessor_hash` and is read. The unattributed residual of an account is null too while any of its
ledger keys is unread, since the two cannot be told apart.

WHAT IT RECORDS. Per account: the door's label, the month's USD and the last 30 days', the number of activity
rows and the last day they cover (the provider's activity lags about a day). Per project: the two windows, the
number of keys, and the names (ledger names, never values) of keys it could not read. Never a key, never a
hash, never a model's prompt, never a per-key figure. An account that does not answer is named in `degraded`
with its HTTP status, and the total says it is partial.

    scan_spend.py store/raw/spend.json
"""
from __future__ import annotations

import json
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

API = "https://openrouter.ai/api/v1"
SCHEMA_VERSION = 2
#: The provider answers `/activity` for the last 30 completed UTC days.
LAST_DAYS = 30
#: Below this, an account's residual (its figure minus its ledger keys') is float noise, not spend.
RESIDUAL_NOISE_USD = 1e-6
WINDOWS = ("month", "last30")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def activity(key: str) -> list[dict]:
    req = urllib.request.Request(f"{API}/activity", headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read()).get("data", []) or []


def key_activity(admin: str, key_hash: str) -> list[dict]:
    """One ledger key's activity, last 30 completed UTC days; raises when the provider will not say."""
    import openrouter
    return openrouter.read_activity(admin, key_hash)


def month_of(rows: list[dict], month: str) -> dict:
    this = [x for x in rows if str(x.get("date", "")).startswith(month)]
    days = sorted({str(x.get("date"))[:10] for x in this})
    return {"usd": round(sum(float(x.get("usage") or 0) for x in this), 6), "rows": len(this),
            "through": days[-1] if days else None}


def window_of(now: datetime) -> tuple[str, str, str]:
    """(month, first day of the last 30 completed UTC days, the last one)."""
    today = now.date()
    return now.strftime("%Y-%m"), (today - timedelta(days=LAST_DAYS)).isoformat(), \
        (today - timedelta(days=1)).isoformat()


def sums(rows: list[dict], month: str, start: str, end: str) -> dict[str, float]:
    """USD per window. The last-30 window is bounded here too: a row outside it is not counted."""
    out = {"month": 0.0, "last30": 0.0}
    for x in rows:
        day = str(x.get("date") or "")[:10]
        usd = float(x.get("usage") or 0)
        if day.startswith(month):
            out["month"] += usd
        if start <= day <= end:
            out["last30"] += usd
    return out


def rotation_windows(rec: dict, month: str, start: str) -> list[str]:
    """The windows a completed rotation hides spend from: the predecessor's hash is gone, so whatever it spent
    inside a window that contains the rotation day cannot be read by hash."""
    if rec.get("predecessor_hash"):
        return []                       # a pending rotation: its predecessor is read like any key
    day = str(rec.get("rotated_on") or "")[:10]
    if not day:
        return []
    try:
        date.fromisoformat(day)
    except ValueError:
        return list(WINDOWS)            # a rotation day that does not parse: neither window can be shown whole
    return [w for w, first in (("month", f"{month}-01"), ("last30", start)) if day >= first]


class Bucket:
    """One project's (or the unattributed) figures: a running sum per window, nulled by any unread part."""

    def __init__(self) -> None:
        self.usd = {w: 0.0 for w in WINDOWS}
        self.blind = {w: False for w in WINDOWS}
        self.keys = 0
        self.unread: list[dict] = []

    def add(self, got: dict[str, float]) -> None:
        for w in WINDOWS:
            self.usd[w] += got.get(w, 0.0)

    def miss(self, key: str, reason: str, windows: list[str]) -> None:
        for w in windows:
            self.blind[w] = True
        self.unread.append({"key": key, "reason": reason, "windows": list(windows)})

    def figure(self, w: str):
        return None if self.blind[w] else round(self.usd[w], 6)

    def doc(self) -> dict:
        return {"month_usd": self.figure("month"), "last30_usd": self.figure("last30"), "keys": self.keys,
                "partial": any(self.blind.values()), "unread": self.unread}


def project_key(text, projects) -> str | None:
    """The bucket a ledger row's `project` names: its vault folder when the registry claims it, else as typed.
    None (or empty) is no claim. An ambiguous name is kept as typed rather than picked."""
    text = str(text or "").strip()
    if not text:
        return None
    import vault_project
    r = vault_project.resolve(text, projects, loaded=True)
    return r.folder if r.folder else text


def per_project(accounts_seen: dict, month: str, start: str, end: str, degraded: list) -> tuple[dict, dict]:
    """Every ledger key read by hash and summed under its project; the rest unattributed."""
    import openrouter
    import vault_project
    unattributed = Bucket()
    try:
        issued = openrouter.ledger().get("issued", {})
    except (OSError, RuntimeError, ValueError) as exc:
        degraded.append({"source": "spend:openrouter:ledger", "reason": f"the door's ledger: {exc}"})
        for w in WINDOWS:
            unattributed.blind[w] = True
        return {}, {**unattributed.doc(), "unledgered_month_usd": None, "unledgered_last30_usd": None}
    projects = vault_project.load_projects()
    if projects is None:
        degraded.append({"source": "spend:registry", "reason": "the registry would not read: each project is "
                                                             "named as its ledger row spells it"})
    buckets: dict[str, Bucket] = {}
    admins: dict[str, str | None] = {}
    # Per account: what its ledger keys were read to have spent, and whether all of them were.
    ledgered = {label: {"usd": {w: 0.0 for w in WINDOWS}, "whole": True} for label in accounts_seen}
    for name, rec in sorted(issued.items()):
        if not isinstance(rec, dict):
            continue
        folder = project_key(rec.get("project"), projects)
        bucket = buckets.setdefault(folder, Bucket()) if folder else unattributed
        bucket.keys += 1
        account = rec.get("account")
        acct = ledgered.setdefault(account, {"usd": {w: 0.0 for w in WINDOWS}, "whole": True})
        hashes = [h for h in (rec.get("hash"), rec.get("predecessor_hash")) if h]
        if not rec.get("hash"):
            bucket.miss(name, "no provider hash in the ledger (pending: minted or not, unknown)", list(WINDOWS))
            degraded.append({"source": f"spend:openrouter:key:{name}", "reason": "no provider hash to read by"})
            acct["whole"] = False
            continue
        if account not in admins:
            try:
                admins[account] = openrouter.read_admin(account)[1]
            except (OSError, RuntimeError, ValueError) as exc:
                admins[account] = None
                degraded.append({"source": f"spend:openrouter:{account}",
                                 "reason": f"no provisioning key to read its keys: {type(exc).__name__}"})
        admin = admins[account]
        if admin is None:
            bucket.miss(name, f"its account {account!r} has no readable provisioning key", list(WINDOWS))
            acct["whole"] = False
            continue
        got = {w: 0.0 for w in WINDOWS}
        try:
            for h in hashes:
                for w, usd in sums(key_activity(admin, h), month, start, end).items():
                    got[w] += usd
        except (OSError, RuntimeError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
            bucket.miss(name, f"GET /activity: {reason}", list(WINDOWS))
            degraded.append({"source": f"spend:openrouter:key:{name}", "reason": f"GET /activity: {reason}"})
            acct["whole"] = False
            continue
        bucket.add(got)
        for w in WINDOWS:
            acct["usd"][w] += got[w]
        hidden = rotation_windows(rec, month, start)
        if hidden:
            # The predecessor's spend stays in the account's figure and so in the residual below: counted once,
            # as unattributed, while this project's window reads null rather than short.
            bucket.miss(name, f"rotated on {rec.get('rotated_on')}: the predecessor's hash is not kept, so its "
                              "spend in this window is not attributable", hidden)
    # The residual: what each account spent on hashes no ledger row holds (and on rotated predecessors).
    residual = {w: 0.0 for w in WINDOWS}
    blind = {w: False for w in WINDOWS}
    for label, acct in ledgered.items():
        seen = accounts_seen.get(label)
        if seen is None:
            for w in WINDOWS:
                blind[w] = True
            continue
        if not acct["whole"]:
            for w in WINDOWS:
                blind[w] = True
            continue
        for w in WINDOWS:
            left = seen[w] - acct["usd"][w]
            if left < -RESIDUAL_NOISE_USD:
                blind[w] = True
                degraded.append({"source": f"spend:openrouter:{label}",
                                 "reason": f"its keys add up to more than the account's {w} figure"})
                continue
            residual[w] += max(left, 0.0)
    for w in WINDOWS:
        if blind[w]:
            unattributed.blind[w] = True
        else:
            unattributed.usd[w] += residual[w]
    un = unattributed.doc()
    un["unledgered_month_usd"] = None if blind["month"] else round(residual["month"], 6)
    un["unledgered_last30_usd"] = None if blind["last30"] else round(residual["last30"], 6)
    if any(blind.values()):
        un["partial"] = True
    return {k: b.doc() for k, b in sorted(buckets.items())}, un


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
    try:
        stashed = openrouter.admins()
    except (OSError, RuntimeError) as exc:
        stashed = []
        degraded.append({"source": "spend:openrouter", "reason": f"the door's account store: {exc}"})
    for label, _path in stashed:
        try:
            _label, key = openrouter.read_admin(label)
        except (OSError, RuntimeError, ValueError) as exc:
            degraded.append({"source": f"spend:openrouter:{label}", "reason": f"unreadable: {type(exc).__name__}"})
            continue
        try:
            rows = activity(key)
        except urllib.error.HTTPError as exc:
            degraded.append({"source": f"spend:openrouter:{label}", "reason": f"GET /activity HTTP {exc.code}"})
            continue
        except OSError as exc:
            degraded.append({"source": f"spend:openrouter:{label}", "reason": f"unreachable: {type(exc).__name__}"})
            continue
        finally:
            key = None
        windows = sums(rows, month, start, end)
        seen[label] = windows
        accounts.append({"provider": "openrouter", "account": label, "month": month, **month_of(rows, month),
                         "last30_usd": round(windows["last30"], 6)})
    projects, unattributed = per_project(seen, month, start, end, degraded)
    total = round(sum(a["usd"] for a in accounts), 6)
    atomic.write_json(out_path, {
        "schema_version": SCHEMA_VERSION, "scanned_at": now.isoformat(), "month": month,
        "window": {"last30_from": start, "last30_through": end}, "accounts": accounts,
        "total_usd": total, "projects": projects, "unattributed": unattributed,
        "partial": bool(degraded), "degraded": degraded,
    }, indent=1)
    print(f"spend.json: {month} ${total:.2f} across {len(accounts)} account(s), {len(projects)} project(s)"
          + (f", {len(degraded)} not read" if degraded else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
