"""The operations verbs for an agent (OBS-559, CP-18): the MCP tool `observatory_ops`.

The CLI's maintenance, backup, update and disk verbs reach an agent here: `workspace-backup --pause-writers`,
`machine-backup now`, `backups migrate`, `backups decrypt`, `cleanup` and `disk-guard` (planned or applied), `update
--apply`, `maintain run|ensure|uninstall|app` and `auto-update on|off`. Each act builds the CLI's own argument list
and runs it in a detached job (fabric-interop/0.1, `fabric.job.get`) through the CLI's own function: `workspace.main`,
`workspace.machine_command` for the two disk verbs (with their JSON summary line, the one the maintenance job reads),
and, for an update, the engine's CLI in a process of its own, as the maintenance pass runs it, so a cancel of the job
never stops pip or the release's upgrade half-way. Nothing below takes a backup, frees disk or installs a release by
itself.

What this module adds is what stdio cannot prove. An act that deletes or is hard to undo (`backups migrate` removes
the older legacy copies, `backups decrypt` writes the plaintext of a backup, an applied cleanup or disk guard removes
files, an update replaces the running engine) needs the operator's words in contract DEC-0121's shape, checked by
`key_acts.decided` as the key page's acts are; the journal `store/logs/ops-acts.jsonl` keeps them beside the caller
`agent:mcp`, written before the job starts. A decrypted backup is written only to a new path inside the workspace.
The acts that touch the scheduler run with `OBSERVATORY_SYSTEM_SETUP=1`, as the scheduler and the plugin hook do,
unless this server was started with `=0`. Not offered: `workspace-backup --output` (a plaintext copy of the vault's
secrets outside the managed root) and `update --no-rollback`.

No secret crosses: an argument shaped like a credential is refused and never echoed, and every string of a job's
answer is scrubbed with the one redactor, `memory_redact.redact` (OBS-602). Design: docs/design/control-plane/ops-tools.md.
"""
from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import memory_redact  # noqa: E402
import key_acts  # noqa: E402
import osprivacy  # noqa: E402

Refusal = key_acts.Refusal
CALLER = key_acts.CALLER
TOOL = "observatory.ops"
SETUP_ENV = "OBSERVATORY_SYSTEM_SETUP"
LEVELS = ("normal", "low", "critical")
VERSION = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}")

#: Each act, the arguments it takes, and what it writes (the envelope's `writeScopes`).
ACTIONS: dict[str, dict] = {
    "workspace-backup": {"takes": set(), "writes": ["observatory:backups"]},
    "machine-backup": {"takes": set(), "writes": ["observatory:backups"]},
    "backups-migrate": {"takes": set(), "writes": ["observatory:backups"]},
    "backups-decrypt": {"takes": {"file", "output"}, "writes": ["observatory:workspace"]},
    "cleanup-plan": {"takes": {"level"}, "writes": ["observatory:store/raw/cleanup-plan.json"]},
    "cleanup": {"takes": {"include", "level"}, "writes": ["machine:cleanup"]},
    "disk-guard-plan": {"takes": {"level", "largeFiles"}, "writes": ["observatory:store/raw/disk-guard.json"]},
    "disk-guard": {"takes": {"level", "largeFiles"}, "writes": ["machine:disk-guard"]},
    "update": {"takes": {"version", "reinstall"}, "writes": ["machine:engine", "observatory:workspace"]},
    "maintain-run": {"takes": set(), "writes": ["observatory:store/maintenance.json"]},
    "maintain-ensure": {"takes": set(), "writes": ["machine:scheduler"]},
    "maintain-uninstall": {"takes": set(), "writes": ["machine:scheduler"]},
    "maintain-app": {"takes": set(), "writes": ["machine:app"]},
    "auto-update-on": {"takes": set(), "writes": ["observatory:auto-update"]},
    "auto-update-off": {"takes": set(), "writes": ["observatory:auto-update"]},
}
TAKES = {action: spec["takes"] for action, spec in ACTIONS.items()}
#: The decision's `action` per gated act, its title, and why it needs the operator's words.
GATED = {
    "backups-migrate": ("backups.migrate", "Move legacy backup copies into the backups root, removing the older ones",
                        "removes the older legacy backup copies"),
    "backups-decrypt": ("backups.decrypt", "Write the plaintext of a backup inside the workspace",
                        "writes the plaintext of a backup, secrets included"),
    "cleanup": ("disk.cleanup", "Remove the cleanup plan's tier now", "removes files"),
    "disk-guard": ("disk.guard", "Let the disk guard remove temporary files, the Trash, caches and build output now",
                   "removes files, the Trash included"),
    "update": ("engine.update", "Install an engine release and upgrade the workspace",
               "replaces the running engine"),
}
#: Acts that touch launchd, systemd or the credential store: run with SETUP_ENV=1 unless this server has it =0.
SETUP = {"maintain-run", "maintain-ensure", "auto-update-on", "update"}
#: Acts run as the engine's CLI in a process group of their own, so a cancel of the job never reaches them.
OWN_PROCESS = {"update"}
CAPABILITIES = {f"ops.{action}" for action in ACTIONS}
STDERR_LINES, LINE_CHARS = 8, 300


