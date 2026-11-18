#!/usr/bin/env python3
"""The coding sessions watched with the host's load, with history (OBS-630; docs/design/control-plane/sessions.md).

The account manager (Switchboard) starts and routes coding sessions; Observatory watches them and keeps the history;
Fabric Dashboards shows them. `sessions_view.summary(monitor=True)` reads this module's view of the manager beside its
process list, so each session row says how the manager reaches it, and the summary carries one host sample:

* THE ROUTE. A session reads `managed: true` when the manager launched it (a launch id, or its rows list the process
  on one of its groups) or when it runs in a group folder whose route the manager keeps: the folder's Claude Code
  settings point the session at the manager's local proxy (`ANTHROPIC_BASE_URL`), and `switchboard project bindings`
  lists those folders with their group. Before this, the reader asked only `sessions --machine`, which marks managed
  the launched processes alone and names no group at all, so most routed sessions read unmanaged and every
  `account_group` was null.
* THE SOURCES. The manager's sessions feed (`sessions-feed.json`, one record per session and one per host each
  minute) when it is present and fresh; until then its CLI: `sessions --rows` (each live process with its state, its
  launch's group and account), `project bindings` (the folder routes), `capacity` (the live-session cap and the
  board's share) and `accounts list` (each account's group and usage: `usage` alone names no group). Each call is
  bounded by `CLI_TIMEOUT`; what was not read is named in `degraded`, never read as zero or as "no room".
* THE HOST. One sample a tick: load per core, memory pressure, swap, free disk, the pool's use and the board's share,
  and the groups with an account with room. `record()` keeps them, and each session's state, last turn and group,
  in a bounded series file (`sessions-series.json`), the way `tools/disk_growth_series.py` keeps the disk's.
* THE FINDINGS are `tools/coding_session_findings.py`'s, read from the summary: a critical host, a group with no
  account with room, a session served by a group other than its own, a stalled session and a session outside the
  manager, each with an owner and a fix. Free disk under 5 GB is `host.disk_low` (tools/build_findings.py), so no
  fact is raised twice. Warnings and criticals reach the operator once per finding through
  `tools/notify_findings.py`, the engine's one notify path.

Never written: an account's label or address, a token, a full folder path. Folders are compared in memory and only
their group and last component leave this module.
"""
from __future__ import annotations

import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from typing import Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import paths                                                                       # noqa: E402
sys.path.append(str(pathlib.Path(__file__).resolve().parent / "dashboard"))
from page_util import iso as _iso                                                 # noqa: E402

#: Seconds each manager command may take.
CLI_TIMEOUT = 5
#: The feed: its file name in the manager's data folder, the variable that names another place, and its age limit.
FEED = "sessions-feed.json"
FEED_ENV = "OBSERVATORY_SESSIONS_FEED"
FEED_FRESH_SECONDS = 600
FEED_LIMIT = 4 * 1024 * 1024
#: The manager's data folder name (its bundle id).
MANAGER_DATA = "ai.passioncode.fabric-switchboard"
#: An account's usage older than this is not known.
USAGE_FRESH_SECONDS = 3600
#: The settings (config/machine.json) and their defaults. The stall line is never under the manager's own quiet
#: line (30 minutes): a session silent that long outside a turn is quiet, not busy, so only one inside a turn stalls.
SETTINGS = {"sessions_load_per_core_critical": 2.0, "sessions_memory_pressure_critical": 4,
            "sessions_stall_minutes": 30}
MANAGER_QUIET_MINUTES = 30
PRESSURE_LEVEL = {"normal": 1, "warning": 2, "critical": 4}
#: The manager's word for a live session it counts (inside a turn, or silent under its quiet line).
BUSY = "working"
#: The series: hours kept, host samples, sessions and samples a session.
SERIES = "sessions-series.json"
KEEP_HOURS = 48
KEEP_HOST = 400
KEEP_SESSIONS = 200
KEEP_SESSION_SAMPLES = 100
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")


def _gap(source: str, code: str, reason: str) -> dict:
    return {"source": source, "code": code, "reason": reason}


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _name(value) -> str | None:
    return value if isinstance(value, str) and NAME.fullmatch(value) else None


