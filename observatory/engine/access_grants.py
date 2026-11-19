"""Who holds the provider keys this engine issued, in the one shape every product answers (OBS-658, DEC-0187).

Fabric Dashboards joins the answers of every product that issues access (Fabric's agent access, an account manager's
seats, a media or text service's client keys, this vault's provider keys) by agent: an agent's Access tab and one Access
page. This module is Observatory's half: the capability `access.grants.list` answers an `access-grants/1` document
(fabric-agent-contract DEC-0187 at commit 320c7ab9, vendored in `fabric/access-grants/`) listing the OpenRouter keys the
OpenRouter door issued (`tools/openrouter.py`, its ledger `openrouter-issued.json`).

One grant per key the ledger records, and one per tombstone of a key it revoked:

- **id**: a digest of the account, the key's name and the day it was issued, so a grant keeps its id across answers,
  across a rotation (the same grant, a new credential) and into its revocation. Never the provider's hash (AG-7).
- **holder** (AG-2): the agent that reads the key, by service key `<id>.<instance>`, when that is measured: the key's
  vault folder resolves to one project (`vault_project.resolve`, the vault's own rule) and the registry attributes
  exactly one service to that project (`ops_digest_store.service_projects`: the curated name or the measured source
  repository, never a name's look). A key delivered to this engine itself is held by this engine. Anything else is
  `unresolved` with the key's name in the ledger as its label: a host shows it as unresolved and never guesses.
- **subject** (AG-3): the provider account the key spends, `openrouter` and the door's account label.
- **money** (AG-4): the ledger's ceiling and its reset (`day`, `week`, `month`; a ceiling without a reset is the key's
  whole life, `total`); the day's spend is the provider's `usage_daily` for the key, read from its account's listing,
  and `null` (never 0) when the listing could not be read, was read on another UTC day or does not hold the key.
- **issued** and **revoked** (AG-5): the time from the vault's movements journal (the door journals every issue and
  revoke), else the ledger's day at midnight UTC. The door records no caller identity (the command line and the stdio
  MCP server have none), so an act is recorded `by: operator`, `via: config`: the operator's standing delegation acting
  through the engine's own door, never a guessed agent.

`doors` is absent: the issue door (`observatory_key_act`, `mint`) takes its own input and the revoke door that takes
`$defs/revokeInput` is OBS-659. Read-only: nothing here mints, revokes or changes a key, and no value, prefix, hash,
person's name or address is read into the answer.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import re
import sys
import time
from collections import defaultdict
from typing import Any, Callable

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PROTOCOL = "access-grants/1"
SCHEMA = ROOT / "fabric" / "access-grants" / "access-grants.schema.json"
PROVIDER = "openrouter"
#: The ledger's reset and the contract's window (DEC-0183 SL-1). No reset: a lifetime ceiling.
PERIODS = {"daily": "day", "weekly": "week", "monthly": "month"}
#: Why a tombstone left the ledger, as the revocation's reason. A rotation's tombstones are not revocations: the
#: grant goes on under its successor, so they are not listed.
REVOKED_REASONS = {
    "revoked": "Revoked through the OpenRouter door.",
    "replaced-by-issue": "Replaced by a new key issued under the same name.",
    "forgotten-pending": "An issue recorded as pending was cleared; whether its key was minted was not known.",
}
ROTATION_REASONS = frozenset({"rotated", "stale-successor"})
#: How long one account's listing is reused: a host reads the list every few minutes.
LISTING_TTL = 120.0
PAGE_MAX = 500
DEGRADED_MAX = 16
OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SERVICE_KEY = re.compile(r"^[a-z][a-z0-9-]{1,62}\.[a-z][a-z0-9-]{0,31}$")
CURSOR = re.compile(r"^offset-(\d{1,9})$")


# ── time ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def iso(when: dt.datetime) -> str:
    return when.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> dt.datetime | None:
    """An RFC 3339 time or a `YYYY-MM-DD` day (midnight UTC), aware; None for anything else."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            return dt.datetime.combine(dt.date.fromisoformat(text), dt.time(), dt.timezone.utc)
        when = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


# ── FAC-SEM-185 and grantState, ported from the contract's src/access-grants-rules.ts at 320c7ab9 ────────────────

def _strict(value: Any) -> float:
    """`strictTime`: epoch seconds of an RFC 3339 date-time, NaN otherwise."""
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$",
                                                  value):
        return math.nan
    when = parse_time(value)
    return when.timestamp() if when else math.nan


