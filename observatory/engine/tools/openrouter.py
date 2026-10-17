#!/usr/bin/env python3
"""The one door to OpenRouter keys: stash, issue, rotate, revoke, limits, ping.

    T="$(project-observatory full-path)/tools"            # the installed engine's tools
    python "$T/openrouter.py" stash --as main < key-file   # provisioning key, labeled; a protected file
    python "$T/openrouter.py" adopt --as main               # take the legacy file in
    python "$T/openrouter.py" issue --name my-agent --limit 10 [--reset daily] --to vault:PROJECT/prod/OPENROUTER_API_KEY
    python "$T/openrouter.py" issue-for-policy --policy FILE [--limit USD | --split REF=USD,…] [--dry-run]
    python "$T/openrouter.py" list | ping
    python "$T/openrouter.py" limit my-agent --set 25 [--reset daily]
    python "$T/openrouter.py" disable my-agent              # or enable
    python "$T/openrouter.py" rotate my-agent               # or --leaked
    python "$T/openrouter.py" revoke my-agent
    python "$T/openrouter.py" claim old-tool --where PATH < key-file   # a key minted elsewhere, by hash

PROJECT in a `vault:` destination is the project's folder name; its registry id
is accepted and normalised to that folder before anything is minted.

The same shape as `tools/cloudflare.py`, deliberately: one admin credential per
account, stashed and never handed out; every working key ISSUED from it, narrow,
delivered to a named destination; a meta record beside everything that lets
`list` and `ping` answer "what exists, who spends it, what is its ceiling"
without opening a value.

Two provider differences the shape has to absorb:

* OpenRouter names no account. A Cloudflare admin token can be asked whose it
  is; a provisioning key cannot, so `stash` REQUIRES `--as <label>`. The
  operator labels the stash at the door, which is the only moment anyone knows
  which dashboard it came from. Unlabeled provisioning keys are keys nobody can
  rotate confidently afterwards.

* Rotation is create-then-delete, not rolling. The provisioning API cannot
  reissue a value for an existing key, so `rotate` creates the successor first,
  DELIVERS it, and only then deletes the predecessor. The order matters: a
  delete-first rotation that fails halfway leaves the consumer with a dead key
  and nothing else, and a consumer that falls back to another provider silently
  would hide that failure.

Limits are part of issue, not an afterthought: every issued key carries a USD
ceiling (`--limit`, default 10), `limit <name> --set` moves it, and `ping`
reports usage against it. A key without a ceiling is an unbounded liability
attached to a file on disk.
"""
from __future__ import annotations
import argparse
import contextlib
import datetime
import decimal
import hashlib
import json
import math
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request


#: What closes the leak register once the leaked values are dead. It named `vault.py
#: rotate`, which writes a new value and leaves the register open (2026-10-06); the
#: command is `settle`, with this engine's own interpreter, as every handed command is.
SETTLE_HINT = ("the leaked values are dead; close each row the register names with "
               f"`{__import__('shlex').quote(sys.executable)} \"$(project-observatory full-path)/tools/vault.py\" "
               "settle PROJECT ENV NAME --how \"rotated at the provider\" "
               "--revocation-evidence \"…\" --consumer-evidence \"…\"`")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import oslocks
import osprivacy
import private_io
import paths                                                                    

API = "https://openrouter.ai/api/v1"
ADMIN_STORE = paths.source_path("secret_store", paths.SECRETS) / 'openrouter-admin'
LEGACY = paths.source_path("secret_store", paths.SECRETS) / 'openrouter-provisioning'
#: name -> where it was delivered and from which account it was minted. Names
#: and places only, never values: this ledger is what rotation reads to deliver
#: a successor to the same place, and what `list` prints.
LEDGER = paths.source_path("secret_store", paths.SECRETS) / 'openrouter-issued.json'
#: Where an issued key can be delivered. `vault:` slots go through the project
#: vault so they inherit its metadata and leak register; the two named
#: destinations reuse `tools/install_key.py`'s writers so claude-mem's dotenv
#: handling stays in one place.
NAMED_DESTINATIONS = ("observatory", "claude-mem")


def _journal(event: str, secret: str, *, require_success: bool = False, **detail) -> None:
    """The vault's movements journal: every issue, rotation and
    revocation this door performs is on the record beside the vault's own.
    Past external changes keep best-effort journaling. A pre-action attestation can
    require success before local state is cleared."""
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import vault
        vault.journal(event, secret, tool="openrouter.py", **detail)
    except Exception as exc:                                                      
        if require_success:
            raise RuntimeError("attestation could not be journaled; pending row retained") from None
        print(f"  (movement not journaled: {type(exc).__name__})", file=sys.stderr)


def today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


# ─────────────────────────── the wire ───────────────────────────────────────

class Unreachable(RuntimeError):
    """OpenRouter could not be reached at all: no answer, so nothing is known
    about the key. A RuntimeError, so every caller that treats "the provider
    failed" alike keeps doing so; `stash` tells it apart from a refusal."""


