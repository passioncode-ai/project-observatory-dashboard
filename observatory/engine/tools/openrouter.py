#!/usr/bin/env python3
"""The one door to OpenRouter keys: stash, issue, rotate, revoke, limits, ping.

    T="$(project-observatory full-path)/tools"            # the installed engine's tools
    python "$T/openrouter.py" stash --as main < key-file   # provisioning key, labeled; a protected file
    python "$T/openrouter.py" adopt --as main               # take the legacy file in
    python "$T/openrouter.py" issue --name my-agent --limit 10 [--reset daily] --to vault:PROJECT/prod/OPENROUTER_API_KEY
    python "$T/openrouter.py" issue-for-policy --policy FILE [--limit USD | --split REF=USD,…] [--dry-run]
    python "$T/openrouter.py" list | ping
    python "$T/openrouter.py" spend my-agent [--date YYYY-MM-DD] [--json]
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
import contextvars
import datetime
import decimal
import hashlib
import functools
import json
import math
import os
import pathlib
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import urllib.parse


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
             method: str | None = None, *, max_bytes: int | None = None) -> dict:
    """Endpoint and status in errors — never headers, never the body we sent."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{API}{path}", data=data, method=method or ("POST" if data else "GET"),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read() if max_bytes is None else r.read(max_bytes + 1)
            if max_bytes is not None and len(raw) > max_bytes:
                raise RuntimeError("Provider response exceeded the read limit; body withheld")
            return json.loads(raw)
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
    return doc


_REPAIR_PUBLICATION = contextvars.ContextVar("ceiling_repair_publication", default=False)


def save_ledger(doc: dict) -> None:
    if not _REPAIR_PUBLICATION.get():
        _guard_repair_publication(ledger(), doc)
    write_secret(LEDGER, json.dumps(doc, ensure_ascii=False, indent=2))


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
            response = _request(f"/keys/{rec['hash']}", admin)
            row = response.get("data") if isinstance(response, dict) else None
            if not isinstance(row, dict) or row.get("hash") != rec["hash"]:
                raise RuntimeError("Provider identity is unreadable or does not match the recorded key")
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


def _ledger_writer(function):
    @functools.wraps(function)
    def locked(*args, **kwargs):
        with _policy_lock():
            return function(*args, **kwargs)
    return locked


def issue_key(name: str, limit: float, account: str | None, to: str,
              project: str | None, reset: str = "monthly") -> dict:
    with _policy_lock():
        return _issue_key_locked(name, limit, account, to, project, reset)


def _issue_key_locked(name: str, limit: float, account: str | None, to: str,
              project: str | None, reset: str = "monthly", *, policy_binding: dict | None = None) -> dict:
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
    previous = doc["issued"].get(name) or {}
    has_binding = policy_binding is not None or "policy_binding" in previous
    binding = policy_binding if policy_binding is not None else previous.get("policy_binding")
    _require_resolved_rotation(previous)
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
    doc["issued"][name] = {"account": label, "hash": None, "destination": to, "project": project,
                           "limit_usd": limit, "limit_reset": reset, "issued_on": today(), "pending": True}
    if has_binding:
        doc["issued"][name]["policy_binding"] = binding
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
    if has_binding:
        doc["issued"][name]["policy_binding"] = binding
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
    _guard_ceiling_repair(doc["issued"].get(name) or {})
    doc["issued"].pop(name, None)
    save_ledger(doc)


def _undo_mint(admin: str, name: str, row: dict) -> None:
    """A minted key that was not delivered: delete it and drop its pending row; when the delete
    fails, keep the row marked `orphan`, so the key stays counted and named. Always raises."""
    _guard_ceiling_repair(ledger()["issued"].get(name) or {})
    _guard_ceiling_repair(row)
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
#: Close ceilings avoid automatic re-PATCH on every run. This is only mutation
#: tolerance: aggregate admission still counts every observed positive fraction.
SAME_CEILING_USD = 0.005
#: How long a run waits for another `issue-for-policy` run to finish before it refuses.
POLICY_LOCK_WAIT_S = 30.0


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
                    "held-ceiling": "the observed ceiling remains above the requested share; reconcile provider precision before retrying",
                    "blocked": "the project's other keys leave no room under the limit; the lines above say which",
                    "not-in-policy": "`revoke {name}` if nothing spends it, or name its slot in the policy",
                    "held-rotation": "reconcile the rotation identities and destination before another mutation; do not blindly retry",
                    "held-pending": PENDING_REMEDY}


@contextlib.contextmanager
def _policy_lock():
    """One ledger writer at a time, across processes: an exclusive lock on a file beside the
    ledger, held from the plan through the last provider write. Two runs at once each read the
    other's keys before either moved them, and opposite splits ended both keys high (round 3: $16
    against a $10 limit). A second run waits up to POLICY_LOCK_WAIT_S, then refuses."""
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
                    raise RuntimeError(f"another key writer has held the policy lock for "
                                       f"{POLICY_LOCK_WAIT_S:.0f} s; nothing was read or moved — run again "
                                       f"when it is done") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            oslocks.flock(fd, oslocks.LOCK_UN)
    finally:
        os.close(fd)


def _read_held(name: str, rec: dict, account: str | None) -> tuple[str | None, dict | None, str]:
    """A ledger row's key as the PROVIDER has it, read by hash (`find_key`), with the account the
    ledger recorded (else `--account`). Returns (state, row, why): state None for a live key,
    `held-disabled`, `held-pending` for a key minted whose delivery was never recorded (or whose
    minting is unknown: a pending row with no hash is not read at all), `held-missing` only for a
    key the provider answers 404 for — a key renamed in the provider's dashboard is still live and
    still found by its hash —, or `held-unread` for anything that could not be read (no stashed admin
    for the account, an error, an answer that is not JSON), in a dry run too. Unread is never in
    place, and never counted as gone."""
    try:
        _guard_ceiling_repair(rec)
    except RuntimeError:
        return "held-unread", None, "ceiling repair is unresolved; refresh its evidence before another mutation"
    if rec.get("pending") and not rec.get("hash"):
        return "held-pending", None, "whether its key was minted is unknown (no hash was recorded)"
    try:
        _label, admin = read_admin(rec.get("account") or account)
        row = find_key(admin, name)
    except (RuntimeError, LookupError, OSError, ValueError) as exc:
        return "held-unread", None, f"read failed ({type(exc).__name__})"
    if not row:
        return "held-missing", None, ""
    if not isinstance(row, dict) or (rec.get("hash") and row.get("hash") != rec["hash"]):
        return "held-unread", None, "provider identity does not match the ledger"
    if rec.get("rotation_status") not in (None, "complete") or rec.get("predecessor_hash") or rec.get("successor_hash"):
        return "held-rotation", row, "rotation is unresolved; reconcile both identities and destination before another mutation"
    if rec.get("pending"):
        return "held-pending", row, "minted, but its delivery was never recorded" + (
            " (its cleanup delete failed)" if rec.get("orphan") else "")
    if row.get("disabled"):
        return "held-disabled", row, ""
    return None, row, ""


def _ceiling_of(row: dict | None) -> int | float | None:
    limit = (row or {}).get("limit")
    if not isinstance(limit, (int, float)) or isinstance(limit, bool):
        return None
    # Keep integers exact and refuse unsupported observations before any float
    # conversion: an enormous JSON integer must not overflow or lose capacity.
    return limit if 0 <= limit <= MAX_USD and math.isfinite(limit) else None