def rules(value: Any) -> list[dict]:
    """FAC-SEM-185: findings `{code, instancePath, message}` for an `access-grants/1` answer; never raises."""
    findings: list[dict] = []

    def flag(path: str, message: str) -> None:
        findings.append({"code": "FAC-SEM-185", "instancePath": path, "message": message})
    if not isinstance(value, dict):
        return findings
    grants = value.get("grants") if isinstance(value.get("grants"), list) else []
    generated = _strict(value.get("generatedAt"))
    ids: set[str] = set()
    for i, grant in enumerate(grants):
        if not isinstance(grant, dict):
            continue
        at = f"/grants/{i}"
        gid = str(grant.get("id", ""))
        if gid in ids:
            flag(f"{at}/id", f"grant id {gid} appears twice")
        ids.add(gid)
        metered = isinstance(grant.get("limitUsd"), (int, float)) and not isinstance(grant.get("limitUsd"), bool)
        if metered and "limitPeriod" not in grant:
            flag(f"{at}/limitPeriod", "a ceiling names its window: limitPeriod is present when limitUsd is a number")
        if not metered and "limitPeriod" in grant:
            flag(f"{at}/limitPeriod", "limitPeriod is the window of a ceiling, so it is present only when limitUsd "
                                      "is a number")
        issued = grant["issued"] if isinstance(grant.get("issued"), dict) else {}
        revoked = grant["revoked"] if isinstance(grant.get("revoked"), dict) else None
        for name, act in (("issued", issued), ("revoked", revoked)):
            if act is None:
                continue
            if act.get("via") == "agent" and "agent" not in act:
                flag(f"{at}/{name}/agent", "an act that came through an agent's call names that agent")
            if act.get("via") != "agent" and "agent" in act:
                flag(f"{at}/{name}/agent", "agent names the caller of an act that came through an agent's call, so "
                                           "it is present only with via agent")
            if act.get("by") == "agent" and act.get("via") != "agent":
                flag(f"{at}/{name}/via", "an act an agent decided on its own account came through its call: via is "
                                         "agent")
        issued_at = _strict(issued.get("at"))
        if not math.isnan(issued_at) and not math.isnan(generated) and issued_at > generated:
            flag(f"{at}/issued/at", "a grant is not issued after the answer's generatedAt")
        if revoked is not None:
            revoked_at = _strict(revoked.get("at"))
            if not math.isnan(revoked_at) and not math.isnan(issued_at) and revoked_at < issued_at:
                flag(f"{at}/revoked/at", "a grant is not revoked before it was issued")
            if not math.isnan(revoked_at) and not math.isnan(generated) and revoked_at > generated:
                flag(f"{at}/revoked/at", "a grant is not revoked after the answer's generatedAt")
        expires = _strict(grant.get("expiresAt"))
        if not math.isnan(expires) and not math.isnan(issued_at) and expires <= issued_at:
            flag(f"{at}/expiresAt", "a grant expires after it was issued")
    return findings


def grant_state(grant: Any, now: float) -> str | None:
    """`grantState`: `revoked`, `expired` (expiresAt at or before `now`, epoch seconds) or `active`; None for what is
    not a grant or not a time."""
    if not isinstance(grant, dict) or not isinstance(now, (int, float)) or not math.isfinite(now):
        return None
    if isinstance(grant.get("revoked"), dict):
        return "revoked"
    expires = _strict(grant.get("expiresAt"))
    return "expired" if not math.isnan(expires) and expires <= now else "active"


# ── sources ──────────────────────────────────────────────────────────────────────────────────────────────────────

class Unread(RuntimeError):
    """A source that could not be read; its message is safe to show (no value, no path)."""


@dataclasses.dataclass
class Sources:
    """What an answer is built from; the defaults read this machine, a test passes its own."""
    ledger: Callable[[], dict]
    movements: Callable[[], list[dict]]
    #: account label -> (UTC day read, {provider hash: usage_daily or None}); raises Unread.
    spend: Callable[[str], tuple[str, dict[str, float | None]]]
    #: vault folder -> the service keys the registry attributes to its project; raises Unread.
    holders: Callable[[str], set[str]]
    issuer: str
    now: dt.datetime


def _ledger() -> dict:
    sys.path.insert(0, str(ROOT / "tools"))
    import openrouter
    try:
        return openrouter.ledger()
    except (OSError, RuntimeError, ValueError) as exc:
        raise Unread(f"the OpenRouter door's ledger could not be read ({type(exc).__name__})") from None


