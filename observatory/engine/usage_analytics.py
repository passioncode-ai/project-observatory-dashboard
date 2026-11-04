"""Anonymous usage and error events (OBS-324): what is sent, when, and the switch that stops it.

The maintainers decided on 2026-11-04 that every install, the ones already installed included,
sends anonymous data about usage and errors, only to improve the product: on by default, a
notice the first time the product starts after the release that brings it, and a switch in the
product's settings that turns it off. This module is that whole path; nothing else in the engine
talks to the analytics host.

WHAT IS SENT. Only the events in `EVENTS`, each with only the properties it declares, every value
either a word from a fixed list, a name the engine itself defines (a command, an MCP tool, an
exception class) or a number. To each event the sink (`wire_event`, the one place every event
passes through) adds three properties: `iid`, a random UUID made for this purpose only and kept
in this workspace (never derived from the machine, an account or a person); `environment`
(`production` for an installed release, `sandbox` for a checkout, an editable install or a
pre-release); and `platform`. The Aptabase system properties carry the engine version, the OS
name and version and the Python version. NEVER: keys, vault values, paths, repository, project,
organisation or person names, registry content, URLs, prompts, messages, arguments or free text.
`record` refuses a property it does not know and a value that is not of its declared shape, so
a caller cannot add one by mistake.

WHEN. `record` appends one line to a local queue (`store/analytics/queue.jsonl`) and returns: no
network, no wait, and it never raises. The maintenance job (every 15 minutes) calls `flush`,
which sends the queue in batches of at most 25 to the analytics host, drops events older than
23 hours (the host refuses anything older than a day), retries a transport error, 429 or 5xx a
bounded number of times and never retries 400 or 404. Every failure lands in a local counter
(`store/analytics/counters.json`), never in an exception or a message to the person. The queue
is bounded (`QUEUE_LIMIT_BYTES`); past it a new event is counted as dropped.

THE SWITCH is the file `analytics` in the workspace home, like `auto-update`: absent means on,
only the word `off` turns it off. So an install that predates this release is on after the
update, and a person who turned it off stays off through every update. `OBSERVATORY_ANALYTICS=off`
(also `0`, `false`, `no`) and `DO_NOT_TRACK` (any value but empty or `0`) turn it off for one
process without touching the file: what CI and a shell that wants nothing sent use. Turning it
off deletes the install id and the queue; turning it on again makes a new install id.

THE APP KEY. The analytics host accepts events for a registered app only. The key is read from
`OBSERVATORY_ANALYTICS_APP_KEY`, else from the file `analytics-app-key` beside this module, which
a release build writes; it is never in the repository. Without one, nothing is collected or
queued, and `full analytics status` says so (`app_key: absent`).
"""
from __future__ import annotations

import datetime
import json
import os
import platform as _platform
import random
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Callable

import configuration as config
import osprivacy

HOST = "https://analytics.sshlg.me"
ENDPOINT = HOST + "/api/v0/events"
#: The switch, a file in the workspace home (absent = on; only `off` is off).
SWITCH_FILE = "analytics"
SWITCH_LABEL = "Send anonymous usage and error data"
SWITCH_ENV = "OBSERVATORY_ANALYTICS"
APP_KEY_ENV = "OBSERVATORY_ANALYTICS_APP_KEY"
APP_KEY_FILE = config.SOURCE / "analytics-app-key"
#: Forces the environment of every event (`production` or `sandbox`); a test or a distributor
#: that knows better than the install check.
ENVIRONMENT_ENV = "OBSERVATORY_ANALYTICS_ENVIRONMENT"
OFF_WORDS = frozenset({"off", "0", "false", "no"})
#: At most this many events in one request (the host refuses 26 or more).
BATCH = 25
#: The host refuses an event older than a day; an older queued one is dropped before sending.
MAX_AGE = datetime.timedelta(hours=23)
#: The queue never grows past this: about 2,000 events, a few days of heavy use.
QUEUE_LIMIT_BYTES = 512 * 1024
#: One request's timeout, and how many times a batch is tried (the first try included).
TIMEOUT_SECONDS = 5.0
ATTEMPTS = 3
#: How long the first-run notice stays on the dashboard after it was first shown there.
DASHBOARD_NOTICE_DAYS = 7