def _home() -> pathlib.Path:
    import paths
    return paths.HOME


def journal_path() -> pathlib.Path:
    import paths
    return paths.STATE / "logs" / "ops-acts.jsonl"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _scrub(value: Any) -> Any:
    """Every string of a journal row or a result through the one redactor (OBS-602)."""
    return memory_redact.scrub(value)


def _inside(output: Any) -> tuple[str, str]:
    """A new path inside the workspace for a decrypted backup: (absolute path, its path from the home). The folder it
    goes in must exist inside the workspace once symlinks are resolved; the path itself must not exist."""
    if not isinstance(output, str) or not output.strip():
        raise Refusal("invalid-argument", "output is required: a new path inside the workspace")
    given = pathlib.PurePath(output)
    if ".." in given.parts:
        raise Refusal("invalid-argument", "output may not climb out with `..`; name a path inside the workspace")
    home = _home().resolve()
    candidate = pathlib.Path(output) if given.is_absolute() else home / given
    if candidate.name in ("", ".") or candidate == home:
        raise Refusal("invalid-argument", "output names a new file or folder, not the workspace itself")
    try:
        parent = candidate.parent.resolve(strict=True)
    except (OSError, RuntimeError):
        raise Refusal("invalid-argument", "output's folder does not exist inside the workspace; create it "
                                          "first") from None
    if parent != home and home not in parent.parents:
        raise Refusal("invalid-argument", "output must stay inside the workspace (a backup's plaintext holds its "
                                          "secrets)")
    final = parent / candidate.name
    if os.path.lexists(final):
        raise Refusal("invalid-argument", "output already exists; a decrypt never overwrites")
    return str(final), final.relative_to(home).as_posix()


def _validate(action: str, params: dict[str, Any]) -> dict[str, Any]:
    """The act's own arguments, checked, without the empty ones; a refusal names the first wrong one."""
    key_acts._check_arguments(action, TAKES, params)
    clean = {k: v for k, v in params.items() if v not in (None, "", [], {})}
    if "level" in clean and clean["level"] not in LEVELS:
        raise Refusal("invalid-argument", f"level is one of {', '.join(LEVELS)}")
    if "include" in clean and clean["include"] != "manual":
        raise Refusal("invalid-argument", "include takes only `manual` (the manual tier, archived then removed)")
    for flag in ("largeFiles", "reinstall"):
        if flag in clean and not isinstance(clean[flag], bool):
            raise Refusal("invalid-argument", f"{flag} is true or false")
        if clean.get(flag) is False:
            clean.pop(flag)
    if "version" in clean and not (isinstance(clean["version"], str) and VERSION.fullmatch(clean["version"])):
        raise Refusal("invalid-argument", "version is a release X.Y.Z; leave it out for the latest")
    if action == "backups-decrypt":
        if not isinstance(clean.get("file"), str) or not clean["file"].strip():
            raise Refusal("invalid-argument", "file is required: the backup's name in the backups root, or its path")
        clean["output"] = _inside(clean.get("output"))[1]
    return clean