def _cents_up(usd) -> int | None:
    """Round an observed ceiling UP without discarding positive fractions.

    A value such as 3.330000001 may be actual exposure; no provider precision
    contract proves it is noise. Counting it as 333 cents could authorize a mint
    beyond the strict policy limit. Missing, nonfinite or negative observations
    or unsupported observations are unknown (unbounded), rather than evidence
    of free capacity. The bound is the same MAX_USD supported by policy inputs.
    """
    if (not isinstance(usd, (int, float)) or isinstance(usd, bool)
            or not 0 <= usd <= MAX_USD or not math.isfinite(usd)):
        return None
    exact = decimal.Decimal(str(usd)) * 100
    return int(exact.to_integral_value(rounding=decimal.ROUND_CEILING))


def _counted(state: str | None, row: dict | None, rec: dict) -> int | None:
    """Cents a key the run will NOT move counts toward the project's total (None: unbounded): its
    provider ceiling rounded up, 0 for a confirmed missing key, and unknown for
    an unread provider identity. A saved ceiling is not a current upper bound
    because another writer can change it. A pending mint retains its recorded
    reservation, but without a current identity-bound row it also supplies no
    headroom. Pending delivery with a current row counts that observation.
    """
    if state == "held-missing":
        return 0
    if state == "held-unread" or (state == "held-pending" and row is None):
        return None
    if row is None:
        return None
    return _cents_up(_ceiling_of(row))


def _identity_refs(rec: dict) -> set[str]:
    return {value for field in ("hash", "predecessor_hash", "successor_hash")
            if isinstance(value := rec.get(field), str) and value}


def _require_resolved_rotation(rec: dict, *, repair_name: str | None = None) -> None:
    _guard_ceiling_repair(rec, allow_name=repair_name)
    if rec.get("rotation_status") not in (None, "complete") or rec.get("predecessor_hash") or rec.get("successor_hash"):
        raise RuntimeError("Unresolved rotation: reconcile provider identities and destination before another mutation")
    refs = _identity_refs(rec)
    if refs:
        for other in ledger().get("issued", {}).values():
            unresolved = (other.get("rotation_status") not in (None, "complete")
                          or other.get("predecessor_hash") or other.get("successor_hash"))
            if unresolved and refs & _identity_refs(other):
                raise RuntimeError("Identity belongs to an unresolved rotation; no alias mutation is allowed")


def _rotation_exposure(rec: dict) -> int | None:
    """Current extra exposure beyond rec.hash, or unknown; never a saved bound.

    Reservations remain in the ledger to represent uncertain effects. Only a
    current matching provider row (or confirmed 404) supplies admission evidence.
    """
    extra = []
    status = rec.get("rotation_status")
    if status not in (None, "complete", "mint-pending", "delivery-unknown",
                      "successor-delivered-predecessor-pending"):
        return None
    if ("predecessor_hash" in rec or "predecessor_limit_usd" in rec
            or status == "successor-delivered-predecessor-pending"):
        extra.append(rec.get("predecessor_hash"))
    if ("successor_hash" in rec or status in ("mint-pending", "delivery-unknown")
            or ("successor_limit_usd" in rec and status != "successor-delivered-predecessor-pending")):
        extra.append(rec.get("successor_hash"))
    total = 0
    for digest in extra:
        if not isinstance(digest, str) or not digest:
            return None
        if digest == rec.get("hash"):
            continue
        counted = None
        if digest:
            try:
                _, admin = read_admin(rec.get("account"))
                response = _request(f"/keys/{digest}", admin)
                row = response.get("data") if isinstance(response, dict) else None
                if isinstance(row, dict) and row.get("hash") == digest:
                    counted = _cents_up(_ceiling_of(row))
                # A malformed or foreign response cannot prove current capacity.
            except (OSError, ValueError, RuntimeError, LookupError) as exc:
                if isinstance(exc, RuntimeError) and "HTTP 404" in str(exc):
                    counted = 0
        if counted is None:
            return None
        total += counted
    return total


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