NOTICE = ("Project Observatory sends anonymous usage and error data to help improve it: which "
          "commands and tools run, their outcome and duration, error kinds, the version and the "
          "platform. It never sends keys, paths, project or person names, or anything from your "
          "workspace. To turn it off: project-observatory full analytics off")

#: A name the engine defines: a command (`doctor`, `auto-update`) or a tool (`observatory_status`).
_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,59}\Z")
#: An exception class name.
_KIND = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,59}\Z")
_INT = "int"

#: THE CATALOGUE IN CODE. Each event: its properties and, for each, the words it may carry, a
#: pattern, or `int`. docs/analytics-events.md is the same list for people, with the screens
#: and the file:line of every emit; tests/test_usage_analytics.py holds the two together.
EVENTS: dict[str, dict[str, object]] = {
    "command_run": {"surface": frozenset({"cli"}), "command": _NAME,
                    "outcome": frozenset({"ok", "error", "refused"}), "exit_code": _INT, "duration_ms": _INT},
    "mcp_tool_called": {"surface": frozenset({"mcp"}), "tool": _NAME,
                        "outcome": frozenset({"ok", "error"}), "duration_ms": _INT},
    "tick_finished": {"surface": frozenset({"tick"}), "outcome": frozenset({"ok", "failed", "skipped"}),
                      "steps_failed": _INT, "duration_ms": _INT},
    "error_raised": {"surface": frozenset({"cli", "mcp", "tick"}), "kind": _KIND},
    "analytics_notice_shown": {"surface": frozenset({"cli", "dashboard"})},
}
#: Added by the sink to every event; a caller never sets them.
SINK_PROPS = ("iid", "environment", "platform")

#: (url, body, headers, timeout) -> HTTP status; raises OSError on a transport failure.
Transport = Callable[[str, bytes, dict, float], int]


class Refused(ValueError):
    """An event or a property the catalogue does not hold. A programming error, never a
    runtime one: `record` swallows it into the counter `refused`."""


# --- where things are ----------------------------------------------------------------

def _base(base: Path | None) -> Path:
    return Path(base) if base is not None else config.home()


def folder(base: Path | None = None) -> Path:
    return _base(base) / "store" / "analytics"


def is_workspace(base: Path | None = None) -> bool:
    """Only an initialised workspace gets an analytics folder: a command run before `full init`
    must not create the home it was pointed at."""
    try:
        return (_base(base) / "workspace.json").is_file()
    except Exception:  # noqa: BLE001
        return False


def switch_path(base: Path | None = None) -> Path:
    return _base(base) / SWITCH_FILE


def _private_write(path: Path, text: str) -> None:
    """Owner-only, atomic, never through a link."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise OSError(f"{path.name} is a link")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = osprivacy.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | osprivacy.NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _read_json(path: Path) -> dict:
    try:
        if path.is_symlink():
            return {}
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


# --- the switch ----------------------------------------------------------------------

def read_switch(base: Path | None = None) -> str | None:
    """`on` or `off` from the switch file, or None when there is none. Only `off` is off; a
    link or a folder in its place is not a switch."""
    file = switch_path(base)
    try:
        if file.is_symlink() or not file.is_file():
            return None
        text = file.read_text(encoding="utf-8", errors="replace")[:64]
    except OSError:
        return None
    return "off" if text.strip().lower() == "off" else "on"


def env_off(environ: dict | None = None) -> str | None:
    """The environment variable that turns collection off for this process, or None."""
    env = os.environ if environ is None else environ
    if str(env.get(SWITCH_ENV, "")).strip().lower() in OFF_WORDS:
        return SWITCH_ENV
    dnt = str(env.get("DO_NOT_TRACK", "")).strip()
    if dnt and dnt != "0":
        return "DO_NOT_TRACK"
    return None


def enabled(base: Path | None = None, environ: dict | None = None) -> bool:
    """On unless a person turned it off in the file or this process's environment says off."""
    return read_switch(base) != "off" and env_off(environ) is None


