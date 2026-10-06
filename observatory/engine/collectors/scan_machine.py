#!/usr/bin/env python3
"""What is running on this machine, where it came from, and where the disk went.

WHY. On 2026-09-27 the disk was at 100% with 4.3 GB free and ninety-nine
processes of one idle desktop app held 6.5 GB of memory, and nothing in the
observatory could say so: it watched projects, not the machine they run on. The
cleanup that followed was done by hand from `ps`, `du` and `git worktree list`.
This collector is that survey, made repeatable.

WHAT IT MEASURES
  processes  every process's RSS and CPU, grouped by ORIGIN — the nearest
             ancestor that explains it: an agent session (Claude Code, Codex),
             a launchd job, an application bundle, the system. A process whose
             working directory is inside a registered project is attributed to it.
  memory     physical size, used/compressed/free, swap.
  disk       the volume holding the home directory, and the places named in
             config/machine.json (caches, simulators, VM disks, histories),
             sized every `every_hours` because sizing takes minutes.

WHAT IT NEVER KEEPS. A command line can carry a token (`--api-key=…`,
`?token=…`), and witr's JSON carries the whole environment. Neither is stored:
a process is kept as its executable and, for an interpreter, the script it runs.

    scan_machine.py OUT.json            survey; disk places sized within a time budget, oldest first
    scan_machine.py OUT.json --disk     size every place now, with the larger manual budget
    scan_machine.py --explain PID       why this process is running (witr if installed)
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths  # noqa: E402

#: Interpreters whose first script argument names what actually runs.
INTERPRETERS = {"python", "python3", "node", "bun", "deno", "ruby", "perl", "java", "bash", "sh", "zsh"}
#: Command-line words that name secrets; anything after them is dropped.
SECRETISH = re.compile(r"(?i)(token|secret|password|passwd|api[-_]?key|auth|bearer|cookie|session)")
TOP = 40


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(cmd: list[str], timeout: int = 60) -> str | None:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


# --- processes ----------------------------------------------------------------

def process_table() -> dict[int, dict]:
    out = run(["ps", "-axo", "pid=,ppid=,rss=,%cpu=,user=,comm="], timeout=30)
    if out is None:
        return {}
    table: dict[int, dict] = {}
    for line in out.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        try:
            pid, ppid, rss = int(parts[0]), int(parts[1]), int(parts[2])
            cpu = float(parts[3])
        except ValueError:
            continue
        table[pid] = {"pid": pid, "ppid": ppid, "rss_kb": rss, "cpu": cpu, "user": parts[4], "exe": parts[5]}
    return table


def script_of(pid: int) -> str:
    """For an interpreter, the script or module it runs; never the rest of argv."""
    out = run(["ps", "-o", "args=", "-p", str(pid)], timeout=5)
    if not out:
        return ""
    words = out.split()
    for i, w in enumerate(words[1:], 1):
        if SECRETISH.search(w):
            return ""
        if w == "-m" and i + 1 < len(words):
            return words[i + 1]
        if not w.startswith("-") and ("/" in w or w.endswith((".py", ".js", ".mjs", ".ts", ".sh"))):
            return w if not SECRETISH.search(w) else ""
    return ""


def launchd_labels() -> dict[int, str]:
    out = run(["launchctl", "list"], timeout=15) if sys.platform == "darwin" else None
    labels: dict[int, str] = {}
    for line in (out or "").splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].isdigit():
            labels[int(parts[0])] = parts[2]
    return labels


def working_dirs() -> dict[int, str]:
    out = run(["lsof", "-a", "-d", "cwd", "-Fpn"], timeout=60)
    cwd: dict[int, str] = {}
    pid = None
    for line in (out or "").splitlines():
        if line.startswith("p"):
            pid = int(line[1:]) if line[1:].isdigit() else None
        elif line.startswith("n") and pid is not None:
            cwd[pid] = line[1:]
    return cwd


def app_of(exe: str) -> str | None:
    m = re.search(r"/Applications/([^/]+)\.app/", exe)
    return m.group(1) if m else None


def agent_of(exe: str) -> str | None:
    base = os.path.basename(exe)
    if "/claude/versions/" in exe or base == "claude":
        return "claude-code"
    if "/.codex/" in exe or base == "codex" or "Codex Framework" in exe:
        return "codex"
    return None


def simulator_names() -> dict[str, str]:
    """Booted simulator UUID -> device name, so a device reads as one."""
    if sys.platform != "darwin" or not shutil.which("xcrun"):
        return {}
    out = run(["xcrun", "simctl", "list", "devices", "booted", "-j"], timeout=20)
    try:
        devices = json.loads(out or "{}").get("devices") or {}
    except ValueError:
        return {}
    return {d["udid"]: d["name"] for runtime in devices.values() for d in runtime if d.get("udid")}


def display_name(exe: str) -> str:
    """A process's name for grouping: `npm exec pkg@1 …` -> `npm:pkg`, else the basename."""
    if exe.startswith("npm exec "):
        pkg = exe.split()[2] if len(exe.split()) > 2 else "npm"
        return "npm:" + re.sub(r"@[\d.]+$", "", pkg)
    return os.path.basename(exe.split(" --")[0]) or exe[:40]