def _movements() -> list[dict]:
    sys.path.insert(0, str(ROOT / "tools"))
    import vault
    try:
        return [r for r in vault.movements() if isinstance(r, dict) and str(r.get("secret") or "").startswith(
            PROVIDER + "/")]
    except Exception as exc:                                                 # noqa: BLE001  degraded, named
        raise Unread(f"the vault's movements journal could not be read ({type(exc).__name__})") from None


_LISTINGS: dict[str, tuple[float, str, dict[str, float | None]]] = {}


def _spend(account: str) -> tuple[str, dict[str, float | None]]:
    hit = _LISTINGS.get(account)
    if hit and time.monotonic() - hit[0] < LISTING_TTL:
        return hit[1], hit[2]
    if os.environ.get("OBSERVATORY_OFFLINE") == "1":
        raise Unread("offline: the provider's listing was not asked")
    sys.path.insert(0, str(ROOT / "tools"))
    import openrouter
    try:
        _label, admin = openrouter.read_admin(account)
        rows = openrouter._all_keys(admin)
    except Exception as exc:                                                 # noqa: BLE001  degraded, named
        raise Unread(f"{type(exc).__name__}") from None
    day = dt.datetime.now(dt.timezone.utc).date().isoformat()
    out = {r["hash"]: openrouter._counter(r, "usage_daily") for r in rows if isinstance(r, dict) and r.get("hash")}
    _LISTINGS[account] = (time.monotonic(), day, out)
    return day, out


def _holder_index() -> Callable[[str], set[str]]:
    """The registry read once per answer: each project's attributed services, and the vault's folder rule."""
    import identifiers
    import ops_digest_store
    import paths
    import vault_project
    try:
        reg = identifiers.load_registry()
        projects = vault_project.load_projects()
        agents = json.loads((pathlib.Path(paths.REGISTRY) / "agents.json").read_text(encoding="utf-8"))["agents"]
    except Exception as exc:                                                 # noqa: BLE001  degraded, named
        raise Unread(f"the registry could not be read ({type(exc).__name__})") from None
    by_project: dict[str, set[str]] = defaultdict(set)
    for row in agents if isinstance(agents, list) else []:
        if not isinstance(row, dict):
            continue
        sid, inst = row.get("service_id"), row.get("instance")
        if not (isinstance(sid, str) and isinstance(inst, str) and SERVICE_KEY.match(f"{sid}.{inst}")):
            continue
        for project in ops_digest_store.service_projects({"id": sid, "instance": inst}, lambda: reg):
            by_project[project].add(f"{sid}.{inst}")

    def holders(folder: str) -> set[str]:
        res = vault_project.resolve(folder, projects)
        if not res.project_id or res.how == "ambiguous":
            return set()
        return set(by_project.get(res.project_id, ()))
    return holders


def _issuer() -> str:
    import service_identity
    return f"{service_identity.SERVICE_ID}.{service_identity.instance()}"


def default_sources() -> Sources:
    def holders(folder: str) -> set[str]:
        if not hasattr(holders, "index"):
            holders.index = _holder_index()                                 # type: ignore[attr-defined]
        return holders.index(folder)                                        # type: ignore[attr-defined]
    return Sources(ledger=_ledger, movements=_movements, spend=_spend, holders=holders, issuer=_issuer(),
                   now=dt.datetime.now(dt.timezone.utc))


# ── the answer ───────────────────────────────────────────────────────────────────────────────────────────────────

def grant_id(account: str, name: str, issued_on: str) -> str:
    digest = hashlib.sha256(f"{account}\0{name}\0{issued_on}".encode("utf-8")).hexdigest()
    return f"{PROVIDER}-{digest[:24]}"


def _account_ref(account: str) -> str:
    return account if OPAQUE.match(account or "") else "account-" + hashlib.sha256(
        (account or "").encode("utf-8")).hexdigest()[:16]


