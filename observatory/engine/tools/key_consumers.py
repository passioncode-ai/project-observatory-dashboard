#!/usr/bin/env python3
"""Who else holds a key, found by the value, before a door revokes or rolls it (OBS-561, CP-11, decision D5).

WHY. A door's rotation delivered the successor to ONE place (the vault slot, or the named file it was issued to) and
then deleted the predecessor. Every other copy of the old value (a platform's config var, another project's `.env`)
kept a dead key: one production bot answered 401 for three hours after such a rotation (2026-11-13). This module
finds those copies by the value the door holds in memory, delivers the successor to each through that consumer's own
door, and reads it back, so the door revokes only when every copy it found carries the new value.

THE VALUE NEVER LEAVES MEMORY. The old and the new value are compared in this process (`hmac.compare_digest`); what a
consumer row, an outcome, a degradation or a printed line carries is names and places only: an application, a
variable, a file path, a service label. A provider's answer is never echoed (the Heroku door's own wire strips it).

THE SOURCES.

  heroku     every application the Heroku door's stashed admin tokens (or, when the `heroku` integration is on and
             none is stashed, the CLI session) can list, its config vars read fresh; delivery is a `PATCH` of that
             one variable with a JSON body built in memory, read back with a `GET`
  env-file   every file the local inventory `store/raw/env.json` lists (`collectors/scan_env.py`), read fresh and
             parsed with the scan's own parser; delivery replaces that one variable's value in place, atomically,
             keeping the file's mode. A file Git TRACKS is never written: a new key there is one commit from
             publication, so it holds the revocation and is named with what to do
  launchd    the services `use_secret.py serve` started with the vault slot (`use_secret.consumers`); they read the
             slot once at start, so their delivery is the slot itself and they are named with the restart command
  vault      every other slot of the local vault holding the same value (a key copied into a second project's
             slot); delivery is the vault's own `store_value`, which archives the old value beside the slot

A SOURCE THAT COULD NOT BE READ IS `degraded`, NEVER "NO CONSUMER". The door holds the revocation on it. A source
that is not in use on this machine (no Heroku token stashed and the integration off; no vault slot to look up
services by) is `skipped`, with the reason, and holds nothing. Every degraded and skipped entry carries a `code`.

ONE GUARD FOR EVERY REVOKE (OBS-594). `revoke_guard` is the check every revoke runs before its provider call: the
OpenRouter door, the key server, the MCP key act, `tools/revoke_key.py` and the DigitalOcean door. It reads the
sources above, the OpenRouter scan (`OpenRouterScanSource`, by the key's hash, label and name) and the App Platform
apps that bind a database cluster (`DigitalOceanAppsSource`, the DigitalOcean door's finder), and refuses with a typed
code (`consumer-in-use`, `consumer-source-unreadable`, `consumer-source-unknown`) instead of revoking blind. Design:
docs/design/control-plane/revoke-guard.md.

A DOOR THAT ROLLS IN PLACE DELIVERS TOO (OBS-607). Cloudflare and Heroku kill a value when they roll it, so a copy
found elsewhere used to refuse the roll. `plan_roll` finds the copies first; when every one of them is one this module
can write, the door mints a successor beside the old key instead of rolling it, writes its own slot or file, and
`deliver_then_revoke` delivers to every copy, reads each back, runs `revoke_guard` over the value sources (no copy of
the old value may be left), and only then revokes the old key. A copy it could not deliver to holds the revoke: the
door keeps the old key alive and records `held_record` (names, places and a keyed digest of the old value, never the
value), and its next `rotate` finishes through `finish_held`, which finds the old value again in a recorded copy by
that digest.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import pathlib
import re
import stat
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "collectors"))
import paths  # noqa: E402

#: The local env inventory every tick takes (`tools/tick.py`, step `env`).
ENV_INVENTORY = paths.SCRATCH / "env.json"

#: An assignment line of a `.env`, as `collectors/scan_env.py` reads one (its `ASSIGN`).
_ASSIGN = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=)(.*)$")


class ConsumersHeld(RuntimeError):
    """A revocation held because a consumer of the old value was not delivered and read back, or a source could not
    be read. `lines` name each one; never a value."""

    def __init__(self, message: str, lines: list[str]):
        super().__init__(message)
        self.lines = lines


def same(a, b) -> bool:
    """Two values equal, compared in constant time; anything not a non-empty string is never equal."""
    if not isinstance(a, str) or not isinstance(b, str) or not a or not b:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def describe(c: dict) -> str:
    """One consumer, by name and place."""
    kind = c.get("kind")
    if kind == "heroku":
        return f"heroku app {c.get('app')}, config var {c.get('var')}"
    if kind == "env-file":
        return f"env file {c.get('path')}, variable {c.get('var')}"
    if kind == "launchd":
        return f"service {c.get('label')}"
    if kind == "openrouter-destination":
        return f"destination {c.get('destination')} (found by the key's {c.get('by')} in the OpenRouter scan)"
    if kind == "openrouter-serves":
        return f"{c.get('serves')} (the OpenRouter scan says the key serves it)"
    if kind == "vault":
        return f"vault slot {c.get('slot')}"
    if kind == "digitalocean-app":
        return f"App Platform app {c.get('name')} ({c.get('app')})"
    return str(kind)


# ─────────────────────────── heroku ─────────────────────────────────────────

class _HerokuDoor:
    """The Heroku door's own wire (`tools/heroku.py`): its `_request` and paged `_list`, whose errors name the
    endpoint class and the status, never a body."""

    def __init__(self):
        import heroku as door
        self.door = door

    def request(self, method: str, path: str, token: str, payload: dict | None = None):
        return self.door._request(method, path, token, payload)

    def list(self, path: str, token: str) -> list:
        return self.door._list(path, token)


def heroku_tokens() -> tuple[list[str], str | None, str | None]:
    """(tokens, skipped reason, degraded reason). The Heroku door's stashed admin tokens; else, when the `heroku`
    integration is on, the CLI session's token (`collectors/scan_heroku.token`), as the remote-env scan reads it."""
    import heroku as door
    try:
        stashed = door.admins()
        if stashed:
            import private_io
            return [private_io.read(p).strip() for _n, p in stashed], None, None
    except (OSError, RuntimeError) as exc:
        return [], None, f"the Heroku door's admin stash could not be read ({type(exc).__name__})"
    import configuration
    if not configuration.enabled("heroku"):
        return [], "no Heroku admin token is stashed and the heroku integration is off", None
    import scan_heroku
    tok, why = scan_heroku.token()
    if not tok:
        return [], None, f"no Heroku token: {why}"
    return [tok], None, None