def _when(value) -> datetime | None:
    """An instant from epoch seconds or ISO text."""
    if _num(value) is not None and value > 0:
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def settings(config: dict | None = None) -> dict:
    """The thresholds: config/machine.json's `sessions_*` keys over the defaults. `config` is given by tests."""
    if config is None:
        config = {}
        for f in (paths.config_file("machine.json"), paths.ROOT / "defaults" / "machine.json"):
            try:
                config = json.loads(f.read_text(encoding="utf-8"))
                break
            except (OSError, ValueError):
                continue
    out = dict(SETTINGS)
    for key, default in SETTINGS.items():
        value = _num((config or {}).get(key)) if isinstance(config, dict) else None
        if value is not None and value > 0:
            out[key] = int(value) if isinstance(default, int) else value
    out["sessions_stall_minutes"] = max(out["sessions_stall_minutes"], MANAGER_QUIET_MINUTES)
    return out


# THE MANAGER'S ANSWERS

def _cli(binary: str, runner: Callable, argv: list[str], source: str, degraded: list[dict]):
    """One manager command's `data`, or None with its reason in `degraded`."""
    try:
        code, out = runner([binary, "--json", *argv], CLI_TIMEOUT)
    except subprocess.TimeoutExpired:
        degraded.append(_gap(source, "timeout", f"the account manager did not answer `{' '.join(argv)}` in "
                                                f"{CLI_TIMEOUT} s"))
        return None
    except FileNotFoundError:
        degraded.append(_gap(source, "not-installed", "the account manager's command is gone"))
        return None
    except OSError as exc:
        degraded.append(_gap(source, "failed", f"the account manager could not be run: {type(exc).__name__}"))
        return None
    if code != 0:
        degraded.append(_gap(source, "failed", f"the account manager answered `{' '.join(argv)}` with exit {code}"))
        return None
    try:
        doc = json.loads(out)
    except ValueError:
        doc = None
    data = doc.get("data") if isinstance(doc, dict) else None
    if data is None:
        degraded.append(_gap(source, "malformed", f"the account manager's answer to `{' '.join(argv)}` has no data"))
    return data


def _room(account: dict, now: datetime) -> tuple[bool | None, datetime | None]:
    """(whether this account has room now, its nearest reset). None: its usage is not known."""
    usage = account.get("usage") if isinstance(account.get("usage"), dict) else None
    if usage is None:
        return None, None
    seen = _when(usage.get("observed_at"))
    if seen is None or (now - seen).total_seconds() > USAGE_FRESH_SECONDS:
        return None, None
    windows = [w for w in usage.get("windows") or [] if isinstance(w, dict)]
    used = [_num(w.get("used_percent")) for w in windows] or [_num(usage.get("used_percent"))]
    if any(u is None for u in used):
        return None, None
    resets = [r for r in (_when(w.get("resets_at")) for w in windows if (_num(w.get("used_percent")) or 0) >= 100)
              if r is not None] or [r for r in [_when(usage.get("resets_at"))] if r is not None]
    full = any(u >= 100 for u in used)
    return (not full), (min(resets) if full and resets else None)


def groups_with_room(accounts: list, now: datetime) -> dict:
    """{group: {accounts, with_room, unknown, next_reset_at}} over the accounts this machine serves: an account
    turned off, moved to another machine or given to an agent serves no session here and is not counted."""
    out: dict[str, dict] = {}
    for a in accounts or []:
        if not isinstance(a, dict) or not _name(a.get("pool")):
            continue
        if a.get("enabled") is False or a.get("elsewhere") is True or a.get("location") or a.get("assigned_to"):
            continue
        g = out.setdefault(a["pool"], {"accounts": 0, "with_room": 0, "unknown": 0, "next_reset_at": None})
        g["accounts"] += 1
        room, reset = _room(a, now)
        if room is None:
            g["unknown"] += 1
        elif room:
            g["with_room"] += 1
        elif reset is not None and (g["next_reset_at"] is None or _iso(reset) < g["next_reset_at"]):
            g["next_reset_at"] = _iso(reset)
    return dict(sorted(out.items()))


