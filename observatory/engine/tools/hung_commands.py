#!/usr/bin/env python3
"""Commands an agent session started and left hanging (OBS-530).

On 2026-11-12 a coding session ran `wc -l` with no file and no input closed; the command read
its terminal for 33 hours and the session waited on it all that time, with nothing on the
machine saying so. A peer project's rule, taken as it stands: a process is a hung command when

- an agent session (`claude`, `codex`, `scan_machine.agent_of`) is among its ancestors, and it is
  not that agent itself;
- it is a leaf of the process tree (no child: a shell waiting on its child is not the one hung);
- it is not `sleep` and not an MCP server: not a direct child of the agent (a stdio MCP server, a hook
  the agent itself waits on; a command a session runs is a shell's child) and no argument names `mcp`
  (a server started through `npx` or `uvx` sits one level lower);
- it is older than an hour;
- its CPU time did not grow between two samples at least five minutes apart (the previous
  sample is the receipt this module wrote last time);
- its standard input is not `/dev/null` (a command with input closed ends; one reading a pipe or
  a terminal nobody writes to does not).

What is done about one: a finding names it with its session's folder (`machine.hung_commands`).
Only a leaf command of the short list `LEAVES` (`wc`, `cat`, `grep`, `head`) reading no file is
stopped, with SIGTERM, and only with `--apply` (the maintenance pass gives it when
`features.auto_cleanup` is on). A session, a service or any other command is never signalled.
Each stop is journalled. Argument lists are read only to decide "no file"; none is stored.

    hung_commands.py [--receipt PATH] [--journal PATH] [--apply] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "collectors"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

SCHEMA = "hung-commands/1"
MIN_AGE_SECONDS = 3600
#: Two samples closer than this say nothing about a command that reads its input once a minute.
MIN_INTERVAL_SECONDS = 300
#: CPU time that grows less than this between samples did not grow (ps rounds to hundredths).
CPU_EPSILON = 0.05
#: Commands that only read their input and print: stopping one loses nothing but its output.
LEAVES = ("wc", "cat", "grep", "head")
#: Options of the leaves that take a value, so the value is not read as a file.
_VALUED = {"head": {"-n", "-c"}, "grep": {"-e", "-f", "-m", "-A", "-B", "-C", "--color", "--colour"},
           "wc": set(), "cat": set()}
NEVER = ("sleep",)
#: Start times read from `etime` drift by the sampling; two readings of one process agree within this.
SAME_START = 120


def parse_duration(text: str) -> float | None:
    """`ps` etime and time: [[dd-]hh:]mm:ss[.cc]."""
    text = text.strip()
    days = 0
    if "-" in text:
        d, text = text.split("-", 1)
        if not d.isdigit():
            return None
        days = int(d)
    parts = text.split(":")
    try:
        values = [float(p) for p in parts]
    except ValueError:
        return None
    if not 1 <= len(values) <= 3:
        return None
    seconds = 0.0
    for v in values:
        seconds = seconds * 60 + v
    return days * 86400 + seconds


def parse_table(text: str) -> dict[int, dict]:
    """`ps -axo pid=,ppid=,uid=,etime=,time=,comm=`."""
    table: dict[int, dict] = {}
    for line in text.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6 or not (parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit()):
            continue
        age, cpu = parse_duration(parts[3]), parse_duration(parts[4])
        if age is None or cpu is None:
            continue
        table[int(parts[0])] = {"pid": int(parts[0]), "ppid": int(parts[1]), "uid": int(parts[2]),
                                "age_s": age, "cpu_s": cpu, "exe": parts[5].strip()}
    return table


def _run(argv: list[str], timeout: int = 30) -> str | None:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 or p.stdout else None


def read_table() -> dict[int, dict] | None:
    out = _run(["ps", "-axo", "pid=,ppid=,uid=,etime=,time=,comm="])
    return parse_table(out) if out is not None else None


def _agent(exe: str) -> str | None:
    import scan_machine
    return scan_machine.agent_of(exe)


def session_of(pid: int, table: dict[int, dict]) -> int | None:
    """The nearest agent ancestor's pid (never the process itself), or None."""
    seen, p = {pid}, table[pid]["ppid"]
    while p in table and p not in seen and p > 1:
        if _agent(table[p]["exe"]):
            return p
        seen.add(p)
        p = table[p]["ppid"]
    return None


