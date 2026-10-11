"""The meter: Observatory answers the contract's `grant-check` (DEC-0037/0038/0039; Fabric ADR-0144 §4, OBS-55).

Before a call that spends, a callee asks the meter. It presents an onward delegation token minted for the meter
(aud `project-observatory`, scope `grant.check`, effect none) and a `grant-check-request` (the amount and the jti
of the token it was called with). The meter:

1. verifies that token offline against the estate's keys; any refusal is `principal-unproven` (DL-21);
2. reads the grant the token names from the hub's grant projection — `<grantsDir>/<meta.id>.json`, written by
   Fabric's hub in the contract's standing-grant shape (Fabric ADR-0144 amendment 1). The file is never
   authority by itself: a missing one is `no-grant`, an unreadable one `unknown-state`, one at another revision
   than the token's ref `no-grant`, a revoked one `grant-revoked`, an expired one `grant-expired`. With no
   grant directory configured every check is `unknown-state`: an unknown grant state is a denial;
3. reserves the amount against every window of the grant and every level above it, in one SQLite write
   transaction, so two concurrent checks cannot together overrun a limit:

   - L3 `call`  the grant's `perCallMaxUsd` (contract DEC-0041 adds the `call` window);
   - L2 `task`  the grant's spend on one task — keyed by the token's `task` claim, else the first
                `act.session` of the chain, else the whole grant (`grant:<ref>`) — never the unsigned
                `tokenJti` (DEC-0041);
   - L1         the grant's `total`, `project`, `day`, `week` and `month` windows, calendar windows in UTC
                (a week starts on Monday);
   - L0 `month` the estate's total across every provider key (USD 1,000 a month by default) and the ceiling of
                the provider key the grant's credential source names, where one is configured.

An allowed check answers a receipt and `reservedUsd` held until `expiresAt`; spend is settled against the
receipt (`settle`), and an unsettled reservation lapses at `expiresAt`. A settled receipt counts what was spent,
not what was reserved. The meter keeps no caller identity: a reservation records the grant, the project, the
task key, the key reference and the token's jti, never the principal or the chain (DL-22).

Configuration is `config/meter.json`; every field is optional:

    {"audience": "project-observatory", "capability": "grant.check", "issuer": "urn:fabric:estate:<id>",
     "hubJson": "<path to the hub's hub.json: its jwks and grantsDir>",
     "jwks": "<path to a JWK Set file>", "grantsDir": "<path>",
     "estateMonthUsd": 1000, "alertRatio": 0.8, "keyCeilingsUsd": {"<project-key ref>": 100},
     "reservationSeconds": 3600}

`jwks` and `grantsDir` override what `hubJson` names. The keys are re-read at most every 10 minutes, and at most
once a minute on an unknown `kid` (DL-15).
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import delegation

AUDIENCE = "project-observatory"
CAPABILITY = "grant.check"
ESTATE_MONTH_USD = 1000.0
ALERT_RATIO = 0.8
RESERVATION_SECONDS = 3600
JWKS_CACHE_SECONDS = 600
JWKS_RETRY_SECONDS = 60
GRANT_WINDOWS = ("total", "task", "project", "day", "week", "month")
#: Where each of a grant's windows stands among the contract's levels.
WINDOW_LEVEL = {"call": "L3", "task": "L2", "total": "L1", "project": "L1", "day": "L1", "week": "L1",
                "month": "L1"}
CREDENTIAL_SOURCES = ("project-key", "capability")
_JTI = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_UNSAFE_ID = re.compile(r"[/\\\x00]|\.\.")
REQUEST_FIELDS = {"amountUsd", "tokenJti", "grantRef", "purpose"}


class RequestError(ValueError):
    """A malformed request: a transport error (`invalid-input`), not a refusal (DL-19)."""


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: Any) -> Optional[int]:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return int(moment.timestamp())


def window_start(window: str, now: int) -> int:
    """The start of the calendar window holding `now`, in UTC."""
    day = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "day":
        return int(day.timestamp())
    if window == "week":
        return int((day - timedelta(days=day.weekday())).timestamp())
    if window == "month":
        return int(day.replace(day=1).timestamp())
    raise ValueError(window)


def validate_request(request: Any) -> dict:
    if not isinstance(request, dict):
        raise RequestError("the request must be a JSON object")
    unknown = set(request) - REQUEST_FIELDS
    if unknown:
        raise RequestError(f"unknown fields: {', '.join(sorted(unknown))}")
    amount = request.get("amountUsd")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount < 0 or amount != amount \
            or amount == float("inf"):
        raise RequestError("amountUsd must be a number of USD at least 0")
    if not isinstance(request.get("tokenJti"), str) or not _JTI.match(request["tokenJti"]):
        raise RequestError("tokenJti must be the jti of the token the callee was called with")
    ref = request.get("grantRef")
    if ref is not None and (not isinstance(ref, str) or not 1 <= len(ref) <= 256):
        raise RequestError("grantRef must be 1-256 characters")
    purpose = request.get("purpose")
    if purpose is not None and (not isinstance(purpose, str) or not 1 <= len(purpose) <= 200):
        raise RequestError("purpose must be one line of 1-200 characters")
    return request


def load_config(path: Optional[Path]) -> dict:
    if path is None or not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must hold a JSON object")
    return value


class KeySource:
    """The estate's JWK Set: from a file, or from hub.json's `jwks`. Cached; re-read on an unknown kid."""

    def __init__(self, read: Callable[[], Any], clock: Callable[[], float] = time.monotonic):
        self._read, self._clock = read, clock
        self._keys: Any = None
        self._at = self._retry_at = float("-inf")
        self._lock = threading.Lock()

    def get(self, kid: Optional[str] = None) -> Any:
        with self._lock:
            now = self._clock()
            stale = now - self._at >= JWKS_CACHE_SECONDS
            unknown = kid is not None and delegation.has_keys(self._keys) and \
                not any(isinstance(k, dict) and k.get("kid") == kid for k in self._keys["keys"])
            if stale or (unknown and now - self._retry_at >= JWKS_RETRY_SECONDS):
                if unknown and not stale:
                    self._retry_at = now
                try:
                    keys = self._read()
                except (OSError, ValueError):
                    keys = None
                if delegation.has_keys(keys) or self._keys is None or stale:
                    self._keys, self._at = keys, now
            return self._keys


def _token_kid(token: Any) -> Optional[str]:
    parsed, problem = delegation.parse_compact(token)
    return None if problem else parsed[0].get("kid")


class Meter:
    def __init__(self, *, db_path: Path, config: Optional[dict] = None, base: Optional[Path] = None,
                 clock: Callable[[], float] = time.time):
        self.config = dict(config or {})
        self.db_path = db_path
        self.clock = clock
        self.base = base or Path.cwd()
        self.audience = self.config.get("audience", AUDIENCE)
        self.capability = self.config.get("capability", CAPABILITY)
        self.issuer = self.config.get("issuer")
        self.estate_month = float(self.config.get("estateMonthUsd", ESTATE_MONTH_USD))
        self.alert_ratio = float(self.config.get("alertRatio", ALERT_RATIO))
        self.key_ceilings = {str(k): float(v) for k, v in (self.config.get("keyCeilingsUsd") or {}).items()}
        self.reservation_seconds = int(self.config.get("reservationSeconds", RESERVATION_SECONDS))
        self.keys = KeySource(self._read_jwks)
        self._ensure_db()

    # -- configuration -----------------------------------------------------------------------------------------
    def _path(self, value: Any) -> Optional[Path]:
        if not isinstance(value, str) or not value:
            return None
        p = Path(value).expanduser()
        return p if p.is_absolute() else self.base / p

    def _hub(self) -> dict:
        path = self._path(self.config.get("hubJson"))
        if path is None:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}

    def _read_jwks(self) -> Any:
        path = self._path(self.config.get("jwks"))
        if path is not None:
            return json.loads(path.read_text(encoding="utf-8"))
        return self._hub().get("jwks")

    def grants_dir(self) -> Optional[Path]:
        path = self._path(self.config.get("grantsDir"))
        if path is not None:
            return path
        try:
            return self._path(self._hub().get("grantsDir"))
        except (OSError, ValueError):
            return None

    # -- the ledger --------------------------------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.db_path.parent, 0o700)
        except OSError:
            pass
        fresh = not self.db_path.exists()
        with contextlib.closing(self._connect()) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS reservations (
                receipt TEXT PRIMARY KEY, grant_ref TEXT NOT NULL, grant_id TEXT NOT NULL, project TEXT,
                task_key TEXT NOT NULL, key_ref TEXT, token_jti TEXT NOT NULL, amount REAL NOT NULL,
                created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, settled_usd REAL, settled_at INTEGER,
                purpose TEXT)""")
            conn.execute("CREATE INDEX IF NOT EXISTS reservations_created ON reservations(created_at)")
        if fresh:
            try:
                os.chmod(self.db_path, 0o600)
            except OSError:
                pass

    @staticmethod
    def _counted(alias: str = "") -> str:
        a = f"{alias}." if alias else ""
        return (f"CASE WHEN {a}settled_usd IS NOT NULL THEN {a}settled_usd "
                f"WHEN {a}expires_at > :now THEN {a}amount ELSE 0 END")

    def _sum(self, conn: sqlite3.Connection, where: str, params: dict) -> float:
        row = conn.execute(f"SELECT COALESCE(SUM({self._counted()}), 0) FROM reservations WHERE {where}",
                           params).fetchone()
        return float(row[0])

    # -- the check ---------------------------------------------------------------------------------------------
    def _denied(self, now: int, code: str, reason: str, level: Optional[str] = None,
                window: Optional[str] = None) -> dict:
        doc = {"decision": "denied", "level": level, "code": code, "reason": reason, "checkedAt": iso(now)}
        if window is not None:
            doc["window"] = window
        return doc

    def _grant(self, ref: str, now: int) -> tuple[Optional[dict], Optional[dict]]:
        """The grant the ref names, or the denial: the projection is read, never trusted alone."""
        directory = self.grants_dir()
        if directory is None:
            return None, self._denied(now, "unknown-state", "no grant directory is configured; an unknown "
                                      "grant state is a denial")
        grant_id, sep, revision = ref.rpartition("#")
        if not sep or not grant_id or not revision.isdigit() or _UNSAFE_ID.search(grant_id):
            return None, self._denied(now, "no-grant", "the grant reference is not <id>#<revision>")
        path = directory / f"{grant_id}.json"
        try:
            grant = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None, self._denied(now, "no-grant", "the hub holds no such grant")
        except (OSError, ValueError, UnicodeDecodeError):
            return None, self._denied(now, "unknown-state", "the grant could not be read")
        meta = grant.get("meta") if isinstance(grant, dict) else None
        if not isinstance(meta, dict) or grant.get("kind") != "standing-grant" or meta.get("id") != grant_id:
            return None, self._denied(now, "unknown-state", "the grant file is not this standing grant")
        if meta.get("revision") != int(revision):
            return None, self._denied(now, "no-grant", f"the grant is at revision {meta.get('revision')}, "
                                      f"the token names {revision}")
        state = grant.get("state")
        if state == "revoked":
            return None, self._denied(now, "grant-revoked", "the grant was revoked")
        expires, starts = parse_time(grant.get("expiresAt")), parse_time(grant.get("startsAt"))
        if state == "expired" or (expires is not None and now >= expires):
            return None, self._denied(now, "grant-expired", "the grant has expired")
        if state != "active" or expires is None or starts is None:
            return None, self._denied(now, "unknown-state", "the grant's state or times cannot be read")
        if now < starts:
            return None, self._denied(now, "no-grant", "the grant has not started")
        return grant, None

    @staticmethod
    def task_key(claims: dict, grant_ref: str) -> str:
        """The task window's key: the token's signed `task` claim, else the first `act.session` from the
        outermost actor, else the whole grant (`grant:<ref>`, one window, closed). Never the request's
        `tokenJti`: it is not signed, and a caller choosing it could open a new window per call and walk past
        L2 (Fabric ADR-0144 amendment 2, 217aff4d; contract DEC-0041)."""
        if isinstance(claims.get("task"), str) and claims["task"]:
            return f"task:{claims['task']}"
        node = claims.get("act")
        while isinstance(node, dict):
            if isinstance(node.get("session"), str) and node["session"]:
                return f"session:{node['sub']}:{node['session']}"
            node = node.get("act")
        return f"grant:{grant_ref}"

    def check(self, token: Any, request: Any) -> dict:
        """One grant check: the answer document (`grant-check.schema.json`). Raises RequestError on bad input."""
        now = int(self.clock())
        accepted = delegation.verify_meter_token(token, meter=self.audience, capability=self.capability,
                                                 jwks=self.keys.get(_token_kid(token)), now=now,
                                                 issuer=self.issuer)
        if not accepted.ok:
            return self._denied(now, "principal-unproven", f"the meter's token was refused ({accepted.detail})")
        request = validate_request(request)
        claims = accepted.claims
        refs = claims["grant"]
        ref = request.get("grantRef")
        if ref is None:
            if len(refs) != 1:
                return self._denied(now, "no-grant", "the token names several grants; name one in grantRef")
            ref = refs[0]
        elif ref not in refs:
            return self._denied(now, "no-grant", "grantRef is not among the token's grants")
        grant, denial = self._grant(ref, now)
        if denial:
            return denial
        if claims.get("project") is not None and claims["project"] != grant.get("project"):
            return self._denied(now, "no-grant", "the token's project is not the grant's")
        amount = float(request["amountUsd"])
        spend = grant.get("spend")
        if not isinstance(spend, dict):
            if amount > 0:
                return self._denied(now, "no-grant", "the grant authorizes no spending")
            spend = {"mode": "unlimited", "credentialSource": None}
        source = spend.get("credentialSource")
        key_ref = None
        if source is not None:
            if not isinstance(source, dict) or source.get("kind") not in CREDENTIAL_SOURCES:
                return self._denied(now, "credential-source-refused", "the credential source is neither the "
                                    "project's own key nor another agent's capability")
            if source["kind"] == "project-key":
                key_ref = source.get("ref")
        limits = spend.get("limits") if spend.get("mode") == "limited" else []
        if spend.get("mode") not in ("limited", "unlimited") or (spend.get("mode") == "limited" and (
                not isinstance(limits, list) or not limits)):
            return self._denied(now, "unknown-state", "the grant's spend block cannot be read")
        per_call = spend.get("perCallMaxUsd")
        if isinstance(per_call, (int, float)) and amount > per_call:
            return self._denied(now, "limit-reached", f"above the grant's per-call maximum of ${per_call:g}",
                                "L3", "call")
        task = self.task_key(claims, ref)
        grant_id = grant["meta"]["id"]
        receipt = "rcpt_" + secrets.token_urlsafe(18)
        expires_at = min(now + self.reservation_seconds, 2**62)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            remaining: list[float] = []
            for limit in sorted(limits, key=lambda l: ("call", "task", "total", "project", "day", "week",
                                                       "month").index(l.get("window", "month"))
                                if l.get("window") in GRANT_WINDOWS else 99):
                window, cap = limit.get("window"), limit.get("limitUsd")
                if window not in GRANT_WINDOWS or isinstance(cap, bool) or not isinstance(cap, (int, float)):
                    conn.execute("ROLLBACK")
                    return self._denied(now, "unknown-state", "a limit of the grant cannot be read")
                params: dict = {"now": now, "grant": grant_id}
                if window == "total":
                    used = self._sum(conn, "grant_id = :grant", params)
                elif window == "task":
                    used = self._sum(conn, "grant_id = :grant AND task_key = :task", {**params, "task": task})
                elif window == "project":
                    used = self._sum(conn, "project = :project", {**params, "project": grant.get("project")})
                else:
                    used = self._sum(conn, "grant_id = :grant AND created_at >= :since",
                                     {**params, "since": window_start(window, now)})
                if used + amount > cap + 1e-9:
                    conn.execute("ROLLBACK")
                    return self._denied(now, "limit-reached", f"the grant's {window} limit of ${cap:g} is "
                                        f"reached (${used:.2f} counted)", WINDOW_LEVEL[window], window)
                remaining.append(cap - used - amount)
            month = {"now": now, "since": window_start("month", now)}
            estate = self._sum(conn, "created_at >= :since", month)
            if estate + amount > self.estate_month + 1e-9:
                conn.execute("ROLLBACK")
                return self._denied(now, "limit-reached", f"the estate's monthly total of "
                                    f"${self.estate_month:g} is reached (${estate:.2f} counted)", "L0", "month")
            if key_ref is not None and key_ref in self.key_ceilings:
                ceiling = self.key_ceilings[key_ref]
                used = self._sum(conn, "created_at >= :since AND key_ref = :key", {**month, "key": key_ref})
                if used + amount > ceiling + 1e-9:
                    conn.execute("ROLLBACK")
                    return self._denied(now, "limit-reached", f"the provider key's monthly ceiling of "
                                        f"${ceiling:g} is reached (${used:.2f} counted)", "L0", "month")
            conn.execute(
                "INSERT INTO reservations (receipt, grant_ref, grant_id, project, task_key, key_ref, token_jti, "
                "amount, created_at, expires_at, purpose) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (receipt, ref, grant_id, grant.get("project"), task, key_ref, request["tokenJti"], amount, now,
                 expires_at, request.get("purpose")))
            conn.execute("COMMIT")
        except sqlite3.Error:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return self._denied(now, "unknown-state", "the meter's ledger could not be written")
        finally:
            conn.close()
        return {"decision": "allowed", "receipt": receipt, "grantRef": ref, "reservedUsd": amount,
                "remainingUsd": round(min(remaining), 6) if remaining else None,
                "expiresAt": iso(expires_at), "checkedAt": iso(now)}

    # -- settlement and status ---------------------------------------------------------------------------------
    def settle(self, receipt: str, spent_usd: float) -> dict:
        if isinstance(spent_usd, bool) or not isinstance(spent_usd, (int, float)) or spent_usd < 0 \
                or spent_usd != spent_usd or spent_usd == float("inf"):
            raise RequestError("the spent amount must be a number of USD at least 0")
        now = int(self.clock())
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute("SELECT settled_usd FROM reservations WHERE receipt = ?", (receipt,)).fetchone()
            if row is None:
                raise RequestError("no such receipt")
            if row["settled_usd"] is not None:
                raise RequestError("the receipt is already settled")
            conn.execute("UPDATE reservations SET settled_usd = ?, settled_at = ? WHERE receipt = ?",
                         (float(spent_usd), now, receipt))
        return {"receipt": receipt, "settledUsd": float(spent_usd), "settledAt": iso(now)}

    def status(self) -> dict:
        now = int(self.clock())
        month = {"now": now, "since": window_start("month", now)}
        with contextlib.closing(self._connect()) as conn:
            estate = self._sum(conn, "created_at >= :since", month)
            held = conn.execute("SELECT COUNT(*) FROM reservations WHERE settled_usd IS NULL AND expires_at > ?",
                                (now,)).fetchone()[0]
            keys = {r["key_ref"]: float(r["used"]) for r in conn.execute(
                f"SELECT key_ref, SUM({self._counted()}) AS used FROM reservations WHERE created_at >= :since "
                "AND key_ref IS NOT NULL GROUP BY key_ref", month)}
        directory = self.grants_dir()
        try:
            keys_ok = delegation.has_keys(self._read_jwks())
        except (OSError, ValueError):
            keys_ok = False
        degraded = []
        if not keys_ok:
            degraded.append("meter:no-estate-keys")
        if directory is None or not directory.is_dir():
            degraded.append("meter:no-grant-directory")
        ratio = estate / self.estate_month if self.estate_month > 0 else 1.0
        return {"audience": self.audience, "capability": self.capability,
                "month": iso(window_start("month", now))[:7], "estateUsd": round(estate, 6),
                "estateLimitUsd": self.estate_month, "estateRatio": round(ratio, 4),
                "alert": ratio >= self.alert_ratio, "openReservations": held,
                "keys": {k: {"usedUsd": round(v, 6), "ceilingUsd": self.key_ceilings.get(k)}
                         for k, v in sorted(keys.items())},
                "degraded": degraded}


def default_meter() -> Meter:
    """The installed meter: its ledger under STATE, its configuration in config/meter.json."""
    import paths
    config_path = paths.config_file("meter.json")
    return Meter(db_path=Path(paths.STATE) / "meter" / "meter.db", config=load_config(config_path),
                 base=config_path.parent)
