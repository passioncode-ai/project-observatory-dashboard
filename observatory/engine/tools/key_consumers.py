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

A SOURCE THAT COULD NOT BE READ IS `degraded`, NEVER "NO CONSUMER". The door holds the revocation on it. A source
that is not in use on this machine (no Heroku token stashed and the integration off; no vault slot to look up
services by) is `skipped`, with the reason, and holds nothing.
"""
from __future__ import annotations

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
    name = "heroku"

    def __init__(self, tokens=heroku_tokens, api=None):
        self._tokens = tokens
        self._api = api
        self._token_of: dict[str, str] = {}

    @property
    def api(self):
        if self._api is None:
            self._api = _HerokuDoor()
        return self._api

    def find(self, old: str) -> tuple[list[dict], list[dict], list[dict]]:
        tokens, skip, bad = self._tokens()
        if skip:
            return [], [], [{"source": self.name, "reason": skip}]
        if bad or not tokens:
            return [], [{"source": self.name, "reason": bad or "no Heroku token"}], []
        found, degraded, apps = [], [], {}
        for tok in tokens:
            try:
                for app in self.api.list("/apps", tok):
                    if isinstance(app, dict) and app.get("id") and app["id"] not in apps:
                        apps[app["id"]] = (app.get("name") or app["id"], tok)
            except RuntimeError as exc:
                degraded.append({"source": self.name, "reason": f"the application list could not be read: {exc}"})
        for app_id, (app_name, tok) in sorted(apps.items(), key=lambda kv: kv[1][0]):
            try:
                config = self.api.request("GET", f"/apps/{app_id}/config-vars", tok)
            except RuntimeError as exc:
                degraded.append({"source": self.name, "app": app_name,
                                 "reason": f"config vars of {app_name} could not be read: {exc}"})
                continue
            if not isinstance(config, dict):
                degraded.append({"source": self.name, "app": app_name,
                                 "reason": f"config vars of {app_name} came back in no known shape"})
                continue
            for var, value in sorted(config.items()):
                if same(value, old):
                    self._token_of[app_id] = tok
                    found.append({"kind": "heroku", "app": app_name, "app_id": app_id, "var": var})
        return found, degraded, []

    def deliver(self, c: dict, new: str) -> None:
        tok = self._token_of.get(c["app_id"])
        if tok is None:
            raise RuntimeError("no token reached this application in this run")
        # The body is built here, in memory, and travels as the request body: never argv, never a log.
        self.api.request("PATCH", f"/apps/{c['app_id']}/config-vars", tok, {c["var"]: new})

    def read_back(self, c: dict, new: str) -> bool:
        config = self.api.request("GET", f"/apps/{c['app_id']}/config-vars", self._token_of[c["app_id"]])
        return isinstance(config, dict) and same(config.get(c["var"]), new)


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
    name = "env-file"

    def __init__(self, inventory: pathlib.Path | None = None, exclude: tuple = (), projects: pathlib.Path | None = None):
        self.inventory = inventory or ENV_INVENTORY
        self.projects = projects or paths.DATA
        self.exclude = {str(pathlib.Path(p)) for p in exclude}
        self._env = None

    @property
    def env(self):
        if self._env is None:
            self._env = _scan_env()
        return self._env

    def find(self, old: str) -> tuple[list[dict], list[dict], list[dict]]:
        if not pathlib.Path(self.inventory).exists() and not pathlib.Path(self.projects).is_dir():
            # No projects folder on this machine, so no inventory to take: not in use, not unread.
            return [], [], [{"source": self.name, "reason": "no projects folder on this machine, so no env file is "
                                                            "inventoried"}]
        try:
            doc = json.loads(pathlib.Path(self.inventory).read_text(encoding="utf-8"))
            files, root = doc["files"], pathlib.Path(doc["root"])
            if not isinstance(files, list):
                raise ValueError("files is not a list")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return [], [{"source": self.name, "reason": f"the local env inventory could not be read "
                                                        f"({type(exc).__name__}); run `project-observatory full env`"}], []
        found, degraded = [], []
        unread = [d for d in (doc.get("degraded") or []) if isinstance(d, dict)]
        if unread:
            degraded.append({"source": self.name,
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
                degraded.append({"source": self.name, "path": str(path),
                                 "reason": f"{path} could not be read ({type(exc).__name__})"})
                continue
            pairs, _ = self.env.parse(text)
            for var, value in pairs:
                if same(value, old):
                    found.append({"kind": "env-file", "path": str(path), "var": var,
                                  "git": rec.get("git"), "project": rec.get("project")})
        return found, degraded, []

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


# ─────────────────────────── launchd services ───────────────────────────────

class LaunchdSource:
    name = "launchd"

    def __init__(self, slot: tuple[str, str, str] | None, consumers=None):
        self.slot = slot
        self._consumers = consumers

    def find(self, old: str) -> tuple[list[dict], list[dict], list[dict]]:
        if not self.slot:
            return [], [], [{"source": self.name, "reason": "the key is not delivered to a vault slot, so no "
                                                            "service started from one"}]
        try:
            fn = self._consumers
            if fn is None:
                import use_secret
                fn = use_secret.consumers
            rows = fn(*self.slot)
        except Exception as exc:                                                # noqa: BLE001  degraded, named
            return [], [{"source": self.name, "reason": f"the services started with the slot could not be read "
                                                        f"({type(exc).__name__})"}], []
        return [{"kind": "launchd", "label": r.get("consumer", "?"), "started": r.get("at")}
                for r in rows if isinstance(r, dict)], [], []


# ─────────────────────────── the finder and the delivery ────────────────────

def default_sources(vault_slot: tuple[str, str, str] | None = None, exclude: tuple = ()) -> list:
    return [HerokuSource(), EnvFileSource(exclude=exclude), LaunchdSource(vault_slot)]


def find(old: str | None, sources: list) -> dict:
    """Every consumer of `old` the sources can see, what could not be read, and what was not in use."""
    out = {"consumers": [], "degraded": [], "skipped": [], "_sources": {}}
    if not old:
        out["skipped"].append({"source": "value", "reason": "the destination holds no value, so there is no copy "
                                                            "of it to look for"})
        return out
    for src in sources:
        try:
            found, degraded, skipped = src.find(old)
        except Exception as exc:                                                # noqa: BLE001  degraded, named
            found, degraded, skipped = [], [{"source": src.name, "reason": f"{type(exc).__name__} while reading"}], []
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