def ancestry(pid: int, table: dict[int, dict]) -> list[int]:
    chain, seen = [], set()
    while pid in table and pid not in seen and pid > 1:
        seen.add(pid)
        chain.append(pid)
        pid = table[pid]["ppid"]
    return chain


def origin(pid: int, table: dict[int, dict], labels: dict[int, str],
           sims: dict[str, str] | None = None) -> tuple[str, int]:
    """(origin label, the pid that defines it). Nearest agent session first, then
    a launchd job (a simulator device by its name), then the outermost
    application bundle, then the system. A user process whose parent is launchd
    and which no job, app or agent explains is DETACHED — named so rather than
    filed under "other". Most are harmless helpers; a detached interpreter
    (node, python, an `npm exec` server) is usually a session's server that
    outlived it, which is what the findings look for."""
    chain = ancestry(pid, table)
    for p in chain:
        agent = agent_of(table[p]["exe"])
        if agent:
            root = p
            for q in chain[chain.index(p):]:
                if agent_of(table[q]["exe"]) == agent:
                    root = q
            return f"agent:{agent}", root
    for p in chain:
        label = labels.get(p)
        if label and label.startswith("com.apple.CoreSimulator.SimDevice."):
            udid = label.rsplit(".", 1)[-1]
            return f"simulator:{(sims or {}).get(udid, udid)}", p
        if label and not label.startswith("application."):
            return f"launchd:{label}", p
    apps = [(p, app_of(table[p]["exe"])) for p in chain]
    apps = [(p, a) for p, a in apps if a]
    if apps:
        p, a = apps[-1]
        return f"app:{a}", p
    if table.get(pid, {}).get("user") == "root":
        return "system", 1
    top = chain[-1] if chain else pid
    name = display_name(table.get(top, {}).get("exe", ""))
    exe = table.get(top, {}).get("exe", "")
    if table.get(top, {}).get("ppid") == 1 and not exe.startswith(("/System/", "/usr/libexec/", "/usr/sbin/")):
        return f"detached:{name}", top
    return f"process:{name}", top