def _request(path: str, key: str, payload: dict | None = None,
             method: str | None = None) -> dict:
    """Endpoint and status in errors — never headers, never the body we sent."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{API}{path}", data=data, method=method or ("POST" if data else "GET"),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        e.close()
        raise RuntimeError(f"openrouter answered HTTP {e.code}; provider response withheld") from None
    except OSError as e:
        raise Unreachable(f"openrouter unreachable: {type(e).__name__}") from None


# ─────────────────────────── the admin stash ────────────────────────────────

def admins() -> list[tuple[str, pathlib.Path]]:
    if any(p.is_symlink() for p in (ADMIN_STORE, *ADMIN_STORE.parents)):
        raise RuntimeError("Admin store must not contain symbolic links")
    if not ADMIN_STORE.is_dir():
        return []
    return [(p.name, p) for p in sorted(ADMIN_STORE.iterdir())
            if p.is_file() and not p.name.startswith(".")
            and not p.name.endswith(".meta.json")]


def read_admin(label: str | None) -> tuple[str, str]:
    have = admins()
    if not have:
        raise RuntimeError("no provisioning key stashed — "
                           "run openrouter stash --as <label> with a protected file redirected to stdin")
    if label:
        for name, p in have:
            if name == label:
                return name, private_io.read(p).strip()
        raise RuntimeError(f"no stashed account called {label!r} "
                           f"(have: {', '.join(n for n, _ in have)})")
    if len(have) > 1:
        raise RuntimeError(f"{len(have)} accounts stashed — name one with "
                           f"--account: {', '.join(n for n, _ in have)}")
    name, p = have[0]
    return name, private_io.read(p).strip()


def write_secret(path: pathlib.Path, value: str) -> None:
    private_io.check(meta(path))
    private_io.write(path, value + "\n")


def meta(path: pathlib.Path) -> pathlib.Path:
    return path.with_name(path.name + ".meta.json")


def write_meta(path: pathlib.Path, text: str, encoding: str = "utf-8") -> None:
    private_io.write(meta(path), text)


def stash_value(value: str, label: str, origin: str) -> int:
    if not value:
        print("nothing on stdin", file=sys.stderr)
        return 2
    if len(value.split()) > 1:
        print(f"refused: {len(value.split())} whitespace-separated pieces arrived, "
              f"not one key", file=sys.stderr)
        return 1
    if not label:
        print("refused: --as <label> is required — OpenRouter names no account, "
              "so the stash is signed at the door or never "
              "(e.g. --as personal, --as team, --as staging)", file=sys.stderr)
        return 2
    label = slug(label)
    if not label:
        raise ValueError("A plain account label is required")
    private_io.check(ADMIN_STORE / label)
    private_io.check(meta(ADMIN_STORE / label))
    # CAN IT PROVISION? A provisioning key answers /keys; an inference key does
    # not, and stashing an inference key here would make `issue` fail on the day
    # it is needed.
    try:
        _request("/keys?include_disabled=false", value)
    except Unreachable as exc:
        print(f"refused: OpenRouter could not be reached ({exc}), so whether this key can "
              f"manage keys is unknown; nothing was stashed. Run the same command again "
              f"when the network is back", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"refused: this key cannot manage keys — {exc}\n  a PROVISIONING "
              f"key is minted at openrouter.ai/settings/provisioning-keys; an "
              f"inference key belongs in `python \"$(project-observatory full-path)/tools/install_key.py\"` instead",
              file=sys.stderr)
        return 1
    dest = ADMIN_STORE / label
    existed = dest.is_file()
    write_secret(dest, value)
    write_meta(dest, json.dumps(
        {"kind": "provisioning", "label": label, "stashed_on": today(),
         "origin": origin,
         "why": "issues working keys; never handed to a consumer"},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{'replaced' if existed else 'stashed'}: provisioning key as {label!r}")
    print(f"issue a working key with:\n"
          f"  python \"$(project-observatory full-path)/tools/openrouter.py\" issue --name <consumer> --limit 10 "
          f"--account {label} --to <destination>")
    return 0


def cmd_adopt(label: str) -> int:
    """The legacy single provisioning file, moved under a label."""
    if not LEGACY.is_file():
        print(f"nothing to adopt: {LEGACY} does not exist", file=sys.stderr)
        return 1
    rc = stash_value(private_io.read(private_io.legacy_path(LEGACY)).strip(), label,
                     origin=f"adopted from {LEGACY.name}")
    if rc == 0:
        # FIVE OTHER READERS know the legacy path (install_key, revoke_key,
        # keyserver, scan_leaks, scan_openrouter — measured by grep, 2026-09-13);
        # deleting it would break all five in the same minute. A relative
        # symlink keeps one canonical value and every reader: they see whichever
        # account was adopted first, exactly what they saw when there was one.
        LEGACY.unlink()
        LEGACY.symlink_to(pathlib.Path("openrouter-admin") / slug(label))
        print("the legacy path is now a symlink into the stash — its five "
              "readers keep working against one canonical value")
    return rc


# ─────────────────────────── the ledger ──────────────────────────────────────

def ledger() -> dict:
    private_io.check(LEDGER)
    if not LEDGER.exists():
        return {"issued": {}}
    try:
        doc = json.loads(private_io.read(LEDGER))
    except (OSError, ValueError):
        raise RuntimeError("Issued-key ledger is unreadable; refusing to replace it") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("issued"), dict):
        raise RuntimeError("Issued-key ledger has an unsupported shape")
    if "retired" in doc and not isinstance(doc["retired"], list):
        # Refused before any door acts, so no write ever replaces tombstones it could not read (J-11).
        raise RuntimeError("Issued-key ledger's retired list has an unsupported shape; refusing to replace it")
    return doc


def save_ledger(doc: dict) -> None:
    write_secret(LEDGER, json.dumps(doc, ensure_ascii=False, indent=2))


#: How deep this thread is inside the ledger lock: `issue-for-policy` holds it while it calls `issue_key`.
_LOCK_STATE = threading.local()

#: Every verb that reads the ledger and writes it back, each holding `_ledger_lock` for the whole operation.
LOCKED_VERBS = ("issue", "issue-for-policy", "rotate", "revoke", "revoke --forget-pending", "claim", "limit",
                "enable", "disable")


@contextlib.contextmanager
def _ledger_lock():
    """One door operation on the ledger at a time, across processes and threads: an exclusive lock on a file
    beside the ledger, held from the read of the ledger through its last write and the provider calls between.

    WHY. Every verb reads the ledger, calls the provider and writes the ledger back whole. Two at once each wrote
    the copy they had read, so the second write dropped the first one's change: two rotations at once left one
    key's ledger row naming a deleted hash (J-11 round 2). Two `issue-for-policy` runs each read the other's keys
    before either moved them, and opposite splits ended both keys high (round 3: $16 against a $10 limit).

    Re-entrant within a thread. A second operation waits up to POLICY_LOCK_WAIT_S, then refuses: long enough for
    a rotation whose delivery takes its full minute, so a mint from the key page waits rather than fails."""
    depth = getattr(_LOCK_STATE, "depth", 0)
    if depth:
        _LOCK_STATE.depth = depth + 1
        try:
            yield
        finally:
            _LOCK_STATE.depth = depth
        return
    LEDGER.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = osprivacy.open(LEDGER.parent / ".openrouter-policy.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        give_up = time.monotonic() + POLICY_LOCK_WAIT_S
        while True:
            try:
                oslocks.flock(fd, oslocks.LOCK_EX | oslocks.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= give_up:
                    raise RuntimeError(f"another door operation ({', '.join(LOCKED_VERBS)}) has held the ledger's "
                                       f"policy lock for {POLICY_LOCK_WAIT_S:.1f} s; nothing was read or moved — "
                                       f"run again when it is done") from None
                time.sleep(0.05)
        _LOCK_STATE.depth = 1
        try:
            yield
        finally:
            _LOCK_STATE.depth = 0
            oslocks.flock(fd, oslocks.LOCK_UN)
    finally:
        os.close(fd)


_policy_lock = _ledger_lock


def _locked(fn):
    """Run a door verb under `_ledger_lock`."""
    import functools

    @functools.wraps(fn)
    def inner(*a, **k):
        with _ledger_lock():
            return fn(*a, **k)
    return inner


#: How long a retired key's tombstone stays in the ledger. The provider's activity report covers the last 30
#: completed UTC days, so a key retired longer ago than that can hold no spend in either window the spend scan
#: counts (`collectors/scan_spend.py`); the margin keeps a late scan whole.
RETIRED_KEEP_DAYS = 62
#: How close the snapshot's days must come to the provider's own counters for the snapshot to be whole.
RECONCILE_USD = 0.0001
#: How many of the latest completed days the provider's activity report may still be missing. Each of them must
#: fall inside the month or the week the counters check, or be reached by the report, for a snapshot to be whole.
REPORT_LAG_DAYS = 3


def untracked_through(rec: dict) -> str | None:
    """The latest day an untracked rotation of this key can have happened on, or None when every rotation left
    a tombstone. Rotations made before tombstones existed left none; they happened on or before the last one
    made before the first tracked rotation (`tombstones_since`), or on `rotated_on` when none was tracked. The
    spend scan nulls every window starting on or before that day, for the live row and, after a revoke, for its
    tombstone (`_retire` carries it). "unknown" when the day was not recorded: every window.

    A rotation still pending (`predecessor_hash` on the row) is not untracked: its predecessor is read by its
    hash while the row is live, and carried as `predecessor_hash` into a tombstone. So it is subtracted here,
    the one place both the door and the spend scan take this rule from."""
    if rec.get("predecessor_hash") and type(rec.get("rotations")) is int and rec["rotations"] >= 1:
        rec = {**rec, "rotations": rec["rotations"] - 1}
    rotations = rec.get("rotations")
    tracked = rec.get("tombstoned_rotations")
    tracked = tracked if type(tracked) is int and tracked >= 0 else 0
    rotated_on = str(rec.get("rotated_on") or "")[:10]
    if type(rotations) is not int or rotations < 0:
        rotations = 1 if rotated_on else 0
    if rotations <= tracked:
        return None
    if tracked == 0:
        return rotated_on or "unknown"
    return str(rec.get("tombstones_since") or "unknown")[:10]


def _retire(doc: dict, name: str, rec: dict, key_hash: str | None, reason: str,
            snapshot: dict | None = None, deleted: bool | None = True, row_state: bool = True) -> None:
    """Leave a tombstone for a key that left the ledger's `issued` map (revoked, forgotten, overwritten) or was
    replaced by its successor (rotated): its name, account, provider hash, project, the UTC day it was retired,
    whether it is known to be gone at the provider (`deleted`), and what it had spent (`snapshot`, from
    `usage_snapshot`, read just before the DELETE).

    WHY (J-11). The spend scan attributes a project's spend by reading each key's activity by its hash. A key
    deleted here took its hash with it, so whatever it spent earlier in the month moved to `unattributed` while
    its project read as whole and lower. And the provider does not keep a deleted key's history: on 2026-10-15
    `GET /activity?api_key_hash=` for a hash that never existed, and `GET /keys/{hash}` for a key revoked
    earlier, both answered 404. So the tombstone carries the key's spend by day, read while the key still
    existed; with no snapshot the scan reads the windows it covers as unknown, never as zero.

    What made the row's own figure unknown goes with it: a rotation made before tombstones existed
    (`untracked_through`) and a rotation still pending (`predecessor_hash`, its spend unread). Otherwise a revoke
    would turn the scan's honest null for that project into a whole, lower figure (round 4, P9/P10).

    A rotation's own tombstone passes `row_state=False`: the row stays, and carries that state itself.

    Tombstones whose `retired_on` is older than RETIRED_KEEP_DAYS are pruned on each write. An entry that is not
    an object, or whose `retired_on` does not parse, is kept as it is (the scan names it); a `retired` that is
    not a list is refused by `ledger()` before any door acts."""
    retired = doc.setdefault("retired", [])
    if not isinstance(retired, list):
        raise RuntimeError("Issued-key ledger's retired list has an unsupported shape; refusing to replace it")
    now = datetime.datetime.now(datetime.timezone.utc).date()
    cutoff = now - datetime.timedelta(days=RETIRED_KEEP_DAYS)

    def keep(r) -> bool:
        if not isinstance(r, dict) or r.get("deleted", True) is not True:
            # A key not known to be deleted may still spend; the scan follows it while it is kept (round 6, R6-3).
            return True
        try:
            return datetime.date.fromisoformat(str(r.get("retired_on") or "")[:10]) >= cutoff
        except ValueError:
            return True
    retired[:] = [r for r in retired if keep(r)]
    stone = {"name": name, "account": rec.get("account"), "hash": key_hash, "project": rec.get("project"),
             "retired_on": now.isoformat(), "reason": reason, "deleted": deleted}
    stone.update(snapshot or {"usage": "unread", "usage_error": "no snapshot (no hash to read by)"})
    horizon = untracked_through(rec) if row_state else None
    if horizon:
        stone["untracked_through"] = horizon
    if row_state and rec.get("predecessor_hash"):
        stone["predecessor_hash"] = rec["predecessor_hash"]
        stone["predecessor_usage"] = "unread"
    retired.append(stone)


def _counter(row: dict, field: str) -> float | None:
    value = (row or {}).get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def usage_snapshot(admin: str, key_hash: str | None) -> dict:
    """What a key has spent, by UTC day, read just before it is deleted, and whether that reading is whole.

    Two reads, in this order: the activity report (`read_activity`: the last 30 completed UTC days), then the
    key itself (`GET /keys/{hash}`), the last request before the DELETE. Today and the read time come from that
    last read (`read_at`), never from an earlier read or a clock taken before a delivery. The key's own counters
    are kept: `usage_daily`, `usage_weekly`, `usage_monthly` (the provider's current UTC day, week from Monday,
    and calendar month).

    RECONCILED. The snapshot is whole only when the activity days of the current month plus `usage_daily` equal
    `usage_monthly`, and the days of the current week plus `usage_daily` equal `usage_weekly`, each within
    RECONCILE_USD, and each of the last REPORT_LAG_DAYS completed days either falls in the month or the week
    checked or was reached by the report (`activity_through`): a day the report may still be missing and no
    counter covers cannot be vouched for (a two-day lag on the Tuesday a month begins; Monday the 1st, unless
    the report reaches yesterday). Then today's spend is `usage_daily` and every day through today is known. Otherwise (`reconciled: false`: the activity report has not caught up, the
    reads straddled midnight, the key spent while the successor was delivered, or a counter is missing) only the
    activity days are kept, and the spend scan reads the windows holding any day after the last activity day,
    through the retirement day, as unknown. A mismatch is never booked as unattributed while a project reads
    whole.

    Never raises: a failed read gives `usage: "unread"` with the error's type, and the delete still proceeds; the
    scan then reads the windows the key may have spent in as unknown. No value is stored: only day totals, the
    three counters and times."""
    def unread(why: str) -> dict:
        return {"usage": "unread", "usage_error": why,
                "read_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    if not key_hash:
        return unread("no provider hash")
    try:
        rows = read_activity(admin, key_hash)
        by_day: dict[str, float] = {}
        for r in rows:
            if not isinstance(r, dict):
                raise ValueError("an activity row that is not an object")
            usd = float(r.get("usage") or 0)
            if not math.isfinite(usd):
                raise ValueError("an activity usage that is not finite")
            day = str(r.get("date") or "")[:10]
            datetime.date.fromisoformat(day)
            by_day[day] = by_day.get(day, 0.0) + usd
        row = _request(f"/keys/{key_hash}", admin).get("data")
        read_at = datetime.datetime.now(datetime.timezone.utc)
        if not isinstance(row, dict):
            raise ValueError("the key's row is not an object")
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError) as exc:
        return unread(type(exc).__name__)
    today = read_at.date()
    daily, weekly, monthly = (_counter(row, f) for f in ("usage_daily", "usage_weekly", "usage_monthly"))
    monday = today - datetime.timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    def total(first: datetime.date) -> float:
        return sum(u for d, u in by_day.items() if first.isoformat() <= d < today.isoformat())
    completed = [d for d in by_day if d < today.isoformat()]
    reached = max(completed) if completed else ""
    checked_from = min(month_start, monday).isoformat()
    recent = [(today - datetime.timedelta(days=n)).isoformat() for n in range(1, REPORT_LAG_DAYS + 1)]
    covered = all(d >= checked_from or d <= reached for d in recent)
    reconciled = (daily is not None and weekly is not None and monthly is not None
                  and abs(total(month_start) + daily - monthly) <= RECONCILE_USD
                  and abs(total(monday) + daily - weekly) <= RECONCILE_USD
                  and covered)
    snap = {"usage": "read", "read_at": read_at.isoformat(), "reconciled": reconciled,
            "usage_daily": daily, "usage_weekly": weekly, "usage_monthly": monthly,
            "activity_through": reached or None}
    if reconciled:
        by_day[today.isoformat()] = daily
    snap["usage_by_day"] = {d: round(u, 9) for d, u in sorted(by_day.items()) if d <= today.isoformat()}
    return snap


# ─────────────────────────── delivery ────────────────────────────────────────

def deliver(value: str, to: str, *, rotate: bool = False) -> str:
    """Write the key where it will be spent. Returns a human-readable place.

    The two named destinations reuse `tools/install_key.py`'s writers (the
    claude-mem dotenv merge lives there and nowhere else); a `vault:`
    destination goes through the project vault, which gives the key a slot,
    metadata and a place in the leak register.
    """
    if to in NAMED_DESTINATIONS:
        sys.path.insert(0, str(ROOT / "tools"))
        import install_key
        dest, consumer = install_key.DESTINATIONS[to]
        install_key.write(dest, value, to)
        return str(dest)
    if to.startswith("vault:"):
        parts = to[len("vault:"):].split("/")
        if len(parts) != 3:
            raise RuntimeError(f"a vault destination is vault:<project>/<env>/<NAME>, "
                               f"got {to!r}")
        import subprocess
        p = subprocess.run(
            [sys.executable, str(ROOT / "tools/vault.py"), "rotate" if rotate else "put",
             parts[0], parts[1], parts[2]],
            input=value, capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            raise RuntimeError("Vault refused credential delivery; child output withheld")
        return f"vault {parts[0]}/{parts[1]}/{parts[2]}"
    raise RuntimeError(f"unknown destination {to!r} — one of "
                       f"{', '.join(NAMED_DESTINATIONS)} or vault:<project>/<env>/<NAME>")


# ─────────────────────────── issue / limits / rotate ─────────────────────────

def vault_destination(to: str) -> str:
    """A `vault:` destination with its project normalised to the vault FOLDER,
    checked BEFORE a key is minted: a key minted for a slot the vault then
    refuses is a live credential with nowhere to go. A registry id names the
    same project as its folder; a name two projects claim is refused here.
    Any other destination is returned unchanged."""
    if not str(to or "").startswith("vault:"):
        return to
    parts = to[len("vault:"):].split("/")
    if len(parts) != 3 or not all(parts):
        raise ValueError("a vault destination is vault:<project>/<env>/<NAME>")
    sys.path.insert(0, str(ROOT / "tools"))
    import vault
    try:
        folder, said = vault.project_folder(parts[0], parts[1], parts[2])
        vault.validate_names(folder, parts[1], parts[2])
    except vault.VaultBoundaryError as exc:
        raise ValueError(str(exc)) from None
    if said:
        print(f"project: {said}", file=sys.stderr)
    return f"vault:{folder}/{parts[1]}/{parts[2]}"


#: The provider pages `/keys` by this many rows (measured 2026-10-04: 100, 100, 100, … and a
#: short last page). A listing longer than PAGES_MAX pages is refused, never truncated.
PAGE = 100
PAGES_MAX = 200


class ListingTooLong(RuntimeError):
    """The provider listed more than PAGES_MAX pages. A RuntimeError, so every reader that
    refuses a partial answer keeps refusing; `rows` holds the pages that were read, for the
    one caller that can act on them and say what it did not read (`_name_check`)."""

    def __init__(self, message: str, rows: list[dict]):
        super().__init__(message)
        self.rows = rows


def _all_keys(admin: str, include_disabled: bool = True) -> list[dict]:
    """Every key the provisioning key governs, every page of them.

    Reading one page called keys past the first 100 "NOT at the provider" and refused
    `limit` on a live key (2026-10-04); the collector (`scan_openrouter.listing`) already
    paged, the door did not.
    """
    flag = "true" if include_disabled else "false"
    rows: list[dict] = []
    offset = 0
    for _ in range(PAGES_MAX):
        page = _request(f"/keys?include_disabled={flag}&offset={offset}", admin).get("data", [])
        rows.extend(page)
        if len(page) < PAGE:
            return rows
        offset += len(page)
    raise ListingTooLong(f"the provider listed more than {PAGES_MAX} pages of keys; refusing a partial answer — "
                         "re-issue the key through this door, or record its hash in the ledger, so it is read by hash",
                         rows)


def find_key(admin: str, name: str) -> dict | None:
    """The provider's row for a key this door issued.

    By the hash the ledger recorded at issue (`GET /keys/{hash}`): one request however many
    keys the account holds (more than 20 000 on 2026-10-04, past any sane listing). Only a
    key the ledger has no hash for is searched page by page.
    """
    rec = ledger().get("issued", {}).get(name) or {}
    if rec.get("hash"):
        try:
            row = _request(f"/keys/{rec['hash']}", admin).get("data") or None
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        # The hash is the key's identity — the SHA-256 of its value, issued or claimed alike. A
        # rename in the provider's dashboard must not read as a deletion: `revoke` would drop the
        # row and leave the key live (review 2026-10-12), and `issue-for-policy` would count a live
        # key as gone and raise its neighbours past the limit (round 3). Only a 404 is gone.
        return row
    for row in _all_keys(admin):
        if row.get("name") == name:
            return row
    return None


def _name_check(admin: str, name: str) -> str | None:
    """Refuse a name a key already carries; None when everything was read, else what was not.

    A name the ledger recorded is read by its hash, completely. Any other name is searched in
    the provider's listing — and an account governing more keys than PAGES_MAX pages (another
    system mints a key per user under the same provisioning key: more than 20 000 on
    2026-10-05) could then issue NOTHING, every issue refused for a listing it would never
    finish. The pages that were read are still searched and a match there is still refused;
    past them the issue goes ahead with the unread remainder named. That is safe for this
    door: once issued, every operation reads the key by the hash recorded at issue, so a
    same-named key past the listing is never mistaken for it.
    """
    if (ledger().get("issued", {}).get(name) or {}).get("hash"):
        rows, caveat = [r for r in [find_key(admin, name)] if r], None
    else:
        try:
            rows, caveat = _all_keys(admin), None
        except ListingTooLong as exc:
            rows = exc.rows
            caveat = (f"the provider lists more than {len(rows)} keys; the name was checked against "
                      f"the first {len(rows)} and this door's ledger, not the rest")
    if any(r.get("name") == name for r in rows):
        raise ValueError(f"a key called {name!r} already exists — "
                         f"`rotate {name}` replaces its value, `limit {name} --set` "
                         f"moves its ceiling; a second key with the same name is how "
                         f"spend becomes unattributable")
    return caveat


#: The periods a key's ceiling can reset on (the provider's `limit_reset`). A daily reset is
#: what a per-day spending ceiling needs (Fabric Switchboard SB-72: paid fallback agents run
#: under a daily ceiling).
RESETS = ("daily", "weekly", "monthly")


def _reset(value: str | None) -> str:
    if value not in RESETS:
        raise ValueError(f"the ceiling resets daily, weekly or monthly, not {value!r}")
    return value


@_locked
def issue_key(name: str, limit: float, account: str | None, to: str,
              project: str | None, reset: str = "monthly") -> dict:
    """Mint, deliver, record. The ONE issuer: `tools/keyserver.py`'s HTTP
    `mint` calls this, so a key born from a button and a key born from the
    command line are the same key, in the same ledger, with the same ceiling.
    Raises ValueError for a caller's mistake and RuntimeError for the provider's.
    """
    name = slug(name)
    if not name:
        raise ValueError("the consumer's name cannot be empty")
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError("a key without a ceiling is an unbounded liability; give one")
    reset = _reset(reset)
    to = vault_destination(to)
    doc = ledger()
    if (doc["issued"].get(name) or {}).get("pending"):
        raise ValueError(f"an issue of {name!r} is recorded as pending: {PENDING_REMEDY.format(name=name)}")
    label, admin = read_admin(account)
    try:
        name_check = _name_check(admin, name)
    except ValueError as exc:
        raise ValueError(str(exc).replace(" already exists", f" already exists in {label!r}", 1)) from None
    # A PENDING ROW before the mint (round 4): a run killed between the mint and its record, or a
    # cleanup that failed, used to leave a live key no ledger knew — and on an account listing past
    # PAGES_MAX the name check could not see it, so the next run minted a second. The row names the
    # key, its slot and its ceiling, and gets the hash the moment the provider answers.
    old_row = doc["issued"].get(name)
    if isinstance(old_row, dict) and (old_row.get("successor_pending") or old_row.get("stale_successors")):
        # Writing the pending row would drop a successor a rotation minted and may have delivered: a live key,
        # maybe the one the slot holds, would leave the ledger (round 7). As for revoke: finish it first.
        raise ValueError(f"{name}: a rotation left a successor whose delivery is unknown; run `rotate {name}` to "
                         f"finish it first, then issue")
    if isinstance(old_row, dict) and old_row.get("hash"):
        # The provider has no key by this name (the check above), so the row's key is gone or renamed; its hash
        # would be lost with the row. Asked by hash: gone (404) is a tombstone `deleted: True` with the snapshot
        # a failed revoke kept (`retiring_snapshot`), if any (round 5, R5-7); still live under another name is
        # refused, so no key that may still spend ever leaves the ledger untracked (R5-1).
        try:
            alive = bool(_request(f"/keys/{old_row['hash']}", admin).get("data"))
        except RuntimeError as exc:
            if "HTTP 404" not in str(exc):
                raise
            alive = False
        kept = old_row.get("retiring_snapshot") if isinstance(old_row.get("retiring_snapshot"), dict) else None
        if alive:
            # A live key under another name would be dropped from tracking while it keeps spending: refused.
            raise ValueError(f"the ledger's key {name!r} is still live at the provider under another name; "
                             f"`revoke {name}` it first (its spend is snapshotted and kept), then issue again")
        _retire(doc, name, old_row, old_row["hash"], "replaced-by-issue", kept, deleted=True)
    doc["issued"][name] = {"account": label, "hash": None, "destination": to, "project": project,
                           "limit_usd": limit, "limit_reset": reset, "issued_on": today(), "pending": True}
    save_ledger(doc)
    # A RESET, never a lifetime cap (trap T17): a lifetime cap works until the
    # total is reached and then stops — months later, with no warning. Found in
    # the audit that merged keyserver's mint into this door: it set the reset,
    # the door did not. Monthly unless the caller asks for daily or weekly.
    try:
        d = _request("/keys", admin, {"name": name, "limit": limit, "limit_reset": reset})
    except RuntimeError as exc:
        # Only a 4xx proves nothing was minted (the provider refused the request). A 5xx, a timeout
        # or an unreachable provider does not: the key may exist, and on an account listing past
        # PAGES_MAX nothing could find it again — the pending row stays and holds its slot (fix 5).
        code = re.search(r"HTTP (\d{3})", str(exc))
        if code and 400 <= int(code.group(1)) < 500:
            _forget(name)
        raise
    value = d.get("key") or (d.get("data") or {}).get("key")
    row = d.get("data") or d
    doc = ledger()
    doc["issued"][name].update(hash=row.get("hash"), label=row.get("label"))
    save_ledger(doc)
    try:
        if not value:
            raise RuntimeError("the provider answered without the key's value")
        place = deliver(value, to)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        # SubprocessError: the vault's `put` timed out. A minted key that reached no slot is never
        # left live and untracked: deleted, or kept in the ledger as an orphan when the delete fails.
        _undo_mint(admin, name, row)
    doc = ledger()
    doc["issued"][name] = {
        "account": label, "hash": row.get("hash"), "label": row.get("label"), "destination": to,
        "delivered_to": place, "project": project,
        "limit_usd": limit, "limit_reset": reset, "issued_on": today(),
        "rotated_on": None, "rotations": 0, "name_check": name_check}
    save_ledger(doc)
    _journal("issue", f"openrouter/{label}/{name}", to=to, limit_usd=limit, name_check=name_check)
    return {"name": name, "account": label, "label": row.get("label"),
            "limit": limit, "limit_reset": reset, "delivered_to": place,
            "name_check": name_check}


#: What to do about a pending row (a key minted, or perhaps minted, whose delivery was never recorded).
PENDING_REMEDY = ("its key was minted but never recorded as delivered — `revoke {name}` deletes it and clears the row, "
                  "then issue again; if the ledger holds no hash for it, whether it was minted is unknown: `claim "
                  "{name}` with its value if one reached the slot, or delete any key called {name!r} at the "
                  "provider, then `revoke {name}` (past the listing, where the door cannot look the name up: "
                  "`revoke {name} --forget-pending --reason \"…\"`)")


def _forget(name: str) -> None:
    doc = ledger()
    doc["issued"].pop(name, None)
    save_ledger(doc)


def _undo_mint(admin: str, name: str, row: dict) -> None:
    """A minted key that was not delivered: delete it and drop its pending row; when the delete
    fails, keep the row marked `orphan`, so the key stays counted and named. Always raises."""
    if row.get("hash"):
        try:
            _request(f"/keys/{row['hash']}", admin, method="DELETE")
        except (OSError, ValueError, RuntimeError):
            doc = ledger()
            doc["issued"].setdefault(name, {})["orphan"] = True
            save_ledger(doc)
            raise RuntimeError("Partial failure: delivery failed and successor cleanup failed; the key is kept in "
                               "the ledger as an orphan — `revoke " + name + "` deletes it") from None
        _forget(name)
        raise RuntimeError("Delivery failed; successor deleted. Predecessor was not revoked") from None
    raise RuntimeError("Partial failure: delivery failed and successor identity is unavailable; the pending row "
                       "stays — inspect provider keys")


def cmd_issue(name: str, limit: float, account: str | None, to: str,
              project: str | None, reset: str = "monthly") -> int:
    try:
        r = issue_key(name, limit, account, to, project, reset)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"issued {r['name']!r} from {r['account']!r}, ceiling ${r['limit']:g} {r['limit_reset']}, "
          f"delivered to {r['delivered_to']}")
    if r.get("name_check"):
        print(f"note: {r['name_check']}", file=sys.stderr)
    return 0


# ─────────────────────────── a project's own key, from its spend policy ───────

#: The spend policy's period (contract DEC-0037: `month` only) as the provider's ceiling reset.
POLICY_RESET = {"month": "monthly"}


def _project_folder(project: str, env: str, name: str) -> str:
    """The vault folder a project name or registry id resolves to (`vault.project_folder`)."""
    sys.path.insert(0, str(ROOT / "tools"))
    import vault
    try:
        return vault.project_folder(project, env, name)[0]
    except vault.VaultBoundaryError as exc:
        raise ValueError(str(exc)) from None


def _policy_project(uri: str) -> str:
    """`urn:project:<slug>`, `project:<slug>` or a bare slug → the slug the vault resolves."""
    text = str(uri or "")
    for prefix in ("urn:project:", "project:"):
        if text.startswith(prefix):
            return text[len(prefix):]
    if ":" in text or not text:
        raise ValueError(f"the policy's project is {text!r}; this door reads urn:project:<slug>")
    return text


class PolicyPartial(RuntimeError):
    """`issue_for_policy` stopped in the middle: some steps were carried out and one failed. `plan`
    holds every step with its action — what was done, `failed` with its reason, `not-reached` — so
    the caller can say which keys already exist. A RuntimeError, so a caller that only knew the
    provider's refusals still exits 1; a re-run is idempotent and does only what is missing."""

    def __init__(self, message: str, plan: list[dict]):
        super().__init__(message)
        self.plan = plan