def _capacity(data) -> dict | None:
    if not isinstance(data, dict):
        return None
    keep = {k: _int(data.get(k)) for k in ("cap", "used", "free", "total", "board_share", "board_used", "board_free")}
    return keep if any(v is not None for v in keep.values()) else None


def _empty_view(source: str | None) -> dict:
    return {"source": source, "at": None, "by_pid": {}, "by_launch": {}, "routes": [], "capacity": None,
            "groups": None, "host": None}


def cli_view(binary: str, runner: Callable, now: datetime, degraded: list[dict]) -> dict:
    """The manager's CLI answers, normalised. Labels (account addresses) and folders stay in memory."""
    view = _empty_view("cli")
    rows = _cli(binary, runner, ["sessions", "--rows"], "manager-rows", degraded)
    bindings = _cli(binary, runner, ["project", "bindings"], "manager-routes", degraded)
    capacity = _cli(binary, runner, ["capacity"], "manager-capacity", degraded)
    accounts = _cli(binary, runner, ["accounts", "list"], "manager-accounts", degraded)
    accounts = accounts.get("accounts") if isinstance(accounts, dict) else None
    label_group = {a["label"]: a["pool"] for a in accounts or []
                   if isinstance(a, dict) and isinstance(a.get("label"), str) and _name(a.get("pool"))}
    rows_list = rows.get("rows") if isinstance(rows, dict) else None
    if rows is not None and not isinstance(rows_list, list):
        degraded.append(_gap("manager-rows", "malformed", "the account manager's rows are not a list"))
    for r in rows_list or []:
        if not isinstance(r, dict) or r.get("group") not in ("running", "waiting", "needs_you"):
            continue
        group = _name(r.get("pool"))
        info = {"group": group, "served_group": label_group.get(r.get("account")) or group,
                "state": _name(r.get("why")), "quiet_minutes": _int(r.get("quiet_minutes"))}
        if _int(r.get("pid")) is not None:
            view["by_pid"][r["pid"]] = info
        if isinstance(r.get("launch_id"), str) and r["launch_id"]:
            view["by_launch"][r["launch_id"]] = info
    blist = bindings.get("bindings") if isinstance(bindings, dict) else None
    if bindings is not None and not isinstance(blist, list):
        degraded.append(_gap("manager-routes", "malformed", "the account manager's folder routes are not a list"))
    for b in blist or []:
        if isinstance(b, dict) and isinstance(b.get("folder"), str) and b["folder"].strip() and _name(b.get("pool")):
            view["routes"].append((b["folder"].replace("\\", "/").rstrip("/"), b["pool"]))
    view["capacity"] = _capacity(capacity)
    if accounts is not None:
        view["groups"] = groups_with_room(accounts, now)
    if isinstance(rows, dict):
        view["at"] = _iso(_when(rows.get("observed_at")) or now)
    return view


# THE FEED

def feed_path(env=None, home: pathlib.Path | None = None, platform: str | None = None) -> pathlib.Path:
    """Where the manager writes its sessions feed: `OBSERVATORY_SESSIONS_FEED`, else its data folder."""
    env = os.environ if env is None else env
    if env.get(FEED_ENV):
        return pathlib.Path(env[FEED_ENV]).expanduser()
    home = pathlib.Path(home or pathlib.Path.home())
    if (platform or sys.platform) == "darwin":
        return home / "Library" / "Application Support" / MANAGER_DATA / FEED
    base = pathlib.Path(env["XDG_DATA_HOME"]) if env.get("XDG_DATA_HOME") else home / ".local" / "share"
    return base / MANAGER_DATA / FEED


#: What the feed's records are read for, and the names each may carry (its schema is not published yet).
FEED_SESSION = {"pid": ("pid",), "group": ("pool", "group", "account_group"),
                "served_group": ("served_pool", "served_group", "account_pool"), "state": ("state", "why"),
                "quiet_minutes": ("quiet_minutes",)}
FEED_HOST = {"load_per_core": ("load_per_core",), "memory_pressure": ("memory_pressure",),
             "swap_gb": ("swap_gb",), "free_disk_gb": ("free_disk_gb",), "capacity": ("capacity",),
             "groups": ("groups",)}