class _Builder:
    def __init__(self, sources: Sources):
        self.s = sources
        self.now = sources.now.astimezone(dt.timezone.utc).replace(microsecond=0)
        self.today = self.now.date().isoformat()
        self.degraded: list[str] = []
        self.counts: dict[str, int] = defaultdict(int)
        try:
            moves = sources.movements()
        except Unread as exc:
            moves = []
            self.note(f"{exc}: issue and revoke times are the ledger's days at midnight UTC")
        self.moves: dict[str, list[tuple[dt.datetime, str]]] = defaultdict(list)
        for row in moves:
            when = parse_time(row.get("at"))
            if when is not None:
                self.moves[str(row.get("secret"))].append((when, str(row.get("event") or "")))
        self.listings: dict[str, tuple[str, dict] | None] = {}
        self.holder_failed = False

    def note(self, text: str) -> None:
        if len(self.degraded) < DEGRADED_MAX and text[:200] not in self.degraded:
            self.degraded.append(text[:200])

    def _moved(self, account: str, name: str, events: tuple[str, ...], day: str | None,
               before: dt.datetime | None = None) -> dt.datetime | None:
        """The latest journal time of one of `events` for this key, on `day` when given, not after `before`."""
        found = [w for w, e in self.moves.get(f"{PROVIDER}/{account}/{name}", ()) if e in events
                 and (day is None or w.date().isoformat() == day) and (before is None or w <= before)]
        return max(found) if found else None

    def _clamp(self, when: dt.datetime) -> dt.datetime:
        return min(when, self.now)

    def holder(self, name: str, destination: str | None, folder: str | None) -> dict:
        dest = destination or ""
        if dest == "observatory":
            return {"kind": "agent", "agent": self.s.issuer}
        if dest.startswith("vault:"):
            folder = dest[len("vault:"):].split("/", 1)[0]
        if folder and not self.holder_failed:
            try:
                keys = {k for k in self.s.holders(folder) if SERVICE_KEY.match(k)}
            except Unread as exc:
                self.holder_failed = True
                self.note(f"{exc}: every holder reads as unresolved")
                keys = set()
            if len(keys) == 1:
                return {"kind": "agent", "agent": next(iter(keys))}
        label = (name or "unnamed")[:120]
        return {"kind": "unresolved", "label": label}

    def spent(self, account: str, key_hash: Any) -> float | None:
        if account not in self.listings:
            try:
                self.listings[account] = self.s.spend(account)
            except Unread as exc:
                self.listings[account] = None
                self.note(f"account {_account_ref(account)}: the provider's listing was not read ({exc}); its "
                          f"keys' spend today is unknown")
        listing = self.listings[account]
        if listing is None or not isinstance(key_hash, str) or not key_hash:
            return None
        day, by_hash = listing
        if day != self.today:
            return None
        if key_hash not in by_hash:
            self.counts["unlisted"] += 1
            return None
        value = by_hash[key_hash]
        return round(float(value), 6) if isinstance(value, (int, float)) and math.isfinite(value) else None

    @staticmethod
    def money(grant: dict, rec: dict) -> None:
        limit = rec.get("limit_usd")
        if isinstance(limit, bool) or not isinstance(limit, (int, float)):
            if "limit_usd" in rec:
                grant["limitUsd"] = None
            return
        if not math.isfinite(limit) or limit <= 0:
            grant["limitUsd"] = None
            return
        grant["limitUsd"] = float(limit)
        grant["limitPeriod"] = PERIODS.get(str(rec.get("limit_reset") or ""), "total")

    def active(self, name: str, rec: dict) -> dict | None:
        if rec.get("pending"):
            self.counts["pending"] += 1
            return None
        account = str(rec.get("account") or "")
        issued_on = str(rec.get("issued_on") or "")
        at = (self._moved(account, name, ("issue", "claim"), issued_on or None)
              or parse_time(issued_on) or self.now)
        at = self._clamp(at)
        grant: dict = {"id": grant_id(account, name, issued_on), "holder": self.holder(name, rec.get("destination"),
                                                                                      rec.get("project")),
                       "subject": {"kind": "account", "provider": PROVIDER, "ref": _account_ref(account)},
                       "scopes": []}
        self.money(grant, rec)
        grant["spentDayUsd"] = self.spent(account, rec.get("hash"))
        grant["issued"] = {"at": iso(at), "by": "operator", "via": "config"}
        expires = parse_time(rec.get("expires_at"))
        if expires is not None and expires > at:
            grant["expiresAt"] = iso(expires)
        return grant

    def retired(self, stone: dict) -> dict | None:
        reason = str(stone.get("reason") or "")
        if reason in ROTATION_REASONS:
            return None
        name = str(stone.get("name") or "")
        account = str(stone.get("account") or "")
        retired_on = str(stone.get("retired_on") or "")[:10]
        retired_day = parse_time(retired_on)
        if not name or retired_day is None:
            self.counts["unreadable-tombstone"] += 1
            return None
        issued_on = str(stone.get("issued_on") or "")
        end_of_day = retired_day + dt.timedelta(days=1)
        issued = (self._moved(account, name, ("issue", "claim"), issued_on or None, end_of_day)
                  or parse_time(issued_on))
        revoked = self._moved(account, name, ("revoke", "forget-pending", "forget-pending-attested"), retired_on) or retired_day
        if issued is None:
            issued = revoked
        issued = self._clamp(issued)
        revoked = self._clamp(max(revoked, issued))
        grant: dict = {"id": grant_id(account, name, issued_on or f"retired:{retired_on}"),
                       "holder": self.holder(name, stone.get("destination"), stone.get("project")),
                       "subject": {"kind": "account", "provider": PROVIDER, "ref": _account_ref(account)},
                       "scopes": []}
        self.money(grant, stone)
        read = parse_time(stone.get("read_at"))
        daily = stone.get("usage_daily")
        grant["spentDayUsd"] = (round(float(daily), 6) if read is not None and read.date().isoformat() == self.today
                                and isinstance(daily, (int, float)) and not isinstance(daily, bool)
                                and math.isfinite(daily) else None)
        grant["issued"] = {"at": iso(issued), "by": "operator", "via": "config"}
        grant["revoked"] = {"at": iso(revoked), "by": "operator", "via": "config",
                            "reason": REVOKED_REASONS.get(reason, f"Left the ledger: {reason or 'no reason recorded'}.")}
        return grant