def collecting(base: Path | None = None, environ: dict | None = None) -> bool:
    """On, with an App-Key, in an initialised workspace: only then is anything queued, sent or
    announced. A build without a key says nothing it does not do."""
    return enabled(base, environ) and app_key(environ)[0] is not None and is_workspace(base)


def app_key(environ: dict | None = None) -> tuple[str | None, str]:
    """(the App-Key, where it came from: `env`, `build` or `absent`). The key is never printed."""
    env = os.environ if environ is None else environ
    value = str(env.get(APP_KEY_ENV, "")).strip()
    source = "env"
    if not value:
        try:
            value = APP_KEY_FILE.read_text(encoding="utf-8").strip()
            source = "build"
        except OSError:
            value = ""
    if re.fullmatch(r"A-[A-Z]{2}-\d{10}", value.upper()):
        return value.upper(), source
    return None, "absent"


def environment(environ: dict | None = None) -> str:
    """`production` for an installed release, `sandbox` for anything else (a checkout, an
    editable install, a pre-release). Decided from where the code lives, which costs nothing:
    an installed wheel sits in a `site-packages` folder; an editable install or a checkout
    does not."""
    env = os.environ if environ is None else environ
    forced = str(env.get(ENVIRONMENT_ENV, "")).strip()
    if forced in ("production", "sandbox"):
        return forced
    if "-" in config.VERSION:
        return "sandbox"
    parts = {p.lower() for p in config.SOURCE.parts}
    return "production" if parts & {"site-packages", "dist-packages"} else "sandbox"


def platform_name() -> str:
    """Growth's `platform`: `macos` on a Mac, `server` on Linux and Windows (its list has no
    desktop value for them); the OS itself travels in `systemProps.osName`."""
    return "macos" if sys.platform == "darwin" else "server"


def write_switch(base: Path, on: bool) -> None:
    """The person's choice. Off also deletes the install id and the queue: nothing that was
    waiting is sent, and turning it on again starts a new install id."""
    _private_write(switch_path(base), "on\n" if on else "off\n")
    if not on:
        for name in ("install-id", "queue.jsonl"):
            try:
                (folder(base) / name).unlink()
            except OSError:
                pass


# --- the install id and the counters -------------------------------------------------

def install_id(base: Path | None = None, *, create: bool = True) -> str | None:
    """This workspace's random install id: a UUID4 made for analytics only, never derived from
    the machine, an account or a person."""
    file = folder(base) / "install-id"
    try:
        value = file.read_text(encoding="utf-8").strip().lower()
        if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", value):
            return value
    except OSError:
        pass
    if not create:
        return None
    value = str(uuid.uuid4())
    _private_write(file, value + "\n")
    return value


