#!/usr/bin/env python3
"""Jobs: work that outlives one MCP request, behind a handle (fabric-interop/0.1, C3.2).

A capability that declares `"job": true` answers `{"job": {"id", "status": "working"}}`
at once, and the caller follows it with `fabric.job.get {id}` and may stop it with
`fabric.job.cancel {id}`. The contract's rules, and where each is kept:

- **Stable across restarts.** A job is a file, `<state>/jobs/<id>.json`, and its
  work runs in a DETACHED process (`tools/run_job.py`), not in the MCP server: a
  stdio server lives as long as one host session, and a job must not die with it.
  A later server — or another host's — reads the same file.
- **Unknown is unknown.** `get` of an id that was never issued, or of a malformed
  one, answers None and the tool answers `unknown-job`; it never creates a job.
  Ids are `job-` + 32 hex characters, checked before any path is built from them.
- **A dead runner is not "working".** A runner writes a heartbeat; a job whose
  runner process is gone, or whose heartbeat stopped, is failed as `interrupted`
  by the next reader, instead of reporting `working` for ever.
- **Terminal is terminal.** `completed`, `failed` and `cancelled` are never left:
  every change is a read-check-write under an exclusive lock on the job, so a
  runner finishing just after a cancel cannot overwrite `cancelled`.
- **One at a time per capability.** A second start while a job of the same
  capability still runs JOINS it — the answer is the running job's handle — since
  two refreshes of one inventory race for one file and prove nothing more.

Records keep the trace of the request that started the job, so every later answer
about it carries the same trace id (C3.4). They hold no input values: the only
job today takes no arguments.
"""
from __future__ import annotations
import errno
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import atomic
import paths

JOB_ID = re.compile(r"^job-[0-9a-f]{32}$")
STATES = ("working", "input_required", "completed", "failed", "cancelled")
TERMINAL = {"completed", "failed", "cancelled"}
#: How often a runner proves it is alive, and how long a silence means it is not.
HEARTBEAT_SECONDS = 5
HEARTBEAT_STALE = timedelta(seconds=60)
POLL_INTERVAL_MS = 2000
#: Finished jobs are kept this long, then removed by the next start.
RETENTION = timedelta(days=7)
ROOT = Path(__file__).resolve().parent


def jobs_dir() -> Path:
    return paths.STATE / "jobs"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def new_id() -> str:
    import secrets
    return "job-" + secrets.token_hex(16)


def _file(job_id: str) -> Path:
    if not JOB_ID.match(job_id or ""):
        raise KeyError(job_id)
    return jobs_dir() / f"{job_id}.json"


@contextmanager
def _locked(job_id: str) -> Iterator[None]:
    directory = jobs_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / f"{job_id}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read(job_id: str) -> dict | None:
    try:
        path = _file(job_id)
    except KeyError:
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # A record that cannot be read is not a job anyone can follow; it is
        # reported as failed rather than as unknown, since the id WAS issued.
        return {"id": job_id, "status": "failed", "updatedAt": now_iso(),
                "error": {"code": "unreadable-job", "message": "the job's record could not be read"}}
    return doc if isinstance(doc, dict) and doc.get("id") == job_id else None


def _write(doc: dict) -> None:
    doc["updatedAt"] = now_iso()
    path = _file(doc["id"])
    atomic.write_json(path, doc)
    os.chmod(path, 0o600)


def _alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    # A zombie still answers kill(0); a runner we started and nobody reaped yet
    # is finished, not running. waitpid only works for our own children.
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


def _reconcile(doc: dict) -> dict:
    """Fail a `working` job whose runner is gone. Called under the job's lock."""
    if doc.get("status") in TERMINAL:
        return doc
    beat = _parse(doc.get("heartbeatAt"))
    stale = beat is None or datetime.now(timezone.utc) - beat > HEARTBEAT_STALE
    if doc.get("pid") is not None and not _alive(doc.get("pid")):
        why = "the process running it exited without finishing"
    elif stale:
        why = f"its runner sent no heartbeat for more than {int(HEARTBEAT_STALE.total_seconds())} s"
    else:
        return doc
    doc.update(status="failed", statusMessage=f"interrupted: {why}",
               error={"code": "interrupted", "message": why})
    _write(doc)
    return doc