def candidates(table: dict[int, dict], uid: int) -> dict[int, int]:
    """{pid: its session's pid} for this account's leaves under an agent, older than an hour."""
    parents = {row["ppid"] for row in table.values()}
    out = {}
    for pid, row in table.items():
        base = os.path.basename(row["exe"])
        if (row["uid"] != uid or pid in parents or row["age_s"] < MIN_AGE_SECONDS or base in NEVER
                or "mcp" in row["exe"].lower() or _agent(row["exe"])):
            continue
        session = session_of(pid, table)
        if session is not None and row["ppid"] != session:
            out[pid] = session
    return out


def start_of(row: dict, now: float) -> float:
    return now - row["age_s"]


def still_idle(row: dict, prev: dict | None, now: float, prev_at: float | None) -> bool:
    """Whether the previous sample is of this same process and its CPU time has not grown since."""
    if not isinstance(prev, dict) or prev_at is None or now - prev_at < MIN_INTERVAL_SECONDS:
        return False
    try:
        same = abs(float(prev["start"]) - start_of(row, now)) <= SAME_START
        return same and row["cpu_s"] - float(prev["cpu_s"]) < CPU_EPSILON
    except (KeyError, TypeError, ValueError):
        return False


def parse_lsof(text: str) -> dict[int, str]:
    """`lsof -Fpn` output: {pid: name of the file}."""
    out: dict[int, str] = {}
    pid = None
    for line in text.splitlines():
        if line.startswith("p"):
            pid = int(line[1:]) if line[1:].isdigit() else None
        elif line.startswith("n") and pid is not None:
            out.setdefault(pid, line[1:])
    return out


def stdin_of(pids: list[int]) -> dict[int, str] | None:
    """{pid: what fd 0 is}; None when it cannot be read at all."""
    if not pids:
        return {}
    if sys.platform.startswith("linux"):
        out = {}
        for pid in pids:
            try:
                out[pid] = os.readlink(f"/proc/{pid}/fd/0")
            except OSError:
                continue
        return out
    text = _run(["lsof", "-a", "-p", ",".join(str(p) for p in pids), "-d", "0", "-Fpn"])
    return parse_lsof(text) if text is not None else None


def cwd_of(pids: list[int]) -> dict[int, str]:
    if not pids:
        return {}
    if sys.platform.startswith("linux"):
        out = {}
        for pid in pids:
            try:
                out[pid] = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                continue
        return out
    return parse_lsof(_run(["lsof", "-a", "-p", ",".join(str(p) for p in pids), "-d", "cwd", "-Fpn"]) or "")


def reads_no_file(name: str, args: list[str]) -> bool:
    """Whether a leaf command's arguments (after its own name) name no file. Conservative: anything
    that could be a file counts as one."""
    valued = _VALUED.get(name, set())
    words, skip, explicit_pattern = [], False, False
    for w in args:
        if skip:
            skip = False
            continue
        if w == "--":
            continue
        if w.startswith("-") and len(w) > 1:
            if w in valued:
                skip = True
                explicit_pattern = explicit_pattern or w in ("-e", "-f")
            continue
        words.append(w)
    if name == "grep" and not explicit_pattern:
        words = words[1:]                                  # the pattern
    return not words


def args_of(pid: int) -> list[str] | None:
    out = _run(["ps", "-o", "args=", "-p", str(pid)], timeout=5)
    return out.split()[1:] if out else None


def folder_name(path: str) -> str:
    """A session's folder as a finding names it: its last part, never the full path."""
    return os.path.basename(path.rstrip("/")) or path