#: The largest amount, in USD, a policy limit or a key ceiling may name. Far past any real ceiling;
#: what it refuses is a number whose cents do not fit a float (1e307 overflowed, round 3).
MAX_USD = 1_000_000_000.0
#: Two ceilings closer than this are the same ceiling: a provider that echoes 3.330000001 or stores
#: cents must not be re-PATCHed on every run.
SAME_CEILING_USD = 0.005
#: How long a door operation waits for another to release the ledger lock before it refuses: past a rotation
#: whose delivery takes its full 60 s plus its provider calls, so a mint from the key page waits, not fails.
POLICY_LOCK_WAIT_S = 150.0


def _cents(usd, what: str) -> int:
    """`usd` in whole cents, rounded DOWN, from its exact decimal value (no float slack: 3.3333334
    is 333 cents, 0.0299999996 is 2). Refuses what is not a positive finite amount up to MAX_USD."""
    if not isinstance(usd, (int, float)) or isinstance(usd, bool) or not math.isfinite(usd) or usd <= 0:
        raise ValueError(f"{what} is not a positive amount in USD")
    if usd > MAX_USD:
        raise ValueError(f"{what} (${usd:g}) is past ${MAX_USD:,.0f}; no provider ceiling is that large")
    return int((decimal.Decimal(repr(float(usd))) * 100).to_integral_value(rounding=decimal.ROUND_FLOOR))