def _pick(record: dict, names: tuple[str, ...]):
    for n in names:
        if n in record:
            return record[n]
    return None


def read_feed(path: pathlib.Path, now: datetime, degraded: list[dict]) -> dict | None:
    """The feed normalised into the CLI view's shape, or None when it is absent, unreadable or stale (then the CLI is
    read). Tolerant: a field a record does not carry is named once in `degraded` (`missing-fields`)."""
    import osprivacy
    try:
        doc = json.loads(osprivacy.read_regular(path, FEED_LIMIT).decode("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        degraded.append(_gap("feed", "unreadable", f"the account manager's sessions feed is unreadable: "
                                                  f"{type(exc).__name__}"))
        return None
    if not isinstance(doc, dict):
        degraded.append(_gap("feed", "malformed", "the account manager's sessions feed is not an object"))
        return None
    records = [r for r in doc.get("records") or [] if isinstance(r, dict)]
    sessions = [r for r in doc.get("sessions") or [] if isinstance(r, dict)] + \
        [r for r in records if r.get("kind") == "session"]
    hosts = ([doc["host"]] if isinstance(doc.get("host"), dict) else []) + \
        [r for r in records if r.get("kind") == "host"]
    host = max(hosts, key=lambda h: str(_when(_pick(h, ("at", "observed_at"))) or ""), default=None)
    at = _when(_pick(doc, ("observed_at", "at", "generated_at"))) or \
        (_when(_pick(host, ("at", "observed_at"))) if host else None)
    if at is None or (now - at).total_seconds() > FEED_FRESH_SECONDS:
        degraded.append(_gap("feed", "stale", "the account manager's sessions feed is older than "
                                              f"{FEED_FRESH_SECONDS // 60} minutes or undated: its CLI is read"))
        return None
    view = _empty_view("feed")
    view["at"] = _iso(at)
    missing: set[str] = set()
    for r in sessions:
        info = {}
        for key, names in FEED_SESSION.items():
            value = _pick(r, names)
            if value is None:
                missing.add(f"session.{key}")
            info[key] = value
        info = {"group": _name(info["group"]), "served_group": _name(info["served_group"]) or _name(info["group"]),
                "state": _name(info["state"]), "quiet_minutes": _int(info["quiet_minutes"])}
        if _int(r.get("pid")) is not None:
            view["by_pid"][r["pid"]] = info
        if isinstance(r.get("launch_id"), str) and r["launch_id"]:
            view["by_launch"][r["launch_id"]] = info
        if isinstance(r.get("folder"), str) and r["folder"].strip() and info["group"]:
            view["routes"].append((r["folder"].replace("\\", "/").rstrip("/"), info["group"]))
    if host is None:
        missing.add("host")
    else:
        sample = {}
        for key, names in FEED_HOST.items():
            value = _pick(host, names)
            if value is None:
                missing.add(f"host.{key}")
            sample[key] = value
        view["capacity"] = _capacity(sample["capacity"])
        groups = sample["groups"]
        if isinstance(groups, dict):
            view["groups"] = {g: {"accounts": _int(v.get("accounts")) or 0, "with_room": _int(v.get("with_room")) or 0,
                                  "unknown": _int(v.get("unknown")) or 0,
                                  "next_reset_at": v.get("next_reset_at") if isinstance(v.get("next_reset_at"), str)
                                  else None}
                              for g, v in sorted(groups.items()) if _name(g) and isinstance(v, dict)}
        pressure = sample["memory_pressure"]
        view["host"] = {"load_per_core": _num(sample["load_per_core"]),
                        "memory_pressure": pressure if pressure in PRESSURE_LEVEL else None,
                        "swap_gb": _num(sample["swap_gb"]), "free_disk_gb": _num(sample["free_disk_gb"])}
    if missing:
        degraded.append(_gap("feed", "missing-fields", "the account manager's sessions feed does not carry: "
                                                      + ", ".join(sorted(missing))))
    return view