def survey(previous: dict | None, *, uid: int | None = None, now: float | None = None, table=None,
           stdin=None, cwd=None, args=None) -> dict:
    """The receipt: this sample and the hung commands it finds against the previous one."""
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    if uid is None:
        import osprivacy
        account = osprivacy.account()
        uid = int(account) if account.isdigit() else -1
    at = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    receipt = {"schema": SCHEMA, "at": at, "samples": {}, "hung": [], "degraded": []}
    if os.name == "nt":
        receipt["degraded"].append({"source": "platform", "reason": "hung commands are not read on Windows yet"})
        return receipt
    table = table if table is not None else read_table()
    if table is None:
        receipt["degraded"].append({"source": "ps", "reason": "the process table could not be read"})
        return receipt
    found = candidates(table, uid)
    receipt["samples"] = {str(pid): {"start": round(start_of(table[pid], now)), "cpu_s": table[pid]["cpu_s"]}
                          for pid in found}
    prev = (previous or {}) if (previous or {}).get("schema") == SCHEMA else {}
    try:
        prev_at = datetime.strptime(prev.get("at", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        prev_at = None
    idle = [pid for pid in found if still_idle(table[pid], (prev.get("samples") or {}).get(str(pid)), now, prev_at)]
    inputs = (stdin or stdin_of)(idle)
    if inputs is None:
        receipt["degraded"].append({"source": "lsof", "reason": "standard input of the idle commands could not be read"})
        return receipt
    idle = [pid for pid in idle if pid in inputs and inputs[pid] != "/dev/null"]
    folders = (cwd or cwd_of)(sorted({found[pid] for pid in idle}))
    for pid in sorted(idle):
        row, session = table[pid], found[pid]
        name = os.path.basename(row["exe"])
        words = (args or args_of)(pid)
        if words is not None and any("mcp" in w.lower() for w in words):
            continue
        receipt["hung"].append({
            "pid": pid, "name": name, "agent": _agent(table[session]["exe"]), "session_pid": session,
            "folder": folder_name(folders.get(session, "")) or None,
            "age_hours": round(row["age_s"] / 3600, 1), "cpu_seconds": row["cpu_s"],
            "stdin": "terminal" if inputs[pid].startswith("/dev/tty") or inputs[pid].startswith("/dev/pts")
            else "pipe" if "pipe" in inputs[pid] or inputs[pid].startswith("->") else "other",
            "leaf": name in LEAVES and words is not None and reads_no_file(name, words)})
    return receipt


def stop_leaves(receipt: dict, *, table=None, kill=os.kill, now: float | None = None) -> list[dict]:
    """SIGTERM every leaf the receipt names, after checking the pid is still that process."""
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    leaves = [h for h in receipt.get("hung") or [] if h.get("leaf")]
    if not leaves or os.name == "nt":
        return []
    table = table if table is not None else (read_table() or {})
    stopped = []
    for h in leaves:
        row, sample = table.get(h["pid"]), (receipt.get("samples") or {}).get(str(h["pid"])) or {}
        if (row is None or os.path.basename(row["exe"]) != h["name"]
                or abs(start_of(row, now) - float(sample.get("start", -1e18))) > SAME_START):
            stopped.append({"pid": h["pid"], "name": h["name"], "result": "gone"})
            continue
        try:
            kill(h["pid"], signal.SIGTERM)
            stopped.append({"pid": h["pid"], "name": h["name"], "folder": h.get("folder"), "result": "stopped"})
        except OSError as exc:
            stopped.append({"pid": h["pid"], "name": h["name"], "result": "error", "detail": type(exc).__name__})
    return stopped


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def run(receipt_path: Path, journal: Path, apply: bool) -> dict:
    try:
        previous = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = None
    receipt = survey(previous if isinstance(previous, dict) else None)
    if apply:
        receipt["stopped"] = stop_leaves(receipt)
        if receipt["stopped"]:
            journal.parent.mkdir(parents=True, exist_ok=True)
            with journal.open("a", encoding="utf-8") as fh:
                for row in receipt["stopped"]:
                    fh.write(json.dumps({"at": receipt["at"], **row}, ensure_ascii=False) + "\n")
    _write(receipt_path, receipt)
    return receipt


def main(argv: list[str] | None = None) -> int:
    import paths
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--receipt", type=Path, default=paths.SCRATCH / "hung-commands.json")
    ap.add_argument("--journal", type=Path, default=paths.STATE / "logs" / "hung-commands.jsonl")
    ap.add_argument("--apply", action="store_true", help="SIGTERM the leaf commands of the short list")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    receipt = run(a.receipt, a.journal, a.apply)
    if a.json:
        print(json.dumps({k: receipt[k] for k in ("at", "hung", "degraded", "stopped") if k in receipt}, ensure_ascii=False))
    else:
        for h in receipt["hung"]:
            print(f"{h['pid']} {h['name']} {h['age_hours']} h in {h['folder'] or 'unknown folder'}"
                  + (" (leaf)" if h["leaf"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
