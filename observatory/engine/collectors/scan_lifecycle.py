#!/usr/bin/env python3
"""Watching the lifecycle contract hold on this machine, product by product.

    scan_lifecycle.py store/raw/lifecycle.json

WHY. The organisation's lifecycle contract (fabric-workspace knowledge/lifecycle.md,
"Watching it hold") makes each product prove its own rules in its own tests, and
asks this engine to measure the machine side continuously, because a measurement
is what tells us a test is missing. Four things are measured, each reported
against the product that owns the process or the file:

  orphan        a product's child or per-session server whose parent died
                (ppid 1) and which no launchd job explains — LC-02, LC-10
  stale-code    a per-session server started before its product's code was
                replaced on disk, or whose code is gone — LC-10, LC-11
  overrun       a launchd job of a product running longer than its own
                StartInterval — LC-03
  log-over-cap  a product's log larger than the cap, and log-readable for one
                another account can read — LC-12

WHO OWNS WHAT. The catalogue is (1) this engine itself, from its own paths;
(2) every fabric-service descriptor installed on this account, which names a
service's launchd label and log files; (3) the products listed in
`config/lifecycle.json` of the workspace, else `defaults/lifecycle.json` — the
shape of OWN below, with `{engine}`, `{state}` and `~` expanded. A process no
product claims is not reported here: the machine survey's detached-server
finding covers strangers.

WHAT IT NEVER KEEPS. A command line can carry a token. Each process is kept as
its executable and, for an interpreter, the script it runs (`redact`), exactly as
the machine survey does; file paths under the home directory are written `~/…`.

WHAT IT NEVER DOES. It starts nothing and signals nothing: `ps`, `launchctl list`
and reading plists and file sizes. `snapshot()` is the only function that runs a
command; `evaluate()` is pure over its input, which is how the tests drive it.
"""
from __future__ import annotations

import fnmatch
import json
import os
import pathlib
import plistlib
import re
import subprocess
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths  # noqa: E402
import osprivacy  # noqa: E402

#: Command-line words that name secrets; a script after one is not kept.
SECRETISH = re.compile(r"(?i)(token|secret|password|passwd|api[-_]?key|auth|bearer|cookie|session)")
SCRIPT_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".ts", ".sh")
DEFAULT_LOG_CAP = 5 * 1024 * 1024
LOG_SUFFIXES = (".log", ".err", ".out", ".jsonl")


def now_z(epoch: float | None = None) -> str:
    at = datetime.fromtimestamp(epoch, timezone.utc) if epoch is not None else datetime.now(timezone.utc)
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def tilde(path: str) -> str:
    home = str(pathlib.Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home.rstrip("/") + "/") else path


def redact(args: str) -> tuple[str, str]:
    """(executable, script) from a command line, and nothing else of it.

    The script is the first argument that looks like a path to a script; any
    secret-looking word before it drops the script too, because what follows
    such a word is the value."""
    words = args.split()
    if not words:
        return "", ""
    exe = words[0]
    for i, w in enumerate(words[1:], 1):
        if SECRETISH.search(w):
            return exe, ""
        if w == "-m" and i + 1 < len(words):
            return exe, words[i + 1]
        if not w.startswith("-") and ("/" in w or w.endswith(SCRIPT_SUFFIXES)):
            return exe, w
    return exe, ""


