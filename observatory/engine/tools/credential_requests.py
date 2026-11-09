#!/usr/bin/env python3
"""Narrow credentials on request: a queue, the administrator's decision, delivery through the issuing door.

    T="$(project-observatory full-path)/tools"          # the installed engine's tools
    python "$T/credential_requests.py" presets [--json]
    python "$T/credential_requests.py" request --agent urn:fabric:agent:acme:alpha-web --provider openrouter \\
        --preset model-key --limit 5 --reset monthly --slot alpha-web/prod/OPENROUTER_API_KEY \\
        --reason "summaries for the release notes" [--context personal | --context org:ID] \\
        [--expires-in-days N | --expires-at YYYY-MM-DDTHH:MM:SSZ] [--json]
    python "$T/credential_requests.py" request --agent urn:fabric:agent:acme:alpha-web --provider cloudflare \\
        --preset email-routing --scope zone=example.com --slot alpha-web/prod/CLOUDFLARE_API_TOKEN \\
        --reason "the contact form's routing rules"
    python "$T/credential_requests.py" list [--state open|issued|refused|withdrawn|revoked|expired|all] [--json]
    python "$T/credential_requests.py" show ID [--json]
    python "$T/credential_requests.py" issue ID [--account LABEL] [--relayed-instruction "…" --source WHERE]
    python "$T/credential_requests.py" refuse ID --reason not-allowed [--note "…"] [--relayed-instruction "…" --source WHERE]
    python "$T/credential_requests.py" pending ID [--note "…"]
    python "$T/credential_requests.py" withdraw ID
    python "$T/credential_requests.py" revoke ID [--relayed-instruction "…" --source WHERE]

WHY (OBS-191 parts 2 and 3). An agent that needs a credential used to get one by a person minting it
at a door and putting it into a slot by hand. Here the consumer files a REQUEST instead: which agent,
for which context (`personal`, or an organization's `org:ID`), which provider and preset, what it
should reach (`--scope`), its spend ceiling (`--limit`), until when, why, and the vault slot it reads.
No value travels with it. The administrator of this space decides: `issue` (the issuing door mints a
narrowed credential with an expiry no later than the request asked, in the account the administrator
names, and delivers it straight into the slot, in the door's own process: the value is never an
argument, never printed, never written anywhere else), `refuse` with one of the contract's reasons, or
`pending`. The consumer may `withdraw` an open request; an issued one is ended by `revoke`, which
revokes at the provider through the same door, proves it, and deletes the slot's value only when that
value is still the one the request was issued (contract DEC-0113: a revocation names its request).

THE SHAPES ARE THE CONTRACT'S (fabric-agent-contract DEC-0111, `credential-request/1` and
`credential-decision/1`): `show --json` prints both documents as they would leave this space, so the
organization exchange (a request in a check-in, the decision and the sealed envelope back) carries
these records unchanged once the node sends them. A request in the `personal` context never leaves
this space and has no request document. What this file adds on top of the contract is local: the
vault slot, the issuing account, and the delivery's provenance.

EVERY REQUEST, DECISION, ISSUE AND REVOCATION IS RECORDED, by name, never a value, in
`credential-requests.jsonl` beside the vault's movement journal (append-only, owner-only), and the
issuing door's own journal lines land in the movement journal as for any issue. The slot's metadata
carries the delivery's provenance (`vault.record_provenance`: issuer door and account, recipient agent,
context and slot, provider, preset, scope, limit, the end, the revoke handle): the fields OBS-360
names, so a hand-over by rotation (OBS-416) can tell the organization's replacement from the personal
value it replaces without opening either.

RULES THAT REFUSE, each before a provider is called:
  * a request in an organization's context is for at most 30 days (FAC-SEM-138), a personal one for at
    most the doors' 365; both for more than a day, since the doors mint in whole days ending no later
    than asked (a request issued within two minutes of a whole day after it was filed gets a day less);
  * the reason and the scope carry no path and nothing shaped like a credential;
  * one open request per slot and context; an issue into a slot that already holds a value is refused
    (organization onboarding ORG-R10: a name already present is never overwritten; request another
    NAME, or rotate through the door that minted the value);
  * an issue in `org:ID` needs this space enrolled in that organization and not removed from it
    (`full org status`); a value never lands in a context this space does not belong to;
  * a preset this file does not offer is refused: Cloudflare's `dns-edit` and `zone-analytics-read`
    name one token per zone, so issuing one for a slot would roll the token another slot holds;
  * a provider answer that does not confirm an end no later than asked is revoked again and the
    request stays open, with the failed attempt recorded.

Values arrive and leave only through the doors (`openrouter.py`, `cloudflare.py`), which keep their
ledgers and records as they do for an `issue` typed by hand.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import math
import os
import pathlib
import re
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import credential_shape  # noqa: E402
import key_expiry  # noqa: E402
import oslocks  # noqa: E402
import osprivacy  # noqa: E402

SCHEMA = "credential-queue/1"
REQUEST_TYPE = "credential-request/1"
DECISION_TYPE = "credential-decision/1"
TOOL = "credential_requests.py"
COMMAND = 'python "$(project-observatory full-path)/tools/credential_requests.py"'
#: FAC-SEM-138: a request in an organization's context asks for at most 30 days.
ORG_MAX_DAYS = 30
DEFAULT_DAYS = {"org": ORG_MAX_DAYS, "personal": key_expiry.DEFAULT_DAYS}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
#: The contract's shapes (fabric-agent-contract schemas at the engine's pin).
AGENT_REF = re.compile(r"urn:fabric:agent:[a-z][a-z0-9]*(?:-[a-z0-9]+)*:[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
ORG_CONTEXT = re.compile(r"org:[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
PROVIDER = re.compile(r"[a-z][a-z0-9-]{1,62}\Z")
PRESET = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
SCOPE_KEY = re.compile(r"[a-z][A-Za-z0-9_]{0,31}\Z")
LINE = re.compile(r"[^\x00-\x1f\u2028\u2029]+\Z")
USD = re.compile(r"(0|[1-9][0-9]{0,8})(\.[0-9]{1,6})?\Z")
RESETS = ("daily", "weekly", "monthly", "never")
#: A slot's NAME as a decision's `credential.slot` carries it (stricter than the vault's own rule).
SLOT_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
REFUSALS = ("not-allowed", "preset-unknown", "expiry-too-long", "scope-too-wide", "provider-unavailable",
            "withdrawn", "unanswered")
#: A path in free text: an absolute or home path, a relative one with a folder, a drive or a UNC path.
#: A URL's own slashes follow a colon and are not matched.
PATHISH = re.compile(r"(?:^|[\s(\"'=,;])(?:~|\.{1,2})?/[^\s/]+/|(?:^|[\s(\"'=,;])/[A-Za-z0-9._-]+\b"
                     r"|\b[A-Za-z]:[\\/]|\\\\[A-Za-z0-9]")
HOST = re.compile(r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z")
STATES = ("open", "issued", "refused", "withdrawn", "revoked", "expired")
#: How long a door may take between this file's clock and its own when it computes an expiry.
MINT_MARGIN = datetime.timedelta(minutes=2)


class Refused(ValueError):
    """A request or a decision this queue will not record or carry out; the message names why and, where one
    exists, the command that changes it. Never quotes a value."""

    def __init__(self, message: str, code: str = "refused") -> None:
        super().__init__(message)
        self.code = code


class IssueFailed(RuntimeError):
    """The door was called and did not deliver: nothing is in the slot. Recorded as a failed attempt; the
    request stays open."""


# ─────────────────────────── time ───────────────────────────────────────────

def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def _stamp(moment: datetime.datetime) -> str:
    return key_expiry.stamp(moment)


def _parse(text) -> datetime.datetime | None:
    at = key_expiry.parse(text)
    if not at:
        return None
    return datetime.datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


# ─────────────────────────── the store ──────────────────────────────────────

def _vault():
    import vault
    return vault


def queue_path() -> pathlib.Path:
    """`credential-requests.jsonl` beside the vault's movement journal, read from the vault's current store."""
    return _vault().STORE / "credential-requests.jsonl"


@contextlib.contextmanager
def _locked():
    """One writer of the queue at a time, held across a door's issue so one request is never issued twice. A
    lock of its own: the vault's lock is taken by the door's delivery inside it."""
    vault = _vault()
    vault._private_dirs(vault.STORE)
    lock = vault.STORE / ".credential-requests.lock"
    vault._no_symlinks(lock)
    fd = osprivacy.open(lock, os.O_CREAT | os.O_RDWR | osprivacy.NONBLOCK | osprivacy.NOFOLLOW, 0o600)
    try:
        oslocks.flock(fd, oslocks.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _append(*rows: dict) -> None:
    vault = _vault()
    for row in rows:
        assert "value" not in row, "a value never enters the queue"
    text = "".join(json.dumps({"schema": SCHEMA, "at": _stamp(_now()), "by": os.environ.get("USER", "unknown"),
                               **row}, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    vault._append_private(queue_path(), text)


def read_rows(path: pathlib.Path | None = None) -> tuple[list[dict], list[dict]]:
    """(rows, degraded). A line that does not parse is named in `degraded` by its number, never skipped in
    silence and never allowed to hide the rows around it."""
    vault = _vault()
    path = path or queue_path()
    vault._no_symlinks(path)
    if not path.exists():
        return [], []
    rows, degraded = [], []
    for number, line in enumerate(vault._read_private(path).splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        if not isinstance(row, dict) or row.get("schema") != SCHEMA:
            degraded.append({"source": "credential-requests", "line": number,
                             "reason": "a line that is not a queue record; it is left as it is"})
            continue
        rows.append(row)
    return rows, degraded


def fold(rows: list[dict], now: datetime.datetime | None = None) -> dict[str, dict]:
    """Every request's state from its rows, in the order written: `open` (no final decision yet, maybe a
    `pending` one), `issued`, `refused`, `withdrawn`, `revoked`, and `expired` for an issued credential past
    its end. A row about an unknown request is ignored; a second final decision cannot be written (`decide`
    refuses it), and one found here anyway does not replace the first."""
    now = now or _now()
    out: dict[str, dict] = {}
    for row in rows:
        event = row.get("event")
        if event == "requested":
            doc = row.get("request")
            if isinstance(doc, dict) and isinstance(doc.get("requestId"), str) and doc["requestId"] not in out:
                out[doc["requestId"]] = {"request": doc, "slot": row.get("slot"), "state": "open",
                                         "requestedBy": row.get("by"), "decisions": [], "attempts": [],
                                         "delivery": None, "revocation": None}
            continue
        entry = out.get(row.get("requestId"))
        if entry is None:
            continue
        if event == "decided" and isinstance(row.get("decision"), dict):
            decision = row["decision"]
            entry["decisions"].append({k: row.get(k) for k in ("decision", "relayed", "note", "at", "by")
                                       if row.get(k) is not None})
            if entry["state"] == "open" and decision.get("outcome") == "issued":
                entry["state"], entry["delivery"] = "issued", row.get("delivery")
            elif entry["state"] == "open" and decision.get("outcome") == "refused":
                entry["state"] = "refused"
        elif event == "withdrawn" and entry["state"] == "open":
            entry["state"] = "withdrawn"
            entry["decisions"].append({"decision": row.get("decision"), "at": row.get("at"), "by": row.get("by")})
        elif event == "attempt-failed":
            entry["attempts"].append({k: row.get(k) for k in ("stage", "reason", "at", "by") if row.get(k)})
        elif event == "revoked" and entry["state"] == "issued":
            entry["state"], entry["revocation"] = "revoked", row.get("revocation")
    for entry in out.values():
        if entry["state"] == "issued":
            end = _parse(((entry.get("delivery") or {}).get("credential") or {}).get("expiresAt"))
            if end is not None and end <= now:
                entry["state"] = "expired"
    return out


def requests(path: pathlib.Path | None = None) -> tuple[dict[str, dict], list[dict]]:
    rows, degraded = read_rows(path)
    return fold(rows), degraded


def resolve_id(text: str, known: dict[str, dict]) -> str:
    """A request id, or a unique prefix of at least 8 characters."""
    text = (text or "").strip().lower()
    if text in known:
        return text
    if len(text) < 8 or not re.fullmatch(r"[0-9a-f-]+", text):
        raise Refused("give the request's id, or at least its first 8 characters (`list` shows them)", "unknown")
    hits = [k for k in known if k.startswith(text)]
    if len(hits) != 1:
        raise Refused(f"{len(hits)} requests start with {text}; give more of the id", "unknown")
    return hits[0]


# ─────────────────────────── the doors ──────────────────────────────────────

class Door:
    """One issuing door, as a request sees it: the presets it offers, the checks a request must pass before
    the administrator is asked, the mint into a slot, and the revocation with its proof."""
    provider = ""
    door = ""

    def presets(self) -> dict[str, dict]:
        raise NotImplementedError

    def check(self, request: dict) -> None:
        preset = self.presets().get(request["preset"])
        if preset is None:
            raise Refused(f"{self.provider} offers no preset {request['preset']!r} here; "
                          f"`{COMMAND} presets` lists them", "preset-unknown")
        scope = request.get("scope") or {}
        allowed = set(preset.get("scope_required", ())) | set(preset.get("scope_optional", ()))
        extra = sorted(set(scope) - allowed)
        if extra:
            raise Refused(f"preset {request['preset']} takes no scope {', '.join(extra)}"
                          + (f" (it takes {', '.join(sorted(allowed))})" if allowed else ""), "scope-too-wide")
        missing = sorted(set(preset.get("scope_required", ())) - set(scope))
        if missing:
            raise Refused(f"preset {request['preset']} needs --scope {missing[0]}=…", "scope-missing")
        if preset.get("limit") == "required" and not request.get("limit"):
            raise Refused(f"a {self.provider} key needs a spend ceiling: --limit USD --reset daily|weekly|monthly",
                          "limit-missing")
        if preset.get("limit") == "refused" and request.get("limit"):
            raise Refused(f"a {self.provider} credential carries no spend ceiling; leave --limit out", "limit-refused")

    def issue(self, request: dict, slot: tuple[str, str, str], account: str | None, expire_days: int) -> dict:
        """Mint and deliver into the empty slot; answers the provenance's `issuer` and `revoke` members.
        Raises IssueFailed when nothing reached the slot."""
        raise NotImplementedError

    def revoke(self, provenance: dict) -> dict:
        """Revoke at the provider and prove it: `{"provider": "revoked" | "gone", "proved": bool}`. Raises
        RuntimeError (with a reason that names no value) when the provider did not revoke it."""
        raise NotImplementedError


def _quiet_door():
    """A door prints its own progress for a person; inside a request its lines go to stderr, so `--json` keeps
    stdout to one document."""
    return contextlib.redirect_stdout(sys.stderr)


#: The presets this file defines itself. Cloudflare's come from its door's own `PRESETS` (those whose token is
#: named by its slot); OpenRouter mints one kind of key, so its one preset is named here.
PRESETS = {
    "model-key": {"provider": "openrouter",
                  "what": "a working key for the account's models, with a USD ceiling and its reset",
                  "limit": "required", "scope_required": (), "scope_optional": ()},
}


class OpenRouterDoor(Door):
    provider, door = "openrouter", "openrouter.py"

    def presets(self) -> dict[str, dict]:
        return {k: v for k, v in PRESETS.items() if v["provider"] == self.provider}

    def check(self, request: dict) -> None:
        super().check(request)
        if (request.get("limit") or {}).get("reset") == "never":
            # A lifetime cap works until the total is reached and then stops for good (trap T17); the door
            # mints with a reset only.
            raise Refused("an OpenRouter ceiling resets daily, weekly or monthly; a lifetime cap stops the key "
                          "for good one day", "limit-refused")

    @staticmethod
    def _module():
        import openrouter
        return openrouter

    def issue(self, request, slot, account, expire_days):
        m = self._module()
        project, env, name = slot
        key_name = m.slug(f"{project}-{env}-{name}")
        limit = request["limit"]
        try:
            with _quiet_door():
                r = m.issue_key(key_name, float(limit["usd"]), account, f"vault:{project}/{env}/{name}", project,
                                limit["reset"], expire_days)
        except (ValueError, RuntimeError, LookupError, OSError) as exc:
            raise IssueFailed(f"openrouter.py refused: {credential_shape.redact(str(exc))}") from None
        command = f'python "$(project-observatory full-path)/tools/openrouter.py" revoke {r["name"]}'
        return {"issuer": {"door": self.door, "account": r["account"]},
                "revoke": {"door": self.door, "handle": r["name"], "account": r["account"], "command": command}}

    def revoke(self, provenance):
        m = self._module()
        handle = (provenance.get("revoke") or {}).get("handle")
        account = (provenance.get("revoke") or {}).get("account")
        if not handle:
            raise RuntimeError("the provenance names no OpenRouter key to revoke")
        try:
            with _quiet_door():
                m.revoke_key(handle)
        except LookupError:
            pass                                      # the ledger no longer knows it: proved below at the provider
        except (ValueError, RuntimeError, OSError) as exc:
            raise RuntimeError(f"openrouter.py refused: {credential_shape.redact(str(exc))}") from None
        try:
            _label, admin = m.read_admin(account)
            proved = m.find_key(admin, handle) is None
        except (ValueError, RuntimeError, LookupError, OSError):
            proved = False
        return {"provider": "revoked", "proved": proved}


class CloudflareDoor(Door):
    provider, door = "cloudflare", "cloudflare.py"

    @staticmethod
    def _module():
        import cloudflare
        return cloudflare

    def presets(self) -> dict[str, dict]:
        out = {}
        for key, preset in self._module().PRESETS.items():
            scope, name = preset.get("scope"), str(preset.get("name") or "")
            # ONE TOKEN PER SLOT. A preset whose token name carries no slot (dns-edit, zone-analytics-read)
            # names one token per zone; issuing it for a request would roll the token another slot holds.
            if "{slot}" not in name:
                continue
            if scope == "zone":
                out[key] = {"what": preset.get("why") or f"{key} on one zone", "limit": "refused",
                            "scope_required": ("zone",), "scope_optional": ("account",)}
            elif scope == "account-vault":
                out[key] = {"what": preset.get("why") or f"{key} on one account", "limit": "refused",
                            "scope_required": (), "scope_optional": ("account",)}
        return out

    def check(self, request: dict) -> None:
        known = self._module().PRESETS.get(request["preset"])
        if known is not None and request["preset"] not in self.presets():
            raise Refused(f"preset {request['preset']} is not issued on request: "
                          + ("its token is named by zone, not by slot, so issuing it for one slot would roll the "
                             "token another slot holds" if "{slot}" not in str(known.get("name") or "")
                             else "it does not deliver into a vault slot")
                          + f"; issue it with cloudflare.py by hand, or ask for a preset `{COMMAND} presets` lists",
                          "preset-unknown")
        super().check(request)
        zone = (request.get("scope") or {}).get("zone")
        if zone is not None and not HOST.fullmatch(zone):
            raise Refused("the zone is a domain name, such as example.com", "scope-invalid")

    def issue(self, request, slot, account, expire_days):
        m = self._module()
        project, env, name = slot
        scope = request.get("scope") or {}
        if account and scope.get("account") and m.slug(account) != m.slug(scope["account"]):
            raise Refused(f"the request asks for account {scope['account']}, and the issue names {account}",
                          "account-mismatch")
        account = account or scope.get("account")
        preset_key = request["preset"]
        preset = m.PRESETS[preset_key]
        target = f"{project}/{env}/{name}"
        try:
            with _quiet_door():
                if preset.get("scope") == "zone":
                    rc = m.cmd_issue_zone(preset_key, scope["zone"], target, account, expire_days=expire_days)
                else:
                    rc = m.cmd_issue_account(preset_key, target, account, expire_days=expire_days)
            _stash, _admin, found = m.find_account(account)
        except (ValueError, RuntimeError, LookupError, OSError) as exc:
            raise IssueFailed(f"cloudflare.py refused: {credential_shape.redact(str(exc))}") from None
        if rc != 0:
            raise IssueFailed("cloudflare.py refused the issue; its own line above says why")
        account_slug = m.slug(found.get("name") or "")
        token = preset["name"].format(zone=scope.get("zone", ""), slot=target)
        return {"issuer": {"door": self.door, "account": account_slug},
                "revoke": {"door": self.door, "account": account_slug, "handle": token, "command": None}}

    def revoke(self, provenance):
        m = self._module()
        handle = (provenance.get("revoke") or {}).get("handle")
        account = (provenance.get("revoke") or {}).get("account")
        if not handle or not account:
            raise RuntimeError("the provenance names no Cloudflare token to revoke")
        try:
            _stash, admin, found = m.find_account(account)
            tid = m.existing_token(admin, found["id"], handle)
            if tid:
                m._request(f"/accounts/{found['id']}/tokens/{tid}", admin, method="DELETE")
            proved = m.existing_token(admin, found["id"], handle) is None
        except (ValueError, RuntimeError, LookupError, OSError, KeyError) as exc:
            raise RuntimeError(f"cloudflare.py refused: {credential_shape.redact(str(exc))}") from None
        m._journal("revoke", f"cloudflare/{account}/{handle}")
        return {"provider": "revoked" if tid else "gone", "proved": proved}


#: The doors a request can be issued through. Heroku, DigitalOcean and Sentry are not offered yet: a
#: request for them is refused with that reason (a backlog row, OBS-191's follow-up, adds them).
DOORS: dict[str, Door] = {"openrouter": OpenRouterDoor(), "cloudflare": CloudflareDoor()}


def presets() -> dict:
    out = {}
    for provider, door in sorted(DOORS.items()):
        try:
            out[provider] = {k: {"what": v.get("what"), "limit": v.get("limit"),
                                 "scopeRequired": list(v.get("scope_required", ())),
                                 "scopeOptional": list(v.get("scope_optional", ()))}
                             for k, v in sorted(door.presets().items())}
        except Exception as exc:  # noqa: BLE001: one door that cannot load does not hide the others
            out[provider] = {"error": type(exc).__name__}
    return out


# ─────────────────────────── a request ──────────────────────────────────────

def _free_text(field: str, text) -> str:
    if not isinstance(text, str) or not text.strip():
        raise Refused(f"{field} is empty", "invalid")
    text = text.strip()
    if len(text) > 300 or not LINE.fullmatch(text):
        raise Refused(f"{field} is one line of at most 300 characters", "invalid")
    if credential_shape.find(text):
        raise Refused(f"{field} looks like it carries a credential; describe the need, never a value", "invalid")
    if PATHISH.search(text):
        raise Refused(f"{field} carries a path; name what is needed, not where it lives", "invalid")
    return text


def _context(text: str | None) -> tuple[str, str]:
    """(the context as written, its kind `personal` or `org`)."""
    text = (text or "personal").strip()
    if text == "personal":
        return text, "personal"
    if ORG_CONTEXT.fullmatch(text):
        return text, "org"
    raise Refused("--context is personal or org:<id>", "invalid")


def _slot(text: str) -> tuple[str, str, str]:
    vault = _vault()
    parts = (text or "").split("/")
    if len(parts) != 3 or not all(parts):
        raise Refused("--slot is PROJECT/ENV/NAME", "invalid")
    try:
        folder, said = vault.project_folder(parts[0], parts[1], parts[2])
        if said:
            print(f"project: {said}", file=sys.stderr)
        vault.validate_names(folder, parts[1], parts[2])
    except vault.VaultBoundaryError as exc:
        raise Refused(f"--slot: {exc}", "invalid") from None
    if not SLOT_NAME.fullmatch(parts[2]):
        raise Refused("the slot's NAME starts with a letter and has at most 64 characters", "invalid")
    return folder, parts[1], parts[2]


def _scope(pairs: list[str] | None) -> dict:
    scope: dict[str, str] = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not SCOPE_KEY.fullmatch(key):
            raise Refused("--scope is KEY=VALUE, the key a lower-case name (zone, account)", "invalid")
        if key in scope:
            raise Refused(f"--scope names {key} twice", "invalid")
        value = value.strip()
        if not value or len(value) > 253 or not LINE.fullmatch(value):
            raise Refused(f"--scope {key} is one line of 1 to 253 characters", "invalid")
        if credential_shape.find(value) or PATHISH.search(value):
            raise Refused(f"--scope {key} names what the credential reaches, never a path or a value", "invalid")
        scope[key] = value
    if len(scope) > 8:
        raise Refused("--scope names at most 8 things", "invalid")
    return scope


def _limit(usd, reset: str | None) -> dict | None:
    if usd is None:
        if reset is not None:
            raise Refused("--reset goes with --limit", "invalid")
        return None
    text = str(usd).strip()
    if not USD.fullmatch(text) or float(text) <= 0 or not math.isfinite(float(text)):
        raise Refused("--limit is a positive amount in USD, such as 5 or 12.50", "invalid")
    reset = reset or "monthly"
    if reset not in RESETS:
        raise Refused("--reset is daily, weekly, monthly or never", "invalid")
    return {"usd": text, "reset": reset}


def build_request(*, agent: str, provider: str, preset: str, slot: str, reason: str, context: str | None = None,
                  scope: list[str] | None = None, limit=None, reset: str | None = None,
                  expires_in_days: int | None = None, expires_at: str | None = None,
                  now: datetime.datetime | None = None) -> tuple[dict, str]:
    """(the credential-request/1 document, the slot `PROJECT/ENV/NAME`), refused before anything is written."""
    now = now or _now()
    if not isinstance(agent, str) or not AGENT_REF.fullmatch(agent):
        raise Refused("--agent is the agent instance asking, urn:fabric:agent:<publisher>:<id>", "invalid")
    context, kind = _context(context)
    if not isinstance(provider, str) or not PROVIDER.fullmatch(provider):
        raise Refused("--provider is a provider's name, such as openrouter or cloudflare", "invalid")
    if provider not in DOORS:
        raise Refused(f"no door here issues {provider} credentials on request (offered: "
                      f"{', '.join(sorted(DOORS))})", "provider-unavailable")
    if not isinstance(preset, str) or not PRESET.fullmatch(preset):
        raise Refused("--preset is a preset's name, `presets` lists them", "invalid")
    project, env, name = _slot(slot)
    if expires_in_days is not None and expires_at is not None:
        raise Refused("give --expires-in-days or --expires-at, not both", "invalid")
    ceiling = ORG_MAX_DAYS if kind == "org" else key_expiry.MAX_DAYS
    if expires_at is not None:
        end = _parse(expires_at)
        if end is None:
            raise Refused("--expires-at is a timestamp, YYYY-MM-DDTHH:MM:SSZ", "invalid")
    else:
        days = DEFAULT_DAYS[kind] if expires_in_days is None else expires_in_days
        if type(days) is not int or days < 2:
            raise Refused("--expires-in-days is a whole number of days, at least 2", "invalid")
        end = now + datetime.timedelta(days=days)
    if end < now + datetime.timedelta(days=1) + MINT_MARGIN:
        raise Refused("the doors mint in whole days that end no later than asked, so a request asks for more "
                      "than one day (two at the least with --expires-in-days)", "expiry-too-short")
    if end > now + datetime.timedelta(days=ceiling):
        raise Refused(f"a request in the {context} context asks for at most {ceiling} days"
                      + (" (FAC-SEM-138)" if kind == "org" else ""), "expiry-too-long")
    doc = {"type": REQUEST_TYPE, "requestId": str(uuid.uuid4()), "agentRef": agent, "context": context,
           "provider": provider, "preset": preset, "expiresAt": _stamp(end),
           "reason": _free_text("--reason", reason), "requestedAt": _stamp(now)}
    parsed_scope = _scope(scope)
    if parsed_scope:
        doc["scope"] = parsed_scope
    parsed_limit = _limit(limit, reset)
    if parsed_limit:
        doc["limit"] = parsed_limit
    DOORS[provider].check(doc)
    return doc, f"{project}/{env}/{name}"


def file_request(**kwargs) -> dict:
    """Write a request into the queue; answers its entry. One open request per slot and context."""
    doc, slot = build_request(**kwargs)
    with _locked():
        known, _degraded = requests()
        for rid, entry in known.items():
            if entry["state"] == "open" and entry.get("slot") == slot \
                    and entry["request"].get("context") == doc["context"]:
                raise Refused(f"request {rid[:8]} for {slot} in {doc['context']} is still open; withdraw it "
                              f"(`{COMMAND} withdraw {rid[:8]}`) or wait for its decision", "duplicate")
        _append({"event": "requested", "request": doc, "slot": slot})
    return {"requestId": doc["requestId"], "state": "open", "request": doc, "slot": slot}


# ─────────────────────────── decisions ──────────────────────────────────────

def _relayed(action: str, target: str, instruction: str | None, source: str | None) -> dict | None:
    """The operator's words when an agent acts on them (contract DEC-0121's decision shape)."""
    if instruction is None and source is None:
        return None
    if not instruction or not source:
        raise Refused("--relayed-instruction and --source go together: the operator's words and where they "
                      "were said", "invalid")
    words = instruction.strip()
    if len(words) > 500 or not LINE.fullmatch(words) or credential_shape.find(words):
        raise Refused("--relayed-instruction is the operator's words, one line of at most 500 characters, "
                      "never a value", "invalid")
    where = _free_text("--source", source)
    return {"by": "operator", "via": "relayed-instruction", "action": action, "target": target[:8],
            "instruction": words, "at": _stamp(_now()), "source": where}


def _open_entry(request_id: str) -> tuple[str, dict]:
    known, _degraded = requests()
    rid = resolve_id(request_id, known)
    entry = known[rid]
    if entry["state"] != "open":
        raise Refused(f"request {rid[:8]} is {entry['state']}, not open: it was decided already", "decided")
    return rid, entry


def decision_document(rid: str, outcome: str, *, reason: str | None = None, credential: dict | None = None,
                      by: str = "administrator", now: datetime.datetime | None = None) -> dict:
    doc = {"type": DECISION_TYPE, "requestId": rid, "outcome": outcome, "decidedAt": _stamp(now or _now())}
    if outcome != "pending":
        doc["by"] = by
    if outcome == "refused":
        doc["reason"] = reason
    if outcome == "issued":
        doc["credential"] = credential
    return doc


def refuse(request_id: str, reason: str, *, note: str | None = None, instruction: str | None = None,
           source: str | None = None) -> dict:
    if reason not in REFUSALS or reason == "withdrawn":
        raise Refused(f"--reason is one of {', '.join(r for r in REFUSALS if r != 'withdrawn')}", "invalid")
    with _locked():
        rid, _entry = _open_entry(request_id)
        relayed = _relayed("credential.refuse", rid, instruction, source)
        row = {"event": "decided", "requestId": rid, "decision": decision_document(rid, "refused", reason=reason)}
        if note:
            row["note"] = _free_text("--note", note)
        if relayed:
            row["relayed"] = relayed
        _append(row)
    return {"requestId": rid, "state": "refused", "decision": row["decision"]}


def pending(request_id: str, *, note: str | None = None) -> dict:
    with _locked():
        rid, _entry = _open_entry(request_id)
        row = {"event": "decided", "requestId": rid, "decision": decision_document(rid, "pending")}
        if note:
            row["note"] = _free_text("--note", note)
        _append(row)
    return {"requestId": rid, "state": "open", "decision": row["decision"]}


def withdraw(request_id: str) -> dict:
    """The consumer takes its open request back. Recorded as the contract's refusal `withdrawn`, by the queue's
    own rule (`by: policy`): nobody decided against it."""
    with _locked():
        rid, _entry = _open_entry(request_id)
        doc = decision_document(rid, "refused", reason="withdrawn", by="policy")
        _append({"event": "withdrawn", "requestId": rid, "decision": doc})
    return {"requestId": rid, "state": "withdrawn", "decision": doc}


def _enrolled(org_id: str) -> str | None:
    """None when this space holds an active enrollment in the organization, else why not."""
    saved = list(sys.path)
    try:
        import paths
        # `node` is the engine's package; `tools/node.py` (the node command) shadows it whenever a tools folder
        # comes first on the path, as importing the vault makes it.
        sys.path.insert(0, str(ROOT))
        from node.org import Enrollment, enrolled_orgs
        node = paths.setting_path("OBSERVATORY_NODE", paths.HOME / "node")
        if org_id not in enrolled_orgs(node):
            return f"this space is not enrolled in organization {org_id} (`project-observatory full org status`)"
        state = Enrollment(pathlib.Path(node).resolve(), org_id).state()
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        return f"this space's enrollment in {org_id} could not be read ({type(exc).__name__})"
    finally:
        sys.path[:] = saved
    if not isinstance(state, dict):
        return f"this space is not enrolled in organization {org_id}"
    if isinstance(state.get("removed"), dict):
        return f"this space was removed from organization {org_id}; nothing is delivered into its context"
    return None


def _fail(rid: str, stage: str, reason: str) -> None:
    _append({"event": "attempt-failed", "requestId": rid, "stage": stage,
             "reason": credential_shape.redact(reason)[:300]})


def issue(request_id: str, *, account: str | None = None, instruction: str | None = None,
          source: str | None = None, now: datetime.datetime | None = None) -> dict:
    """The administrator's `issued`: the door mints into the request's slot, the end and the slot are checked,
    the provenance is written beside the slot, then the decision. Raises Refused before any provider call,
    IssueFailed after one that delivered nothing (recorded; the request stays open)."""
    vault = _vault()
    with _locked():
        rid, entry = _open_entry(request_id)
        request = entry["request"]
        relayed = _relayed("credential.issue", rid, instruction, source)
        if account is not None and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", account)
                                    or credential_shape.find(account)):
            raise Refused("--account is a stash label or an account's slug, as the door's `list` shows it",
                          "invalid")
        door = DOORS.get(request["provider"])
        if door is None:
            raise Refused(f"no door here issues {request['provider']} credentials; refuse it with "
                          f"`{COMMAND} refuse {rid[:8]} --reason provider-unavailable`", "provider-unavailable")
        door.check(request)
        context = request["context"]
        if context.startswith("org:"):
            why = _enrolled(context[len("org:"):])
            if why:
                raise Refused(why, "wrong-context")
        project, env, name = entry["slot"].split("/")
        slot_path = vault._slot(project, env, name)
        if slot_path.is_file():
            raise Refused(f"{entry['slot']} already holds a value, and a name already present is never "
                          f"overwritten (ORG-R10): refuse this request, or file one for another NAME", "slot-filled")
        now = now or _now()
        end = _parse(request["expiresAt"])
        # WHOLE DAYS, ENDING NO LATER THAN ASKED (FAC-SEM-138). The door takes its own clock a moment later
        # than this one, so the days are counted to `MINT_MARGIN` before the end: a request issued within
        # that margin of a whole day after it was filed gets a day less, never a credential that outlives it.
        days = math.floor((end - now - MINT_MARGIN).total_seconds() / 86400) if end else 0
        if days < 1:
            raise Refused(f"the request's end ({request['expiresAt']}) leaves less than a whole day to mint; "
                          f"refuse it with `{COMMAND} refuse {rid[:8]} --reason unanswered`, and the consumer "
                          f"asks again for longer", "expiry-too-short")
        days = min(days, key_expiry.MAX_DAYS)
        try:
            minted = door.issue(request, (project, env, name), account, days)
        except IssueFailed as exc:
            _fail(rid, "issue", str(exc))
            raise
        provenance = {"requestId": rid, "issuedAt": _stamp(_now()), "decidedBy": "administrator",
                      "issuer": minted["issuer"],
                      "recipient": {"agentRef": request["agentRef"], "context": context, "slot": entry["slot"]},
                      "provider": request["provider"], "preset": request["preset"],
                      "scope": request.get("scope") or {}, "limit": request.get("limit"),
                      "revoke": minted["revoke"]}
        meta = vault._read_meta(slot_path) if slot_path.is_file() else {}
        recorded = _parse(meta.get("expires_at"))
        problem = None
        if not slot_path.is_file():
            problem = "the door answered, and the slot holds no value"
        elif recorded is None:
            problem = "the provider's answer did not confirm when the credential ends"
        elif recorded > end:
            problem = f"the provider set an end ({meta['expires_at']}) later than the request asked"
        if problem:
            undone = _undo(door, provenance, (project, env, name))
            _fail(rid, "issue", f"{problem}; {undone}")
            raise IssueFailed(f"{problem}; {undone}")
        provenance["expiresAt"] = meta["expires_at"]
        notes = []
        try:
            vault.record_provenance(project, env, name, provenance, tool=TOOL)
        except (vault.VaultBoundaryError, OSError, ValueError) as exc:
            notes.append(f"provenance not recorded beside the slot: {exc}")
        credential = {"slot": name, "provider": request["provider"], "preset": request["preset"],
                      "expiresAt": meta["expires_at"]}
        row = {"event": "decided", "requestId": rid,
               "decision": decision_document(rid, "issued", credential=credential),
               "delivery": {"slot": entry["slot"], "credential": credential, "provenance": provenance}}
        if relayed:
            row["relayed"] = relayed
        _append(row)
    return {"requestId": rid, "state": "issued", "decision": row["decision"], "slot": entry["slot"],
            "issuer": provenance["issuer"], "expiresAt": meta["expires_at"], "notes": notes}


def _undo(door: Door, provenance: dict, slot: tuple[str, str, str]) -> str:
    """A credential the request cannot keep: revoked at the provider, its value deleted. Says what happened."""
    vault = _vault()
    try:
        result = door.revoke(provenance)
        said = "revoked at the provider" + ("" if result.get("proved") else " (the revocation is not proved)")
    except RuntimeError as exc:
        said = f"NOT revoked at the provider ({exc}); revoke it with the door"
    try:
        vault.remove_value(*slot, tool=TOOL, why="the issued credential did not meet its request")
    except (vault.VaultBoundaryError, OSError, ValueError):
        said += "; the slot's value could not be deleted"
    return said


def revoke(request_id: str, *, instruction: str | None = None, source: str | None = None) -> dict:
    """End an issued request: revoke at the provider through its door and prove it, then delete the slot's
    value when it is still the one this request was issued (DEC-0113). A refused revocation is recorded and
    the request stays `issued`, listed until it is settled."""
    vault = _vault()
    with _locked():
        known, _degraded = requests()
        rid = resolve_id(request_id, known)
        entry = known[rid]
        if entry["state"] not in ("issued", "expired"):
            raise Refused(f"request {rid[:8]} is {entry['state']}; only an issued credential is revoked", "not-issued")
        relayed = _relayed("credential.revoke", rid, instruction, source)
        delivery = entry.get("delivery") or {}
        provenance = delivery.get("provenance") or {}
        door = DOORS.get(entry["request"]["provider"])
        if door is None:
            raise Refused(f"no door here revokes {entry['request']['provider']} credentials", "provider-unavailable")
        try:
            result = door.revoke(provenance)
        except RuntimeError as exc:
            _fail(rid, "revoke", str(exc))
            raise
        project, env, name = entry["slot"].split("/")
        held = vault.provenance_of(project, env, name) if vault._slot(project, env, name).is_file() else None
        if held is None and not vault._slot(project, env, name).is_file():
            slot_state = "absent"
        elif (held or {}).get("requestId") == rid:
            vault.remove_value(project, env, name, tool=TOOL, why=f"request {rid[:8]} revoked")
            slot_state = "removed"
        else:
            slot_state = "kept: it holds another value now"
        revocation = {**result, "slot": slot_state, "revokedAt": _stamp(_now())}
        row = {"event": "revoked", "requestId": rid, "revocation": revocation}
        if relayed:
            row["relayed"] = relayed
        _append(row)
    return {"requestId": rid, "state": "revoked", "revocation": revocation}


# ─────────────────────────── reading ────────────────────────────────────────

def request_document(entry: dict) -> dict | None:
    """The credential-request/1 document as it would leave this space, or None for a personal request, which
    never leaves it."""
    doc = entry.get("request") or {}
    return dict(doc) if str(doc.get("context") or "").startswith("org:") else None


def view(entry: dict) -> dict:
    request = entry["request"]
    last = entry["decisions"][-1]["decision"] if entry["decisions"] else None
    out = {"requestId": request["requestId"], "state": entry["state"], "agentRef": request["agentRef"],
           "context": request["context"], "provider": request["provider"], "preset": request["preset"],
           "slot": entry.get("slot"), "expiresAt": request["expiresAt"], "reason": request["reason"],
           "requestedAt": request["requestedAt"]}
    for key in ("scope", "limit"):
        if request.get(key):
            out[key] = request[key]
    if last:
        out["decision"] = last
    if entry.get("delivery"):
        out["credential"] = entry["delivery"].get("credential")
        out["issuer"] = (entry["delivery"].get("provenance") or {}).get("issuer")
    if entry.get("revocation"):
        out["revocation"] = entry["revocation"]
    last_attempt = entry["attempts"][-1] if entry["attempts"] else None
    # A failed issue matters while the request is open; once issued, only a refused revocation does.
    if last_attempt and (entry["state"] == "open" or last_attempt.get("stage") == "revoke"
                         and entry["state"] in ("issued", "expired")):
        out["lastFailure"] = last_attempt
    return out


def listing(state: str = "open") -> dict:
    known, degraded = requests()
    rows = [view(e) for e in known.values() if state == "all" or e["state"] == state]
    rows.sort(key=lambda r: r["requestedAt"])
    return {"schema": "credential-requests/1", "state": state, "requests": rows, "degraded": degraded}


def show(request_id: str) -> dict:
    known, degraded = requests()
    rid = resolve_id(request_id, known)
    entry = known[rid]
    out = view(entry)
    out["requestDocument"] = request_document(entry)
    out["decisions"] = entry["decisions"]
    out["attempts"] = entry["attempts"]
    out["degraded"] = degraded
    return out


# ─────────────────────────── the command line ───────────────────────────────

def _print(doc: dict, as_json: bool, text: str) -> None:
    print(json.dumps(doc, indent=1, ensure_ascii=False) if as_json else text)


def _line(r: dict) -> str:
    extra = []
    if r.get("scope"):
        extra.append(", ".join(f"{k}={v}" for k, v in r["scope"].items()))
    if r.get("limit"):
        extra.append(f"${r['limit']['usd']} {r['limit']['reset']}")
    tail = f" ({'; '.join(extra)})" if extra else ""
    lines = [f"  {r['requestId'][:8]}  {r['state']:9} {r['provider']}/{r['preset']}{tail} for {r['agentRef']} "
             f"in {r['context']} -> {r['slot']}, until {r['expiresAt'][:10]}",
             f"            why: {r['reason']}"]
    if r.get("issuer"):
        lines.append(f"            issued by {r['issuer'].get('door')}/{r['issuer'].get('account')}, "
                     f"ends {(r.get('credential') or {}).get('expiresAt', '?')[:10]}")
    if r.get("lastFailure"):
        lines.append(f"            last failure ({r['lastFailure'].get('stage')}): {r['lastFailure'].get('reason')}")
    return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
    doc = (__doc__ or "").strip("\n")
    ap = argparse.ArgumentParser(prog="credential_requests.py", description=doc.splitlines()[0],
                                 epilog=doc.split("\n\n", 2)[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("presets", help="what each door offers on request: presets, scopes, ceilings")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("request", help="file a request for a narrowed credential; no value travels with it")
    p.add_argument("--agent", required=True, help="the agent instance asking: urn:fabric:agent:<publisher>:<id>")
    p.add_argument("--context", default="personal", help="personal (the default) or org:<id>")
    p.add_argument("--provider", required=True, help=", ".join(sorted(DOORS)))
    p.add_argument("--preset", required=True, help="a preset the door offers (`presets`)")
    p.add_argument("--scope", action="append", metavar="KEY=VALUE",
                   help="what the credential reaches, by name: zone=example.com, account=SLUG (repeatable)")
    p.add_argument("--limit", metavar="USD", help="a spend ceiling in USD (an OpenRouter key needs one)")
    p.add_argument("--reset", choices=RESETS, help="when the ceiling resets (default monthly)")
    p.add_argument("--slot", required=True, metavar="PROJECT/ENV/NAME", help="the vault slot the consumer reads")
    p.add_argument("--reason", required=True, help="one line the administrator reads; no path, no value")
    end = p.add_mutually_exclusive_group()
    end.add_argument("--expires-in-days", type=int, metavar="N",
                     help="how long the credential lives: 2 to 30 days in an organization's context "
                          "(default 30), 2 to 365 in the personal one (default 90)")
    end.add_argument("--expires-at", metavar="YYYY-MM-DDTHH:MM:SSZ", help="when the credential should end")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("list", help="the requests in one state (default open), names only")
    p.add_argument("--state", default="open", choices=(*STATES, "all"))
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="one request, its decisions, attempts and the documents the contract defines")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    for verb, text in (("issue", "the administrator issues: the door mints into the slot, with provenance"),
                       ("refuse", "the administrator refuses, with the contract's reason"),
                       ("revoke", "revoke an issued credential at its provider, prove it, delete the slot's value")):
        p = sub.add_parser(verb, help=text)
        p.add_argument("id")
        if verb == "issue":
            p.add_argument("--account", help="the door's account to issue from (a stash label or account slug)")
        if verb == "refuse":
            p.add_argument("--reason", required=True, choices=[r for r in REFUSALS if r != "withdrawn"])
            p.add_argument("--note", help="one line beside the decision")
        p.add_argument("--relayed-instruction", metavar="WORDS",
                       help="an agent acting on the operator's word: the words, verbatim")
        p.add_argument("--source", metavar="WHERE", help="where the operator said it (a session, a thread)")
        p.add_argument("--json", action="store_true")
    p = sub.add_parser("pending", help="record that the request was seen and is not decided yet")
    p.add_argument("id")
    p.add_argument("--note", help="one line beside the decision")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("withdraw", help="the consumer takes an open request back")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    return ap


def run(a) -> int:
    if a.cmd == "presets":
        doc = presets()
        lines = []
        for provider, rows in doc.items():
            for key, row in rows.items():
                if key == "error":
                    lines.append(f"  {provider}: the door could not be read ({row})")
                    continue
                need = ", ".join(f"{s}=… (required)" for s in row["scopeRequired"]) or "no scope"
                lines.append(f"  {provider}/{key}: {row['what']}; {need}; ceiling {row['limit']}")
        _print(doc, a.json, "\n".join(lines))
        return 0
    if a.cmd == "request":
        r = file_request(agent=a.agent, provider=a.provider, preset=a.preset, slot=a.slot, reason=a.reason,
                         context=a.context, scope=a.scope, limit=a.limit, reset=a.reset,
                         expires_in_days=a.expires_in_days, expires_at=a.expires_at)
        _print(r, a.json, f"filed request {r['requestId']} for {r['slot']}: it waits for the administrator "
                          f"(`{COMMAND} issue {r['requestId'][:8]}` or `refuse`)")
        return 0
    if a.cmd == "list":
        doc = listing(a.state)
        text = "\n".join(_line(r) for r in doc["requests"]) or f"  no {a.state} request"
        for d in doc["degraded"]:
            text += f"\n  degraded: line {d['line']} of the queue: {d['reason']}"
        _print(doc, a.json, text)
        return 0
    if a.cmd == "show":
        doc = show(a.id)
        _print(doc, a.json, _line(doc) + "".join(
            f"\n            decision: {d.get('decision', {}).get('outcome')} by "
            f"{d.get('decision', {}).get('by', '-')} at {d.get('at', '?')}" for d in doc["decisions"]))
        return 0
    if a.cmd == "issue":
        r = issue(a.id, account=a.account, instruction=a.relayed_instruction, source=a.source)
        _print(r, a.json, f"issued request {r['requestId'][:8]} into {r['slot']} by {r['issuer']['door']}/"
                          f"{r['issuer']['account']}, ends {r['expiresAt']}; value hidden"
                          + "".join(f"\n  note: {n}" for n in r["notes"]))
        return 0
    if a.cmd == "refuse":
        r = refuse(a.id, a.reason, note=a.note, instruction=a.relayed_instruction, source=a.source)
        _print(r, a.json, f"refused request {r['requestId'][:8]}: {a.reason}")
        return 0
    if a.cmd == "pending":
        r = pending(a.id, note=a.note)
        _print(r, a.json, f"request {r['requestId'][:8]} recorded as pending; it stays open")
        return 0
    if a.cmd == "withdraw":
        r = withdraw(a.id)
        _print(r, a.json, f"withdrew request {r['requestId'][:8]}")
        return 0
    if a.cmd == "revoke":
        r = revoke(a.id, instruction=a.relayed_instruction, source=a.source)
        rev = r["revocation"]
        _print(r, a.json, f"revoked request {r['requestId'][:8]} at the provider "
                          f"({'proved' if rev.get('proved') else 'NOT proved: check at the provider'}); "
                          f"the slot: {rev['slot']}")
        return 0 if rev.get("proved") else 1
    return 2


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return run(a)
    except Refused as exc:
        if getattr(a, "json", False):
            print(json.dumps({"error": exc.code, "message": str(exc)}, ensure_ascii=False))
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except (IssueFailed, RuntimeError) as exc:
        if getattr(a, "json", False):
            print(json.dumps({"error": "door-failed", "message": str(exc)}, ensure_ascii=False))
        print(f"failed: {exc}\n  recorded; the request keeps its state (`{COMMAND} show ID`)", file=sys.stderr)
        return 1
    except ValueError as exc:
        # The vault's own boundary (a symbolic link on the way, a file that is not owner-only): its message
        # names the rule, never a value.
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"failed: the queue could not be read or written ({type(exc).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