def count(base: Path | None, name: str, n: int = 1) -> None:
    """Add to a local counter. Never raises: a counter that cannot be written is lost."""
    try:
        file = folder(base) / "counters.json"
        doc = _read_json(file)
        doc[name] = int(doc.get(name) or 0) + n
        doc["updated_at"] = _now().isoformat(timespec="seconds").replace("+00:00", "Z")
        _private_write(file, json.dumps(doc, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001: the counter is the last resort; nothing above it
        pass


def counters(base: Path | None = None) -> dict:
    return _read_json(folder(base) / "counters.json")


# --- recording -----------------------------------------------------------------------

def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


_SESSION: str | None = None


def session_id() -> str:
    """Aptabase's numeric session form: unix seconds x 10^8 + 8 random digits, one per process."""
    global _SESSION
    if _SESSION is None:
        _SESSION = str(int(time.time()) * 100_000_000 + random.randrange(100_000_000))
    return _SESSION


def check(name: str, props: dict) -> dict:
    """The event's properties, checked against the catalogue; `Refused` when anything is off."""
    shape = EVENTS.get(name)
    if shape is None:
        raise Refused(f"unknown event {name!r}")
    out = {}
    for key, value in props.items():
        rule = shape.get(key)
        if rule is None:
            raise Refused(f"{name} has no property {key!r}")
        if rule == _INT:
            if isinstance(value, bool) or not isinstance(value, int):
                raise Refused(f"{name}.{key} must be an integer")
        elif isinstance(rule, frozenset):
            if value not in rule:
                raise Refused(f"{name}.{key} may not be {value!r}")
        elif not (isinstance(value, str) and rule.match(value)):
            raise Refused(f"{name}.{key} is not a name the engine defines")
        out[key] = value
    missing = set(shape) - set(out)
    if missing:
        raise Refused(f"{name} lacks {sorted(missing)}")
    return out


def record(name: str, base: Path | None = None, environ: dict | None = None, **props) -> bool:
    """Queue one event. Returns whether it was queued; never raises and never waits on the
    network. Off, or no App-Key in this build: nothing is written at all."""
    try:
        if not collecting(base, environ):
            return False
        checked = check(name, props)
    except Refused:
        count(base, "refused")
        return False
    except Exception:  # noqa: BLE001
        return False
    try:
        queue = folder(base) / "queue.jsonl"
        try:
            if queue.stat().st_size >= QUEUE_LIMIT_BYTES:
                count(base, "dropped_queue_full")
                return False
        except FileNotFoundError:
            pass
        line = json.dumps({"t": _now().isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                           "s": session_id(), "n": name, "p": checked, "v": config.VERSION,
                           "e": environment(environ)}, separators=(",", ":"))
        queue.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if queue.is_symlink():
            return False
        fd = osprivacy.open(queue, os.O_WRONLY | os.O_CREAT | os.O_APPEND | osprivacy.NOFOLLOW, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except Exception:  # noqa: BLE001: a lost event is counted, never surfaced
        count(base, "dropped_write_failed")
        return False


def error_kind(exc: BaseException) -> str:
    """The exception's class name: a name the engine or Python defines, never its message."""
    name = type(exc).__name__
    return name if _KIND.match(name) else "other"


# --- sending -------------------------------------------------------------------------

def _system_props(version: str, env: str) -> dict:
    os_name = {"darwin": "macOS", "win32": "Windows"}.get(sys.platform, "Linux")
    os_version = _platform.mac_ver()[0] if sys.platform == "darwin" else _platform.release()
    return {"isDebug": env != "production", "osName": os_name, "osVersion": (os_version or "")[:100],
            "appVersion": str(version)[:50], "sdkVersion": f"project-observatory@{config.VERSION}"[:40],
            "engineName": "python", "engineVersion": _platform.python_version()[:30]}


def wire_event(item: dict, iid: str) -> dict:
    """THE SINK: one queued line as the analytics host's event, with `iid`, `environment` and
    `platform` attached. Every event that leaves goes through here, and none leaves without them."""
    env = item.get("e") if item.get("e") in ("production", "sandbox") else "sandbox"
    props = dict(check(item["n"], item.get("p") or {}))
    props.update(iid=iid, environment=env, platform=platform_name())
    return {"timestamp": item["t"], "sessionId": str(item["s"]), "eventName": item["n"],
            "systemProps": _system_props(item.get("v") or config.VERSION, env), "props": props}


def _urllib_transport(url: str, body: bytes, headers: dict, timeout: float) -> int:
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310: a fixed https URL
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except urllib.error.URLError as exc:
        raise OSError(str(exc.reason)) from None


def _parse_time(value: object) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def flush(base: Path | None = None, *, transport: Transport | None = None, environ: dict | None = None,
          sleep: Callable[[float], None] = time.sleep, now: datetime.datetime | None = None) -> dict:
    """Send what is queued. Never raises; the answer is what happened, in counts.

    A test suite (`OBSERVATORY_TEST_SANDBOX`) never reaches the network: without an injected
    transport its flush answers `test-sandbox` and sends nothing."""
    env = os.environ if environ is None else environ
    try:
        if not enabled(base, environ):
            write_switch_safe_drop(base)
            return {"result": "off"}
        key, _ = app_key(environ)
        if key is None:
            return {"result": "no-app-key"}
        if transport is None:
            if env.get("OBSERVATORY_TEST_SANDBOX"):
                return {"result": "test-sandbox"}
            transport = _urllib_transport
        queue = folder(base) / "queue.jsonl"
        if not queue.is_file() or queue.is_symlink():
            return {"result": "empty", "sent": 0}
        # Taken aside first: events recorded while this runs go to a fresh queue.
        sending = queue.with_name(f"queue.sending.{os.getpid()}.jsonl")
        os.replace(queue, sending)
        lines = sending.read_text(encoding="utf-8", errors="replace").splitlines()
        sending.unlink()
        cutoff = (now or _now()) - MAX_AGE
        iid = install_id(base)
        events, expired, broken = [], 0, 0
        for line in lines:
            try:
                item = json.loads(line)
                when = _parse_time(item.get("t"))
                if when is None or when.tzinfo is None:
                    raise ValueError("no time")
                if when < cutoff:
                    expired += 1
                    continue
                events.append(wire_event(item, iid))
            except (ValueError, KeyError, TypeError, Refused):
                broken += 1
        headers = {"App-Key": key, "Content-Type": "application/json",
                   "User-Agent": f"project-observatory/{config.VERSION}"}
        sent = rejected = failed = 0
        kept: list[dict] = []
        for start in range(0, len(events), BATCH):
            batch = events[start:start + BATCH]
            body = json.dumps(batch, separators=(",", ":")).encode("utf-8")
            outcome = "failed"
            for attempt in range(ATTEMPTS):
                try:
                    status = transport(ENDPOINT, body, headers, TIMEOUT_SECONDS)
                except OSError:
                    status = None
                if status is not None and 200 <= status < 300:
                    outcome = "sent"
                    break
                if status in (400, 404):
                    # Never retried: the host will not change its mind about this batch or key.
                    outcome = "rejected"
                    break
                if attempt + 1 < ATTEMPTS:
                    sleep(min(8.0, 1.0 * (2 ** attempt)))
            if outcome == "sent":
                sent += len(batch)
            elif outcome == "rejected":
                rejected += len(batch)
            else:
                failed += len(batch)
                kept.extend(batch)
        if kept:
            # Kept for the next pass in their queued form, so their age still counts.
            with open(queue, "a", encoding="utf-8") as out:
                for ev in kept:
                    out.write(json.dumps({"t": ev["timestamp"], "s": ev["sessionId"], "n": ev["eventName"],
                                          "p": {k: v for k, v in ev["props"].items() if k not in SINK_PROPS},
                                          "v": ev["systemProps"]["appVersion"],
                                          "e": ev["props"]["environment"]}, separators=(",", ":")) + "\n")
        for name, n in (("sent", sent), ("dropped_expired", expired), ("dropped_unreadable", broken),
                        ("rejected", rejected), ("send_failed", failed)):
            if n:
                count(base, name, n)
        result = "sent" if sent and not failed else "failed" if failed else "empty" if not lines else "nothing-sent"
        return {"result": result, "sent": sent, "expired": expired, "rejected": rejected,
                "failed": failed, "unreadable": broken}
    except Exception as exc:  # noqa: BLE001: a flush never stops the job that runs it
        count(base, "flush_error")
        return {"result": "error", "kind": error_kind(exc)}


def write_switch_safe_drop(base: Path | None) -> None:
    """Off: whatever is still queued is dropped, never sent later."""
    try:
        (folder(base) / "queue.jsonl").unlink()
    except OSError:
        pass


# --- the first-run notice ------------------------------------------------------------

def notice_state(base: Path | None = None) -> dict:
    return _read_json(folder(base) / "notice.json")


def mark_notice(base: Path | None, surface: str, at: datetime.datetime | None = None,
                environ: dict | None = None) -> bool:
    """Record that `surface` showed the notice; True the first time. Also queues
    `analytics_notice_shown` for that surface."""
    if surface not in ("cli", "dashboard"):
        raise Refused(f"no notice surface {surface!r}")
    try:
        if not is_workspace(base):
            return False
        doc = notice_state(base)
        if doc.get(surface):
            return False
        doc[surface] = (at or _now()).isoformat(timespec="seconds").replace("+00:00", "Z")
        doc["version"] = config.VERSION
        _private_write(folder(base) / "notice.json", json.dumps(doc, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001
        return False
    record("analytics_notice_shown", base, environ, surface=surface)
    return True


def cli_notice(base: Path | None = None, stream=None, environ: dict | None = None) -> bool:
    """Print the notice once per workspace, to a person's terminal only: a job's log is read by
    nobody, so a notice written there would be marked seen and never read."""
    stream = stream if stream is not None else sys.stderr
    try:
        if not collecting(base, environ) or notice_state(base).get("cli"):
            return False
        if not (hasattr(stream, "isatty") and stream.isatty()):
            return False
        if not mark_notice(base, "cli", environ=environ):
            return False
        stream.write(f"\nObservatory: {NOTICE}\n")
        stream.flush()
        return True
    except Exception:  # noqa: BLE001
        return False


def dashboard_notice(base: Path | None = None, now: datetime.datetime | None = None,
                     environ: dict | None = None) -> bool:
    """Whether the dashboard shows the notice in this build: from the first build that showed
    it, for `DASHBOARD_NOTICE_DAYS` days, and only while collection is on."""
    if not collecting(base, environ):
        return False
    now = now or _now()
    first = _parse_time(notice_state(base).get("dashboard"))
    if first is None:
        mark_notice(base, "dashboard", now, environ)
        return True
    return now - first < datetime.timedelta(days=DASHBOARD_NOTICE_DAYS)


# --- status and the command ----------------------------------------------------------

WHAT_IS_SENT = ["the name of a command, MCP tool or tick and its outcome and duration",
                "the kind of an error (its class name)",
                "the engine version, the OS name and version, the Python version",
                "a random install id made for this purpose only"]
NEVER_SENT = ["keys or vault values", "paths or file names", "repository, project, organisation or person names",
              "registry or store content", "URLs, prompts, messages, arguments or anything you typed"]


def status(base: Path | None = None, environ: dict | None = None) -> dict:
    word = read_switch(base)
    why_off = env_off(environ)
    key, key_source = app_key(environ)
    out = {"label": SWITCH_LABEL, "on": enabled(base, environ),
           "source": f"env:{why_off}" if why_off else "file" if word is not None else "default",
           "file": str(switch_path(base)), "host": HOST, "app_key": key_source,
           "collecting": collecting(base, environ),
           "environment": environment(environ), "platform": platform_name(),
           "what_is_sent": WHAT_IS_SENT, "never_sent": NEVER_SENT,
           "turn_off": "project-observatory full analytics off", "notice": notice_state(base),
           "counters": counters(base)}
    try:
        out["queued"] = sum(1 for _ in (folder(base) / "queue.jsonl").open(encoding="utf-8"))
    except OSError:
        out["queued"] = 0
    if key is None:
        out["note"] = ("this build carries no analytics App-Key, so nothing is collected or sent; "
                       f"a release build ships one, or set {APP_KEY_ENV}")
    if why_off and word != "off":
        out["note"] = f"off for this process by {why_off}; the switch file is not changed"
    return out


def parser():
    import argparse
    ap = argparse.ArgumentParser(prog="project-observatory full analytics",
                                 description=f"{SWITCH_LABEL} (on by default): the file `analytics` in the "
                                             f"workspace home; {SWITCH_ENV}=off or DO_NOT_TRACK=1 turns it off "
                                             "for one process")
    ap.add_argument("action", choices=["status", "on", "off", "send"],
                    help="status: what is sent and what never is; on/off: the switch; send: send the queue now")
    return ap


def main(argv: list[str]) -> int:
    a = parser().parse_args(argv)
    try:
        base = config.home()
        config.validate_workspace(base, required=True)
        if a.action in ("on", "off"):
            write_switch(base, a.action == "on")
            result = status(base)
        elif a.action == "send":
            result = {"send": flush(base), **status(base)}
        else:
            result = status(base)
        print(json.dumps(result, indent=2))
        return 0
    except (config.ConfigurationError, OSError) as exc:
        print(f"Observatory: {exc}", file=sys.stderr)
        return 2