class HerokuSource:
    """`memo`: read the applications' config vars once and compare every later value against that reading (the keys
    page's consumers, OBS-604, ask about many slots in one pass). A door's guard keeps the default: it reads fresh."""
    name = "heroku"

    def __init__(self, tokens=heroku_tokens, api=None, memo: bool = False):
        self._tokens = tokens
        self._api = api
        self._token_of: dict[str, str] = {}
        self.memo = memo
        self._read: tuple | None = None

    @property
    def api(self):
        if self._api is None:
            self._api = _HerokuDoor()
        return self._api

    def find(self, old: str) -> tuple[list[dict], list[dict], list[dict]]:
        if self.memo and self._read is None:
            self._read = self._configs()
        configs, degraded, skipped = self._read if self.memo else self._configs()
        found = []
        for app_id, app_name, tok, config in configs:
            for var, value in sorted(config.items()):
                if same(value, old):
                    self._token_of[app_id] = tok
                    found.append({"kind": "heroku", "app": app_name, "app_id": app_id, "var": var})
        return found, [dict(d) for d in degraded], [dict(d) for d in skipped]

    def _configs(self) -> tuple[list[tuple], list[dict], list[dict]]:
        """([(app id, app name, token, config vars)], degraded, skipped): every application's config, read fresh."""
        tokens, skip, bad = self._tokens()
        if skip:
            return [], [], [{"source": self.name, "code": "not-in-use", "reason": skip}]
        if bad or not tokens:
            return [], [{"source": self.name, "code": "token-unreadable", "reason": bad or "no Heroku token"}], []
        configs, degraded, apps = [], [], {}
        for tok in tokens:
            try:
                for app in self.api.list("/apps", tok):
                    if isinstance(app, dict) and app.get("id") and app["id"] not in apps:
                        apps[app["id"]] = (app.get("name") or app["id"], tok)
            except RuntimeError as exc:
                degraded.append({"source": self.name, "code": "app-list-unreadable",
                                 "reason": f"the application list could not be read: {exc}"})
        for app_id, (app_name, tok) in sorted(apps.items(), key=lambda kv: kv[1][0]):
            try:
                config = self.api.request("GET", f"/apps/{app_id}/config-vars", tok)
            except RuntimeError as exc:
                degraded.append({"source": self.name, "app": app_name, "code": "config-unreadable",
                                 "reason": f"config vars of {app_name} could not be read: {exc}"})
                continue
            if not isinstance(config, dict):
                degraded.append({"source": self.name, "app": app_name, "code": "config-unshaped",
                                 "reason": f"config vars of {app_name} came back in no known shape"})
                continue
            configs.append((app_id, app_name, tok, config))
        return configs, degraded, []

    def deliver(self, c: dict, new: str) -> None:
        tok = self._token_of.get(c["app_id"])
        if tok is None:
            raise RuntimeError("no token reached this application in this run")
        # The body is built here, in memory, and travels as the request body: never argv, never a log.
        self.api.request("PATCH", f"/apps/{c['app_id']}/config-vars", tok, {c["var"]: new})

    def read_back(self, c: dict, new: str) -> bool:
        config = self.api.request("GET", f"/apps/{c['app_id']}/config-vars", self._token_of[c["app_id"]])
        return isinstance(config, dict) and same(config.get(c["var"]), new)

    def current(self, c: dict) -> str | None:
        """What one recorded consumer holds now, in memory (a held rotation looks for its old value there)."""
        tokens, _skip, bad = self._tokens()
        if bad or not tokens:
            raise RuntimeError("no Heroku token")
        for tok in tokens:
            try:
                config = self.api.request("GET", f"/apps/{c['app_id']}/config-vars", tok)
            except RuntimeError:
                continue
            if isinstance(config, dict):
                self._token_of[c["app_id"]] = tok
                value = config.get(c["var"])
                return value if isinstance(value, str) else None
        return None


# ─────────────────────────── local env files ────────────────────────────────