def _policy_provenance(policy: dict, folder: str, limit_cents: int | None) -> dict:
    """Historical caller-supplied policy evidence, never current headroom authority.

    Only an origin kind and exact digest are stored, never the source filename or
    arbitrary policy fields. A later policy application explicitly re-attests its
    named key rows; existing unrelated/legacy rows are not classified by a name.
    """
    try:
        raw = json.dumps(policy, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Policy cannot be bound to an exact canonical digest") from None
    return {"schema_version": 1, "origin": "issue-for-policy",
            "policy_sha256": hashlib.sha256(raw).hexdigest(), "project": folder,
            "mode": policy["mode"], "period": policy["period"],
            "limit_usd": None if limit_cents is None else _usd(limit_cents),
            "headroom": "not-verified"}


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
    * **A key that could not be read has unknown current exposure**, never saved headroom;
      one the provider answers 404 for counts 0; a renamed key is still live and is read.
    * **A pending row** (a key minted whose delivery was never recorded, or whose minting is
      unknown) holds its slot: nothing is minted there, the slot reads `held-pending` and the run
      exits 1 naming `revoke` or `claim` (PENDING_REMEDY).
    * **Lowerings first, raises only when they fit.** Ceilings that go down move first. Raises and
      new keys follow only when the project's total after the run fits the limit; otherwise they
      are `blocked`. Whenever the total the run leaves is past the limit, an `over-limit` line says
      by how much and what ends it, and the run exits 1.
    * **One run at a time** (`_policy_lock`), the ledger and the provider re-read under the lock.
    * The issuing account for a new key is resolved before anything moves, in a dry run too.

    Explicit policy provenance now prevents rotation without a current aggregate
    headroom proof. Unmarked legacy/standalone rotation still permits overlap.

    A failure in the middle stops the run and raises `PolicyPartial` carrying the plan: what was
    done, what failed, what was not reached. Completed exact targets are reused.
    Above-share observations remain explicitly held; unsupported provider precision
    does not promise convergence on another run.
    """
    plan = policy_key_plan(policy, limit_override, split)
    limit_cents = _policy_limit_cents(policy)
    slots = [step for step in plan if "to" in step]
    folder = slots[0]["project"]
    binding = _policy_provenance(policy, folder, limit_cents)
    binding["recorded_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with _policy_lock():
        issued = ledger().get("issued", {})
        scope = {n: r for n, r in sorted(issued.items())
                 if str(r.get("destination") or "").startswith(f"vault:{folder}/")}
        primary: dict[str, str] = {}
        for step in slots:
            holders = [n for n, r in scope.items() if r.get("destination") == step["to"]]
            if holders:
                primary[step["to"]] = step["name"] if step["name"] in holders else holders[0]
        if not dry_run:
            # Exact named sources, not every row sharing a project/folder. Save
            # provenance before any provider write, including an in-place row.
            doc = ledger()
            changed = False
            for selected in primary.values():
                _guard_ceiling_repair(doc["issued"][selected])
            for selected in primary.values():
                rec = doc["issued"][selected]
                prior = rec.get("policy_binding")
                same = isinstance(prior, dict) and all(prior.get(k) == v for k, v in binding.items()
                                                      if k != "recorded_at")
                if not same:
                    rec["policy_binding"] = dict(binding)
                    changed = True
            if changed:
                save_ledger(doc)
        if len(primary) < len(slots):
            read_admin(account)         # the account a new key is minted from, resolved before anything moves
        after, end = _Total(), _Total()  # the total if every planned write happens / what the run leaves
        work, settle, near_targets = [], [], []
        for rotation_name, rotation_rec in scope.items():
            extra = _rotation_exposure(rotation_rec)
            if extra != 0:
                after.add(extra)
                end.add(extra)
                settle.append(f"reconcile rotation {rotation_name} at the provider and destination; do not blindly retry")
                plan.append({"ref": rotation_rec.get("destination"), "name": rotation_name,
                             "action": "held-rotation-exposure", "counted": None if extra is None else _usd(extra),
                             "why": "Additional rotation exposure retained until provider identity and destination are reconciled"})
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
            if state in ("held-missing", "held-unread", "held-pending", "held-rotation"):
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
                    step["action"] = "held-ceiling"
                    step["observed_limit"] = current
                    step["why"] = "The provider ceiling remains above the requested share; no automatic retry"
                    settle.append(f"limit {name} --set {step['limit']:g}")
                    near_targets.append((step, name, held, current, state))
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
        if (not after.unbounded and after.over(limit_cents)
                and any(order == 1 for order, *_ in work)):
            # Close-value no-ops must not strand otherwise admissible slots.
            # Reapply only the caller's existing share, once, through the same
            # fresh-bound and actual-response guards as every other decrease.
            # If admission already fits, retain the no-op tolerance.
            for step, name, held, current, state in near_targets:
                planned = _cents(step["limit"], "a planned ceiling")
                after.add(planned - held)
                end.add(-held)
                step["previous"], step["held"] = current, state
                step.pop("why", None)
                step.pop("observed_limit", None)
                work.append((0, step, name, held))
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
                    observed = _set_limit_locked(name, step["limit"], step["reset"],
                                      policy_ceiling_cents=_cents(step["limit"], "a planned ceiling"),
                                      policy_lowering=order == 0)
                    # Every confirmed delta replaces the projected request. A
                    # larger-than-requested but non-increasing observation can
                    # progress safe peers; it never creates fictional headroom.
                    actual = _cents_up(observed["limit"])
                    delta = actual - _cents(step["limit"], "a planned ceiling")
                    after.add(delta)
                    end.add(delta)
                    room = not after.over(limit_cents)
                    step["observed_limit"] = observed["limit"]
                else:
                    _issue_key_locked(step["name"], step["limit"], account, step["to"], step["project"], step["reset"],
                                      policy_binding=dict(binding))
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
            if moving and actual > _cents(step["limit"], "a planned ceiling"):
                step["action"] = "held-ceiling"
                step["why"] = (f"The provider reported ${observed['limit']:g}; the requested share is unmet; "
                               "actual exposure counts before any increase")
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
    display_total = _Total()
    for step in plan:
        if step.get("action") == "over-limit":
            not_serving = True
            print(step["why"])
            continue
        if step.get("action") == "held-rotation-exposure":
            not_serving = True
            total += step["counted"] if step["counted"] is not None else math.inf
            display_total.add(_cents_up(step["counted"]))
            print(f"{step['ref']}: held-rotation-exposure — {step['why']}")
            continue
        if step.get("action") == "not-in-policy":
            not_serving = True
            total += step["counted"] if step["counted"] is not None else math.inf
            display_total.add(_cents_up(step["counted"]))
            print(f"{step['ref']}: not-in-policy {step['name']!r} — {step['why']}")
            print("  " + HELD_NOT_SERVING["not-in-policy"].format(name=step["name"]))
            continue
        if "to" not in step:
            print(f"{step['ref']}: skipped — {step['why']}")
            continue
        total += step.get("observed_limit", step["limit"])
        display_total.add(_cents_up(step.get("observed_limit", step["limit"])))
        extra = ((f" (was ${step['previous']:g})" if step["previous"] is not None else " (was unbounded)")
                 if "previous" in step else "")
        why = f" — {step['why']}" if step.get("why") else ""
        print(f"{step['ref']}: {step['action']} {step['name']!r}, ceiling ${step['limit']:g} {step['reset']}{extra}{why}")
        if step["action"] in HELD_NOT_SERVING:
            not_serving = True
            print(f"  the slot is not served as planned: " + HELD_NOT_SERVING[step["action"]].format(name=step["name"]))
    has_observed = any("observed_limit" in step for step in plan)
    total_basis = (" in all (observed ceilings rounded up; other amounts planned)"
                   if has_observed else " in all as planned")
    shown_total = display_total.text() if has_observed else ("unbounded" if math.isinf(total) else f"${round(total, 2):g}")
    print("ceilings: " + shown_total + total_basis + (f", project limit ${policy['limitUsd']:g}"
                                                                 if policy.get("mode") == "limited"
                                                                 else ", no project limit (L0 binds)"))
    # The meter's L0 per key (docs/design/METER.md M4) is configured, not read from the provider: once a
    # ceiling moves here, `keyCeilingsUsd` in config/meter.json must say the same, by the policy's ref.
    implied = {step["ref"]: step.get("observed_limit", step["limit"]) for step in plan if "to" in step}
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


REPAIR_RESOLVED = {"observed-satisfied", "observed-unmet", "superseded"}
REPAIR_PENDING = {"intent", "effect-unknown", "observation-pending", "observation-unknown"}


def _repair_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _repair_context(rec: dict) -> dict:
    context = {k: rec.get(k) for k in ("account", "hash", "destination", "limit_reset")}
    if (any(not isinstance(context[k], str) or not context[k] for k in context)
            or not re.fullmatch(r"[a-fA-F0-9]{64}", context["hash"])
            or context["limit_reset"] not in RESETS):
        raise RuntimeError("Ceiling repair identity context is incomplete")
    binding = rec.get("policy_binding")
    if "policy_binding" in rec and not isinstance(binding, dict):
        raise RuntimeError("Ceiling repair policy context is malformed")
    try:
        raw = json.dumps(binding, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(raw) > 16384: raise ValueError
    except (TypeError, ValueError, RecursionError):
        raise RuntimeError("Ceiling repair policy context is malformed") from None
    context["policy_digest"] = hashlib.sha256(raw.encode()).hexdigest()
    return context


def _repair_digest(context: dict) -> str:
    return hashlib.sha256(json.dumps(context, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _repair_stamp(value) -> bool:
    try:
        stamp = datetime.datetime.fromisoformat(value)
        return stamp.tzinfo is not None and stamp <= datetime.datetime.now(datetime.timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return False


def _validate_repair(rec: dict) -> dict | None:
    if "ceiling_repair" not in rec: return None
    proof = rec["ceiling_repair"]
    try:
        if (not isinstance(proof, dict) or type(proof.get("schema_version")) is not int
                or proof["schema_version"] != 1
                or proof.get("status") not in REPAIR_RESOLVED | REPAIR_PENDING
                or not isinstance(proof.get("attempt_id"), str)
                or not re.fullmatch(r"[a-f0-9]{32}", proof["attempt_id"])
                or not _repair_stamp(proof.get("attempted_at"))
                or not isinstance(proof.get("context"), dict)
                or proof.get("context_digest") != _repair_digest(proof["context"])
                or _cents_up(proof.get("target")) is None or proof["target"] <= 0
                or _cents_up(proof.get("before")) is None or proof["before"] <= proof["target"]):
            raise ValueError
        effect = proof.get("effect")
        if not isinstance(effect, dict) or effect.get("status") not in ("unconfirmed", "observed"):
            raise ValueError
        if effect["status"] == "unconfirmed":
            if effect.get("actual") is not None or effect.get("observed_at") is not None: raise ValueError
        elif (_cents_up(effect.get("actual")) is None or not 0 < effect["actual"] <= proof["before"]
              or not _repair_stamp(effect.get("observed_at"))):
            raise ValueError
        context = proof["context"]
        if (set(context) != {"account", "hash", "destination", "limit_reset", "policy_digest"}
                or any(not isinstance(v, str) or not v for v in context.values())
                or not re.fullmatch(r"[a-fA-F0-9]{64}", context["hash"])
                or not re.fullmatch(r"[a-f0-9]{64}", context["policy_digest"])
                or context["limit_reset"] not in RESETS):
            raise ValueError
        if proof["status"] == "superseded":
            if (not _repair_stamp(proof.get("superseded_at"))
                    or proof.get("superseded_context") != _repair_context(rec)
                    or proof.get("basis") != "historical-context"
                    or not isinstance(proof.get("previous_observation"), dict)):
                raise ValueError
        elif proof["context"] != _repair_context(rec):
            raise ValueError
        if proof["status"] in ("observed-satisfied", "observed-unmet"):
            if (_cents_up(proof.get("actual")) is None or proof["actual"] <= 0
                    or not _repair_stamp(proof.get("observed_at"))
                    or proof.get("basis") not in ("same-operation-response", "subsequent-current-read")
                    or proof.get("reason") is not None
                    or (proof["status"] == "observed-satisfied") != (proof["actual"] <= proof["target"])
                    or (proof["basis"] == "same-operation-response" and proof["actual"] > proof["before"])
                    or datetime.datetime.fromisoformat(proof["observed_at"]) < datetime.datetime.fromisoformat(proof["attempted_at"])):
                raise ValueError
        elif proof.get("actual") is not None or proof.get("observed_at") is not None:
            raise ValueError
        prior = proof.get("previous_observation")
        if prior is not None and (not isinstance(prior, dict)
                or _cents_up(prior.get("actual")) is None or prior["actual"] <= 0
                or not _repair_stamp(prior.get("observed_at"))
                or prior.get("basis") not in ("same-operation-response", "subsequent-current-read")):
            raise ValueError
    except (TypeError, ValueError, RuntimeError, RecursionError):
        raise RuntimeError("Ceiling repair evidence is malformed or its identity context changed; no mutation is allowed") from None
    return proof


def _repair_refs(rec: dict) -> set[str]:
    refs = _identity_refs(rec)
    proof = rec.get("ceiling_repair")
    context = proof.get("context") if isinstance(proof, dict) else None
    if isinstance(context, dict) and isinstance(context.get("hash"), str): refs.add(context["hash"])
    return refs


def _guard_ceiling_repair(rec: dict, *, allow_name: str | None = None) -> None:
    proof = _validate_repair(rec)
    if proof and proof["status"] not in REPAIR_RESOLVED and allow_name is None:
        raise RuntimeError("Ceiling repair is unresolved; refresh its status before another mutation")
    refs = _repair_refs(rec)
    for name, other in ledger()["issued"].items():
        if not isinstance(other, dict) or not refs.intersection(_repair_refs(other)): continue
        held = _validate_repair(other)
        if held and held["status"] not in REPAIR_RESOLVED and name != allow_name:
            raise RuntimeError("Identity belongs to an unresolved ceiling repair; no alias mutation is allowed")


def _guard_repair_publication(before: dict, after: dict) -> None:
    """Check old evidence before any local identity/provenance publication."""
    old, new = before["issued"], after["issued"]
    changed = {n for n in old.keys() | new.keys() if old.get(n) != new.get(n)}
    changed_refs = set()
    for name in changed:
        for rec in (old.get(name), new.get(name)):
            if isinstance(rec, dict): changed_refs.update(_repair_refs(rec))
    for name, rec in old.items():
        if not isinstance(rec, dict) or "ceiling_repair" not in rec: continue
        if name not in changed and not changed_refs.intersection(_repair_refs(rec)): continue
        proof = _validate_repair(rec)
        if proof["status"] not in REPAIR_RESOLVED:
            raise RuntimeError("Ceiling repair blocks local identity or policy publication")
        target = new.get(name)
        if name in changed and target is not None and target.get("ceiling_repair") != proof:
            raise RuntimeError("Ceiling repair evidence may only change through its repair door")
        if name in changed and (target is None or _repair_context(target) != _repair_context(rec)):
            _journal("ceiling-repair-superseded", f"openrouter/{rec['account']}/{name}",
                     require_success=True, attempt_id=proof["attempt_id"])
            if target is not None:
                stale = dict(proof, status="superseded", actual=None, observed_at=None,
                             superseded_at=_repair_now(), basis="historical-context")
                stale["previous_observation"] = (proof["previous_observation"] if proof["status"] == "superseded"
                                                  else {k: proof.get(k) for k in ("actual", "observed_at", "basis")})
                stale["superseded_context"] = _repair_context(target)
                target["ceiling_repair"] = stale
    for name in changed:
        if (isinstance(new.get(name), dict) and "ceiling_repair" in new[name]
                and "ceiling_repair" not in (old.get(name) or {})):
            raise RuntimeError("New ceiling repair evidence requires its repair door")


def _persist_repair(doc: dict, name: str) -> None:
    # Recheck the publication context after provider calls too. This is a local
    # precheck under the writer lock, not a CAS against uncooperative writers.
    current = ledger()
    old, new = current["issued"], doc["issued"]
    if (set(current) != set(doc) or any(current[k] != doc[k] for k in current if k != "issued")
            or set(old) != set(new) or any(old[k] != new[k] for k in old if k != name)):
        raise RuntimeError("Ledger changed during ceiling repair; no observation was published")
    before, after = old.get(name), new.get(name)
    if (not isinstance(before, dict) or not isinstance(after, dict)
            or {k: v for k, v in before.items() if k not in ("ceiling_repair", "limit_usd")}
               != {k: v for k, v in after.items() if k not in ("ceiling_repair", "limit_usd")}):
        raise RuntimeError("Identity context changed during ceiling repair; no observation was published")
    prior, proposed = _validate_repair(before), _validate_repair(after)
    if proposed is None:
        raise RuntimeError("Ceiling repair publication requires evidence")
    if proposed["status"] == "intent":
        if prior and prior["status"] not in REPAIR_RESOLVED:
            raise RuntimeError("Another ceiling repair is unresolved")
    elif (not prior or prior["attempt_id"] != proposed["attempt_id"]
          or prior["status"] == "superseded"
          or (proposed["status"] != "observation-pending" and prior["status"] not in ("intent", "observation-pending"))):
        raise RuntimeError("Ceiling repair attempt changed before publication")
    token = _REPAIR_PUBLICATION.set(True)
    try: save_ledger(doc)
    finally: _REPAIR_PUBLICATION.reset(token)


def _repair_record(name: str) -> tuple[dict, dict]:
    doc = ledger(); rec = doc["issued"].get(name)
    if not isinstance(rec, dict): raise LookupError("Ceiling repair requires an exact issued name")
    _repair_context(rec)
    if rec.get("pending") or rec.get("orphan"):
        raise RuntimeError("Issuance is unresolved; no ceiling repair is allowed")
    return doc, rec


def _repair_observation(row: dict | None, rec: dict) -> int | float:
    amount = _ceiling_of(row) if isinstance(row, dict) else None
    if (not isinstance(row, dict) or row.get("hash") != rec["hash"]
            or row.get("limit_reset") != rec["limit_reset"] or amount is None or amount <= 0):
        raise RuntimeError("Ceiling repair observation is unknown")
    return amount


def _repair_result(doc: dict, name: str, proof: dict, actual: int | float, basis: str) -> dict:
    result = dict(proof, status="observed-satisfied" if actual <= proof["target"] else "observed-unmet",
                  actual=actual, observed_at=_repair_now(), basis=basis, reason=None)
    if basis == "same-operation-response":
        result["effect"] = {"status": "observed", "actual": actual, "observed_at": result["observed_at"]}
    rec = doc["issued"][name]
    _journal("ceiling-repair-observed", f"openrouter/{rec['account']}/{name}",
             require_success=True, attempt_id=proof["attempt_id"], actual=actual, basis=basis)
    rec["ceiling_repair"], rec["limit_usd"] = result, actual
    _persist_repair(doc, name)
    return result


def _repair_unknown(doc: dict, name: str, proof: dict, status: str, reason: str) -> dict:
    result = dict(proof, status=status, actual=None, observed_at=None, reason=reason)
    doc["issued"][name]["ceiling_repair"] = result
    _persist_repair(doc, name)
    return result


def decrease_limit(name: str, limit: float) -> dict:
    """One explicit observed decrease. The local lock cannot serialize external writers."""
    if _cents_up(limit) is None or limit <= 0: raise ValueError("A supported positive ceiling is required")
    with _policy_lock():
        doc, rec = _repair_record(name)
        _require_resolved_rotation(rec)
        context = _repair_context(rec)
        _, admin = read_admin(rec["account"])
        row = find_key(admin, name)
        before = _repair_observation(row, rec)
        if limit > before: raise RuntimeError("Requested repair is no longer a decrease; no limit was changed")
        if limit == before:
            return {"status": "observed-satisfied", "actual": before, "observed_at": _repair_now(),
                    "basis": "subsequent-current-read", "target": limit}
        proof = {"schema_version": 1, "attempt_id": os.urandom(16).hex(), "status": "intent",
                 "attempted_at": _repair_now(), "context": context, "context_digest": _repair_digest(context),
                 "target": limit, "before": before, "actual": None, "observed_at": None,
                 "basis": "not-observed", "reason": "effect-not-observed",
                 "effect": {"status": "unconfirmed", "actual": None, "observed_at": None}}
        rec["ceiling_repair"] = proof
        _persist_repair(doc, name)
        _journal("ceiling-repair-intent", f"openrouter/{rec['account']}/{name}", require_success=True,
                 attempt_id=proof["attempt_id"], target=limit, before=before)
        try:
            response = _request(f"/keys/{rec['hash']}", admin,
                                {"limit": limit, "limit_reset": rec["limit_reset"]}, method="PATCH")
            actual = _repair_observation(response.get("data") if isinstance(response, dict) else None, rec)
            if actual > before: raise RuntimeError("Ceiling repair response exceeded the fresh bound")
        except (OSError, ValueError, RuntimeError, LookupError, subprocess.SubprocessError):
            return _repair_unknown(doc, name, proof, "effect-unknown", "provider-effect-unconfirmed")
        return _repair_result(doc, name, proof, actual, "same-operation-response")


def repair_status(name: str, *, refresh: bool = False) -> dict:
    with _policy_lock():
        doc, rec = _repair_record(name)
        proof = _validate_repair(rec)
        if proof is None: raise LookupError("No ceiling repair is recorded for this issued name")
        if not refresh: return dict(proof)
        if proof["status"] == "superseded":
            raise RuntimeError("Repair belongs to a superseded context; start a new explicit repair")
        _require_resolved_rotation(rec, repair_name=name)
        previous = ({k: proof.get(k) for k in ("actual", "observed_at", "basis")}
                    if proof.get("actual") is not None else proof.get("previous_observation"))
        pending = dict(proof, status="observation-pending", attempted_at=_repair_now(), actual=None,
                       observed_at=None, reason="not-observed-this-attempt", basis="not-observed")
        if previous: pending["previous_observation"] = previous
        rec["ceiling_repair"] = pending
        _persist_repair(doc, name)
        _journal("ceiling-repair-refresh", f"openrouter/{rec['account']}/{name}", require_success=True,
                 attempt_id=proof["attempt_id"])
        try:
            _, admin = read_admin(rec["account"])
            actual = _repair_observation(find_key(admin, name), rec)
        except (OSError, ValueError, RuntimeError, LookupError, subprocess.SubprocessError):
            return _repair_unknown(doc, name, pending, "observation-unknown", "provider-read-unavailable")
        return _repair_result(doc, name, pending, actual, "subsequent-current-read")


def set_limit(name_or_label: str, limit: float, reset: str | None = None) -> dict:
    with _policy_lock():
        return _set_limit_locked(name_or_label, limit, reset)


def _set_limit_locked(name_or_label: str, limit: float, reset: str | None = None, *,
                      policy_ceiling_cents: int | None = None, policy_lowering: bool = False) -> dict:
    """Moves a key's ceiling. Without a `reset` the key keeps the period it has: moving a daily
    ceiling never turns it into a monthly one behind the caller's back."""
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError("a positive ceiling is required")
    name, rec = resolve_issued(name_or_label)
    _require_resolved_rotation(rec)
    reset = _reset(reset if reset is not None else (rec.get("limit_reset") or "monthly"))
    label, admin = read_admin(rec["account"])
    row = find_key(admin, name)
    if not row:
        raise LookupError(f"the ledger records {name!r} but {label!r} holds no such key — "
                          f"it was deleted at the provider; `revoke {name}` clears the row")
    if policy_ceiling_cents is not None and (not rec.get("hash") or row.get("hash") != rec["hash"]):
        raise RuntimeError("Policy ceiling identity is unresolved; no limit was changed")
    before_ceiling = _ceiling_of(row) if policy_ceiling_cents is not None else None
    if (policy_ceiling_cents is not None and before_ceiling is None
            and not ("limit" in row and row["limit"] is None)):
        raise RuntimeError("Policy fresh ceiling is unknown; no limit was changed")
    if policy_lowering and before_ceiling is not None and limit > before_ceiling:
        raise RuntimeError("Policy lowering is no longer non-increasing; refresh before changing the limit")
    response = _request(f"/keys/{row['hash']}", admin, {"limit": limit, "limit_reset": reset},
                        method="PATCH")
    confirmed_limit = limit
    if policy_ceiling_cents is not None:
        # Confirm the same-operation observation, not the requested amount.
        # A complete non-increasing observation may exceed the request; the
        # caller counts it and retains an unmet-share outcome. Unknown effects
        # and raises beyond the admitted request still stop without retry.
        current = response.get("data") if isinstance(response, dict) else None
        observed = _ceiling_of(current) if isinstance(current, dict) else None
        if (not isinstance(current, dict) or current.get("hash") != rec["hash"]
                or observed is None or observed <= 0
                or (_cents_up(observed) > policy_ceiling_cents
                    and not (policy_lowering and before_ceiling is not None
                             and observed <= before_ceiling))
                or current.get("limit_reset") != reset):
            raise RuntimeError("Policy ceiling effect is unconfirmed; the provider may have changed; refresh its evidence before further increases")
        confirmed_limit = observed
    doc = ledger()
    before = doc["issued"][name].get("limit_usd")
    doc["issued"][name]["limit_usd"] = confirmed_limit
    doc["issued"][name]["limit_reset"] = reset
    save_ledger(doc)
    # A ceiling is what a key may spend: moving it is a movement like an issue or a
    # rotation, and it went unrecorded (2026-10-06). Here, not in `cmd_limit`, so the
    # keyserver's live action records it too.
    _journal("limit", f"openrouter/{label}/{name}", limit_usd=confirmed_limit, previous_usd=before,
             limit_reset=reset)
    return {"name": name, "label": row.get("label"), "limit": confirmed_limit, "limit_reset": reset}


def cmd_limit(name: str, set_to: float | None, reset: str | None = None, *,
              decrease_only: bool = False, repair_status_only: bool = False, refresh: bool = False) -> int:
    if decrease_only or repair_status_only or refresh:
        if (reset is not None or (refresh and not repair_status_only)
                or (decrease_only and (set_to is None or repair_status_only))
                or (repair_status_only and set_to is not None)):
            print("refused: use --decrease-only with --set, or --repair-status with optional --refresh", file=sys.stderr)
            return 2
        try:
            result = decrease_limit(name, set_to) if decrease_only else repair_status(name, refresh=refresh)
        except (ValueError, LookupError, RuntimeError):
            print("Ceiling repair refused; inspect its status and reconcile identity or unresolved evidence before another mutation", file=sys.stderr)
            return 1
        actual = "unknown" if result.get("actual") is None else f"${result['actual']!s}"
        status, time = result['status'], result.get('observed_at') or 'unknown'
        print(f"{name}: recorded {status}; observed ceiling {actual}; observed at {time}")
        return 0 if result['status'] == 'observed-satisfied' else 1
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


@_ledger_writer
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
    _require_resolved_rotation(rec)
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
    _guard_ceiling_repair(row)
    successor = row.get("hash")
    if not successor:
        raise RuntimeError("Partial failure: delivery failed and successor identity is unavailable; inspect provider keys. Predecessor was not revoked")
    try:
        _request(f"/keys/{successor}", admin, method="DELETE")
    except (OSError, ValueError, RuntimeError):
        raise RuntimeError("Partial failure: delivery failed and successor cleanup failed; inspect provider keys. Predecessor was not revoked") from None
    raise RuntimeError("Delivery failed; successor deleted. Predecessor was not revoked") from None


def rotate_one(name: str, doc: dict | None = None) -> dict:
    """Persist each uncertain transition before doing it. Never repeat an unknown mint.

    `doc` remains accepted for callers, but is never trusted across the writer lock.
    A delivery timeout can already have changed the vault: neither key is deleted
    until delivery was confirmed. No credential value is written to this receipt.
    """
    with _policy_lock():
        try:
            return _rotate_one_locked(name)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"Rotation incomplete ({type(exc).__name__}); inspect its ledger phase before retrying") from None