def _usd(cents: int) -> float:
    return cents / 100


def _same(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(a - b) < SAME_CEILING_USD


def _split_ceilings(slots: list[dict], mode: str, limit_cents: int | None,
                    limit_override: float | None, split: dict | None) -> list[int]:
    """Each slot's ceiling in cents, by the rule ENG-002 asks for: the ceilings of every key a
    policy touches sum to at most the project limit. Equal shares rounded down to the cent by
    default; `split` (ref → USD, the ref as the policy writes it or as its vault slot) gives explicit
    amounts; `limit_override` is one ceiling PER KEY. Every amount is rounded down to the cent and
    compared with the exact limit. With no project limit each key still needs its own ceiling,
    explicit, and L0 bounds the sum."""
    n = len(slots)
    limit_text = f"${_usd(limit_cents):.2f}" if limit_cents is not None else ""
    if split is not None and limit_override is not None:
        raise ValueError("give --limit (one ceiling per key) or --split (an amount per slot), not both")
    if split is not None:
        by_slot: dict[str, int] = {}
        names = {}
        for s in slots:
            names[s["ref"]] = s["to"]
            names[s["to"]] = s["to"]
            names[s["to"][len("vault:"):]] = s["to"]
        for ref, usd in split.items():
            if ref not in names:
                raise ValueError(f"--split names {ref!r}, which is not a project-key source of this policy")
            if names[ref] in by_slot:
                raise ValueError(f"--split names {names[ref]} twice (as {ref!r} too); give each slot one amount")
            cents = _cents(usd, f"--split's amount for {ref!r}")
            if cents < 1:
                raise ValueError(f"--split gives {ref!r} less than a cent")
            by_slot[names[ref]] = cents
        missing = [s["ref"] for s in slots if s["to"] not in by_slot]
        if missing:
            raise ValueError(f"--split leaves {', '.join(missing)} with no ceiling; a key is never unbounded")
        ceilings = [by_slot[s["to"]] for s in slots]
        if limit_cents is not None and sum(ceilings) > limit_cents:
            raise ValueError(f"--split sums to ${_usd(sum(ceilings)):.2f}, more than the project limit ({limit_text})")
        return ceilings
    if limit_override is not None:
        cents = _cents(limit_override, "--limit")
        if cents < 1:
            raise ValueError("--limit is less than a cent")
        if limit_cents is not None and cents * n > limit_cents:
            raise ValueError(f"{n} key(s) at ${_usd(cents):.2f} each cannot exceed the project's limit "
                             f"({limit_text}); --limit is per key — give at most ${_usd(limit_cents // n):.2f}, "
                             f"or --split")
        return [cents] * n
    if mode == "no-project-limit":
        raise ValueError("the policy sets no project limit; the key still needs its own ceiling — give --limit")
    share = limit_cents // n
    if share < 1:
        raise ValueError(f"a {limit_text} limit over {n} keys leaves less than a cent for each; "
                         "raise the limit or name fewer project-key sources")
    return [share] * n


def _policy_limit_cents(policy: dict) -> int | None:
    """The project limit in whole cents (rounded down), None for `no-project-limit`."""
    if policy.get("mode") != "limited":
        return None
    limit = policy.get("limitUsd")
    if not isinstance(limit, (int, float)) or isinstance(limit, bool) or not math.isfinite(limit):
        raise ValueError("a limited policy carries limitUsd")
    if limit <= 0:
        raise ValueError("the project limit is $0 — it spends nothing, so no key is issued")
    return _cents(limit, "the project limit")


def policy_key_plan(policy: dict, limit_override: float | None = None,
                    split: dict | None = None) -> list[dict]:
    """What a project's spend policy asks of this door, before anything is minted (OBS-58).

    A policy (contract `spend-policy`, DEC-0037) names its credential sources. Each `project-key`
    source whose `ref` is a vault slot (`vault:<project>/<env>/<NAME>`, or the bare
    `<project>/<env>/<NAME>` the meter and `use_secret` read) gets a key issued FOR THAT PROJECT
    into that slot. A slot of another project is a borrow, refused here as the registry refuses it
    (contract spend-policy rule 1). The ceilings of all the policy's keys together are at most the
    project limit (ENG-002: each key used to get the whole limit, N keys N times it) — an equal
    split by default, explicit with `split`, or `limit_override` per key, every amount in whole
    cents — and reset with the policy's period. A policy with no project limit still needs a
    ceiling for each key, given explicitly: a key without one is an unbounded liability, and L0
    alone is not a key's ceiling.
    """
    if not isinstance(policy, dict) or policy.get("kind") != "spend-policy":
        raise ValueError("not a spend policy (kind must be spend-policy)")
    project = _policy_project(policy.get("project"))
    mode = policy.get("mode")
    if mode not in ("limited", "no-project-limit"):
        raise ValueError(f"unknown policy mode {mode!r}")
    limit_cents = _policy_limit_cents(policy)
    if mode == "no-project-limit" and limit_override is None and split is None:
        raise ValueError("the policy sets no project limit; the key still needs its own ceiling — give --limit")
    reset = POLICY_RESET.get(policy.get("period"))
    if reset is None:
        raise ValueError(f"unknown policy period {policy.get('period')!r}")
    sources = policy.get("credentialSources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("the policy names no credential source")
    plan = []
    for source in sources:
        if not isinstance(source, dict) or source.get("kind") != "project-key":
            continue                    # a capability source spends another agent's wallet, not a key
        ref = str(source.get("ref") or "")
        slot = ref[len("vault:"):] if ref.startswith("vault:") else ref
        parts = slot.split("/")
        if len(parts) != 3 or not all(parts):
            plan.append({"ref": ref, "action": "skipped",
                         "why": "not a vault slot (vault:<project>/<env>/<NAME>); another door's key"})
            continue
        folder = _project_folder(parts[0], parts[1], parts[2])
        own = _project_folder(project, parts[1], parts[2])
        if folder != own:
            raise ValueError(f"{ref} is a slot of {folder!r}, not of the policy's project {own!r}: "
                             f"a project's key is never another's (spend-policy rule 1)")
        to = f"vault:{folder}/{parts[1]}/{parts[2]}"
        if any(step.get("to") == to for step in plan):
            plan.append({"ref": ref, "action": "skipped", "why": f"the same slot as an earlier source ({to})"})
            continue
        name = slug(f"{folder}-{parts[1]}-{parts[2]}")
        clash = next((step for step in plan if step.get("name") == name), None)
        if clash:
            raise ValueError(f"{ref} and {clash['ref']} would both be keys called {name!r} (names ignore case "
                             f"and punctuation); name the slots apart")
        plan.append({"ref": ref, "to": to, "name": name, "project": folder, "reset": reset})
    slots = [step for step in plan if "to" in step]
    if not slots:
        raise ValueError("the policy names no project-key source this door can issue into")
    for step, cents in zip(slots, _split_ceilings(slots, mode, limit_cents, limit_override, split)):
        step["limit"] = _usd(cents)
    return plan


#: Held-row outcomes that leave a slot NOT served: the command exits 1 and names the remedy.
HELD_NOT_SERVING = {"held-disabled": "`enable {name}` lets it spend again, or `revoke {name}` and re-run to issue anew",
                    "held-missing": "`revoke {name}` clears the ledger row; re-run to issue anew",
                    "held-unread": "the provider could not be read; re-run when it answers",
                    "blocked": "the project's other keys leave no room under the limit; the lines above say which",
                    "not-in-policy": "`revoke {name}` if nothing spends it, or name its slot in the policy",
                    "held-pending": PENDING_REMEDY}


def _read_held(name: str, rec: dict, account: str | None) -> tuple[str | None, dict | None, str]:
    """A ledger row's key as the PROVIDER has it, read by hash (`find_key`), with the account the
    ledger recorded (else `--account`). Returns (state, row, why): state None for a live key,
    `held-disabled`, `held-pending` for a key minted whose delivery was never recorded (or whose
    minting is unknown: a pending row with no hash is not read at all), `held-missing` only for a
    key the provider answers 404 for — a key renamed in the provider's dashboard is still live and
    still found by its hash —, or `held-unread` for anything that could not be read (no stashed admin
    for the account, an error, an answer that is not JSON), in a dry run too. Unread is never in
    place, and never counted as gone."""
    if rec.get("pending") and not rec.get("hash"):
        return "held-pending", None, "whether its key was minted is unknown (no hash was recorded)"
    try:
        _label, admin = read_admin(rec.get("account") or account)
        row = find_key(admin, name)
    except (RuntimeError, LookupError, OSError, ValueError) as exc:
        return "held-unread", None, f"read failed ({type(exc).__name__})"
    if not row:
        return "held-missing", None, ""
    if rec.get("pending"):
        return "held-pending", row, "minted, but its delivery was never recorded" + (
            " (its cleanup delete failed)" if rec.get("orphan") else "")
    if row.get("disabled"):
        return "held-disabled", row, ""
    return None, row, ""


def _ceiling_of(row: dict | None) -> float | None:
    limit = (row or {}).get("limit")
    return float(limit) if isinstance(limit, (int, float)) and not isinstance(limit, bool) else None


def _cents_up(usd) -> int | None:
    """A ceiling the provider holds, in cents rounded UP (3.334 is 334), so a total never reads
    lower than what can be spent. Float noise below a millionth of a dollar (an echo of 3.330000001)
    is not a cent; a provider holding $0.0001 more than asked is (fix 5). None is unbounded."""
    if not isinstance(usd, (int, float)) or isinstance(usd, bool) or not math.isfinite(usd):
        return None
    exact = decimal.Decimal(repr(float(usd))) * 100 - decimal.Decimal("0.0001")
    return max(int(exact.to_integral_value(rounding=decimal.ROUND_CEILING)), 0)


def _counted(state: str | None, row: dict | None, rec: dict) -> int | None:
    """Cents a key the run will NOT move counts toward the project's total (None: unbounded): its
    provider ceiling rounded up (none is unbounded), 0 for a key the provider no longer has, and for
    one that could not be read, or whose minting is unknown, the ceiling the ledger last recorded —
    unbounded when it recorded none."""
    if state == "held-missing":
        return 0
    if row is None:
        return _cents_up(rec.get("limit_usd"))
    return _cents_up(_ceiling_of(row))


class _Total:
    """A total in whole cents with unbounded held apart (no float sums: $0.10 x 3 is $0.30)."""

    def __init__(self):
        self.cents, self.unbounded = 0, False

    def add(self, cents: int | None) -> None:
        if cents is None:
            self.unbounded = True
        else:
            self.cents += cents

    def over(self, limit_cents: int | None) -> bool:
        return limit_cents is not None and (self.unbounded or self.cents > limit_cents)

    def text(self) -> str:
        return "unbounded" if self.unbounded else f"${_usd(self.cents):.2f}"


def issue_for_policy(policy: dict, account: str | None = None, limit_override: float | None = None,
                     dry_run: bool = False, split: dict | None = None) -> list[dict]:
    """Carries out `policy_key_plan` so that the project's key ceilings never sum past its limit — not
    at the end, and not at any moment in between (ENG-002, adversarial rounds 3 and 4):

    * **Every key of the project counts**, in whole cents (planned ceilings exactly, provider
      ceilings rounded up, unbounded held apart). Every ledger row whose destination is under
      `vault:<project>/` is read at the provider, pending rows included. A key the policy no longer
      names (a source removed from it, a second key behind one slot) is NOT moved — a $0 ceiling's
      meaning at the provider is not something this door has verified, and `revoke` is the verb for
      a key nobody names — it is counted at its provider ceiling, reported `not-in-policy` and the
      run exits 1.
    * **A key that could not be read counts at its last known ceiling** (the ledger's; unbounded if
      none); one the provider answers 404 for counts 0; a renamed key is still live and is read.
    * **A pending row** (a key minted whose delivery was never recorded, or whose minting is
      unknown) holds its slot: nothing is minted there, the slot reads `held-pending` and the run
      exits 1 naming `revoke` or `claim` (PENDING_REMEDY).
    * **Lowerings first, raises only when they fit.** Ceilings that go down move first. Raises and
      new keys follow only when the project's total after the run fits the limit; otherwise they
      are `blocked`. Whenever the total the run leaves is past the limit, an `over-limit` line says
      by how much and what ends it, and the run exits 1.
    * **One run at a time** (`_policy_lock`), the ledger and the provider re-read under the lock.
    * The issuing account for a new key is resolved before anything moves, in a dry run too.

    The one window this door opens in which the total can pass the limit is `rotate`'s: the
    successor is live before the predecessor is deleted (docs/design/AGENT-SECRETS.md).

    A failure in the middle stops the run and raises `PolicyPartial` carrying the plan: what was
    done, what failed, what was not reached. Idempotent: run it again and nothing moves.
    """
    plan = policy_key_plan(policy, limit_override, split)
    limit_cents = _policy_limit_cents(policy)
    slots = [step for step in plan if "to" in step]
    folder = slots[0]["project"]
    with _policy_lock():
        issued = ledger().get("issued", {})
        scope = {n: r for n, r in sorted(issued.items())
                 if str(r.get("destination") or "").startswith(f"vault:{folder}/")}
        primary: dict[str, str] = {}
        for step in slots:
            holders = [n for n, r in scope.items() if r.get("destination") == step["to"]]
            if holders:
                primary[step["to"]] = step["name"] if step["name"] in holders else holders[0]
        if len(primary) < len(slots):
            read_admin(account)         # the account a new key is minted from, resolved before anything moves
        after, end = _Total(), _Total()  # the total if every planned write happens / what the run leaves
        work, settle = [], []
        named = set(primary.values())
        for step in slots:
            planned = _cents(step["limit"], "a planned ceiling")
            name = primary.get(step["to"])
            if name is None:
                work.append((1, step, None, 0))
                after.add(planned)
                continue
            rec = scope[name]
            step["name"] = name
            state, row, why = _read_held(name, rec, account)
            if why:
                step["why"] = why
            if state in ("held-missing", "held-unread", "held-pending"):
                step["action"] = state
                counted = _counted(state, row, rec)
                after.add(counted)
                end.add(counted)
                if state == "held-pending":
                    settle.append(f"revoke {name}")
                continue
            current = _ceiling_of(row)
            held = _cents_up(current)
            if _same(current, step["limit"]) and row.get("limit_reset") == step["reset"]:
                step["action"] = state or "in-place"
                after.add(held)
                end.add(held)
                if held is not None and held > planned:
                    settle.append(f"limit {name} --set {step['limit']:g}")
                continue
            step["previous"] = current
            step["held"] = state
            after.add(planned)
            lowering = current is None or step["limit"] < current
            work.append((0 if lowering else 1, step, name, held))
        for name, rec in scope.items():
            if name in named:
                continue
            state, row, why = _read_held(name, rec, account)
            counted = _counted(state, row, rec)
            after.add(counted)
            end.add(counted)
            if counted != 0:
                settle.append(f"revoke {name}")
            plan.append({"ref": rec.get("destination"), "name": name, "action": "not-in-policy",
                         "counted": None if counted is None else _usd(counted),
                         "why": (f"a key of this project the policy does not name, counted at "
                                 + ("an unbounded ceiling" if counted is None else f"${_usd(counted):.2f}")
                                 + (f" ({why})" if why else "") + "; not moved by this run")})
        room = not after.over(limit_cents)
        for _order, step, name, _held in sorted(work, key=lambda w: w[0]):
            step["action"] = "not-reached"
        for order, step, name, held in sorted(work, key=lambda w: w[0]):
            moving = name is not None
            if order == 1 and not room:
                step.pop("held", None)
                step["action"] = "blocked"
                end.add(held if moving else 0)
                step["why"] = (f"after this run the project's keys would total {after.text()}, past its "
                               f"${_usd(limit_cents):.2f} limit; nothing is raised or issued")
                continue
            end.add(_cents(step["limit"], "a planned ceiling"))
            if dry_run:
                step["action"] = step.pop("held", None) or ("would-move-ceiling" if moving else "would-issue")
                continue
            try:
                if moving:
                    set_limit(name, step["limit"], step["reset"])
                else:
                    issue_key(step["name"], step["limit"], account, step["to"], step["project"], step["reset"])
            except (ValueError, RuntimeError, LookupError, OSError, subprocess.SubprocessError) as exc:
                step.pop("held", None)
                step["action"] = "failed"
                step["why"] = (str(exc) if isinstance(exc, (ValueError, LookupError))
                               else type(exc).__name__ + ": " + str(exc))
                if isinstance(exc, OSError) and exc.errno is not None:
                    step["errno"] = exc.errno     # ENOSPC, EIO…: the machine's failure, named as such
                done = sum(1 for s in plan if s.get("action") in ("issued", "ceiling-moved", "in-place"))
                raise PolicyPartial(f"partial: {done} of {len(slots)} slots served before {step['ref']} failed; "
                                    f"a re-run does only what is missing", plan) from None
            # A disabled key's ceiling still moves (it would spend that much once enabled), but the
            # slot stays named as not served.
            step["action"] = step.pop("held", None) or ("ceiling-moved" if moving else "issued")
        if end.over(limit_cents):
            head = ("OVER LIMIT: unbounded now" if end.unbounded
                    else f"OVER LIMIT by ${_usd(end.cents - limit_cents):.2f} now")
            plan.append({"ref": f"vault:{folder}/", "name": folder, "action": "over-limit",
                         "why": (f"{head} ({end.text()} against the ${_usd(limit_cents):.2f} limit); "
                                 + ("it ends after: " + ", ".join(settle) if settle
                                    else "it ends once the keys above are read and settled"))})
    return plan


def _parse_split(text: str | None) -> dict | None:
    """`ref=usd,ref=usd` → {ref: usd}. A ref holds `:` and `/` but never `=` or `,`."""
    if text is None:
        return None
    out: dict[str, float] = {}
    for part in [p.strip() for p in text.split(",") if p.strip()]:
        ref, sep, usd = part.rpartition("=")
        if not sep or not ref:
            raise ValueError(f"--split takes ref=usd pairs separated by commas, not {part!r}")
        if ref.strip() in out:
            raise ValueError(f"--split names {ref.strip()!r} twice; give each slot one amount")
        try:
            out[ref.strip()] = float(usd)
        except ValueError:
            raise ValueError(f"--split gives {ref.strip()!r} {usd!r}, not an amount in USD") from None
    if not out:
        raise ValueError("--split is empty")
    return out


def cmd_issue_for_policy(path: str, account: str | None, limit_override: float | None, dry_run: bool,
                         split: str | dict | None = None) -> int:
    try:
        policy = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"refused: cannot read the policy ({type(exc).__name__})", file=sys.stderr)
        return 2
    partial = None
    try:
        amounts = _parse_split(split) if isinstance(split, str) else split
        plan = issue_for_policy(policy, account, limit_override, dry_run, amounts)
    except PolicyPartial as exc:
        plan, partial = exc.plan, str(exc)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except (RuntimeError, LookupError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    not_serving = False
    total = 0.0
    for step in plan:
        if step.get("action") == "over-limit":
            not_serving = True
            print(step["why"])
            continue
        if step.get("action") == "not-in-policy":
            not_serving = True
            total += step["counted"] if step["counted"] is not None else math.inf
            print(f"{step['ref']}: not-in-policy {step['name']!r} — {step['why']}")
            print("  " + HELD_NOT_SERVING["not-in-policy"].format(name=step["name"]))
            continue
        if "to" not in step:
            print(f"{step['ref']}: skipped — {step['why']}")
            continue
        total += step["limit"]
        extra = ((f" (was ${step['previous']:g})" if step["previous"] is not None else " (was unbounded)")
                 if "previous" in step else "")
        why = f" — {step['why']}" if step.get("why") else ""
        print(f"{step['ref']}: {step['action']} {step['name']!r}, ceiling ${step['limit']:g} {step['reset']}{extra}{why}")
        if step["action"] in HELD_NOT_SERVING:
            not_serving = True
            print(f"  the slot is not served as planned: " + HELD_NOT_SERVING[step["action"]].format(name=step["name"]))
    print("ceilings: " + ("unbounded" if math.isinf(total) else f"${round(total, 2):g}") + " in all as planned" + (f", project limit ${policy['limitUsd']:g}"
                                                                 if policy.get("mode") == "limited"
                                                                 else ", no project limit (L0 binds)"))
    # The meter's L0 per key (docs/design/METER.md M4) is configured, not read from the provider: once a
    # ceiling moves here, `keyCeilingsUsd` in config/meter.json must say the same, by the policy's ref.
    implied = {step["ref"]: step["limit"] for step in plan if "to" in step}
    print("keyCeilingsUsd for config/meter.json (the meter's L0 per key; set it to match): "
          + json.dumps(implied, ensure_ascii=False))
    if partial:
        print(partial)
        return 1
    return 1 if not_serving else 0


def resolve_issued(name_or_label: str) -> tuple[str, dict]:
    """(name, ledger row) for a ledger name or the provider's label for a key.

    The dashboard speaks labels (what the provider calls a key) and the
    ledger speaks names; both reach the same row.
    """
    doc = ledger()
    if name_or_label in doc["issued"]:
        return name_or_label, doc["issued"][name_or_label]
    for n, rec in doc["issued"].items():
        try:
            _l, admin = read_admin(rec["account"])
            row = find_key(admin, n)
        except RuntimeError:
            continue
        if row and row.get("label") == name_or_label:
            return n, rec
    raise LookupError(f"no issued key called {name_or_label!r} — `list` shows what exists")


@_locked
def set_limit(name_or_label: str, limit: float, reset: str | None = None) -> dict:
    """Moves a key's ceiling. Without a `reset` the key keeps the period it has: moving a daily
    ceiling never turns it into a monthly one behind the caller's back."""
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError("a positive ceiling is required")
    name, rec = resolve_issued(name_or_label)
    reset = _reset(reset if reset is not None else (rec.get("limit_reset") or "monthly"))
    label, admin = read_admin(rec["account"])
    row = find_key(admin, name)
    if not row:
        raise LookupError(f"the ledger records {name!r} but {label!r} holds no such key — "
                          f"it was deleted at the provider; `revoke {name}` clears the row")
    _request(f"/keys/{row['hash']}", admin, {"limit": limit, "limit_reset": reset},
             method="PATCH")
    doc = ledger()
    before = doc["issued"][name].get("limit_usd")
    doc["issued"][name]["limit_usd"] = limit
    doc["issued"][name]["limit_reset"] = reset
    save_ledger(doc)
    # A ceiling is what a key may spend: moving it is a movement like an issue or a
    # rotation, and it went unrecorded (2026-10-06). Here, not in `cmd_limit`, so the
    # keyserver's live action records it too.
    _journal("limit", f"openrouter/{label}/{name}", limit_usd=limit, previous_usd=before,
             limit_reset=reset)
    return {"name": name, "label": row.get("label"), "limit": limit, "limit_reset": reset}


def cmd_limit(name: str, set_to: float | None, reset: str | None = None) -> int:
    if set_to is None and reset is not None:
        print("refused: --reset moves with a ceiling; give --set too", file=sys.stderr)
        return 2
    if set_to is None:
        try:
            n, rec = resolve_issued(name)
            _label, admin = read_admin(rec["account"])
            row = find_key(admin, n) or {}
        except (LookupError, RuntimeError) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        print(f"{n}: ceiling ${row.get('limit') or 0:g} {row.get('limit_reset') or ''}, "
              f"spent ${row.get('usage', 0):g}"
              + (", DISABLED" if row.get("disabled") else ""))
        return 0
    try:
        r = set_limit(name, set_to, reset)
    except (ValueError, LookupError, RuntimeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"{r['name']}: ceiling moved to ${r['limit']:g} {r['limit_reset']}")
    return 0


@_locked
def toggle_key(name: str, disabled: bool) -> dict:
    """Stop or resume one issued key's spending, reversibly, at the provider.

    The callable half of `cmd_toggle`, so the keyserver can offer disable and
    enable as live actions. Raises `LookupError` for a name the ledger or the
    provider does not know and `RuntimeError` for a provider refusal; prints
    nothing, because the caller says what happened.
    """
    doc = ledger()
    rec = doc["issued"].get(name)
    if not rec:
        raise LookupError(f"no issued key called {name!r}")
    _label, admin = read_admin(rec["account"])
    row = find_key(admin, name)
    if not row:
        raise LookupError(f"{name!r} is not at the provider any more")
    _request(f"/keys/{row['hash']}", admin, {"disabled": disabled}, method="PATCH")
    _journal("disable" if disabled else "enable", f"openrouter/{_label}/{name}")
    return {"name": name, "disabled": disabled}


def cmd_toggle(name: str, disabled: bool) -> int:
    try:
        toggle_key(name, disabled)
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"{name}: {'disabled — it spends nothing until enabled' if disabled else 'enabled'}")
    return 0


def _delivery_failed(admin: str, row: dict) -> None:
    successor = row.get("hash")
    if not successor:
        raise RuntimeError("Partial failure: delivery failed and successor identity is unavailable; inspect provider keys. Predecessor was not revoked")
    try:
        _request(f"/keys/{successor}", admin, method="DELETE")
    except (OSError, ValueError, RuntimeError):
        raise RuntimeError("Partial failure: delivery failed and successor cleanup failed; inspect provider keys. Predecessor was not revoked") from None
    raise RuntimeError("Delivery failed; successor deleted. Predecessor was not revoked") from None


def _alive(admin: str, key_hash: str) -> bool:
    """Whether the provider still holds a key, by its hash: a 404 is gone; any other failure raises."""
    try:
        return bool(_request(f"/keys/{key_hash}", admin).get("data"))
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            raise
        return False


def _gone_snapshot(why: str) -> dict:
    return {"usage": "unread", "usage_error": why,
            "read_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}


#: A row a failure left part done: the rotation it belongs to is finished before a new one starts.
PART_DONE = ("successor-minted-delivery-pending", "successor-delivered-predecessor-pending",
             "predecessor-retired-rename-pending")


def _part_done(rec: dict) -> bool:
    return bool(rec.get("predecessor_hash") or rec.get("successor_pending")
                or rec.get("rotation_status") in PART_DONE)


def _retire_predecessor(doc: dict, name: str, rec: dict, predecessor: str | None, snapshot: dict) -> None:
    """The predecessor of a rotation is gone at the provider: its tombstone, and the row's bookkeeping.

    `tombstoned_rotations` counts the rotations that left one; a row rotated before tombstones existed has fewer
    than `rotations`, and `tombstones_since` bounds when those older rotations happened: the day of the last
    rotation that left no tombstone (`predecessor_rotated_on`, the row's `rotated_on` before this rotation)."""
    previous_rotated_on = rec.get("predecessor_rotated_on")
    for field in ("predecessor_hash", "predecessor_snapshot", "predecessor_rotated_on"):
        rec.pop(field, None)
    _retire(doc, name, rec, predecessor, "rotated", snapshot, row_state=False)
    tombstoned = rec.get("tombstoned_rotations")
    if type(tombstoned) is not int or tombstoned < 0:
        tombstoned = 0
    if tombstoned == 0:
        rec["tombstones_since"] = previous_rotated_on
    rec["tombstoned_rotations"] = tombstoned + 1
    rec["rotation_status"] = "predecessor-retired-rename-pending"


def _resume_rotation(doc: dict, name: str, rec: dict, label: str, admin: str) -> None:
    """Finish the rotation an earlier run left part done (round 5, R5-3/R5-9). `rotate_one` then rotates afresh:
    finishing an old rotation never replaces the current value, and a caller who asked to rotate gets a new one
    (round 6, R6-1).

    - A successor minted whose delivery was never recorded (`successor_pending`): whether the slot holds it is
      unknown, so it is not deleted here. It is kept on the row as a stale successor, read by the spend scan like
      a live key, and deleted by the fresh rotation once a newer successor is delivered (round 6, R6-6).
    - A predecessor still on the row is asked by its hash: live (the earlier DELETE failed) is snapshotted, the
      snapshot written to the row, deleted and retired; gone (404: the DELETE went through and its answer was
      lost) is retired with the snapshot kept on the row before that DELETE (`predecessor_snapshot`).
    - The successor gets the row's name, and the rotation is complete.

    A row whose `predecessor_hash` is its own live hash is refused before any request (R6-2): following it would
    delete the key the consumer holds."""
    predecessor = rec.get("predecessor_hash")
    if predecessor and predecessor == rec.get("hash"):
        raise ValueError(f"{name!r}: the ledger row names its own live key as the predecessor of a rotation, so "
                         f"finishing that rotation would delete the key its consumer holds; nothing was done. "
                         f"Repair the row in the door's ledger: remove `predecessor_hash`, `predecessor_snapshot` and "
                         f"`rotation_status`, then `rotate {name}` again")
    try:
        pending = rec.pop("successor_pending", None)
        if pending:
            stale = rec.get("stale_successors") if isinstance(rec.get("stale_successors"), list) else []
            if pending not in [e.get("hash") for e in stale if isinstance(e, dict)]:
                stale.append({"hash": pending})
            rec["stale_successors"] = stale
            save_ledger(doc)
        renamed_needed = bool(predecessor) or rec.get("rotation_status") in (
            "successor-delivered-predecessor-pending", "predecessor-retired-rename-pending")
        if predecessor:
            if _alive(admin, predecessor):
                snapshot = usage_snapshot(admin, predecessor)
                rec["predecessor_snapshot"] = snapshot
                save_ledger(doc)
                _request(f"/keys/{predecessor}", admin, method="DELETE")
            else:
                kept = rec.get("predecessor_snapshot")
                snapshot = kept if isinstance(kept, dict) else _gone_snapshot("the predecessor was gone at the provider")
            _retire_predecessor(doc, name, rec, predecessor, snapshot)
            save_ledger(doc)
        if renamed_needed and rec.get("hash"):
            _request(f"/keys/{rec['hash']}", admin, {"name": name}, method="PATCH")
        rec["rotation_status"] = "complete"
        save_ledger(doc)
    except (OSError, ValueError, RuntimeError):
        _journal("rotation-partial", f"openrouter/{label}/{name}", status="resume-failed")
        raise RuntimeError("Partial failure: the earlier rotation could not be finished; inspect provider keys "
                           "and ledger before retrying") from None
    _journal("rotation-resumed", f"openrouter/{label}/{name}", to=rec.get("destination"))


def _retire_stale_successors(doc: dict, name: str, rec: dict, admin: str) -> None:
    """Delete and retire the successors an interrupted rotation minted and never recorded as delivered, now that
    a newer successor holds the slot. Each one's spend is snapshotted and written to the row before its DELETE,
    as for a predecessor.

    A snapshot kept from an earlier run is used only when the key is gone (404): while it is alive it may have
    spent since, so it is read again and the new snapshot written before the DELETE (round 7: a kept day-1 snapshot
    of a key that spent on day 2 read whole and short)."""
    for entry in list(rec.get("stale_successors") or []):
        key_hash = entry.get("hash") if isinstance(entry, dict) else None
        if not key_hash:
            continue
        if _alive(admin, key_hash):
            snapshot = usage_snapshot(admin, key_hash)
            entry["snapshot"] = snapshot
            save_ledger(doc)
        else:
            kept = entry.get("snapshot")
            snapshot = kept if isinstance(kept, dict) else _gone_snapshot(
                "the stale successor was gone at the provider")
        try:
            _request(f"/keys/{key_hash}", admin, method="DELETE")
        except RuntimeError as exc:
            if "HTTP 404" not in str(exc):
                raise
        rec["stale_successors"] = [e for e in rec.get("stale_successors") or [] if e is not entry]
        if not rec["stale_successors"]:
            rec.pop("stale_successors")
        _retire(doc, name, rec, key_hash, "stale-successor", snapshot, row_state=False)
        save_ledger(doc)


def _rotate_fresh(doc: dict, name: str, rec: dict, label: str, admin: str) -> dict:
    """Mint a successor, record it, deliver it, then retire the predecessor: the current value is replaced."""
    rotations = rec.get("rotations", 0)
    limit = rec.get("limit_usd") or 10
    previous_hash = rec.get("hash")
    previous_rotated_on = rec.get("rotated_on")
    previous_status = rec.get("rotation_status")
    old = find_key(admin, name)
    d = _request("/keys", admin,
                 {"name": f"{name}-next", "limit": limit,
                  "limit_reset": rec.get("limit_reset") or "monthly"})
    value = d.get("key") or (d.get("data") or {}).get("key")
    new_row = d.get("data") or d
    if not value:
        _delivery_failed(admin, new_row)
    # The minted successor is on the row before its delivery (round 6, R6-6): a failure between the delivery and
    # the next write used to leave it live and unknown, and a retry minted a second one.
    if new_row.get("hash"):
        rec["successor_pending"] = new_row["hash"]
        rec["rotation_status"] = "successor-minted-delivery-pending"
        try:
            save_ledger(doc)
        except (OSError, ValueError, RuntimeError):
            _delivery_failed(admin, new_row)

    def undo() -> None:
        rec.pop("successor_pending", None)
        rec["rotation_status"] = previous_status
        try:
            save_ledger(doc)
        except (OSError, ValueError, RuntimeError):
            pass
    try:
        place = deliver(value, rec["destination"], rotate=True)
    except (OSError, ValueError, RuntimeError):
        try:
            _delivery_failed(admin, new_row)
        except RuntimeError as failure:
            if "successor deleted" in str(failure):
                undo()
            raise
    rec.pop("successor_pending", None)
    rec.update({"hash": new_row.get("hash"), "label": new_row.get("label"), "delivered_to": place,
                "rotated_on": today(), "rotations": rotations + 1,
                "rotation_status": "successor-delivered-predecessor-pending",
                "predecessor_hash": old.get("hash") if old else None,
                "predecessor_rotated_on": previous_rotated_on})
    predecessor = (old or {}).get("hash") or previous_hash
    try:
        save_ledger(doc)
        _retire_stale_successors(doc, name, rec, admin)
        # What the predecessor spent, read while it still exists (the provider keeps no history of a deleted key),
        # and kept on the row BEFORE the DELETE: a failure after the DELETE must not lose it (round 4, P5).
        snapshot = usage_snapshot(admin, predecessor) if old else _gone_snapshot(
            "the predecessor was not found at the provider")
        rec["predecessor_snapshot"] = snapshot
        save_ledger(doc)
        if old:
            _request(f"/keys/{old['hash']}", admin, method="DELETE")
        # The predecessor is gone: its tombstone now, before the rename, so a failed PATCH keeps it (J-11).
        _retire_predecessor(doc, name, rec, predecessor, snapshot)
        save_ledger(doc)
        _request(f"/keys/{new_row['hash']}", admin, {"name": name}, method="PATCH")
        rec["rotation_status"] = "complete"
        save_ledger(doc)
    except (OSError, ValueError, RuntimeError):
        _journal("rotation-partial", f"openrouter/{label}/{name}",
                 status="successor-delivered-provider-or-ledger-finalization-failed")
        raise RuntimeError("Partial failure: successor delivered, but provider or ledger finalization failed; inspect provider keys and ledger before retrying") from None
    _journal("rotate", f"openrouter/{label}/{name}", to=rec["destination"])
    return {"name": name, "delivered_to": place, "rotations": rec["rotations"],
            "rotated_on": rec["rotated_on"], "replaced": True}


@_locked
def rotate_one(name: str, doc: dict | None = None) -> dict:
    """Replace the key's value: deliver and verify a successor before revoking the predecessor, keeping recovery
    metadata. A row a failure left part done is finished first (`_resume_rotation`), then rotated afresh, so the
    result always names a new value (`replaced`) and says whether an earlier rotation was finished (`resumed`)."""
    # Without a caller's own `doc`, the ledger is read here, under the ledger lock: a copy read before the lock
    # may be stale (two rotations at once). A caller that passes `doc` owns it and writes it back through us.
    doc = doc if doc is not None else ledger()
    if (doc.get("issued", {}).get(name) or {}).get("claimed"):
        raise ValueError(f"{name!r} was claimed, not issued: there is nowhere to deliver a successor; "
                         f"`issue` its replacement to a destination, then `revoke {name}`")
    rec = doc["issued"].get(name)
    if not rec:
        raise LookupError("not in the ledger")
    rotations = rec.get("rotations", 0)
    if type(rotations) is not int or rotations < 0:
        raise RuntimeError("Invalid rotation count; repair ledger before rotating")
    limit = rec.get("limit_usd") or 10
    if not isinstance(limit, (int, float)) or not math.isfinite(limit) or limit <= 0:
        raise ValueError("A finite positive ceiling is required")
    private_io.check(LEDGER)
    label, admin = read_admin(rec["account"])
    resumed = _part_done(rec)
    if resumed:
        _resume_rotation(doc, name, rec, label, admin)
    result = _rotate_fresh(doc, name, rec, label, admin)
    result["resumed"] = resumed
    return result


def cmd_rotate(name: str | None, leaked: bool) -> int:
    doc = ledger()
    targets = list(doc["issued"]) if not name else [name]
    if leaked:
        targets = [n for n in doc["issued"] if _leaked(n, doc["issued"][n])]
        if not targets:
            print("no open leak names an issued OpenRouter key")
            return 0
    bad = 0
    if not name:
        # A claimed key has nowhere to deliver a successor; a bulk run names it and goes on
        # (review 2026-10-12: the first claimed key ended `rotate --leaked` with a traceback).
        for n in [t for t in targets if (doc["issued"].get(t) or {}).get("claimed")]:
            print(f"  {n}: claimed, not issued — `issue` its replacement, then `revoke {n}`", file=sys.stderr)
            bad += 1
        targets = [t for t in targets if not (doc["issued"].get(t) or {}).get("claimed")]
    for n in targets:
        try:
            r = rotate_one(n)               # re-read under the lock: `doc` above was read before it
            if not r.get("replaced"):
                # Never "rotated" for a run that replaced nothing (round 6, R6-1).
                print(f"  {n}: an earlier rotation was finished; the current value was not replaced — "
                      f"run rotate again", file=sys.stderr)
                bad += 1
                continue
            print(f"  {n}: rotated, successor delivered to {r['delivered_to']}, predecessor deleted"
                  + ("; an earlier rotation left part done was finished first" if r.get("resumed") else ""))
        except (LookupError, ValueError) as exc:
            print(f"  {n}: {exc}", file=sys.stderr)
            bad += 1
        except RuntimeError as exc:
            print(f"  {n}: could not rotate — {exc}", file=sys.stderr)
            bad += 1
    if leaked and not bad and targets:
        # `settle` closes a leak; `rotate` writes a new value and leaves the register open.
        print(SETTLE_HINT)
    return 1 if bad else 0


def _leaked(name: str, rec: dict) -> bool:
    sys.path.insert(0, str(ROOT / "tools"))
    import vault
    try:
        rows = vault.open_leaks()
    except Exception:                                                             
        return False
    blob_of = lambda r: json.dumps(r, ensure_ascii=False).lower()               
    return any(name.lower() in blob_of(r)
               or (rec.get("destination", "").startswith("vault:")
                   and rec["destination"][len("vault:"):].lower() in blob_of(r))
               for r in rows)


def _label_of(value: str) -> str:
    """What the provider calls a key — prefix, ellipsis, three characters — from its value."""
    sys.path.insert(0, str(ROOT / "collectors"))
    from scan_openrouter import label_of
    return label_of(value)


@_locked
def claim_key(name: str, value: str, account: str | None = None, where: str = "") -> dict:
    """Bring a key this door did NOT mint under its control, so `disable`, `limit` and
    `revoke` reach it — the door hashes the value and never stores or prints it.

    WHY (2026-10-11). Agent tools on one machine kept OpenRouter keys made by hand in the
    dashboard, in plaintext files. The door could issue replacements into the vault but
    could not revoke the old keys: `revoke` finds a key by the ledger's name, and they had
    no row. Revoking them at the dashboard is the manual step this door exists to remove.

    HOW IT IS FOUND. The value arrives on stdin. The provider's row is read by the value's
    SHA-256 (one request, exact). Only when no stashed account answers to the hash is the
    listing searched by label — and a label is a prefix plus THREE characters, which collide
    across thousands of keys (tests/test_openrouter_keys.py), so that path is accepted only
    when exactly one key in every listing carries the label AND its ceiling is the one the
    key reports for itself (`GET /key` with the value). Anything else is refused.
    """
    name = slug(name)
    if not name:
        raise ValueError("the key needs a name in this ledger")
    value = (value or "").strip()
    label = _label_of(value)
    if not label:
        raise ValueError("that is not an OpenRouter key")
    doc = ledger()
    if name in doc["issued"]:
        raise ValueError(f"the ledger already has a key called {name!r}")
    accounts = [(account, read_admin(account)[1])] if account else \
        [(lbl, read_admin(lbl)[1]) for lbl, _p in admins()]
    if not accounts:
        raise RuntimeError("no provisioning key is stashed; `stash --as <label>` first")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    if any(r.get("hash") == digest for r in doc["issued"].values()):
        raise ValueError("this key is already in the ledger")
    found: list[tuple[str, dict]] = []
    for acct, admin in accounts:
        try:
            row = _request(f"/keys/{digest}", admin).get("data") or None
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):
                continue
            raise
        if row and row.get("label") == label:
            found.append((acct, row))
    how = "hash"
    if not found:
        how = "label"
        own = _request("/key", value).get("data") or {}
        if own.get("limit") is None:
            # Without a ceiling there is no second fact to compare: `None == None` matched every
            # unlimited dashboard key (review 2026-10-12).
            raise ValueError("the key reports no ceiling, so a label match cannot be confirmed; set a ceiling "
                             "on it in the provider's dashboard and claim again, or claim from the account "
                             "that holds it by hash")
        for acct, admin in accounts:
            try:
                rows = _all_keys(admin)
            except ListingTooLong:
                # "Exactly one key in EVERY listing" cannot be shown from a listing not read whole —
                # a match past the pages read would be another key with the same label.
                raise ValueError(f"{acct!r} lists more keys than this door reads, so a label match cannot be "
                                 "shown to be the only one; refusing") from None
            found += [(acct, r) for r in rows if r.get("label") == label]
        if len(found) == 1 and found[0][1].get("limit") != own.get("limit"):
            found = []
    if len(found) != 1:
        raise ValueError("no stashed account holds this key" if not found else
                         f"{len(found)} keys carry this label; refusing to pick one")
    acct, row = found[0]
    place = f"external:{where or 'unrecorded'}"
    doc = ledger()
    # Re-checked on the ledger that is written: the provider calls above take seconds, and a
    # second claim in the meantime would otherwise be overwritten (review 2026-10-12).
    if name in doc["issued"]:
        raise ValueError(f"the ledger already has a key called {name!r}")
    if any(r.get("hash") == row.get("hash") for r in doc["issued"].values()):
        raise ValueError("this key is already in the ledger")
    doc["issued"][name] = {
        "account": acct, "hash": row.get("hash"), "destination": place, "delivered_to": place,
        "project": None, "limit_usd": row.get("limit"), "limit_reset": row.get("limit_reset"),
        "issued_on": None, "claimed_on": today(), "claimed": True, "claimed_by": how,
        "provider_name": row.get("name"), "label": label, "rotated_on": None, "rotations": 0}
    save_ledger(doc)
    _journal("claim", f"openrouter/{acct}/{name}", where=where, found_by=how)
    return {"name": name, "account": acct, "label": label, "limit": row.get("limit"), "found_by": how}


def cmd_claim(name: str, account: str | None, where: str) -> int:
    if sys.stdin.isatty():
        print("give the key on stdin, from the file that holds it — never as an argument", file=sys.stderr)
        return 2
    try:
        r = claim_key(name, sys.stdin.read(), account, where)
    except (ValueError, LookupError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"claimed {r['name']!r} in {r['account']!r} ({r['label']}, ceiling ${r['limit'] or 0:g}, found by "
          f"{r['found_by']}); `disable {r['name']}` stops it, `revoke {r['name']}` deletes it")
    return 0


@_locked
def forget_pending_key(name: str, reason: str) -> dict:
    """Clear a pending row with no hash WITHOUT reading the provider: the one remedy that works on an
    account listing past PAGES_MAX, where the name cannot be looked up (fix 5). Only after the key
    called NAME was deleted at the provider, or is known never to have been minted; `reason` says
    which, and the journal keeps it. Refuses any other row — a row with a hash is revoked by hash."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("--forget-pending needs --reason: what makes the key gone (deleted at the provider "
                         "by name, or never minted)")
    doc = ledger()
    rec = doc["issued"].get(name)
    if not rec:
        raise LookupError(f"no issued key called {name!r}")
    if not rec.get("pending") or rec.get("hash"):
        raise LookupError(f"{name!r} is not a pending row without a hash; `revoke {name}` deletes it by its hash")
    _journal("forget-pending-attested", f"openrouter/{rec.get('account')}/{name}",
             require_success=True, reason=reason, destination=rec.get("destination"),
             attestation_only=True)
    doc["issued"].pop(name)
    # No hash to read by: the spend scan reads the project's windows that include this day as unread.
    _retire(doc, name, rec, None, "forgotten-pending")
    save_ledger(doc)
    _journal("forget-pending", f"openrouter/{rec.get('account')}/{name}", reason=reason,
             destination=rec.get("destination"))
    return {"name": name, "forgotten": True}


@_locked
def revoke_key(name_or_label: str) -> dict:
    """Delete at the provider, then clear the ledger row — in that order: a
    forgotten live key is worse than a stale row. REFUSED for a key something
    is reading right now (`serves` in the OpenRouter scan): revoking the key a
    consumer holds takes the consumer down with no way back."""
    name, rec = resolve_issued(name_or_label)
    scan = paths.SCRATCH / "openrouter.json"
    if scan.is_file():
        try:
            doc_scan = json.loads(scan.read_text(encoding="utf-8"))
            # By HASH first (round 4): a key renamed in the provider's dashboard is still the key a
            # consumer holds, and the scan records each consumer's key hash.
            for dest, held in (doc_scan.get("destination_hashes") or {}).items():
                if rec.get("hash") and isinstance(held, dict) and held.get("hash") == rec["hash"]:
                    raise ValueError(f"{name} is what {dest} is reading right now; "
                                     f"issue its replacement first, then revoke this one")
            # A key past the listing appears in the scan only as the label a destination holds
            # (`destinations`, fix 5). A label two keys share refuses too: the safe side.
            for dest, lab in (doc_scan.get("destinations") or {}).items():
                if rec.get("label") and lab == rec["label"]:
                    raise ValueError(f"{name} is what {dest} is reading right now (by its label); "
                                     f"issue its replacement first, then revoke this one")
            for k in doc_scan.get("keys", []):
                names = {name, rec.get("provider_name")} - {None}
                labels = {name_or_label, rec.get("label")} - {None}
                # A claimed key carries the provider's name, not the ledger's, and the scan has no
                # hash; its label (recorded at claim) is what a consumer is found by. A label two
                # keys share refuses here too: a consumer that may be reading it is not taken down.
                if k.get("serves") and (k.get("name") in names or k.get("label") in labels):
                    raise ValueError(f"{name} is what {k['serves']} is reading right now; "
                                     f"issue its replacement first, then revoke this one")
        except (OSError, ValueError) as exc:
            if isinstance(exc, ValueError) and "reading right now" in str(exc):
                raise
    if rec.get("successor_pending") or rec.get("stale_successors"):
        raise ValueError(f"{name}: a rotation left a successor whose delivery is unknown; `rotate {name}` "
                         f"finishes it (and deletes that successor once a new one holds the slot), then revoke")
    _label, admin = read_admin(rec["account"])
    row = find_key(admin, name)
    key_hash = rec.get("hash") or (row or {}).get("hash")
    if row:
        # What the key spent, read while it still exists (the provider keeps no history of a deleted key), and
        # kept on the row BEFORE the DELETE: a failure after the DELETE must not lose it (round 4, P4).
        snapshot = usage_snapshot(admin, key_hash)
        doc = ledger()
        if name in doc["issued"]:
            doc["issued"][name]["retiring_snapshot"] = snapshot
            save_ledger(doc)
        _request(f"/keys/{row['hash']}", admin, method="DELETE")
    else:
        # Gone at the provider: a retry after a failure past the DELETE finds the snapshot it kept.
        snapshot = rec.get("retiring_snapshot") if isinstance(rec.get("retiring_snapshot"), dict) else {
            "usage": "unread", "usage_error": "the key was not found at the provider",
            "read_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    doc = ledger()
    current = doc["issued"].pop(name, None)
    _retire(doc, name, current if isinstance(current, dict) else rec, key_hash, "revoked", snapshot)
    save_ledger(doc)
    _journal("revoke", f"openrouter/{rec['account']}/{name}")
    return {"name": name, "label": (row or {}).get("label"), "revoked": True,
            "delivered_to": rec.get("delivered_to")}


def cmd_revoke(name: str, forget_pending: bool = False, reason: str | None = None) -> int:
    if forget_pending:
        try:
            forget_pending_row = forget_pending_key(name, reason or "")
        except ValueError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        except (LookupError, RuntimeError) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        print(f"forgot the pending row {forget_pending_row['name']!r} (journaled with its reason); "
              f"nothing was read or deleted at the provider")
        return 0
    try:
        r = revoke_key(name)
    except (ValueError, LookupError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        # The ledger could not be written (a full disk): the provider may already have deleted the
        # key; the row stays, and the next revoke finds it gone (404) and clears it.
        print(f"refused: the ledger could not be written ({type(exc).__name__}: {exc})\n  the ledger row was "
              f"kept; run the revoke again once the disk takes writes", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        rec = (ledger().get("issued", {}).get(name) or {}) if LEDGER.exists() else {}
        hint = (f"\n  past the listing the name cannot be looked up: once the key called {name!r} is deleted at "
                f"the provider (or was never minted), `revoke {name} --forget-pending --reason \"…\"` clears "
                f"the row" if rec.get("pending") and not rec.get("hash") else "")
        print(f"refused: {exc}\n  the ledger row was kept — a forgotten live key "
              f"is worse than a stale row{hint}", file=sys.stderr)
        return 1
    print(f"revoked {r['name']!r}; note the delivered copy at {r['delivered_to']} is "
          f"now a dead value — the consumer will degrade at next use")
    return 0


# ─────────────────────────── list / ping ─────────────────────────────────────

def cmd_list() -> int:
    a = admins()
    doc = ledger()
    print(f"provisioning keys ({len(a)}) — read by this program only:")
    for label, p in a:
        m = {}
        if meta(p).is_file():
            try:
                m = json.loads(meta(p).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        print(f"  {label:20s} stashed {m.get('stashed_on', '?')}  "
              f"mode {oct(p.stat().st_mode)[-3:]}")
    rows = doc["issued"]
    print(f"issued keys ({len(rows)}):")
    for n, r in sorted(rows.items()):
        print(f"  {n:24s} from {r['account']:14s} ${r.get('limit_usd') or 0:<6g} "
              f"-> {r['delivered_to']}"
              + (f"  [{r['project']}]" if r.get("project") else "")
              + (f"  rotated {r['rotated_on']} x{r['rotations']}" if r.get("rotated_on") else ""))
    if not a:
        print("\nstart with openrouter stash --as <label>, reading a protected file on stdin")
    return 0


def cmd_ping() -> int:
    """Every stashed and issued key, against the provider. No values printed."""
    bad = 0
    doc = ledger()
    for label, p in admins():
        key = private_io.read(p).strip()
        try:
            # One page proves the provisioning key is alive; the account can hold far more keys
            # than any listing should walk (more than 20 000 on 2026-10-04).
            first = _request("/keys?include_disabled=true&offset=0", key).get("data", [])
            more = len(first) >= PAGE
            print(f"  admin/{label}: alive, {len(first)}{'+' if more else ''} key(s) at the provider")
            mine = {n for n, r in doc["issued"].items() if r["account"] == label}
            stray = sorted({r.get("name") for r in first} - mine - {None})
            if stray:
                # Keys the ledger does not know are spend capacity nobody here manages — said, not
                # hidden; with more than one page the list is a sample, and says so.
                print(f"    unmanaged at the provider{' (first page only)' if more else ''}: "
                      f"{', '.join(stray[:6])}{' …' if len(stray) > 6 else ''}")
        except RuntimeError as exc:
            print(f"  admin/{label}: DEAD — {exc}", file=sys.stderr)
            bad += 1
            continue
        for n, rec in sorted(doc["issued"].items()):
            if rec["account"] != label:
                continue
            try:
                row = find_key(key, n)
            except RuntimeError as exc:
                print(f"  {n}: could not be read — {exc}", file=sys.stderr)
                bad += 1
                continue
            if not row:
                print(f"  {n}: in the ledger but NOT at the provider — revoke it "
                      f"or re-issue", file=sys.stderr)
                bad += 1
                continue
            ceiling = row.get("limit")
            spent = row.get("usage", 0)
            state = "DISABLED" if row.get("disabled") else "alive"
            warn = ""
            if ceiling and spent >= 0.9 * ceiling:
                warn = "  <- near its ceiling"
                bad += row.get("disabled", False) is False and spent >= ceiling
            print(f"  {n}: {state}, spent ${spent:g} of ${ceiling or 0:g}{warn}")
    if not admins():
        print("nothing stashed")
    return 1 if bad else 0


def read_activity(admin: str, key_hash: str, date: str | None = None) -> list[dict]:
    """One issued key's activity at the provider: rows by UTC day and model.

    GET /activity is the management key's read (the last 30 completed UTC
    days, or one `date`), filtered by the issued key's hash. The hash travels
    to the provider only; callers must not print it. A refusal raises — an
    unreadable report is not a measured zero.
    """
    path = "/activity?api_key_hash=" + key_hash + (f"&date={date}" if date else "")
    rows = _request(path, admin).get("data")
    if not isinstance(rows, list):
        raise RuntimeError("openrouter answered /activity without a data list")
    return rows


def cmd_spend(name: str, date: str | None, as_json: bool) -> int:
    """What an issued key cost, by day and model, from the provider's own record."""
    if date is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError("--date is YYYY-MM-DD (a UTC day in the last 30)")
    rec = ledger()["issued"].get(name)
    if not rec:
        raise LookupError(f"no issued key called {name!r} — `list` shows what exists")
    if not rec.get("hash"):
        raise LookupError(f"{name}: the ledger row has no provider hash to filter by")
    _label, admin = read_admin(rec["account"])
    rows = read_activity(admin, rec["hash"], date)
    keep = ("date", "model", "requests", "prompt_tokens", "completion_tokens",
            "reasoning_tokens", "usage")
    # The provider sends the day as "YYYY-MM-DD 00:00:00"; the day is the unit.
    out = sorted(({**{k: r.get(k) for k in keep}, "date": str(r.get("date") or "")[:10]}
                  for r in rows),
                 key=lambda r: (str(r["date"]), str(r["model"])))
    total = round(sum(float(r.get("usage") or 0) for r in out), 9)
    if as_json:
        print(json.dumps({"key": name, "date": date, "rows": out, "totalUsd": total},
                         ensure_ascii=False))
        return 0
    scope = f"on {date}" if date else "in the last 30 UTC days"
    print(f"{name}: {len(out)} row(s) {scope}, from the provider's activity report")
    for r in out:
        print(f"  {r['date']}  {r['model']:34s} {r['requests'] or 0:>5} req  "
              f"in {r['prompt_tokens'] or 0} out {r['completion_tokens'] or 0}  "
              f"${float(r['usage'] or 0):.6f}")
    print(f"  total ${total:.6f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_st = sub.add_parser("stash", help="provisioning key on stdin, labeled by account")
    p_st.add_argument("--as", dest="label", required=True,
                      help="which OpenRouter account this is — the provider will not say")
    p_ad = sub.add_parser("adopt", help="move the legacy provisioning file under a label")
    p_ad.add_argument("--as", dest="label", required=True)
    p_is = sub.add_parser("issue", help="mint a working key and deliver it")
    p_is.add_argument("--name", required=True, help="the consumer, e.g. fabric-agent")
    p_is.add_argument("--limit", type=float, default=10.0, help="USD ceiling (default 10)")
    p_is.add_argument("--account", help="which stashed account to mint from")
    p_is.add_argument("--to", required=True,
                      help="observatory | claude-mem | vault:<project>/<env>/<NAME>")
    p_is.add_argument("--project", help="the project this key serves")
    p_is.add_argument("--reset", choices=RESETS, default="monthly",
                      help="when the ceiling starts again (default monthly)")
    p_ip = sub.add_parser("issue-for-policy",
                          help="issue a project's own keys from its spend policy (OBS-58): their ceilings together "
                          "stay within the project limit at every moment — every key of the project counts, one the "
                          "policy no longer names is reported not-in-policy and not moved, raises that would not fit "
                          "are blocked (exit 1); one run at a time")
    p_ip.add_argument("--policy", required=True, help="the project's spend-policy JSON (contract DEC-0037)")
    p_ip.add_argument("--account", help="which stashed account to mint from")
    p_ip.add_argument("--limit", type=float, help="each key's ceiling: required when the policy sets no "
                      "project limit (or --split); otherwise N keys x --limit may not exceed it. Default: the "
                      "project limit split equally, rounded down to the cent")
    p_ip.add_argument("--split", help="explicit ceilings, ref=usd,ref=usd — one per project-key source, "
                      "summing to at most the project limit")
    p_ip.add_argument("--dry-run", action="store_true",
                      help="say what would be issued or moved; mint and move nothing. It still READS the provider "
                      "for every held slot: with no stashed admin for a slot's account (or a row naming none while "
                      "several are stashed and no --account), the slot reads held-unread and the run exits 1")
    sub.add_parser("list", help="what exists — names, ceilings, places; never values")
    sub.add_parser("ping", help="alive? spent how much of its ceiling?")
    p_sp = sub.add_parser("spend", help="one issued key's cost by day and model (provider activity)")
    p_sp.add_argument("name")
    p_sp.add_argument("--date", help="one UTC day, YYYY-MM-DD, in the last 30")
    p_sp.add_argument("--json", action="store_true")
    p_li = sub.add_parser("limit", help="show or move a key's USD ceiling; for a key issued by a spend policy, "
                          "raising it can take it past its share of the project limit until the next "
                          "issue-for-policy run")
    p_li.add_argument("name")
    p_li.add_argument("--set", type=float, dest="set_to")
    p_li.add_argument("--reset", choices=RESETS, default=None,
                      help="with --set: the new period; without it the key keeps its own")
    p_di = sub.add_parser("disable", help="stop a key spending, reversibly")
    p_di.add_argument("name")
    p_en = sub.add_parser("enable", help="let a disabled key spend again")
    p_en.add_argument("name")
    p_ro = sub.add_parser("rotate", help="create successor, deliver, delete predecessor. While it runs the slot "
                          "holds two live keys (the one window past a spend policy's limit). Until the J-3 change "
                          "counts them, a rotate that FAILS can leave the successor NAME-next live and untracked, "
                          "or the predecessor live and uncounted (the row then says rotation_status "
                          "successor-delivered-predecessor-pending with its predecessor_hash): delete at the "
                          "provider whichever of the two the slot does not hold, then run issue-for-policy, which "
                          "names any total still past the limit")
    p_ro.add_argument("name", nargs="?")
    p_ro.add_argument("--leaked", action="store_true",
                      help="rotate every issued key named in an open leak")
    p_re = sub.add_parser("revoke", help="delete at the provider, clear the ledger row")
    p_re.add_argument("name")
    p_re.add_argument("--forget-pending", action="store_true",
                      help="clear a pending row with no hash without reading the provider — only once the key "
                      "called NAME is deleted there or was never minted (an account listing past PAGES_MAX)")
    p_re.add_argument("--reason", help="with --forget-pending: what makes the key gone; journaled")
    p_cl = sub.add_parser("claim", help="bring a key minted elsewhere under this door; value on stdin")
    p_cl.add_argument("name", help="what the ledger will call it")
    p_cl.add_argument("--account", help="the stashed account to look in (default: every one)")
    p_cl.add_argument("--where", default="", help="where the copy was found: a path, never a value")
    a = ap.parse_args()
    if a.cmd == "stash":
        if sys.stdin.isatty():
            print("paste the provisioning key on stdin", file=sys.stderr)
            return 2
        return stash_value(sys.stdin.read().strip(), a.label, origin="stdin")
    if a.cmd == "adopt":
        return cmd_adopt(a.label)
    if a.cmd == "issue":
        return cmd_issue(a.name, a.limit, a.account, a.to, a.project, a.reset)
    if a.cmd == "issue-for-policy":
        return cmd_issue_for_policy(a.policy, a.account, a.limit, a.dry_run, a.split)
    if a.cmd == "list":
        return cmd_list()
    if a.cmd == "ping":
        return cmd_ping()
    if a.cmd == "spend":
        try:
            return cmd_spend(a.name, a.date, a.json)
        except (LookupError, ValueError) as exc:
            print(str(exc).strip("'\""), file=sys.stderr)
            return 2
        except RuntimeError as exc:
            # A refused or unreadable report is said as such — never a $0.
            print(f"{a.name}: the activity report is unavailable, no figure: {exc}",
                  file=sys.stderr)
            return 1
    if a.cmd == "limit":
        return cmd_limit(a.name, a.set_to, a.reset)
    if a.cmd == "disable":
        return cmd_toggle(a.name, True)
    if a.cmd == "enable":
        return cmd_toggle(a.name, False)
    if a.cmd == "rotate":
        return cmd_rotate(a.name, a.leaked)
    if a.cmd == "revoke":
        return cmd_revoke(a.name, a.forget_pending, a.reason)
    if a.cmd == "claim":
        return cmd_claim(a.name, a.account, a.where)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError):
        print("Private credential operation refused; values hidden", file=sys.stderr)
        raise SystemExit(2)