def _scan_env():
    import importlib.util
    spec = importlib.util.spec_from_file_location("scan_env_for_consumers", ROOT / "collectors" / "scan_env.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _atomic_replace(path: pathlib.Path, text: str) -> None:
    """Write `text` over `path` by a rename in the same folder, keeping the file's permission bits."""
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, tmp = tempfile.mkstemp(prefix=".consumer-write-", dir=str(path.parent))
    try:
        os.chmod(tmp, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class EnvFileSource:
    """`memo`: read the inventoried files once and compare every later value against that reading (OBS-604); a
    door's guard keeps the default and reads them fresh."""
    name = "env-file"

    def __init__(self, inventory: pathlib.Path | None = None, exclude: tuple = (), projects: pathlib.Path | None = None,
                 memo: bool = False):
        self.inventory = inventory or ENV_INVENTORY
        self.projects = projects or paths.DATA
        self.exclude = {str(pathlib.Path(p)) for p in exclude}
        self._env = None
        self.memo = memo
        self._read: tuple | None = None

    @property
    def env(self):
        if self._env is None:
            self._env = _scan_env()
        return self._env

    def find(self, old: str) -> tuple[list[dict], list[dict], list[dict]]:
        if self.memo and self._read is None:
            self._read = self._files()
        files, degraded, skipped = self._read if self.memo else self._files()
        found = []
        for path, rec, pairs in files:
            for var, value in pairs:
                if same(value, old):
                    found.append({"kind": "env-file", "path": str(path), "var": var,
                                  "git": rec.get("git"), "project": rec.get("project")})
        return found, [dict(d) for d in degraded], [dict(d) for d in skipped]

    def _files(self) -> tuple[list[tuple], list[dict], list[dict]]:
        """([(path, inventory record, parsed pairs)], degraded, skipped): every inventoried file, read fresh."""
        if not pathlib.Path(self.inventory).exists() and not pathlib.Path(self.projects).is_dir():
            # No projects folder on this machine, so no inventory to take: not in use, not unread.
            return [], [], [{"source": self.name, "code": "not-in-use",
                             "reason": "no projects folder on this machine, so no env file is inventoried"}]
        try:
            doc = json.loads(pathlib.Path(self.inventory).read_text(encoding="utf-8"))
            files, root = doc["files"], pathlib.Path(doc["root"])
            if not isinstance(files, list):
                raise ValueError("files is not a list")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return [], [{"source": self.name, "code": "inventory-unreadable",
                         "reason": f"the local env inventory could not be read ({type(exc).__name__}); run "
                                   f"`project-observatory full env`"}], []
        files_read, degraded = [], []
        unread = [d for d in (doc.get("degraded") or []) if isinstance(d, dict)]
        if unread:
            degraded.append({"source": self.name, "code": "inventory-incomplete",
                             "reason": f"the local env inventory could not read {len(unread)} file(s)"})
        for rec in files:
            if not isinstance(rec, dict) or not rec.get("path"):
                continue
            path = root / rec["path"]
            if str(path) in self.exclude:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                continue                     # gone since the inventory: it holds nothing now
            except OSError as exc:
                degraded.append({"source": self.name, "path": str(path), "code": "file-unreadable",
                                 "reason": f"{path} could not be read ({type(exc).__name__})"})
                continue
            pairs, _ = self.env.parse(text)
            files_read.append((path, rec, pairs))
        return files_read, degraded, []

    def deliver(self, c: dict, new: str, old: str) -> None:
        if c.get("git") == "tracked":
            raise ConsumersHeld("tracked in Git: a new key written there is one commit from publication; move the "
                                "value to the vault (`vault.py put`), take it out of the file, then rotate again", [])
        if "\n" in new or "\r" in new or "\n" in old:
            raise RuntimeError("a value with a line break is not replaced in place")
        path = pathlib.Path(c["path"])
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        replaced = 0
        for i, line in enumerate(lines):
            m = _ASSIGN.match(line.rstrip("\r\n"))
            if not m or m.group(2) != c["var"]:
                continue
            pairs, _ = self.env.parse(line)
            if not pairs or not same(pairs[0][1], old):
                continue
            head = m.group(1) + m.group(2) + m.group(3)
            lines[i] = head + line[len(head):].replace(old, new, 1)
            replaced += 1
        if not replaced:
            raise RuntimeError("the variable no longer holds the old value")
        _atomic_replace(path, "".join(lines))

    def read_back(self, c: dict, new: str, old: str) -> bool:
        pairs, _ = self.env.parse(pathlib.Path(c["path"]).read_text(encoding="utf-8"))
        mine = [v for k, v in pairs if k == c["var"]]
        return bool(mine) and all(same(v, new) for v in mine) and not any(same(v, old) for _k, v in pairs)

    def current(self, c: dict) -> str | None:
        pairs, _ = self.env.parse(pathlib.Path(c["path"]).read_text(encoding="utf-8"))
        return next((v for k, v in pairs if k == c["var"]), None)


# ─────────────────────────── launchd services ───────────────────────────────

class LaunchdSource:
    name = "launchd"

    def __init__(self, slot: tuple[str, str, str] | None, consumers=None):
        self.slot = slot
        self._consumers = consumers

    def find(self, old: str) -> tuple[list[dict], list[dict], list[dict]]:
        if not self.slot:
            return [], [], [{"source": self.name, "code": "no-slot",
                             "reason": "the key is not delivered to a vault slot, so no service started from one"}]
        try:
            fn = self._consumers
            if fn is None:
                import use_secret
                fn = use_secret.consumers
            rows = fn(*self.slot)
        except Exception as exc:                                                # noqa: BLE001  degraded, named
            return [], [{"source": self.name, "code": "services-unreadable",
                         "reason": f"the services started with the slot could not be read ({type(exc).__name__})"}], []
        return [{"kind": "launchd", "label": r.get("consumer", "?"), "started": r.get("at")}
                for r in rows if isinstance(r, dict)], [], []


# ─────────────────────────── other vault slots ──────────────────────────────

class VaultSource:
    """Every other slot of the local vault that holds the same value (OBS-607): read in memory, compared in constant
    time. The key's own slot is left out: it holds the value by design and the door writes it itself. Delivery is the
    vault's own `store_value` (the old value archived beside the slot, as `vault.py rotate` keeps it), read back with
    `vault_value`. A slot that cannot be read is `degraded`, never "no copy"."""

    name = "vault"

    def __init__(self, exclude_slot: tuple | None = None, store: pathlib.Path | None = None):
        self.exclude = tuple(exclude_slot) if exclude_slot else None
        self._store = store

    @property
    def store(self) -> pathlib.Path:
        if self._store is None:
            import vault
            self._store = vault.STORE
        return pathlib.Path(self._store)

    def find(self, old: str) -> tuple[list[dict], list[dict], list[dict]]:
        store = self.store
        if not store.is_dir():
            return [], [], [{"source": self.name, "code": "not-in-use", "reason": "no vault on this machine"}]
        import vault
        try:
            files = sorted(store.rglob("*"))
        except OSError as exc:
            return [], [{"source": self.name, "code": "store-unreadable",
                         "reason": f"the vault could not be listed ({type(exc).__name__})"}], []
        found, degraded = [], []
        for path in files:
            if path.name.endswith((".meta.json", ".tmp")) or ".retired-" in path.name or not path.is_file():
                continue
            parts = path.relative_to(store).parts
            if len(parts) != 3 or parts == self.exclude:
                continue
            label = "/".join(parts)
            try:
                value = vault._read_private(path, private=False).strip()
            except Exception as exc:                                            # noqa: BLE001  degraded, named
                degraded.append({"source": self.name, "slot": label, "code": "slot-unreadable",
                                 "reason": f"the vault slot {label} could not be read ({type(exc).__name__})"})
                continue
            if same(value, old):
                found.append({"kind": "vault", "slot": label})
        return found, degraded, []

    def deliver(self, c: dict, new: str) -> None:
        import vault
        vault.store_value(*c["slot"].split("/"), new, replace=True, tool="key_consumers.py")

    def read_back(self, c: dict, new: str) -> bool:
        return same(vault_value(*c["slot"].split("/")), new)

    def current(self, c: dict) -> str | None:
        return vault_value(*c["slot"].split("/"))


# ─────────────────────────── the finder and the delivery ────────────────────

def default_sources(vault_slot: tuple[str, str, str] | None = None, exclude: tuple = ()) -> list:
    return [HerokuSource(), EnvFileSource(exclude=exclude), LaunchdSource(vault_slot), VaultSource(vault_slot)]


def consumer_sources(slot: str | tuple | None = None, exclude=None) -> list:
    """Where the copies of a door's value are looked for before a revoke or a roll: Heroku config vars, the local env
    files the engine inventories, the services started from the vault slot, the vault's other slots (OBS-607). The
    one copy every door calls (OBS-601):
    `slot` is the key's vault slot (`project/env/NAME` or a tuple, None when it is not delivered to one), `exclude`
    the file it was delivered to, which holds it by design and is never counted as a copy. Tests replace it."""
    if isinstance(slot, str):
        parts = tuple(slot.split("/"))
        slot = parts if len(parts) == 3 and all(parts) else None
    return default_sources(tuple(slot) if slot else None, exclude=(str(exclude),) if exclude else ())


def find(old: str | None, sources: list) -> dict:
    """Every consumer of `old` the sources can see, what could not be read, and what was not in use."""
    out = {"consumers": [], "degraded": [], "skipped": [], "_sources": {}}
    if not old:
        out["skipped"].append({"source": "value", "code": "no-value",
                               "reason": "the destination holds no value, so there is no copy of it to look for"})
        return out
    for src in sources:
        try:
            found, degraded, skipped = src.find(old)
        except Exception as exc:                                                # noqa: BLE001  degraded, named
            found, degraded, skipped = [], [{"source": src.name, "code": "source-failed",
                                             "reason": f"{type(exc).__name__} while reading"}], []
        for d in degraded:
            d.setdefault("code", "source-unreadable")
        for d in skipped:
            d.setdefault("code", "not-in-use")
        for c in found:
            out["_sources"][id(c)] = src
        out["consumers"] += found
        out["degraded"] += degraded
        out["skipped"] += skipped
    return out


def undeliverable(found: dict) -> list[str]:
    """Consumers known before any delivery to hold the revocation (a Git-tracked env file)."""
    return [describe(c) + ": tracked in Git; move the value to the vault and take it out of the file"
            for c in found["consumers"] if c.get("kind") == "env-file" and c.get("git") == "tracked"]


def blocks(found: dict) -> list[str]:
    """Lines that hold a revocation before anything is minted: an unreadable source or an undeliverable consumer."""
    return ([f"source {d['source']} could not be read: {d['reason']}" for d in found["degraded"]]
            + undeliverable(found))


def deliver_all(found: dict, old: str, new: str) -> list[dict]:
    """Deliver `new` to every consumer and read it back. One outcome per consumer: `delivered` (read back equal),
    `restart` (a service that reads the slot at start), or `held` with the reason."""
    outcomes = []
    for c in found["consumers"]:
        src = found["_sources"].get(id(c))
        row = {"consumer": describe(c), "kind": c.get("kind")}
        if c.get("kind") == "launchd":
            row.update(status="restart", restart=f"launchctl kickstart -k gui/$(id -u)/{c.get('label')}")
            outcomes.append(row)
            continue
        try:
            if isinstance(src, EnvFileSource):
                src.deliver(c, new, old)
                ok = src.read_back(c, new, old)
            else:
                src.deliver(c, new)
                ok = src.read_back(c, new)
        except ConsumersHeld as exc:
            row.update(status="held", reason=str(exc))
        except Exception as exc:                                                # noqa: BLE001  held, named
            row.update(status="held", reason=f"delivery failed: {exc if isinstance(exc, RuntimeError) else type(exc).__name__}")
        else:
            if ok:
                row["status"] = "delivered"
            else:
                row.update(status="held", reason="the new value did not read back")
        outcomes.append(row)
    return outcomes


def held(outcomes: list[dict]) -> list[str]:
    return [f"{o['consumer']}: {o['reason']}" for o in outcomes if o.get("status") == "held"]


def public(found: dict) -> dict:
    """The finder's answer without its internal map: names and places only."""
    return {k: v for k, v in found.items() if not k.startswith("_")}


def vault_value(project: str, env: str, name: str) -> str | None:
    """A vault slot's current value, in memory, or None when the slot holds none."""
    import vault
    slot = vault._slot(project, env, name)
    if not slot.is_file():
        return None
    return vault._read_private(slot).strip() or None


def retired_value(project: str, env: str, name: str, matches) -> str | None:
    """The newest of a slot's retired values (`vault.py rotate` keeps them beside the slot) for which `matches`
    answers True: how a held rotation finds its predecessor's value again without having stored it."""
    import vault
    slot = vault._slot(project, env, name)
    for p in sorted(slot.parent.glob(f"{slot.name}.retired-*"), reverse=True):
        try:
            value = vault._read_private(p).strip()
        except (OSError, RuntimeError, vault.VaultBoundaryError):
            continue
        if value and matches(value):
            return value
    return None


def vault_answer(project: str, env: str, name: str, sources: list | None = None) -> dict:
    """The consumers of a vault slot's current value, by name and place (the MCP tool's answer)."""
    try:
        value = vault_value(project, env, name)
    except Exception as exc:                                                    # noqa: BLE001  named, typed
        return {"error": {"code": "slot-unreadable", "message": f"the slot could not be read ({type(exc).__name__})"},
                "degraded": []}
    found = find(value, sources if sources is not None else default_sources((project, env, name)))
    return {"slot": f"{project}/{env}/{name}", **public(found), "holds": blocks(found)}


def roll_guard(old: str | None, sources: list, why: str | None = None) -> tuple[list[str], list[str]]:
    """For a door that rolls a value in place (Cloudflare, Heroku), where the old value dies with the roll and the
    successor flow to other consumers is later work: (lines that refuse the roll, services to restart after it).
    A copy outside the vault, or a source that could not be read, refuses; a service started from the slot only
    needs a restart, since the slot is what it reads."""
    if why:
        return [f"source destination could not be read: {why}"], []
    found = find(old, sources)
    refuse = [f"{describe(c)}: holds the value, and this door does not deliver to it yet"
              for c in found["consumers"] if c.get("kind") != "launchd"]
    refuse += [f"source {d['source']} could not be read: {d['reason']}" for d in found["degraded"]]
    restart = [f"{describe(c)}: reads the slot at start; restart it: launchctl kickstart -k gui/$(id -u)/{c['label']}"
               for c in found["consumers"] if c.get("kind") == "launchd"]
    return refuse, restart


def guard_roll(slot: str | None = None, path=None) -> tuple[list[str], list[str]]:
    """The roll guard of a door that rolls a value in place (OBS-601: Cloudflare names the token `path` it issued,
    Heroku the vault `slot`): reads the old value in memory, then `roll_guard` over `consumer_sources`. A value that
    could not be read refuses the roll, named by its exception's type, and no source is looked at."""
    if (slot is None) == (path is None):
        raise ValueError("guard_roll takes a vault slot or a token path, one of them")
    try:
        if path is not None:
            import private_io
            old = private_io.read(pathlib.Path(path)).strip() or None
        else:
            old = vault_value(*slot.split("/"))
        why = None
    except Exception as exc:                                                    # noqa: BLE001  named, never echoed
        old, why = None, (f"the {'token file' if path is not None else 'slot'} could not be read "
                          f"({type(exc).__name__})")
    return roll_guard(old, consumer_sources(slot, exclude=path) if not why else [], why)


# ─────────────────────────── DigitalOcean App Platform ──────────────────────

class DigitalOceanAppsSource:
    """App Platform apps whose spec binds a managed database cluster, and the literal database-ish variables they hold
    themselves (the DigitalOcean door's `db consumers`, folded here by OBS-594). `list_apps` answers `doctl apps
    list`; `is_literal(key, value)` and `form(key, value)` are the door's own readings of a variable. A literal is
    named by its scope, key and form, never by its value."""

    name = "digitalocean-apps"

    def __init__(self, list_apps, is_literal, form):
        self._list_apps = list_apps
        self._is_literal = is_literal
        self._form = form

    def apps(self, cluster: str) -> list[dict]:
        """The door's rows, unchanged: {app, name, binds, literals} per app that binds `cluster`. Errors of the
        listing reach the caller as they are."""
        out = []
        for app in self._list_apps() or []:
            spec = app.get("spec") or {}
            bound = [d for d in spec.get("databases") or [] if d.get("cluster_name") == cluster]
            if not bound:
                continue
            literals = []
            for scope in [spec, *[x for k in ("services", "workers", "jobs") for x in spec.get(k) or []]]:
                for e in scope.get("envs") or []:
                    v = str(e.get("value", ""))
                    if "${" not in v and self._is_literal(e["key"], v):
                        literals.append(f"{scope.get('name', 'app')}:{e['key']} ({self._form(e['key'], v)})")
            out.append({"app": app["id"], "name": spec.get("name"),
                        "binds": [(d.get("name"), d.get("db_user") or "default") for d in bound],
                        "literals": literals})
        return out

    def find_key(self, key: dict) -> tuple[list[dict], list[dict], list[dict]]:
        cluster = key.get("cluster")
        if not cluster:
            return [], [], [{"source": self.name, "code": "not-a-cluster",
                             "reason": "the key is not a managed database's credential, so no app binds it"}]
        try:
            rows = self.apps(cluster)
        except Exception as exc:                                                # noqa: BLE001  degraded, named
            return [], [{"source": self.name, "code": "app-list-unreadable",
                         "reason": f"the App Platform app list could not be read ({type(exc).__name__})"}], []
        return [{"kind": "digitalocean-app", "app": r["app"], "name": r["name"]} for r in rows], [], []


# ─────────────────────────── the revoke guard (OBS-594, CP-11) ──────────────

#: The guard's refusal codes. A door, the key server and an MCP tool answer the same code for the same reason.
IN_USE = "consumer-in-use"
SOURCE_UNREADABLE = "consumer-source-unreadable"
SOURCE_UNKNOWN = "consumer-source-unknown"


class RevokeRefused(ValueError):
    """A revoke refused before the provider was asked: a consumer still holds the key (`consumer-in-use`), a consumer
    source could not be read (`consumer-source-unreadable`), or the caller named a source the guard does not know
    (`consumer-source-unknown`). `consumers` and `unread` name each one by name and place, never a value. A
    ValueError, so a caller that already refused on ValueError keeps refusing."""

    def __init__(self, code: str, message: str, consumers: list[str] | None = None, unread: list[dict] | None = None):
        super().__init__(message)
        self.code = code
        self.consumers = list(consumers or [])
        self.unread = list(unread or [])

    def answer(self) -> dict:
        return {"error": {"code": self.code, "message": str(self), "consumers": self.consumers,
                          "unread": self.unread}}


class OpenRouterScanSource:
    """The OpenRouter scan (`collectors/scan_openrouter.py`, written to `store/raw/openrouter.json` by every tick and by
    `project-observatory full openrouter`): which destination holds which key, by the key's hash (the provider's own
    fingerprint of it) or its label, and which key `serves` a consumer. A key is matched by its hash first: a key
    renamed in the provider's dashboard is still the key a consumer holds. A label two keys share matches too: the
    safe side. A scan that is missing or unreadable is `degraded`, never "no consumer"."""

    name = "openrouter-scan"

    def __init__(self, scan: pathlib.Path | None = None):
        self._scan = scan

    @property
    def path(self) -> pathlib.Path:
        return pathlib.Path(self._scan) if self._scan else paths.SCRATCH / "openrouter.json"

    def find_key(self, key: dict) -> tuple[list[dict], list[dict], list[dict]]:
        path = self.path
        if not path.is_file():
            return [], [{"source": self.name, "code": "scan-missing",
                         "reason": "the OpenRouter scan has not been taken on this machine, so who reads the key is "
                                   "unknown; run `project-observatory full openrouter`, then revoke again"}], []
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                raise ValueError("the scan is not an object")
            hashes = doc.get("destination_hashes") or {}
            labels_at = doc.get("destinations") or {}
            keys = doc.get("keys") or []
            if not isinstance(hashes, dict) or not isinstance(labels_at, dict) or not isinstance(keys, list):
                raise ValueError("the scan has an unknown shape")
        except (OSError, ValueError, UnicodeError) as exc:
            return [], [{"source": self.name, "code": "scan-unreadable",
                         "reason": f"the OpenRouter scan could not be read ({type(exc).__name__}); run "
                                   f"`project-observatory full openrouter`, then revoke again"}], []
        want_hash = key.get("hash")
        labels = {x for x in key.get("labels") or () if x}
        names = {x for x in key.get("names") or () if x}
        found = []
        for dest, held in sorted(hashes.items()):
            if want_hash and isinstance(held, dict) and held.get("hash") == want_hash:
                found.append({"kind": "openrouter-destination", "destination": dest, "by": "hash"})
        seen = {c["destination"] for c in found}
        for dest, label in sorted(labels_at.items()):
            if label in labels and dest not in seen:
                found.append({"kind": "openrouter-destination", "destination": dest, "by": "label"})
        served = set()
        for k in keys:
            if (isinstance(k, dict) and k.get("serves") and k["serves"] not in served
                    and (k.get("name") in names or k.get("label") in labels)):
                served.add(k["serves"])
                found.append({"kind": "openrouter-serves", "serves": k["serves"]})
        return found, [], []


#: The sources a caller may name instead of passing one: each is built from the key being revoked.
GUARD_SOURCES = {
    "openrouter-scan": lambda key: OpenRouterScanSource(),
    "heroku": lambda key: HerokuSource(),
    "env-file": lambda key: EnvFileSource(exclude=tuple(key.get("exclude") or ())),
    "launchd": lambda key: LaunchdSource(key.get("slot")),
    "vault": lambda key: VaultSource(key.get("slot")),
}


def revoke_guard(subject: str, key: dict, sources: list, value: str | None = None) -> dict:
    """The one check every revoke runs BEFORE it asks the provider (OBS-594). `key` names what is being revoked by
    its fingerprints: `hash`, `labels`, `names` (a scan's matches), `cluster` (a managed database), `unread` (why the
    door could not read the value). `value` travels apart from that record, as its own argument: held in memory,
    compared in constant time by the value sources, never printed or kept. `sources` are source objects or names
    from `GUARD_SOURCES`.

    Raises `RevokeRefused` when a consumer holds the key (`consumer-in-use`, each consumer named), when a source
    could not be read (`consumer-source-unreadable`, each one with its own `code`), or when a source is not one the
    guard knows, or none is named (`consumer-source-unknown`): a revoke never proceeds blind. Returns the finding
    (empty `consumers` and `degraded`, and what was `skipped` as not in use) when the revoke may go ahead. No
    override lives here: a door that offers a person's override today (`rotate --force`) journals it itself."""
    built, unknown = [], []
    for src in sources or []:
        if isinstance(src, str):
            make = GUARD_SOURCES.get(src)
            if make is None:
                unknown.append(src)
                continue
            src = make(key)
        if not hasattr(src, "find_key") and not hasattr(src, "find"):
            unknown.append(getattr(src, "name", type(src).__name__))
            continue
        built.append(src)
    if unknown or not built:
        names = ", ".join(map(str, unknown)) or "none"
        raise RevokeRefused(SOURCE_UNKNOWN, f"{subject}: the revoke was refused because its consumer check names a "
                                            f"source the guard does not know ({names}); nothing was revoked",
                            unread=[{"source": str(u), "code": "source-unknown"} for u in unknown])
    out = {"consumers": [], "degraded": [], "skipped": []}
    for src in built:
        name = getattr(src, "name", type(src).__name__)
        try:
            if hasattr(src, "find_key"):
                found, degraded, skipped = src.find_key(key)
            elif key.get("unread"):
                found, degraded, skipped = [], [{"source": name, "code": "value-unreadable",
                                                 "reason": str(key["unread"])}], []
            elif not value:
                found, degraded, skipped = [], [], [{"source": name, "code": "no-value",
                                                     "reason": "the key's value is held nowhere here, so there is no "
                                                               "copy of it to look for"}]
            else:
                found, degraded, skipped = src.find(value)
        except Exception as exc:                                                # noqa: BLE001  degraded, named
            found, degraded, skipped = [], [{"source": name, "code": "source-failed",
                                             "reason": f"{type(exc).__name__} while reading"}], []
        for d in degraded:
            d.setdefault("source", name)
            d.setdefault("code", "source-unreadable")
        for d in skipped:
            d.setdefault("source", name)
            d.setdefault("code", "not-in-use")
        out["consumers"] += found
        out["degraded"] += degraded
        out["skipped"] += skipped
    if out["consumers"]:
        held_by = [describe(c) for c in out["consumers"]]
        raise RevokeRefused(IN_USE, f"{subject} is what {'; '.join(held_by)} is reading right now; issue its "
                                    f"replacement first (`mint` on the key server), then revoke this one",
                            consumers=held_by, unread=out["degraded"])
    if out["degraded"]:
        lines = [f"{d['source']} ({d['code']}): {d.get('reason', 'could not be read')}" for d in out["degraded"]]
        raise RevokeRefused(SOURCE_UNREADABLE, f"{subject}: a revoke never proceeds blind, and who reads this key "
                                               f"could not be checked: {'; '.join(lines)}", unread=out["degraded"])
    return out


# ─────────────────────────── a roll that delivers (OBS-607, CP-11) ──────────

def empty_plan(holds: list[str] | None = None) -> dict:
    """A plan with no copy found: the door rolls in place as it always did (or refuses on `holds`)."""
    return {"old": None, "found": {"consumers": [], "degraded": [], "skipped": [], "_sources": {}}, "sources": [],
            "holds": list(holds or []), "restart": [], "copies": []}


def plan_roll(slot: str | None = None, path=None) -> dict:
    """What a door that rolls in place must know before it touches the key (Cloudflare names the token `path` it
    issued, Heroku the vault `slot`): the old value (in memory, never printed), every copy of it found by
    `consumer_sources`, the lines that `holds` the rotation before anything is minted (a source that could not be
    read, a copy this module cannot write: `blocks`), the services to `restart`, and the `copies` a successor must
    reach. No copy outside the vault: the door rolls in place. Copies, all writable: the door mints a successor beside
    the old key and `deliver_then_revoke`s. A value that could not be read holds, named by its exception's type."""
    if (slot is None) == (path is None):
        raise ValueError("plan_roll takes a vault slot or a token path, one of them")
    try:
        if path is not None:
            import private_io
            old = private_io.read(pathlib.Path(path)).strip() or None
        else:
            old = vault_value(*slot.split("/"))
    except Exception as exc:                                                    # noqa: BLE001  named, never echoed
        return empty_plan([f"source destination could not be read: the "
                           f"{'token file' if path is not None else 'slot'} could not be read ({type(exc).__name__})"])
    sources = consumer_sources(slot, exclude=path)
    found = find(old, sources)
    return {"old": old, "found": found, "sources": sources, "holds": blocks(found),
            "restart": [f"{describe(c)}: reads the slot at start; restart it: launchctl kickstart -k "
                        f"gui/$(id -u)/{c['label']}" for c in found["consumers"] if c.get("kind") == "launchd"],
            "copies": [c for c in found["consumers"] if c.get("kind") != "launchd"]}


def deliver_then_revoke(subject: str, found: dict, old: str, new: str, revoke, sources: list) -> dict:
    """Deliver `new` to every copy `found` named and read each back; then `revoke_guard` over the value sources (a
    copy of `old` still found anywhere refuses); then `revoke()`, the door's own call that deletes the old key. Answers
    {outcomes, held, revoked, code?}: `held` names each copy that holds the revoke (a delivery refused or not read
    back, a copy the guard still finds, a source it could not read, a revoke the provider refused), and the old key is
    then alive. Never raises for a consumer; never names a value."""
    outcomes = deliver_all(found, old, new)
    stuck = held(outcomes)
    if stuck:
        return {"outcomes": outcomes, "held": stuck, "revoked": False, "code": "consumer-not-delivered"}
    checks = [s for s in sources if not isinstance(s, LaunchdSource)]
    try:
        revoke_guard(subject, {}, checks, value=old)
    except RevokeRefused as exc:
        lines = exc.consumers + [f"source {d.get('source')} ({d.get('code')}): {d.get('reason', 'could not be read')}"
                                 for d in exc.unread]
        return {"outcomes": outcomes, "held": lines or [str(exc)], "revoked": False, "code": exc.code}
    try:
        revoke()
    except Exception as exc:                                                    # noqa: BLE001  held, named
        why = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        return {"outcomes": outcomes, "held": [f"the old key could not be revoked: {why}"], "revoked": False,
                "code": "revoke-failed"}
    return {"outcomes": outcomes, "held": [], "revoked": True}


def _digest_key() -> bytes:
    """This machine's random key (32 bytes, owner-only, made on first use) for `digest`: a held rotation records a
    keyed digest of the old value, never the value and never a plain hash of it."""
    import private_io
    path = paths.STATE / "state" / "consumer-digest.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        key = bytes.fromhex(private_io.read(path).strip())
        if len(key) != 32:
            raise RuntimeError("the consumer digest key is unreadable")
        return key
    key = os.urandom(32)
    private_io.write(path, key.hex() + "\n")
    return key


def digest(value: str) -> str:
    """HMAC-SHA256 of a value under this machine's key: says "the same value", never the value."""
    return "hmac-sha256:" + hmac.new(_digest_key(), value.encode("utf-8"), hashlib.sha256).hexdigest()


def held_record(found: dict, old: str, held_lines: list[str], predecessor, code: str | None = None) -> dict:
    """What a door keeps on its record while a rotation is held: the old key's id at the provider, the copies found
    (names and places), the lines that hold it, why (`code`), and the keyed digest that finds the old value again."""
    copies = [{k: v for k, v in c.items() if k in ("kind", "app", "app_id", "var", "path", "slot", "git", "project")}
              for c in found["consumers"] if c.get("kind") != "launchd"]
    return {"predecessor": predecessor, "digest": digest(old), "consumers": copies, "held": list(held_lines),
            "code": code}


def recover(record: dict, sources: list) -> str | None:
    """The old value of a held rotation, in memory: the first recorded copy that still holds a value with the
    recorded digest. None when no copy holds it any more (or none can be read)."""
    want = str(record.get("digest") or "")
    by_name = {getattr(s, "name", None): s for s in sources}
    for c in record.get("consumers") or []:
        src = by_name.get(c.get("kind"))
        if src is None or not hasattr(src, "current"):
            continue
        try:
            value = src.current(c)
        except Exception:                                                       # noqa: BLE001  that copy is unread
            continue
        if isinstance(value, str) and value and hmac.compare_digest(digest(value), want):
            return value
    return None


def finish_held(subject: str, record: dict, new: str | None, sources: list, revoke) -> dict:
    """Finish a held rotation: find the old value again (`recover`), find its copies afresh, deliver the successor
    (`new`, read from the door's own slot or file) to each, then revoke through the guard. Answers what
    `deliver_then_revoke` answers; a predecessor whose value no recorded copy holds any more stays held
    (`predecessor-missing`), since its other copies cannot be looked for without it."""
    if not new:
        return {"outcomes": [], "held": ["the door's own slot holds no successor to deliver"], "revoked": False,
                "code": "no-successor"}
    if record.get("code") == "revoke-failed":
        # Every copy was delivered and the guard passed before the provider refused the revoke: only the revoke is
        # left, and the old value is in no copy any more to look for.
        try:
            revoke()
        except Exception as exc:                                                # noqa: BLE001  held, named
            why = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
            return {"outcomes": [], "held": [f"the old key could not be revoked: {why}"], "revoked": False,
                    "code": "revoke-failed"}
        return {"outcomes": [], "held": [], "revoked": True}
    old = recover(record, sources)
    if not old:
        return {"outcomes": [], "revoked": False, "code": "predecessor-missing",
                "held": ["the old key's value is in none of its recorded copies any more, so its other copies "
                         "cannot be looked for; `--force` revokes it anyway"]}
    found = find(old, sources)
    early = blocks(found)
    if early:
        return {"outcomes": [], "held": early, "revoked": False, "code": "consumer-source-unreadable"}
    return deliver_then_revoke(subject, found, old, new, revoke, sources)