def elapsed_seconds(etime: str) -> int | None:
    """`ps -o etime` ([[dd-]hh:]mm:ss) as seconds."""
    try:
        days, _, rest = etime.strip().rpartition("-")
        parts = [int(x) for x in rest.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        h, m, s = parts[-3:]
        return (int(days) if days else 0) * 86400 + h * 3600 + m * 60 + s
    except ValueError:
        return None


# --- the catalogue -------------------------------------------------------------

def load_config() -> dict:
    for f in (paths.config_file("lifecycle.json"), ROOT / "defaults" / "lifecycle.json"):
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {}


def _expand(value: str) -> str:
    return os.path.expanduser(value.replace("{engine}", str(ROOT)).replace("{state}", str(paths.STATE)))


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "product"


#: This engine, described from its own paths so an installed wheel, a source
#: checkout and a test sandbox each name exactly themselves. Its launchd labels
#: are `org.project-observatory.<workspace digest>.<tick|server>`.
OWN = {"product": "Project Observatory",
       "labels": ["org.project-observatory.*"],
       "children": ["{engine}/tools/", "{engine}/collectors/", "{engine}/agent/", "{engine}/store/",
                    "{engine}/dashboard/"],
       "session_servers": [{"match": "{engine}/mcp/server.py", "code": "{engine}/configuration.py"}],
       "logs": ["{state}/logs"]}


def catalogue(config: dict, descriptors: list[dict]) -> list[dict]:
    """Every product this scan attributes to, merged by name: this engine first."""
    cap = int(config.get("log_cap_bytes") or DEFAULT_LOG_CAP)
    products: dict[str, dict] = {}
    config = {**config, "products": [OWN, *(config.get("products") or [])]}

    def entry(name: str) -> dict:
        return products.setdefault(name, {"product": name, "slug": slug(name), "labels": [], "children": [],
                                          "session_servers": [], "logs": [], "log_cap_bytes": cap})

    for p in config.get("products") or []:
        if not isinstance(p, dict) or not p.get("product"):
            continue
        e = entry(str(p["product"]))
        e["labels"] += [str(x) for x in p.get("labels") or []]
        e["children"] += [_expand(str(x)) for x in p.get("children") or []]
        e["session_servers"] += [{"match": _expand(str(s.get("match", ""))),
                                  **({"code": _expand(str(s["code"]))} if s.get("code") else {})}
                                 for s in p.get("session_servers") or [] if isinstance(s, dict) and s.get("match")]
        e["logs"] += [_expand(str(x)) for x in p.get("logs") or []]
        if p.get("log_cap_bytes"):
            e["log_cap_bytes"] = int(p["log_cap_bytes"])
    for d in descriptors:
        if not isinstance(d, dict):
            continue
        life = d.get("lifecycle") or {}
        label = life.get("label") if isinstance(life, dict) else None
        owner = next((e for e in products.values()
                      if label and any(fnmatch.fnmatchcase(label, g) for g in e["labels"])), None)
        e = owner or entry(str(d.get("name") or d.get("id") or "service"))
        if label and label not in e["labels"] and owner is None:
            e["labels"].append(label)
        logs = ((d.get("paths") or {}).get("logs") or []) if isinstance(d.get("paths"), dict) else []
        e["logs"] += [str(x) for x in logs if isinstance(x, str) and x not in e["logs"]]
    for e in products.values():
        e["logs"] = list(dict.fromkeys(e["logs"]))
    return list(products.values())


def descriptors() -> list[dict]:
    try:
        import fabric_service
        return [doc for _path, doc in fabric_service.read_descriptors()]
    except Exception:                                           # noqa: BLE001 — a host may have none
        return []


# --- the live snapshot -----------------------------------------------------------

def _run(cmd: list[str], timeout: int) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout if p.returncode == 0 else ""


def snapshot() -> dict:
    """The process table (redacted at once) and the launchd map. The only live read."""
    procs = []
    for line in _run(["ps", "-axo", "pid=,ppid=,etime=,args="], 30).splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        exe, script = redact(parts[3])
        procs.append({"pid": int(parts[0]), "ppid": int(parts[1]),
                      "elapsed_s": elapsed_seconds(parts[2]), "exe": exe, "script": script})
    launchd: dict[int, str] = {}
    if sys.platform == "darwin":
        for line in _run(["launchctl", "list"], 15).splitlines()[1:]:
            cols = line.split("\t")
            if len(cols) == 3 and cols[0].isdigit():
                launchd[int(cols[0])] = cols[2]
    import time
    return {"now": time.time(), "processes": procs, "launchd": launchd}


# --- evaluation ------------------------------------------------------------------

def _owner(proc: dict, products: list[dict]) -> tuple[dict, str] | None:
    """(product, role) for a process a product claims: `session` or `child`."""
    text = f"{proc.get('exe') or ''} {proc.get('script') or ''}"
    for p in products:
        if any(s["match"] and s["match"] in text for s in p["session_servers"]):
            return p, "session"
    for p in products:
        if any(c and c in text for c in p["children"]):
            return p, "child"
    return None


def _interval(label: str) -> int | None:
    plist = pathlib.Path.home() / "Library/LaunchAgents" / f"{label}.plist"
    try:
        doc = plistlib.loads(plist.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    value = doc.get("StartInterval")
    return int(value) if isinstance(value, int) and value > 0 else None


def _log_files(entry: str) -> list[pathlib.Path]:
    p = pathlib.Path(entry)
    if p.is_symlink():
        return []
    if p.is_dir():
        try:
            return sorted(f for f in p.iterdir() if f.suffix in LOG_SUFFIXES and f.is_file() and not f.is_symlink())
        except OSError:
            return []
    return [p] if p.is_file() else []


def evaluate(snap: dict, products: list[dict]) -> dict:
    now = float(snap.get("now") or 0)
    launchd = {int(k): v for k, v in (snap.get("launchd") or {}).items()}
    obs: list[dict] = []
    for proc in snap.get("processes") or []:
        owned = _owner(proc, products)
        name = os.path.basename(proc.get("script") or proc.get("exe") or "")
        if owned:
            product, role = owned
            base = {"product": product["product"], "slug": product["slug"], "pid": proc["pid"], "process": name}
            if proc.get("ppid") == 1 and proc["pid"] not in launchd:
                obs.append({"kind": "orphan", **base, "role": role, "elapsed_s": proc.get("elapsed_s")})
            if role == "session" and proc.get("elapsed_s") is not None:
                started = now - proc["elapsed_s"]
                for server in product["session_servers"]:
                    if server["match"] not in f"{proc.get('exe') or ''} {proc.get('script') or ''}" \
                            or not server.get("code"):
                        continue
                    try:
                        changed = os.stat(server["code"]).st_mtime
                    except OSError:
                        changed = None
                    if changed is None or changed > started + 1:
                        obs.append({"kind": "stale-code", **base, "started_at": now_z(started),
                                    "code": tilde(server["code"]),
                                    "code_changed_at": now_z(changed) if changed is not None else None})
        label = launchd.get(proc["pid"])
        if label and proc.get("elapsed_s") is not None:
            for product in products:
                if any(fnmatch.fnmatchcase(label, g) for g in product["labels"]):
                    interval = _interval(label)
                    if interval and proc["elapsed_s"] > interval:
                        obs.append({"kind": "overrun", "product": product["product"], "slug": product["slug"],
                                    "label": label, "pid": proc["pid"], "elapsed_s": proc["elapsed_s"],
                                    "interval_s": interval})
                    break
    for product in products:
        seen = set()
        for entry in product["logs"]:
            for f in _log_files(entry):
                if f in seen:
                    continue
                seen.add(f)
                try:
                    st = f.stat()
                except OSError:
                    continue
                base = {"product": product["product"], "slug": product["slug"], "file": tilde(str(f))}
                if st.st_size > product["log_cap_bytes"]:
                    obs.append({"kind": "log-over-cap", **base, "bytes": st.st_size,
                                "cap": product["log_cap_bytes"]})
                if not osprivacy.private(f):
                    obs.append({"kind": "log-readable", **base, "mode": osprivacy.describe(f)})
    obs.sort(key=lambda o: (o["product"], o["kind"], str(o.get("pid") or o.get("file") or "")))
    return {"schema_version": 1, "measured_at": now_z(now) if now else now_z(),
            "products": [{"product": p["product"], "slug": p["slug"], "labels": p["labels"],
                          "logs": [tilde(x) for x in p["logs"]]} for p in products],
            "observations": obs}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    doc = evaluate(snapshot(), catalogue(load_config(), descriptors()))
    out = pathlib.Path(argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    import atomic
    atomic.write_json(out, doc)
    counts: dict[str, int] = {}
    for o in doc["observations"]:
        counts[o["kind"]] = counts.get(o["kind"], 0) + 1
    print(f"lifecycle: {len(doc['products'])} product(s) watched; "
          + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "nothing out of contract"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
