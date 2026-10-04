#!/usr/bin/env python3
"""The one door to OpenRouter keys: stash, issue, rotate, revoke, limits, ping.

    T="$(project-observatory full-path)/tools"            # the installed engine's tools
    python "$T/openrouter.py" stash --as main < key-file   # provisioning key, labeled; a protected file
    python "$T/openrouter.py" adopt --as main               # take the legacy file in
    python "$T/openrouter.py" issue --name my-agent --limit 10 --to vault:PROJECT/prod/OPENROUTER_API_KEY
    python "$T/openrouter.py" list | ping
    python "$T/openrouter.py" limit my-agent --set 25
    python "$T/openrouter.py" disable my-agent              # or enable
    python "$T/openrouter.py" rotate my-agent               # or --leaked
    python "$T/openrouter.py" revoke my-agent

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
import datetime
import json
import math
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
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


def _journal(event: str, secret: str, **detail) -> None:
    """The vault's movements journal: every issue, rotation and
    revocation this door performs is on the record beside the vault's own.
    Never raises — a journal failure must not undo a rotation that happened."""
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import vault
        vault.journal(event, secret, tool="openrouter.py", **detail)
    except Exception as exc:                                                      
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
    return doc


def save_ledger(doc: dict) -> None:
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
    raise RuntimeError(f"the provider listed more than {PAGES_MAX} pages of keys; refusing a partial answer")


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
        return row if row and row.get("name") == name else None
    for row in _all_keys(admin):
        if row.get("name") == name:
            return row
    return None


def issue_key(name: str, limit: float, account: str | None, to: str,
              project: str | None) -> dict:
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
    to = vault_destination(to)
    doc = ledger()
    label, admin = read_admin(account)
    if find_key(admin, name):
        raise ValueError(f"a key called {name!r} already exists in {label!r} — "
                         f"`rotate {name}` replaces its value, `limit {name} --set` "
                         f"moves its ceiling; a second key with the same name is how "
                         f"spend becomes unattributable")
    # A MONTHLY RESET, not a lifetime cap (trap T17): a lifetime cap works until
    # the total is reached and then stops — months later, with no warning.
    # Found in the audit that merged keyserver's mint into this door: it set
    # the reset, the door did not.
    d = _request("/keys", admin, {"name": name, "limit": limit, "limit_reset": "monthly"})
    value = d.get("key") or (d.get("data") or {}).get("key")
    row = d.get("data") or d
    if not value:
        _delivery_failed(admin, row)
    try:
        place = deliver(value, to)
    except (OSError, ValueError, RuntimeError):
        _delivery_failed(admin, row)
    doc = ledger()
    doc["issued"][name] = {
        "account": label, "hash": row.get("hash"), "destination": to,
        "delivered_to": place, "project": project,
        "limit_usd": limit, "limit_reset": "monthly", "issued_on": today(),
        "rotated_on": None, "rotations": 0}
    save_ledger(doc)
    _journal("issue", f"openrouter/{label}/{name}", to=to, limit_usd=limit)
    return {"name": name, "account": label, "label": row.get("label"),
            "limit": limit, "limit_reset": "monthly", "delivered_to": place}


def cmd_issue(name: str, limit: float, account: str | None, to: str,
              project: str | None) -> int:
    try:
        r = issue_key(name, limit, account, to, project)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"issued {r['name']!r} from {r['account']!r}, ceiling ${r['limit']:g} monthly, "
          f"delivered to {r['delivered_to']}")
    return 0


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


def set_limit(name_or_label: str, limit: float, reset: str = "monthly") -> dict:
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError("a positive ceiling is required")
    name, rec = resolve_issued(name_or_label)
    label, admin = read_admin(rec["account"])
    row = find_key(admin, name)
    if not row:
        raise LookupError(f"the ledger records {name!r} but {label!r} holds no such key — "
                          f"it was deleted at the provider; `revoke {name}` clears the row")
    _request(f"/keys/{row['hash']}", admin, {"limit": limit, "limit_reset": reset},
             method="PATCH")
    doc = ledger()
    doc["issued"][name]["limit_usd"] = limit
    doc["issued"][name]["limit_reset"] = reset
    save_ledger(doc)
    return {"name": name, "label": row.get("label"), "limit": limit, "limit_reset": reset}


def cmd_limit(name: str, set_to: float | None) -> int:
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
        r = set_limit(name, set_to)
    except (ValueError, LookupError, RuntimeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"{r['name']}: ceiling moved to ${r['limit']:g} {r['limit_reset']}")
    return 0


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


def rotate_one(name: str, doc: dict | None = None) -> dict:
    """Deliver and verify a successor before revoking the predecessor; keep recovery metadata."""
    doc = doc if doc is not None else ledger()
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
    old = find_key(admin, name)
    d = _request("/keys", admin,
                 {"name": f"{name}-next", "limit": limit,
                  "limit_reset": rec.get("limit_reset") or "monthly"})
    value = d.get("key") or (d.get("data") or {}).get("key")
    new_row = d.get("data") or d
    if not value:
        _delivery_failed(admin, new_row)
    try:
        place = deliver(value, rec["destination"], rotate=True)
    except (OSError, ValueError, RuntimeError):
        _delivery_failed(admin, new_row)
    rec.update({"hash": new_row.get("hash"), "delivered_to": place,
                "rotated_on": today(), "rotations": rotations + 1,
                "rotation_status": "successor-delivered-predecessor-pending",
                "predecessor_hash": old.get("hash") if old else None})
    try:
        save_ledger(doc)
        if old:
            _request(f"/keys/{old['hash']}", admin, method="DELETE")
        _request(f"/keys/{new_row['hash']}", admin, {"name": name}, method="PATCH")
        rec["rotation_status"] = "complete"
        rec.pop("predecessor_hash", None)
        save_ledger(doc)
    except (OSError, ValueError, RuntimeError):
        _journal("rotation-partial", f"openrouter/{label}/{name}",
                 status="successor-delivered-provider-or-ledger-finalization-failed")
        raise RuntimeError("Partial failure: successor delivered, but provider or ledger finalization failed; inspect provider keys and ledger before retrying") from None
    _journal("rotate", f"openrouter/{label}/{name}", to=rec["destination"])
    return {"name": name, "delivered_to": place, "rotations": rec["rotations"],
            "rotated_on": rec["rotated_on"]}


def cmd_rotate(name: str | None, leaked: bool) -> int:
    doc = ledger()
    targets = list(doc["issued"]) if not name else [name]
    if leaked:
        targets = [n for n in doc["issued"] if _leaked(n, doc["issued"][n])]
        if not targets:
            print("no open leak names an issued OpenRouter key")
            return 0
    bad = 0
    for n in targets:
        try:
            r = rotate_one(n, doc)
            print(f"  {n}: rotated, successor delivered to {r['delivered_to']}, predecessor deleted")
        except LookupError as exc:
            print(f"  {n}: {exc}", file=sys.stderr)
            bad += 1
        except RuntimeError as exc:
            print(f"  {n}: could not rotate — {exc}", file=sys.stderr)
            bad += 1
    if leaked and not bad and targets:
        print("the leaked values are dead; settle the register with "
              "`python \"$(project-observatory full-path)/tools/vault.py\" rotate` for each row it names")
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


def revoke_key(name_or_label: str) -> dict:
    """Delete at the provider, then clear the ledger row — in that order: a
    forgotten live key is worse than a stale row. REFUSED for a key something
    is reading right now (`serves` in the OpenRouter scan): revoking the key a
    consumer holds takes the consumer down with no way back."""
    name, rec = resolve_issued(name_or_label)
    scan = paths.SCRATCH / "openrouter.json"
    if scan.is_file():
        try:
            for k in json.loads(scan.read_text(encoding="utf-8")).get("keys", []):
                if k.get("serves") and (k.get("name") == name or k.get("label") == name_or_label):
                    raise ValueError(f"{name} is what {k['serves']} is reading right now; "
                                     f"issue its replacement first, then revoke this one")
        except (OSError, ValueError) as exc:
            if isinstance(exc, ValueError) and "reading right now" in str(exc):
                raise
    _label, admin = read_admin(rec["account"])
    row = find_key(admin, name)
    if row:
        _request(f"/keys/{row['hash']}", admin, method="DELETE")
    doc = ledger()
    doc["issued"].pop(name, None)
    save_ledger(doc)
    _journal("revoke", f"openrouter/{rec['account']}/{name}")
    return {"name": name, "label": (row or {}).get("label"), "revoked": True,
            "delivered_to": rec.get("delivered_to")}


def cmd_revoke(name: str) -> int:
    try:
        r = revoke_key(name)
    except (ValueError, LookupError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"refused: {exc}\n  the ledger row was kept — a forgotten live key "
              f"is worse than a stale row", file=sys.stderr)
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
            first = _request(f"/keys?include_disabled=true&offset=0", key).get("data", [])
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
    sub.add_parser("list", help="what exists — names, ceilings, places; never values")
    sub.add_parser("ping", help="alive? spent how much of its ceiling?")
    p_li = sub.add_parser("limit", help="show or move a key's USD ceiling")
    p_li.add_argument("name")
    p_li.add_argument("--set", type=float, dest="set_to")
    p_di = sub.add_parser("disable", help="stop a key spending, reversibly")
    p_di.add_argument("name")
    p_en = sub.add_parser("enable", help="let a disabled key spend again")
    p_en.add_argument("name")
    p_ro = sub.add_parser("rotate", help="create successor, deliver, delete predecessor")
    p_ro.add_argument("name", nargs="?")
    p_ro.add_argument("--leaked", action="store_true",
                      help="rotate every issued key named in an open leak")
    p_re = sub.add_parser("revoke", help="delete at the provider, clear the ledger row")
    p_re.add_argument("name")
    a = ap.parse_args()
    if a.cmd == "stash":
        if sys.stdin.isatty():
            print("paste the provisioning key on stdin", file=sys.stderr)
            return 2
        return stash_value(sys.stdin.read().strip(), a.label, origin="stdin")
    if a.cmd == "adopt":
        return cmd_adopt(a.label)
    if a.cmd == "issue":
        return cmd_issue(a.name, a.limit, a.account, a.to, a.project)
    if a.cmd == "list":
        return cmd_list()
    if a.cmd == "ping":
        return cmd_ping()
    if a.cmd == "limit":
        return cmd_limit(a.name, a.set_to)
    if a.cmd == "disable":
        return cmd_toggle(a.name, True)
    if a.cmd == "enable":
        return cmd_toggle(a.name, False)
    if a.cmd == "rotate":
        return cmd_rotate(a.name, a.leaked)
    if a.cmd == "revoke":
        return cmd_revoke(a.name)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError):
        print("Private credential operation refused; values hidden", file=sys.stderr)
        raise SystemExit(2)