def _rotate_one_locked(name: str) -> dict:
    doc = ledger()
    rec = doc["issued"].get(name)
    if not rec:
        raise LookupError("not in the ledger")
    identities = _identity_refs(rec)
    bound_alias = any(isinstance(peer, dict) and "policy_binding" in peer
                      and identities.intersection(_identity_refs(peer))
                      for peer in doc["issued"].values())
    if "policy_binding" in rec or bound_alias:
        # Exact hash aliases share the restriction, including reserved identities.
        # Account names are local aliases, not provider ownership proofs; changing
        # a local account/display name cannot bypass a key's policy provenance.
        # Presence refuses even if corrupted/stale; it is not headroom proof.
        raise RuntimeError("Rotation refused: policy-managed key has no verified current aggregate headroom; its policy snapshot is not authorization to mint overlap. No provider call or ledger change was made")
    if rec.get("claimed"):
        raise ValueError("claimed key has no successor destination; issue its replacement before revocation")
    if rec.get("rotation_status") not in (None, "complete") or rec.get("predecessor_hash") or rec.get("successor_hash"):
        raise RuntimeError("Previous rotation outcome is unresolved; reconcile the provider identities and destination before another rotation; no new key was minted")
    _require_resolved_rotation(rec)
    rotations = rec.get("rotations", 0)
    if type(rotations) is not int or rotations < 0:
        raise ValueError("Invalid rotation count; repair ledger before rotating")
    private_io.check(LEDGER)
    label, admin = read_admin(rec["account"])
    old = find_key(admin, name)
    if not isinstance(old, dict) or not isinstance(old.get("hash"), str) or not old["hash"]:
        raise ValueError("Predecessor identity could not be established; nothing was minted")
    if not isinstance(rec.get("hash"), str) or old["hash"] != rec["hash"]:
        raise RuntimeError("Predecessor identity does not match the ledger; nothing was minted or changed")
    limit = old.get("limit")
    if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(limit) or limit <= 0:
        raise ValueError("A finite positive provider ceiling is required; nothing was minted")
    reset = old.get("limit_reset")
    if reset not in (None, "daily", "weekly", "monthly"):
        raise ValueError("Unknown provider reset period; nothing was minted")
    # A lost POST response still reserves this exposure. This write precedes POST.
    rec.update(hash=old["hash"], limit_usd=limit, limit_reset=reset,
               rotation_status="mint-pending", successor_limit_usd=limit)
    save_ledger(doc)
    try:
        d = _request("/keys", admin, {"name": f"{name}-next", "limit": limit, "limit_reset": reset})
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        raise RuntimeError("Successor creation outcome unknown; its reservation remains; reconcile at the provider before any retry") from None
    new_row = d.get("data") if isinstance(d, dict) else None
    value = d.get("key") if isinstance(d, dict) else None
    if not isinstance(new_row, dict) or not isinstance(new_row.get("hash"), str) or not new_row["hash"] or new_row["hash"] == old["hash"]:
        raise RuntimeError("Successor identity unknown; its reservation remains; reconcile at the provider before any retry")
    rec.update(successor_hash=new_row["hash"], rotation_status="delivery-unknown")
    save_ledger(doc)  # if interrupted next, both identities remain represented
    if not isinstance(value, str) or not value:
        raise RuntimeError("Successor value unavailable; delivery unconfirmed; reconcile the recorded identities before retrying")
    if hashlib.sha256(value.encode("utf-8")).hexdigest() != new_row["hash"]:
        raise RuntimeError("Successor value identity does not match its provider receipt; no delivery or revocation attempted; reconcile before retrying")
    try:
        place = deliver(value, rec["destination"], rotate=True)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        raise RuntimeError("Successor delivery outcome unknown; both keys retained; verify the destination before reconciliation") from None
    rec.update(hash=new_row["hash"], delivered_to=place, limit_usd=limit, limit_reset=reset,
               rotated_on=today(), rotations=rotations + 1,
               rotation_status="successor-delivered-predecessor-pending",
               predecessor_hash=old["hash"], predecessor_limit_usd=limit)
    rec.pop("successor_hash", None)
    save_ledger(doc)
    try:
        _request(f"/keys/{old['hash']}", admin, method="DELETE")
        _request(f"/keys/{new_row['hash']}", admin, {"name": name}, method="PATCH")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        raise RuntimeError("Rotation finalization outcome unknown; successor delivered and predecessor exposure retained; reconcile before retrying") from None
    rec["rotation_status"] = "complete"
    for field in ("predecessor_hash", "predecessor_limit_usd", "successor_limit_usd", "pending", "orphan"):
        rec.pop(field, None)
    save_ledger(doc)
    _journal("rotate", f"openrouter/{label}/{name}", to=rec["destination"])
    return {"name": name, "delivered_to": place, "rotations": rec["rotations"], "rotated_on": rec["rotated_on"]}


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
            r = rotate_one(n, doc)
            print(f"  {n}: rotated, successor delivered to {r['delivered_to']}, predecessor deleted")
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