def list_grants(arguments: dict | None = None, sources: Sources | None = None) -> dict:
    """`access.grants.list`: an `access-grants/1` answer for `$defs/listInput` (already validated by the caller)."""
    args = arguments if isinstance(arguments, dict) else {}
    sources = sources or default_sources()
    b = _Builder(sources)
    out: dict = {"protocol": PROTOCOL, "issuer": sources.issuer, "generatedAt": iso(b.now), "grants": []}
    try:
        doc = sources.ledger()
    except Unread as exc:
        b.note(f"{exc}: no grant is listed, which is not the same as none issued")
        out["degraded"] = b.degraded
        return out
    grants: list[dict] = []
    for name, rec in sorted((doc.get("issued") or {}).items()):
        if isinstance(rec, dict):
            g = b.active(str(name), rec)
            if g is not None:
                grants.append(g)
    include_revoked = args.get("includeRevoked") is True
    if include_revoked:
        for stone in doc.get("retired") or []:
            if isinstance(stone, dict):
                g = b.retired(stone)
                if g is not None:
                    grants.append(g)
    else:
        now = b.now.timestamp()
        grants = [g for g in grants if grant_state(g, now) == "active"]
    holder = args.get("holder")
    if isinstance(holder, str):
        grants = [g for g in grants if g["holder"].get("agent") == holder]
    if isinstance(args.get("subject"), str):
        # Every grant here opens a provider account, never an agent (AG-3): none opens the subject asked for.
        grants = []
    seen: set[str] = set()
    unique = []
    for g in sorted(grants, key=lambda g: (g["issued"]["at"], g["id"], "revoked" in g)):
        if g["id"] in seen:
            # Two ledger rows with one account, name and day (a tombstone and its re-issue on the same day) are
            # one grant id: the live one is kept, the other counted.
            b.counts["same-id"] += 1
            continue
        seen.add(g["id"])
        unique.append(g)
    start = 0
    cursor = args.get("cursor")
    if isinstance(cursor, str):
        m = CURSOR.match(cursor)
        if m is None:
            b.note("the cursor is not one this service issued; the list starts from the beginning")
        else:
            start = int(m.group(1))
    limit = args.get("limit") if isinstance(args.get("limit"), int) and not isinstance(args.get("limit"), bool) \
        else PAGE_MAX
    limit = max(1, min(PAGE_MAX, limit))
    out["grants"] = unique[start:start + limit]
    if start + limit < len(unique):
        out["nextCursor"] = f"offset-{start + limit:08d}"
    if b.counts["pending"]:
        b.note(f"{b.counts['pending']} issues are recorded as pending at the door and are not listed")
    if b.counts["unlisted"]:
        b.note(f"{b.counts['unlisted']} keys were not in their account's listing; their spend today is unknown")
    if b.counts["unreadable-tombstone"]:
        b.note(f"{b.counts['unreadable-tombstone']} tombstones without a name or a day were not listed")
    if b.counts["same-id"]:
        b.note(f"{b.counts['same-id']} tombstones share an id with a key issued the same day and are not listed")
    if b.degraded:
        out["degraded"] = b.degraded
    return out