def command(action: str, params: dict[str, Any]) -> list[str]:
    """The CLI's argument list for one act, as a person types it after `project-observatory full`."""
    level = ["--level", params["level"]] if params.get("level") else []
    big = ["--large-files"] if params.get("largeFiles") else []
    return {
        "workspace-backup": lambda: ["workspace-backup", "--pause-writers", "--json"],
        "machine-backup": lambda: ["machine-backup", "now"],
        "backups-migrate": lambda: ["backups", "migrate"],
        "backups-decrypt": lambda: ["backups", "decrypt", params["file"], str(_home().resolve() / params["output"])],
        "cleanup-plan": lambda: ["cleanup", *level],
        "cleanup": lambda: ["cleanup", "--apply", *(["--include", "manual"] if params.get("include") else []), *level],
        "disk-guard-plan": lambda: ["disk-guard", *level, *big],
        "disk-guard": lambda: ["disk-guard", "--apply", *level, *big],
        "update": lambda: ["update", "--apply", "--unattended",
                           *(["--version", params["version"]] if params.get("version") else []),
                           *(["--reinstall"] if params.get("reinstall") else [])],
        "maintain-run": lambda: ["maintain", "run"],
        "maintain-ensure": lambda: ["maintain", "ensure"],
        "maintain-uninstall": lambda: ["maintain", "uninstall"],
        "maintain-app": lambda: ["maintain", "app"],
        "auto-update-on": lambda: ["auto-update", "on"],
        "auto-update-off": lambda: ["auto-update", "off"],
    }[action]()


def _target(action: str, params: dict[str, Any]) -> str:
    """The object a gated act's decision names."""
    if action == "backups-migrate":
        return "legacy-copies"
    if action == "backups-decrypt":
        return params["output"]
    if action == "cleanup":
        return "manual" if params.get("include") else "auto"
    if action == "disk-guard":
        return params.get("level") or "current"
    return params.get("version") or "latest"


def _journal(row: dict, *, job: str) -> None:
    """Written BEFORE the job's process starts: a log written after loses the crash. Every string is scrubbed but the
    job's id, which `jobs.JOB_ID` has checked (its hex would read as a credential)."""
    file = journal_path()
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = osprivacy.open(file, os.O_WRONLY | os.O_APPEND | os.O_CREAT | osprivacy.NOFOLLOW, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as out:
        out.write(json.dumps({**_scrub(row), "job": job}, ensure_ascii=False, sort_keys=True) + "\n")


def act(action: str, *, file: str | None = None, output: str | None = None, include: str | None = None,
        level: str | None = None, largeFiles: bool | None = None, version: str | None = None,
        reinstall: bool | None = None, decision: Any = None, span_record: dict | None = None, start=None) -> dict:
    """Check one act, journal it and start its job; the answer is the job's handle, or a typed refusal."""
    given = {"file": file, "output": output, "include": include, "level": level, "largeFiles": largeFiles,
             "version": version, "reinstall": reinstall}
    try:
        params = _validate(action, given)
        record = None
        if action in GATED:
            act_id, title, why = GATED[action]
            record = key_acts.decided(act_id, title, TOOL, _target(action, params), decision, label=action, why=why)
        elif decision is not None:
            raise Refusal("invalid-argument", f"{action} takes no decision: it deletes nothing and can be undone")
        if action in SETUP and os.environ.get(SETUP_ENV) == "0":
            raise Refusal("system-setup-off", f"{action} touches the scheduler or the credential store, and this "
                                              f"server runs with {SETUP_ENV}=0")
        import jobs
        start = start or jobs.start
        row = {"at": _now(), "action": action, "caller": CALLER, "caller_verified": False, "params": params}
        if record:
            row["decision"] = record
            row["target"] = record.get("target")

        def prepared(doc: dict) -> None:
            _journal(row, job=doc["id"] if jobs.JOB_ID.match(str(doc.get("id"))) else "unreadable")
        if span_record is None:
            import interop
            span_record = interop.span_for(None).record()
        try:
            doc, joined = start(f"ops.{action}", span_record, arguments={"action": action, "params": params},
                                prepared=prepared, exclusive=True)
        except jobs.JobBusy:
            raise Refusal("busy", f"another {action} with other arguments is running; follow it with "
                                  "fabric.job.get, then call again") from None
        except OSError as exc:
            raise Refusal("failed", f"the job could not be recorded ({type(exc).__name__})") from None
        if doc.get("status") != "working":
            raise Refusal("failed", "the job did not start: "
                                    + str((doc.get("error") or {}).get("message") or doc.get("status"))[:200])
        out: dict[str, Any] = {"action": action, "job": {"id": doc["id"], "status": doc["status"]},
                               "follow": "fabric.job.get"}
        if joined:
            out["joined"] = True
        if record:
            out["recordedAs"] = {"by": record["by"], "via": record["via"], "caller": CALLER}
        return out
    except Refusal as refusal:
        return refusal.answer()


# --- the runner (tools/run_job.py), in the detached job's own process ---------------------------------------------

def _inprocess(argv: list[str]) -> tuple[int, str, str]:
    """The CLI's own function in this process, its stdout and stderr captured. A cancel (SystemExit from SIGTERM)
    goes on: the act's own `finally` runs, as at a terminal."""
    import workspace
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            if argv[0] in ("cleanup", "disk-guard"):
                code = workspace.machine_command(argv[0], argv[1:], json_summary=True)
            else:
                code = workspace.main(argv)
        except Exception as exc:                                    # noqa: BLE001  said, by its kind
            print(f"Observatory: {type(exc).__name__}: {memory_redact.redact(str(exc))[:LINE_CHARS]}",
                  file=sys.stderr)
            code = 1
    return int(code or 0), out.getvalue(), err.getvalue()


def _process(argv: list[str], job: dict) -> tuple[int, str, str]:
    """The engine's CLI in a process group of its own (`maintenance.Commands.apply`'s route): a cancel of the job
    stops following it, never the act. Its output goes to files beside the job's record, removed once read."""
    import configuration
    import jobs
    import osproc
    folder = jobs.jobs_dir()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    files = [folder / f"{job['id']}.{kind}" for kind in ("out", "err")]
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "OBSERVATORY_ROOT"}}
    full = [configuration.engine_python(), "-P", "-m", "observatory", "--home", str(_home()), "full", *argv]
    try:
        with open(files[0], "wb") as out, open(files[1], "wb") as err:
            for f in files:
                os.chmod(f, 0o600)
            proc = subprocess.Popen(full, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env,
                                    cwd=str(ROOT.parent), **osproc.new_group())
            code = proc.wait()
        texts = [f.read_text(encoding="utf-8", errors="replace") for f in files]
    finally:
        for f in files:
            f.unlink(missing_ok=True)
    return int(code or 0), texts[0], texts[1]