@_ledger_writer
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
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    _guard_ceiling_repair({"hash": digest})
    if any(digest in _identity_refs(r) for r in doc["issued"].values()):
        raise ValueError("this key is already in the ledger")
    accounts = [(account, read_admin(account)[1])] if account else \
        [(lbl, read_admin(lbl)[1]) for lbl, _p in admins()]
    if not accounts:
        raise RuntimeError("no provisioning key is stashed; `stash --as <label>` first")
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
    if any(row.get("hash") in _identity_refs(r) for r in doc["issued"].values()):
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


@_ledger_writer
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
    _guard_ceiling_repair(rec)
    if not rec.get("pending") or rec.get("hash"):
        raise LookupError(f"{name!r} is not a pending row without a hash; `revoke {name}` deletes it by its hash")
    _journal("forget-pending-attested", f"openrouter/{rec.get('account')}/{name}",
             require_success=True, reason=reason, destination=rec.get("destination"),
             attestation_only=True)
    doc["issued"].pop(name)
    save_ledger(doc)
    _journal("forget-pending", f"openrouter/{rec.get('account')}/{name}", reason=reason,
             destination=rec.get("destination"))
    return {"name": name, "forgotten": True}


def _revoke_scan() -> tuple[dict, str]:
    """Read the collector's complete schema, bounded and fail closed.

    This proves only the saved OpenRouter destination survey, not all live vault
    or remote consumers. Its producer writes schema_version/scanned_at and the
    three collections even for a known-empty survey; absent fields are unknown.
    """
    path = paths.SCRATCH / "openrouter.json"
    maximum = 4 * 1024 * 1024
    try:
        with os.fdopen(osprivacy.open(path, os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.NONBLOCK), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
                raise ValueError
            raw = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
        binding = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        if len(raw) > maximum or binding(before) != binding(after) or binding(after) != binding(path.lstat()):
            raise ValueError
        doc = json.loads(raw)
        if not isinstance(doc, dict) or type(doc.get("schema_version")) is not int or doc["schema_version"] != 1:
            raise ValueError
        stamp = doc.get("scanned_at")
        if not isinstance(stamp, str):
            raise ValueError
        measured = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if measured.tzinfo is None:
            raise ValueError
        age = (datetime.datetime.now(datetime.timezone.utc) - measured).total_seconds()
        if not 0 <= age <= 36 * 3600:
            raise ValueError
        if doc.get("degraded") != []:
            raise ValueError
        keys, dests, hashes = (doc.get(k) for k in ("keys", "destinations", "destination_hashes"))
        if not isinstance(keys, list) or not isinstance(dests, dict) or not isinstance(hashes, dict):
            raise ValueError
        if len(keys) + len(dests) + len(hashes) > 10000:
            raise ValueError
        for row in keys:
            if not isinstance(row, dict) or not all(k in row for k in ("name", "label", "serves")):
                raise ValueError
            if any(row[k] is not None and not isinstance(row[k], str) for k in ("name", "label", "serves")):
                raise ValueError
        if any(not isinstance(k, str) or not k or not isinstance(v, str) or not v for k, v in dests.items()):
            raise ValueError
        for dest, held in hashes.items():
            if not isinstance(dest, str) or not dest or not isinstance(held, dict):
                raise ValueError
            if not isinstance(held.get("hash"), str) or not held["hash"]:
                raise ValueError
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        raise RuntimeError("Consumer evidence unavailable: OpenRouter scan is missing, unreadable, changed, malformed, degraded or outside its 36-hour window; refresh the scan before retrying; no key was revoked") from None
    return doc, hashlib.sha256(raw).hexdigest()


def _require_no_scan_holders(scan: dict, name: str, rec: dict, row: dict | None, alias: str) -> None:
    # The provider row is bound to rec.hash by find_key. Rotation can leave a
    # predecessor label in the legacy ledger: it is an extra refusal witness,
    # never a substitute for the current provider label. Labels stay in memory.
    row = row or {}
    hashes = {h for h in (rec.get("hash"), row.get("hash")) if isinstance(h, str) and h}
    labels = {v for v in (alias, rec.get("label"), row.get("label")) if isinstance(v, str) and v}
    names = {v for v in (name, rec.get("provider_name"), row.get("name")) if isinstance(v, str) and v}
    held = any(item["hash"] in hashes for item in scan["destination_hashes"].values())
    held = held or any(label in labels for label in scan["destinations"].values())
    held = held or any(k.get("serves") and (k.get("name") in names or k.get("label") in labels)
                       for k in scan["keys"])
    if held:
        raise ValueError("A known consumer is reading right now from this key; deliver its replacement and refresh the OpenRouter scan before revocation")


@_ledger_writer
def revoke_key(name_or_label: str) -> dict:
    """Refuse unknown consumer evidence and use current provider identity under lock.

    Saved source identity is rechecked after provider lookup. This bounds source
    drift across that read; it is not a transaction with external consumer writers.
    Full vault and remote-holder evidence remains separate, unimplemented coverage.
    """
    name, rec = resolve_issued(name_or_label)
    _require_resolved_rotation(rec)
    _scan, receipt = _revoke_scan()
    _label, admin = read_admin(rec["account"])
    row = find_key(admin, name)
    # Never let an unrelated name lookup supply the deletion identity.
    if row and (not rec.get("hash") or row.get("hash") != rec["hash"]):
        raise RuntimeError("Recorded key identity is unresolved; no key was revoked")
    if row and (not isinstance(row.get("label"), str) or not row["label"]):
        raise RuntimeError("Current provider consumer identity is unreadable; no key was revoked")
    current, current_receipt = _revoke_scan()
    if receipt != current_receipt:
        raise RuntimeError("Consumer evidence changed during provider lookup; refresh the scan before retrying; no key was revoked")
    _require_no_scan_holders(current, name, rec, row, name_or_label)
    if row:
        _request(f"/keys/{row['hash']}", admin, method="DELETE")
    doc = ledger()
    doc["issued"].pop(name, None)
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


ACTIVITY_MAX_BYTES = 4 * 1024 * 1024
ACTIVITY_MAX_ROWS = 10000
ACTIVITY_COUNTERS = ("requests", "prompt_tokens", "completion_tokens", "reasoning_tokens")


def _activity_date(value: str | None, *, provider: bool = False) -> str | None:
    if value is None and not provider: return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Activity date must be a completed UTC day in YYYY-MM-DD form")
    day = datetime.date.fromisoformat(value)
    current = datetime.datetime.now(datetime.timezone.utc).date()
    if not 1 <= (current - day).days <= 30:
        raise ValueError("Activity date must be one of the last 30 completed UTC days")
    return value


def read_activity(admin: str, key_hash: str, date: str | None = None) -> list[dict]:
    """One GET for one issued identity; reject malformed evidence without echoing it.

    The endpoint groups by endpoint, so the CLI folds its rows by day/model. Only
    finite provider usage is money evidence; missing counters remain unknown.
    Handles and the management credential travel to the provider, never output.
    """
    _activity_date(date)
    if not isinstance(key_hash, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", key_hash):
        raise ValueError("Issued key has no valid provider hash; repair its ledger metadata")
    query = {"api_key_hash": key_hash}
    if date is not None: query["date"] = date
    try:
        doc = _request("/activity?" + urllib.parse.urlencode(query), admin, max_bytes=ACTIVITY_MAX_BYTES)
        rows = doc.get("data") if isinstance(doc, dict) else None
        if not isinstance(rows, list) or len(rows) > ACTIVITY_MAX_ROWS:
            raise ValueError
        clean = []
        for row in rows:
            if not isinstance(row, dict): raise ValueError
            day = row.get("date")
            # Historical endpoint responses also use the exact UTC midnight form.
            if isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2} 00:00:00", day): day = day[:10]
            _activity_date(day, provider=True)
            if date is not None and day != date: raise ValueError
            model = row.get("model")
            if (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+~-]{0,255}", model)
                    or (admin and admin in model) or key_hash.lower() in model.lower()
                    or re.search(r"sk-|gh[pousr]_", model, re.I)):
                raise ValueError
            usage = row.get("usage")
            if type(usage) not in (int, float) or not math.isfinite(usage) or usage < 0: raise ValueError
            item = {"date": day, "model": model, "usage": usage}
            for field in ACTIVITY_COUNTERS:
                value = row.get(field)
                if value is not None and (type(value) is not int or value < 0): raise ValueError
                item[field] = value
            clean.append(item)
        return clean
    except (OSError, RuntimeError, ValueError, OverflowError, RecursionError):
        raise RuntimeError("Activity report unavailable or invalid; no figure. Check management-key access and retry the read") from None


