"""A per-session MCP server moving itself onto the installed release (OBS-159).

WHY. Every agent session starts its own `mcp/server.py` over stdio and keeps it for
the whole session. Until OBS-159 an update left each of them answering every call
`stale-server` (lifecycle LC-10, `code_freshness.py`) until a person restarted the
session, and the maintenance job installs releases by itself: after each release every
open session on a machine lost Observatory.

WHAT HAPPENS NOW. The stdio relay (`mcp/stdio_relay.py`) notices at the next request
that the code on disk is not the code it started from. It holds that request, lets the
requests already running finish, asks the installed release to start once in a child
(`--reload-check`, the PREFLIGHT: it imports the whole server and names its version,
its MCP revision, its reload protocol and its tools) and, when that answers, replaces
itself with `os.execv`: same pid, same stdio pipes. The negotiated session (the
client's `initialize` parameters, whether `notifications/initialized` came) and the
bytes it had read but not served travel in an unlinked owner-only file whose
descriptor is inherited (STATE_ENV names it). The new process replays the handshake
to its own SDK without answering it on the wire, then serves the held request: the
request that triggered the swap is answered exactly once, by the new release.

WHEN IT CANNOT. Windows (`os.execv` starts a new process there and the host's pipe
would belong to the old one), an update holding the workspace's update lock, a
preflight that fails, an installed release that speaks another reload protocol or
another MCP revision, a tree that changed during the check, or an `execv` that fails.
The server then keeps answering `stale-server`, now with the reason (REFUSAL), and
the session is replaced the old way, when it reconnects.

Each swap and each refusal is one line in `store/logs/mcp-reload.jsonl`: event, from,
to, pid, time and, for a refusal, a reason that carries no path, argument or value.
`full doctor` reads it (`summary`).
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import osprivacy

#: The state hand-over's format. A release that cannot read this number cannot take
#: over a running session, so the preflight's answer must carry the same one.
RELOAD_PROTOCOL = 1
#: The environment variable naming the inherited descriptor of the state file.
STATE_ENV = "OBSERVATORY_MCP_RELOAD_FD"
#: The id of the replayed `initialize`; its answer is swallowed, never written.
SYNTHETIC_ID = "observatory-reload:initialize"
LOG_NAME = "mcp-reload.jsonl"
LOG_MAX_BYTES = 256 * 1024
LOG_KEEP_LINES = 400
#: How long the requests already running may take before a swap gives up for now.
DRAIN_SECONDS = float(os.environ.get("OBSERVATORY_RELOAD_DRAIN_SECONDS", "60"))
PREFLIGHT_SECONDS = 90

#: The reason the last swap attempt was refused, for `stale-server` to name. None
#: while nothing was refused. Set by the relay (or by `main` on a platform without one).
REFUSAL: str | None = None

_SAFE_REASON = re.compile(r"[^A-Za-z0-9 .,;:()'_+=-]")


def platform_can_swap(platform: str | None = None) -> str | None:
    """None where `os.execv` replaces the process in place; else why not."""
    if (platform or sys.platform) == "win32":
        return ("this platform cannot replace a running process in place (os.execv starts a new "
                "one on Windows, and the host's pipe would stay with the old one)")
    return None


def clean_reason(text: str, limit: int = 300) -> str:
    """A reason with no path, argument or value in it: fixed words and versions only."""
    return _SAFE_REASON.sub("", str(text))[:limit].strip()


def update_lock_held(home: Path) -> bool:
    """Whether a `full update --apply` holds this workspace (engine_update.update_lock)."""
    try:
        import engine_update
        return engine_update.update_lock_held(Path(home))
    except Exception:  # noqa: BLE001: an unreadable lock is a busy one: never swap into it
        return True


def preflight(interpreter: str, server: Path, argv: list[str], env: dict | None = None,
              timeout: float = PREFLIGHT_SECONDS) -> tuple[dict | None, str | None]:
    """Start the installed server once with `--reload-check`: (its answer, None) or (None, why not).

    stdin is the null device, so a release older than OBS-159 (which ignores the flag and
    starts serving) reads EOF and exits without an answer: that reads as "cannot take over"."""
    env = {k: v for k, v in (env if env is not None else os.environ).items() if k != STATE_ENV}
    try:
        p = subprocess.run([interpreter, str(server), *argv, "--reload-check"], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, f"the installed release did not answer its start check within {int(timeout)} s"
    except OSError as exc:
        return None, f"the installed release could not be started ({exc.strerror or type(exc).__name__})"
    answer = None
    for line in reversed(p.stdout.splitlines()):
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict) and "reload" in candidate:
            answer = candidate
            break
    if p.returncode != 0 or answer is None:
        if answer is None and p.returncode == 0:
            return None, "the installed release cannot take over a running session (it has no reload check)"
        return None, f"the installed release did not start (exit {p.returncode})"
    return answer, None


def judge(answer: dict, *, protocol: str) -> str | None:
    """None when the installed release can take this session over; else why not."""
    if answer.get("reload") != RELOAD_PROTOCOL:
        return (f"the installed release speaks reload protocol {clean_reason(answer.get('reload'))}, "
                f"this server {RELOAD_PROTOCOL}")
    if answer.get("protocol") != protocol:
        return (f"the installed release serves MCP revision {clean_reason(answer.get('protocol'))}, "
                f"this session negotiated {protocol}")
    if not isinstance(answer.get("version"), str) or not isinstance(answer.get("tools"), list):
        return "the installed release answered its start check without a version or a tool list"
    return None


# --- the state hand-over ------------------------------------------------------------

def write_state(state: dict) -> int:
    """An unlinked owner-only file holding `state`, open and inheritable; its descriptor."""
    handle = tempfile.TemporaryFile(prefix="observatory-reload-")
    try:
        handle.write(json.dumps(state, ensure_ascii=False).encode("utf-8"))
        handle.flush()
        fd = os.dup(handle.fileno())
    finally:
        handle.close()
    os.lseek(fd, 0, os.SEEK_SET)
    os.set_inheritable(fd, True)
    return fd


def take_state(environ: dict | None = None) -> tuple[dict | None, str | None]:
    """The hand-over this process was started with: (state, None), (None, None) when there is
    none, or (None, why it could not be read). The variable is removed and the descriptor
    closed either way, so no child of this server inherits them."""
    environ = os.environ if environ is None else environ
    raw = environ.pop(STATE_ENV, None)
    if raw is None:
        return None, None
    try:
        fd = int(raw)
    except ValueError:
        return None, "the hand-over named no descriptor"
    try:
        chunks = []
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            block = os.read(fd, 1 << 16)
            if not block:
                break
            chunks.append(block)
        state = json.loads(b"".join(chunks).decode("utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"the hand-over could not be read ({type(exc).__name__})"
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if not isinstance(state, dict) or state.get("reload") != RELOAD_PROTOCOL:
        return None, "the hand-over is in another reload protocol"
    return state, None


def encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decode(text: str | None) -> bytes:
    return base64.b64decode(text or "")


# --- the log ------------------------------------------------------------------------

def log_path(home: Path) -> Path:
    return Path(home) / "store" / "logs" / LOG_NAME


def record(home: Path, event: str, **fields) -> dict:
    """One line in the workspace's reload log and on stderr (the host's log of this server).

    Never raises: a log that cannot be written loses the line, never the session."""
    row = {"at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "event": event, "pid": os.getpid(), **fields}
    line = json.dumps(row, sort_keys=True, ensure_ascii=False)
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass
    _append(log_path(home), line)
    if event == "swapped":
        # The machine's product log folder (update_events.directory()) learns the pid and time
        # only, codes-only like updates.jsonl: the lifecycle scan reads it for the servers of
        # every workspace on this account, whatever home each one serves.
        _append(machine_log_path(), json.dumps({"at": row["at"], "event": event, "pid": row["pid"]},
                                               sort_keys=True))
    return row


def machine_log_path() -> Path | None:
    try:
        import update_events
        return update_events.directory() / LOG_NAME
    except Exception:  # noqa: BLE001: no machine log folder, no machine line
        return None


def _append(file: Path | None, line: str) -> None:
    if file is None:
        return
    try:
        file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if file.is_symlink():
            return
        _trim(file)
        fd = osprivacy.open(file, os.O_WRONLY | os.O_APPEND | os.O_CREAT | osprivacy.NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as out:
            out.write(line + "\n")
    except OSError:
        pass


def _trim(file: Path) -> None:
    try:
        if file.stat().st_size <= LOG_MAX_BYTES:
            return
        lines = file.read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_KEEP_LINES:]
        tmp = file.with_name(file.name + f".{os.getpid()}.tmp")
        fd = osprivacy.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | osprivacy.NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write("\n".join(lines) + "\n")
        os.replace(tmp, file)
    except OSError:
        pass


def read_log(home: Path | None, limit: int = 200, *, file: Path | None = None) -> list[dict]:
    try:
        target = file if file is not None else log_path(home)
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except (OSError, TypeError):
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def reload_times(home: Path | None = None) -> dict[int, float]:
    """pid -> the epoch of its latest swap, from the machine's log and, given one, a home's.
    A swap keeps the pid and its start time, so the lifecycle scan reads the code's age
    from here rather than from `ps`."""
    out: dict[int, float] = {}
    rows = read_log(None, LOG_KEEP_LINES, file=machine_log_path())
    if home is not None:
        rows += read_log(home, LOG_KEEP_LINES)
    for row in rows:
        if row.get("event") != "swapped" or not isinstance(row.get("pid"), int):
            continue
        try:
            at = datetime.datetime.strptime(row["at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        out[row["pid"]] = max(at, out.get(row["pid"], 0.0))
    return out


def _alive(pid) -> bool:
    import osproc
    return osproc.alive(pid) if isinstance(pid, int) else False


def summary(home: Path, limit: int = 20) -> dict:
    """For `full doctor`: the latest swaps and refusals, each with whether its server still runs."""
    rows = read_log(home)
    swapped = [{k: r.get(k) for k in ("at", "pid", "from", "to")} | {"running": _alive(r.get("pid"))}
               for r in rows if r.get("event") == "swapped"][-limit:]
    refused = [{k: r.get(k) for k in ("at", "pid", "from", "to", "reason")} | {"running": _alive(r.get("pid"))}
               for r in rows if r.get("event") == "refused"][-limit:]
    return {"log": "store/logs/" + LOG_NAME, "swapped": swapped, "refused": refused,
            "how": ("a session's server moves itself onto an installed release at its next request; "
                    "a refused one answers `stale-server` with the reason until its session reconnects")}