def _last_json(text: str) -> Any:
    """The CLI's JSON answer: the whole output, else the last document that starts a line and runs to the end."""
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        pass
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith(("{", "[")):
            try:
                return json.loads("\n".join(lines[i:]))
            except ValueError:
                continue
    return None


def run(job: dict, *, inprocess=None, process=None) -> dict:
    """One act to its end: the CLI's own function, then the result envelope with its JSON, scrubbed. The job's
    arguments are checked again and the argument list rebuilt here: a record is never run as it reads."""
    import interop
    import jobs
    started = time.monotonic()
    arguments = job.get("arguments") or {}
    action = arguments.get("action")
    if action not in ACTIONS:
        raise RuntimeError("the job names no operations act this engine knows")
    try:
        params = _validate(action, dict.fromkeys(TAKES[action]) | (arguments.get("params") or {}))
    except Refusal as refusal:
        raise RuntimeError(f"the job's arguments no longer check: {refusal}") from None
    argv = command(action, params)
    before = os.environ.get(SETUP_ENV)
    if action in SETUP:
        if before == "0":
            raise RuntimeError(f"{action} touches the scheduler, and this runner has {SETUP_ENV}=0")
        os.environ[SETUP_ENV] = "1"
    if job.get("id"):
        try:
            jobs.update(job["id"], statusMessage=f"running `{' '.join(argv[:2])}`")
        except Exception:                                           # noqa: BLE001  a status line is never the act
            pass
    try:
        if action in OWN_PROCESS:
            code, out, err = (process or _process)(argv, job)
        else:
            code, out, err = (inprocess or _inprocess)(argv)
    finally:
        if action in SETUP:
            if before is None:
                os.environ.pop(SETUP_ENV, None)
            else:
                os.environ[SETUP_ENV] = before
    said = [memory_redact.redact(line)[:LINE_CHARS] for line in err.strip().splitlines()[-STDERR_LINES:]]
    result = _scrub(_last_json(out))
    if result is None and code != 0:
        raise RuntimeError(f"`{' '.join(argv[:2])}` exited {code}: {said[-1] if said else 'no message'}")
    span = interop.Span.from_record(job["trace"])
    not_verified = [] if code == 0 else [{"claim": f"{action} succeeded",
                                          "reason": f"the command exited {code}; `result` and `said` say why"}]
    output = {"action": action, "command": argv[0], "exit": code, "result": result, "said": said}
    return interop.envelope(job_id=job.get("id", ""), capability=f"ops.{action}", span=span, output=output,
                            done=[{"claimId": "OPS-ACT", "statement": f"ran the CLI verb "
                                                                      f"{' '.join(argv[:2])!r}; it exited {code}"}],
                            proof=[], not_verified=not_verified, write_scopes=ACTIONS[action]["writes"],
                            wall_ms=int((time.monotonic() - started) * 1000), created_at=jobs.now_iso())