def project_folders() -> list[tuple[str, str]]:
    """(absolute folder, project id), longest first so nested folders win."""
    try:
        doc = json.loads((paths.REGISTRY / "projects.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    data = str(paths.DATA)
    pairs = []
    for p in doc.get("projects") or []:
        for f in p.get("local_folders") or []:
            pairs.append((os.path.join(data, f), p["id"]))
    return sorted(pairs, key=lambda x: -len(x[0]))


def project_for(path: str, folders: list[tuple[str, str]]) -> str | None:
    for folder, pid in folders:
        if path == folder or path.startswith(folder + "/"):
            return pid
    return None


def survey_processes() -> dict:
    table = process_table()
    if not table:
        return {"degraded": [{"source": "ps", "reason": "the process table could not be read"}]}
    labels = launchd_labels()
    sims = simulator_names()
    cwd = working_dirs()
    folders = project_folders()
    groups: dict[str, dict] = {}
    by_project: dict[str, dict] = {}
    rows = []
    for pid, row in table.items():
        label, root = origin(pid, table, labels, sims)
        g = groups.setdefault(label, {"origin": label, "processes": 0, "rss_mb": 0.0, "cpu": 0.0, "roots": set()})
        g["processes"] += 1
        g["rss_mb"] += row["rss_kb"] / 1024
        g["cpu"] += row["cpu"]
        g["roots"].add(root)
        project = project_for(cwd.get(pid, ""), folders) or project_for(cwd.get(root, ""), folders)
        if project:
            b = by_project.setdefault(project, {"project": project, "processes": 0, "rss_mb": 0.0})
            b["processes"] += 1
            b["rss_mb"] += row["rss_kb"] / 1024
        rows.append((row, label, project))
    rows.sort(key=lambda r: -r[0]["rss_kb"])
    top = []
    for row, label, project in rows[:TOP]:
        exe = row["exe"]
        item = {"pid": row["pid"], "ppid": row["ppid"], "name": os.path.basename(exe), "exe": exe,
                "rss_mb": round(row["rss_kb"] / 1024, 1), "cpu": row["cpu"], "origin": label}
        if os.path.basename(exe).lower().rstrip("0123456789.") in INTERPRETERS or "Python" in exe:
            script = script_of(row["pid"])
            if script:
                item["script"] = script
        if project:
            item["project"] = project
        top.append(item)
    out_groups = []
    for g in sorted(groups.values(), key=lambda g: -g["rss_mb"]):
        out_groups.append({"origin": g["origin"], "processes": g["processes"], "sessions": len(g["roots"]),
                           "rss_mb": round(g["rss_mb"], 1), "cpu": round(g["cpu"], 1)})
    return {"count": len(table), "groups": out_groups,
            "projects": sorted(({**b, "rss_mb": round(b["rss_mb"], 1)} for b in by_project.values()),
                               key=lambda b: -b["rss_mb"]),
            "top": top, "degraded": [] if labels or sys.platform != "darwin" else
            [{"source": "launchctl", "reason": "launchd job labels could not be read"}]}


# --- memory -------------------------------------------------------------------

#: `sysctl` sits in /usr/sbin, which a launchd plist's minimal PATH does not
#: hold; called by bare name the lookup failed and memory simply went missing
#: from the survey, with nothing degraded to say why (audit A42).
SYSCTL = "/usr/sbin/sysctl" if sys.platform == "darwin" and pathlib.Path("/usr/sbin/sysctl").exists() else "sysctl"


def survey_memory() -> dict:
    if sys.platform == "darwin":
        total = run([SYSCTL, "-n", "hw.memsize"])
        vm = run(["vm_stat"]) or ""
        swap = run([SYSCTL, "-n", "vm.swapusage"]) or ""
        if total is None or not vm:
            return {"degraded": [{"source": "memory",
                                  "reason": f"{SYSCTL} or vm_stat did not answer; memory not measured"}]}
        page = int(re.search(r"page size of (\d+)", vm).group(1)) if "page size of" in vm else 16384
        pages = {k.strip().lower(): int(v.strip().rstrip(".")) for k, v in re.findall(r"^([^:]+):\s+(\d+)\.?$", vm, re.M)}
        mb = lambda n: round(n * page / 1048576, 1)  # noqa: E731
        used = re.search(r"used = ([\d.]+)M", swap)
        return {"total_mb": round(int(total) / 1048576, 1) if total else None,
                "free_mb": mb(pages.get("pages free", 0)),
                "active_mb": mb(pages.get("pages active", 0)),
                "inactive_mb": mb(pages.get("pages inactive", 0)),
                "wired_mb": mb(pages.get("pages wired down", 0)),
                "compressed_mb": mb(pages.get("pages occupied by compressor", 0)),
                "swap_used_mb": float(used.group(1)) if used else None}
    try:
        info = dict(line.split(":", 1) for line in open("/proc/meminfo"))
        kb = lambda k: int(info[k].split()[0])  # noqa: E731
        return {"total_mb": round(kb("MemTotal") / 1024, 1), "free_mb": round(kb("MemAvailable") / 1024, 1),
                "swap_used_mb": round((kb("SwapTotal") - kb("SwapFree")) / 1024, 1)}
    except (OSError, KeyError, ValueError):
        return {"degraded": [{"source": "memory", "reason": "no memory statistics on this platform"}]}


# --- disk ---------------------------------------------------------------------

def machine_config() -> dict:
    for f in (paths.config_file("machine.json"), ROOT / "defaults" / "machine.json"):
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {}


def volume() -> dict:
    home = pathlib.Path.home()
    st = shutil.disk_usage(home)
    # On APFS the Data volume is what fills; statvfs of the home directory is it.
    return {"path": str(home), "total_gb": round(st.total / 1e9, 1), "free_gb": round(st.free / 1e9, 1),
            "free_percent": round(100 * st.free / st.total, 1) if st.total else None}


def swap_on_disk() -> dict | None:
    """Memory written to disk. On macOS the swap files live on the VM volume,
    which shares the APFS container with the data volume — so swap growth IS free
    space shrinking, and a disk that fills while nothing is being written is often
    this (measured 2026-09-27: 15 GB of swap files, the main cause of a 11 GB fall
    in free space in one evening). One stat per file; never a `du`."""
    folder = pathlib.Path("/System/Volumes/VM") if sys.platform == "darwin" else None
    total, files = 0, 0
    if folder and folder.is_dir():
        try:
            for entry in os.scandir(folder):
                if entry.name.startswith("swapfile") and entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
                    files += 1
        except OSError:
            return None
        return {"path": str(folder), "gb": round(total / 1073741824, 2), "files": files}
    try:
        rows = open("/proc/swaps").read().splitlines()[1:]
    except OSError:
        return None
    for row in rows:
        parts = row.split()
        if len(parts) >= 3 and parts[1] == "file":
            total += int(parts[2]) * 1024
            files += 1
    return {"path": "/proc/swaps", "gb": round(total / 1073741824, 2), "files": files} if files else None


def size_kb(path: pathlib.Path, timeout: float) -> int | None:
    """One `du -skx`: -x stays on the path's own file system, so a mounted
    simulator image or VM volume is not counted as the host disk's usage. du
    exits non-zero when one entry is unreadable yet still prints the total, so
    the total is read regardless; a timeout means "not measured", never zero."""
    try:
        p = subprocess.run(["du", "-skx", str(path)], capture_output=True, text=True, timeout=max(1.0, timeout))
    except subprocess.TimeoutExpired:
        return None
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return int(p.stdout.split()[0])
    except (IndexError, ValueError):
        return None


def _stamp_age(stamp: str | None) -> float:
    try:
        return time.time() - datetime.strptime(stamp or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return float("inf")


#: Places a job with no person present must not open (lifecycle LC-06), relative to
#: the home directory. macOS guards each behind a privacy consent: Documents,
#: Downloads, Desktop and the media folders by name, iCloud Drive, and every other
#: app's container ("data from other apps"). A `du` of one from the tick raised that
#: consent at night as "python3.14 would like to access…" (measured 2026-10-02: the
#: AppData grant was rewritten the same second the tick sized an OrbStack container).
PROTECTED = ("Documents", "Downloads", "Desktop", "Pictures", "Movies", "Music",
             "Library/Mobile Documents", "Library/Containers", "Library/Group Containers",
             "Library/Mail", "Library/Messages", "Library/Safari", "Library/Calendars",
             "Library/Reminders", "Library/Application Support/AddressBook",
             "Library/Application Support/CallHistoryDB", "Library/Application Support/MobileSync")


def protected_place(path: pathlib.Path, home: pathlib.Path | None = None) -> bool:
    """True when `path` is, or is inside, a privacy-guarded place of `home`.

    Decided by the path alone, never by a flag in the config: a workspace that adds
    `~/Downloads` to its own machine.json must not thereby send the tick into it.
    The check runs before any stat, because even looking at a container can count
    as access."""
    home = pathlib.Path.home() if home is None else home
    try:
        rel = pathlib.PurePath(os.path.normpath(str(path))).relative_to(os.path.normpath(str(home)))
    except ValueError:
        return False
    parts = rel.parts
    for guarded in PROTECTED:
        g = pathlib.PurePath(guarded).parts
        if parts[:len(g)] == g:
            return True
    return False


def survey_disk(previous: dict | None, force: bool) -> dict:
    """Size the configured places within a time BUDGET per run.

    WHY A BUDGET. Sizing is `du` over trees of millions of files; on a machine
    under memory pressure with simulators running, one location took minutes and
    the whole survey held the tick for over twenty (measured 2026-09-27). So
    each run measures the places whose numbers are oldest first, stops when
    `disk_budget_seconds` is spent, and keeps every other place's last number
    with its own `measured_at`. A place that never fits is reported, not guessed.
    `--disk` (the operator, at a terminal) takes `disk_budget_seconds_manual`.

    PROTECTED PLACES ARE MANUAL ONLY. Without `--disk` a place `protected_place`
    names is not sized, not even stat'ed: it is listed in `manual_only` with its
    last manual number, if any. Only a person's `--disk` run measures it."""
    cfg = machine_config()
    every = float(cfg.get("every_hours", 12)) * 3600
    budget = float(cfg.get("disk_budget_seconds_manual" if force else "disk_budget_seconds", 900 if force else 90))
    per = float(cfg.get("disk_location_timeout_seconds", 60))
    prev = {r["path"]: r for r in ((previous or {}).get("disk") or {}).get("locations") or [] if isinstance(r, dict)}
    out = {"volume": volume(), "thresholds": {k: cfg.get(k) for k in ("free_space_warning_percent", "free_space_critical_percent")}}
    wanted, manual_only = [], []
    for loc in cfg.get("locations") or []:
        path = pathlib.Path(os.path.expanduser(loc["path"]))
        if not force and protected_place(path):
            manual_only.append({"path": loc["path"], "label": loc.get("label", loc["path"]),
                                "reason": "privacy-guarded: sized only by `full machine --disk`"})
            continue
        if path.exists():
            wanted.append((loc, path))
    due = sorted((x for x in wanted if force or _stamp_age(prev.get(x[0]["path"], {}).get("measured_at")) >= every),
                 key=lambda x: -_stamp_age(prev.get(x[0]["path"], {}).get("measured_at")))
    started, measured, degraded = time.monotonic(), {}, []
    for loc, path in due:
        left = budget - (time.monotonic() - started)
        if left <= 1:
            break
        kb = size_kb(path, min(per, left))
        if kb is None:
            degraded.append({"source": loc["path"], "reason": f"not sized within {min(per, left):.0f} s; the last number is kept"})
            continue
        measured[loc["path"]] = {"gb": round(kb / 1048576, 2), "measured_at": now()}
    rows = []
    for loc, _path in wanted:
        base = {**{k: loc[k] for k in ("path", "kind", "label", "reclaim") if k in loc},
                **({"command": loc["command"]} if loc.get("command") else {})}
        if loc["path"] in measured:
            rows.append({**base, **measured[loc["path"]]})
        elif loc["path"] in prev and "gb" in prev[loc["path"]]:
            old = prev[loc["path"]]
            rows.append({**base, "gb": old["gb"], "measured_at": old.get("measured_at") or
                         ((previous or {}).get("disk") or {}).get("measured_at")})
    swap = swap_on_disk()
    if swap and swap["gb"] > 0:
        rows.append({"path": swap["path"], "kind": "swap", "label": "Swap files (memory written to disk)",
                     "reclaim": "memory", "gb": swap["gb"], "measured_at": now()})
    # A guarded place keeps its last MANUAL number on the page, marked as such.
    for m in manual_only:
        old = prev.get(m["path"])
        if old and "gb" in old:
            rows.append({**{k: old[k] for k in ("path", "kind", "label", "reclaim", "command") if k in old},
                         "gb": old["gb"], "measured_at": old.get("measured_at"), "manual_only": True})
    rows.sort(key=lambda r: -r["gb"])
    pending = len([1 for loc, _p in wanted if loc["path"] not in measured and loc["path"] not in prev])
    out.update({"locations": rows, "measured_at": now(), "measured_now": len(measured), "never_measured": pending,
                "manual_only": manual_only})
    if degraded:
        out["degraded"] = degraded
    return out


# --- explain ------------------------------------------------------------------

def explain(pid: int) -> dict:
    """Why this process runs. witr when installed (its JSON, minus the process
    environment), else the native ancestry and launchd label."""
    table = process_table()
    if pid not in table:
        return {"pid": pid, "error": "no such process"}
    labels = launchd_labels()
    label, root = origin(pid, table, labels, simulator_names())
    native = {"pid": pid, "origin": label, "origin_pid": root,
              "ancestry": [{"pid": p, "name": os.path.basename(table[p]["exe"]), "exe": table[p]["exe"],
                            **({"launchd": labels[p]} if p in labels else {})} for p in ancestry(pid, table)]}
    witr = shutil.which("witr")
    if not witr:
        return {**native, "witr": "not installed — `brew install witr` adds ports, files and service detail"}
    out = run([witr, "--pid", str(pid), "--json", "--no-color"], timeout=20)
    try:
        doc = json.loads(out or "")
    except ValueError:
        return {**native, "witr": "witr returned no JSON"}
    proc = dict(doc.get("Process") or {})
    proc.pop("Env", None)          # the process environment: keys live there
    proc.pop("Cmdline", None)      # so can tokens
    return {**native, "witr": {"source": doc.get("Source"), "warnings": doc.get("Warnings") or [],
                               "process": proc,
                               "ancestry": [{k: a.get(k) for k in ("PID", "Command", "Service")}
                                            for a in doc.get("Ancestry") or []]}}


def main(argv: list[str]) -> int:
    if "--explain" in argv:
        pid = int(argv[argv.index("--explain") + 1])
        print(json.dumps(explain(pid), indent=1, ensure_ascii=False))
        return 0
    out = pathlib.Path(argv[1])
    try:
        previous = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = None
    doc = {"schema_version": 1, "measured_at": now(), "platform": sys.platform,
           "processes": survey_processes(), "memory": survey_memory(),
           "disk": survey_disk(previous, "--disk" in argv),
           "witr": bool(shutil.which("witr"))}
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, out)
    p = doc["processes"]
    print(f"machine: {p.get('count', 0)} processes in {len(p.get('groups') or [])} origins; "
          f"free {doc['disk']['volume']['free_gb']} GB ({doc['disk']['volume']['free_percent']}%); "
          f"disk places sized now {doc['disk'].get('measured_now', 0)} of {len(doc['disk'].get('locations') or [])}"
          + (f", {len(doc['disk']['degraded'])} over time" if doc['disk'].get('degraded') else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