def view(doc: dict) -> dict:
    """The job as C3.2 publishes it: id, status and only what applies."""
    out: dict[str, Any] = {"id": doc["id"], "status": doc["status"], "updatedAt": doc["updatedAt"]}
    if doc.get("statusMessage"):
        out["statusMessage"] = doc["statusMessage"]
    if doc["status"] not in TERMINAL:
        out["pollIntervalMs"] = POLL_INTERVAL_MS
    if doc.get("inputRequests"):
        out["inputRequests"] = doc["inputRequests"]
    if doc["status"] == "completed" and "result" in doc:
        out["result"] = doc["result"]
    if doc.get("error"):
        out["error"] = doc["error"]
    return out


def get(job_id: str) -> dict | None:
    """The job's record (reconciled), or None when no such job was ever issued."""
    if not JOB_ID.match(job_id or "") or not _file(job_id).exists():
        return None
    with _locked(job_id):
        doc = _read(job_id)
        return None if doc is None else _reconcile(doc)


def _running(capability: str) -> dict | None:
    directory = jobs_dir()
    if not directory.is_dir():
        return None
    for path in sorted(directory.glob("job-*.json")):
        job_id = path.stem
        doc = get(job_id)
        if doc and doc.get("capability") == capability and doc.get("status") not in TERMINAL:
            return doc
    return None


def prune(now: datetime | None = None) -> int:
    """Remove finished jobs older than RETENTION. Returns how many went."""
    now = now or datetime.now(timezone.utc)
    removed = 0
    directory = jobs_dir()
    if not directory.is_dir():
        return 0
    for path in directory.glob("job-*.json"):
        job_id = path.stem
        with _locked(job_id):
            doc = _read(job_id)
            at = _parse((doc or {}).get("updatedAt"))
            if doc and doc.get("status") in TERMINAL and at and now - at > RETENTION:
                path.unlink(missing_ok=True)
                removed += 1
        if not path.exists():
            (directory / f"{job_id}.lock").unlink(missing_ok=True)
    return removed


def runner_command(job_id: str) -> list[str]:
    return [sys.executable, str(ROOT / "tools/run_job.py"), job_id]


def start(capability: str, span_record: dict, *,
          spawn: Callable[[list[str], dict], subprocess.Popen] | None = None) -> tuple[dict, bool]:
    """(job record, joined): a new job for `capability`, or the one already running."""
    running = _running(capability)
    if running is not None:
        return running, True
    prune()
    job_id = new_id()
    doc = {"id": job_id, "capability": capability, "status": "working",
           "statusMessage": "started", "createdAt": now_iso(), "heartbeatAt": now_iso(),
           "pid": None, "trace": span_record}
    with _locked(job_id):
        _write(doc)
    env = {**os.environ, "OBSERVATORY_HOME": str(paths.HOME)}
    try:
        proc = (spawn or _spawn)(runner_command(job_id), env)
    except OSError as exc:
        with _locked(job_id):
            doc = _read(job_id) or doc
            doc.update(status="failed", statusMessage="the runner could not start",
                       error={"code": "runner-failed", "message": type(exc).__name__})
            _write(doc)
        return doc, False
    with _locked(job_id):
        doc = _read(job_id) or doc
        if doc.get("status") not in TERMINAL:
            doc["pid"] = proc.pid
            _write(doc)
    return doc, False


def _spawn(argv: list[str], env: dict) -> subprocess.Popen:
    """Detached: its own session, so it outlives this server and a cancel can stop
    its whole process group; no terminal, no inherited pipes."""
    log = jobs_dir() / "runner.log"
    out = open(log, "ab")
    try:
        return subprocess.Popen(argv, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=out, start_new_session=True, close_fds=True)
    finally:
        out.close()


def cancel(job_id: str) -> dict | None:
    """Stop a running job. A finished one is returned unchanged."""
    if not JOB_ID.match(job_id or "") or not _file(job_id).exists():
        return None
    with _locked(job_id):
        doc = _read(job_id)
        if doc is None:
            return None
        if doc.get("status") in TERMINAL:
            return doc
        pid = doc.get("pid")
        if isinstance(pid, int) and pid > 0 and _alive(pid):
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                pass
        doc.update(status="cancelled", statusMessage="cancelled by the caller")
        _write(doc)
        return doc


def update(job_id: str, **fields: Any) -> dict | None:
    """The runner's write: applied only while the job is not terminal."""
    with _locked(job_id):
        doc = _read(job_id)
        if doc is None or doc.get("status") in TERMINAL:
            return doc
        doc.update(fields)
        _write(doc)
        return doc