def _spend_identity(name: str) -> tuple[str, str]:
    rec = ledger()["issued"].get(name)
    if not isinstance(rec, dict):
        raise LookupError("Unknown issued key; run openrouter.py list")
    account, handle = rec.get("account"), rec.get("hash")
    if not isinstance(account, str) or not account or not isinstance(handle, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", handle):
        raise ValueError("Issued-key identity is incomplete; repair its ledger metadata")
    if (rec.get("pending") or rec.get("orphan") or rec.get("rotation_status") not in (None, "complete")
            or rec.get("predecessor_hash") or rec.get("successor_hash")):
        raise RuntimeError("Issued-key rotation or issuance is unresolved; reconcile it before reading spend")
    return account, handle


def cmd_spend(name: str, date: str | None, as_json: bool) -> int:
    """Read-only current-key snapshot; it does not total retired keys or projects."""
    _activity_date(date)
    identity = _spend_identity(name)
    _label, admin = read_admin(identity[0])
    if (not isinstance(name, str) or not 0 < len(name) <= 200 or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or (admin and admin in name) or identity[1].lower() in name.lower() or re.search(r"sk-|gh[pousr]_", name, re.I)):
        raise ValueError("Issued-key display name is unsafe; repair its ledger metadata")
    rows = read_activity(admin, identity[1], date)
    try: current = _spend_identity(name)
    except (LookupError, ValueError, RuntimeError):
        raise RuntimeError("Issued-key identity changed during the read; retry after reconciliation") from None
    if current != identity:
        raise RuntimeError("Issued-key identity changed during the read; retry after reconciliation")
    grouped = {}
    try:
        with decimal.localcontext() as ctx:
            ctx.prec = 340  # finite IEEE doubles plus a bounded 10k-row sum
            for row in rows:
                key = (row["date"], row["model"])
                if key not in grouped:
                    grouped[key] = {**row, "usage": decimal.Decimal(str(row["usage"]))}
                    continue
                target = grouped[key]; target["usage"] += decimal.Decimal(str(row["usage"]))
                for field in ACTIVITY_COUNTERS:
                    target[field] = target[field] + row[field] if target[field] is not None and row[field] is not None else None
            total = sum((r["usage"] for r in grouped.values()), decimal.Decimal(0))
            total_usd = float(total)
            if not math.isfinite(total_usd): raise ValueError
            out = [grouped[k] for k in sorted(grouped)]
            for row in out: row["usage"] = float(row["usage"])
    except (ValueError, OverflowError, decimal.DecimalException):
        raise RuntimeError("Activity totals are outside the supported numeric range; no figure") from None
    doc = {"key": name, "date": date, "rows": out, "totalUsd": total_usd,
           "measuredAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
           "scope": "current-issued-key-only", "totalBasis": "sum-of-provider-usage",
           "retiredKeysIncluded": False}
    if as_json:
        print(json.dumps(doc, ensure_ascii=False, allow_nan=False))
    else:
        scope = f"on {date}" if date else "in the last 30 completed UTC days"
        print(f"{name}: {len(out)} day/model row(s) {scope}; current issued key only")
        for row in out:
            requests = row['requests'] if row['requests'] is not None else 'unknown'
            print(f"  {row['date']}  {row['model']}  {requests} req  ${row['usage']:.6f}")
        print(f"  total ${total_usd:.6f} (provider usage; retired keys excluded)")
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
    p_sp.add_argument("--date", help="one of the last 30 completed UTC days, YYYY-MM-DD")
    p_sp.add_argument("--json", action="store_true")
    p_li = sub.add_parser("limit", help="show or move a key's USD ceiling; for a key issued by a spend policy, "
                          "raising it can take it past its share of the project limit until the next "
                          "issue-for-policy run")
    p_li.add_argument("name")
    p_li.add_argument("--set", type=float, dest="set_to")
    p_li.add_argument("--decrease-only", action="store_true", help="require a fresh known ceiling and record one non-increasing repair attempt")
    p_li.add_argument("--repair-status", action="store_true", help="show the recorded ceiling repair without provider access")
    p_li.add_argument("--refresh", action="store_true", help="with --repair-status: record a fresh observation attempt; never retry the mutation")
    p_li.add_argument("--reset", choices=RESETS, default=None,
                      help="with --set: the new period; without it the key keeps its own")
    p_di = sub.add_parser("disable", help="stop a key spending, reversibly")
    p_di.add_argument("name")
    p_en = sub.add_parser("enable", help="let a disabled key spend again")
    p_en.add_argument("name")
    p_ro = sub.add_parser("rotate", help="Policy-marked keys refuse rotation without verified aggregate headroom. "
                          "Unmarked legacy/standalone keys reserve successor, verify identity, deliver, then delete predecessor. "
                          "Unknown outcomes retain possible exposure and refuse another rotation or mutation of "
                          "the unresolved identity. Overlapping ceilings can remain above the policy limit. "
                          "Reconcile provider identities and consumer state before recovery; no automatic "
                          "reconciliation is implemented. Local writer locking does not serialize external "
                          "account changes or enforce every standalone command's project policy",
                          description="Policy-marked keys refuse before provider calls: a stored policy snapshot is not current headroom proof. No bypass or automatic migration exists. Unmarked legacy/standalone keys retain the overlap workflow; their project-wide capacity is unverified.")
    p_ro.add_argument("name", nargs="?")
    p_ro.add_argument("--leaked", action="store_true",
                      help="rotate every issued key named in an open leak")
    p_re = sub.add_parser("revoke", help="delete only with current complete OpenRouter consumer evidence",
                          description="Requires a complete OpenRouter scan no older than 36 hours and no known holder. Missing, stale, changed or degraded evidence refuses. This guard does not yet survey every vault or remote holder.")
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
        except (LookupError, ValueError):
            print("Spend request refused; check the issued name and a date in the last 30 completed UTC days. Run openrouter.py list", file=sys.stderr)
            return 2
        except (OSError, RuntimeError):
            print("Activity report unavailable or identity unresolved; no figure. Check management-key access and reconcile pending rotation before retrying", file=sys.stderr)
            return 1
    if a.cmd == "limit":
        return cmd_limit(a.name, a.set_to, a.reset, decrease_only=a.decrease_only, repair_status_only=a.repair_status, refresh=a.refresh)
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