def manager_view(binary: str | None, runner: Callable, now: datetime, degraded: list[dict],
                 feed: pathlib.Path | None = None) -> dict:
    """The feed when present and fresh, else the CLI; with neither, an empty view (named in `degraded`)."""
    view = read_feed(feed if feed is not None else feed_path(), now, degraded)
    if view is not None:
        return view
    if not binary:
        return _empty_view(None)
    return cli_view(binary, runner, now, degraded)


# ONE SESSION

def _folder_group(folder, routes: list[tuple[str, str]]) -> str | None:
    """The group whose folder route holds `folder`: the longest folder that is it or contains it."""
    if not isinstance(folder, str) or not folder.strip():
        return None
    folder = folder.replace("\\", "/").rstrip("/")
    best = None
    for root, group in routes:
        if root and (folder == root or folder.startswith(root + "/")) and (best is None or len(root) > len(best[0])):
            best = (root, group)
    return best[1] if best else None


def route(raw: dict, view: dict) -> dict:
    """What the manager says of one process of `sessions --machine`: whether it is managed and how (`route`:
    `launch`, `folder-route` or None), its group (`account_group`: the group that serves it), the group its folder
    belongs to (`folder_group`), the manager's word for it (`manager_state`), `busy` and `quiet_minutes`.
    A folder route holds for Claude Code only: it lives in the folder's Claude Code settings."""
    launch = raw.get("launch_id") if isinstance(raw.get("launch_id"), str) and raw.get("launch_id") else None
    info = (view.get("by_launch") or {}).get(launch) or (view.get("by_pid") or {}).get(raw.get("pid")) or {}
    agent = str(raw.get("agent") or "")
    folder_group = _folder_group(raw.get("folder"), view.get("routes") or []) if agent.startswith("claude") else None
    launched = raw.get("managed") is True or launch is not None or info.get("group") is not None
    served = info.get("served_group") or info.get("group") or folder_group
    state = info.get("state")
    quiet = info.get("quiet_minutes")
    if quiet is None:
        quiet = _int(raw.get("quiet_minutes"))
    return {"managed": launched or folder_group is not None,
            "route": "launch" if launched else "folder-route" if folder_group else None,
            "account_group": served, "folder_group": folder_group, "manager_state": state,
            "busy": (state == BUSY) if state else None, "quiet_minutes": quiet}


# THE HOST

def _memory(scratch: pathlib.Path, now: datetime) -> dict:
    import memory_snapshot
    return memory_snapshot.read_sources(scratch=scratch, at=now).get("memory") or {}


def host_sample(view: dict, now: datetime, degraded: list[dict], *, scratch: pathlib.Path | None = None,
                disk_root: pathlib.Path | None = None, loadavg: Callable | None = None,
                cpu_count: Callable | None = None, disk_usage: Callable = shutil.disk_usage,
                memory: Callable | None = None) -> dict:
    """One host sample: the feed's host record where it carries a value, else measured here (load from the OS,
    memory pressure and swap from the memory receipts, free disk of the home volume). Each value not read is None
    with its reason in `degraded`."""
    feed = view.get("host") or {}
    sample = {"at": _iso(now), "source": view.get("source") if feed else "host",
              "load_per_core": feed.get("load_per_core"), "memory_pressure": feed.get("memory_pressure"),
              "swap_gb": feed.get("swap_gb"), "free_disk_gb": feed.get("free_disk_gb")}
    if sample["load_per_core"] is None:
        try:
            load = (loadavg or os.getloadavg)()[0]
            cores = (cpu_count or os.cpu_count)()
            sample["load_per_core"] = round(load / cores, 2) if cores else None
        except (OSError, AttributeError, TypeError, IndexError):
            sample["load_per_core"] = None
        if sample["load_per_core"] is None:
            degraded.append(_gap("host", "load-unavailable", "the load per core could not be read on this system"))
    if sample["memory_pressure"] is None or sample["swap_gb"] is None:
        try:
            mem = (memory or _memory)(scratch if scratch is not None else paths.SCRATCH, now)
        except (OSError, ValueError, TypeError) as exc:
            mem = {}
            degraded.append(_gap("host", "memory-unreadable", f"the memory receipts are unreadable: "
                                                              f"{type(exc).__name__}"))
        level = ((mem.get("pressure") or {}).get("level")) if isinstance(mem, dict) else None
        if sample["memory_pressure"] is None:
            sample["memory_pressure"] = level if level in PRESSURE_LEVEL else None
            if sample["memory_pressure"] is None:
                degraded.append(_gap("host", "pressure-unknown", "memory pressure was not measured recently"))
        swap = _num(mem.get("swap_used_mb")) if isinstance(mem, dict) else None
        if sample["swap_gb"] is None and swap is not None:
            sample["swap_gb"] = round(swap / 1024, 2)
    if sample["free_disk_gb"] is None:
        try:
            sample["free_disk_gb"] = round(disk_usage(str(disk_root or pathlib.Path.home())).free / 1e9, 1)
        except OSError as exc:
            degraded.append(_gap("host", "disk-unreadable", f"free disk could not be measured: {type(exc).__name__}"))
    cap = view.get("capacity") or {}
    sample.update({"sessions_used": cap.get("used"), "sessions_cap": cap.get("cap"),
                   "board_used": cap.get("board_used"), "board_share": cap.get("board_share")})
    groups = view.get("groups")
    sample["groups_with_room"] = sorted(g for g, v in groups.items() if v.get("with_room")) if groups is not None \
        else None
    return sample


# THE HISTORY

def series_file() -> pathlib.Path:
    return paths.STATE / SERIES


def read_series(path: pathlib.Path | None = None) -> dict:
    """{host: [sample...], sessions: {key: [[at, state, last_turn_at, group], ...]}}; unreadable is empty."""
    try:
        doc = json.loads((path or series_file()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"host": [], "sessions": {}}
    host = [s for s in (doc.get("host") if isinstance(doc, dict) else None) or [] if isinstance(s, dict)
            and _when(s.get("at"))]
    sessions = {}
    for key, rows in ((doc.get("sessions") if isinstance(doc, dict) else None) or {}).items():
        if _name(key) and isinstance(rows, list):
            kept = [r for r in rows if isinstance(r, list) and len(r) == 4 and _when(r[0])]
            if kept:
                sessions[key] = kept
    return {"host": host, "sessions": sessions}


def session_key(row: dict) -> str | None:
    """A session's key in the history and in its findings: its launch id, else its process id."""
    if isinstance(row.get("launch_id"), str) and _name(row["launch_id"]):
        return row["launch_id"]
    return f"pid:{row['pid']}" if _int(row.get("pid")) is not None else None


HOST_KEYS = ("at", "source", "load_per_core", "memory_pressure", "swap_gb", "free_disk_gb", "sessions_used",
             "sessions_cap", "board_used", "board_share", "groups_with_room")


def record(doc: dict, path: pathlib.Path | None = None) -> dict:
    """Add the summary's host sample and each session's state, last turn and group; drop what is older than
    `KEEP_HOURS`; write. Returns the series. Raises OSError when the file cannot be written."""
    series = read_series(path)
    now = _when(doc.get("measuredAt")) or datetime.now(timezone.utc)
    host = doc.get("host")
    if isinstance(host, dict):
        series["host"].append({k: host.get(k) for k in HOST_KEYS})
    for s in doc.get("sessions") or []:
        key = session_key(s) if isinstance(s, dict) else None
        if key:
            state = s.get("manager_state") or s.get("state")
            series["sessions"].setdefault(key, []).append(
                [_iso(now), state if _name(state) else None, s.get("last_turn_at"), _name(s.get("account_group"))])
    floor = _iso(datetime.fromtimestamp(now.timestamp() - KEEP_HOURS * 3600, timezone.utc))
    series["host"] = [h for h in series["host"] if str(h.get("at")) >= floor][-KEEP_HOST:]
    kept = {k: [r for r in v if str(r[0]) >= floor][-KEEP_SESSION_SAMPLES:] for k, v in series["sessions"].items()}
    kept = {k: v for k, v in kept.items() if v}
    if len(kept) > KEEP_SESSIONS:
        kept = {k: kept[k] for k in sorted(kept, key=lambda k: kept[k][-1][0], reverse=True)[:KEEP_SESSIONS]}
    series["sessions"] = kept
    import atomic
    target = path or series_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(target, {"schema_version": 1, **series})
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return series
