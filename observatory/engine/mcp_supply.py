#!/usr/bin/env python3
"""The supply-chain watch over MCP servers (OBS-555, CP-09): what a server's command line is, and whether its tools moved.

THREE QUESTIONS, ASKED WITHOUT READING A SECRET.

1. Is the command line risky? `judge(command, args)` returns rule ids and nothing else: `unpinned-runner` (a package
   runner that fetches whatever is newest: `npx`, `bunx`, `pnpm dlx`, `uvx`, `pipx run` without an exact version, or
   with `@latest`), `fetch-execute` (a program fetched from a URL and run as it arrives) and `outside-install-places`
   (an executable at a path no installer owns: a temporary folder, a download folder, a relative path). The scan
   calls it at the reader, beside the redaction, and stores the ids; the command line itself, its arguments and the
   environment values never reach a file, a finding or a page. A finding names the server and the rule.
2. Did a tool change? When a server is probed on request, each tool's whole definition (name, description, input
   schema) is hashed with SHA-256 and pinned in `store/state/mcp-pins.json`. The first sight pins; a later probe that
   finds a tool added, removed or changed records it as pending against the ACCEPTED pin (`mcp.tool_changed`), and the
   person accepts the new definitions (`accept`), page button or `observatory_mcp_accept`. Only digests are stored: a
   description can carry anything, and none of it is kept. A change that is reverted clears itself.
3. Does a scanner send content out? A third-party remote scanner receives tool definitions, which is content. It is
   off until the person turns on `features.mcp_remote_scanners` through the configuration door (`remote_scanners`);
   this module sends nothing itself and says which state it found.

The probe that lists tools starts a stdio server on a person's or an agent's request only (never from the tick). It
does not pass a server's declared environment (values are never read), so a server that needs its own credentials
answers nothing and is named in `degraded`, not pinned. A server whose command is judged `fetch-execute` is not started.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

SCHEMA = "mcp-pins/1"
SWITCH = "features.mcp_remote_scanners"
RULES = {
    "unpinned-runner": "a package runner fetches the newest version each time, so what runs can change under the declaration",
    "fetch-execute": "the command fetches a program from a URL and runs it as it arrives",
    "outside-install-places": "the executable sits where no installer owns it (a temporary or download folder, or a relative path)",
}
#: Seconds one server may take to list its tools, and the most the whole refresh may spend on listings.
LIST_TIMEOUT = 20.0
LIST_BUDGET = 300.0

#: `x.y.z` with optional pre-release and build parts: the only thing counted as a pinned version.
EXACT = re.compile(r"v?\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?")
#: Programs that take a package to run: (program, the word before the package, or None when it is the first one).
RUNNERS = {"npx": None, "bunx": None, "uvx": None, "pipx": "run", "pnpm": "dlx", "yarn": "dlx", "npm": "exec",
           "bun": "x", "uv": "tool run", "pnpx": None}
INTERPRETERS = {"sh", "bash", "zsh", "dash", "fish", "python", "python3", "node", "deno", "bun", "ruby", "perl", "php"}
#: A pipe into a shell or an interpreter, or a command substitution of a download, anywhere in an argument.
PIPE_TO_INTERPRETER = re.compile(r"\|\s*(sudo\s+)?(sh|bash|zsh|dash|python3?|node|ruby|perl|php)\b")
FETCHER = re.compile(r"\b(curl|wget|fetch)\b")
URL = re.compile(r"^https?://", re.I)
#: Absolute locations an installer or a package manager owns, as prefixes; and home-relative ones.
KNOWN_ROOTS = ("/usr/", "/bin/", "/sbin/", "/opt/", "/nix/", "/snap/", "/Applications/", "/System/", "/Library/",
               "/home/linuxbrew", "/run/current-system/")
KNOWN_HOME = (".local/", ".cargo/", ".bun/", ".nvm/", ".volta/", ".deno/", ".asdf/", ".pyenv/", ".rbenv/", ".fnm/",
              ".npm-global/", ".claude/", ".codex/", ".cursor/", ".config/", "go/bin/", ".rustup/", ".sdkman/",
              "bin/", "Library/", ".mise/", ".local/share/")
TEMPORARY = ("/tmp/", "/var/tmp/", "/private/tmp/", "/private/var/", "/dev/shm/", "/var/folders/")
DOWNLOAD_PARTS = ("/Downloads/", "/Desktop/", "\\Downloads\\", "/Temp/", "\\Temp\\", "\\AppData\\Local\\Temp\\")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- the command line ---------------------------------------------------------------------------------------------

def _program(command: str) -> str:
    name = re.split(r"[\\/]", command.strip())[-1].lower()
    return name[:-4] if name.endswith(".exe") or name.endswith(".cmd") else name


def _spec_pinned(spec: str, program: str) -> bool:
    """Whether a package spec names one exact version: `pkg@1.2.3`, `@scope/pkg@1.2.3`, `pkg==1.2.3`."""
    if "@" in spec[1:]:
        version = spec.rsplit("@", 1)[1]
    elif "==" in spec:
        version = spec.split("==", 1)[1]
    else:
        return False
    return bool(EXACT.fullmatch(version))


def _unpinned_runner(program: str, args: list[str]) -> bool:
    if program not in RUNNERS:
        return False
    lead = RUNNERS[program]
    rest = list(args)
    if lead is not None:
        words = lead.split()
        if rest[: len(words)] != words:
            return False            # `npm install`, `pipx install`: not a runner call
        rest = rest[len(words):]
    value_flags = {"--from", "--with", "-p", "--package", "--spec"}
    spec = None
    index = 0
    while index < len(rest):
        item = rest[index]
        if item in value_flags and index + 1 < len(rest):
            candidate = rest[index + 1]
            if item in ("--from", "--spec", "-p", "--package") and not candidate.startswith("-"):
                spec = candidate
                break
            index += 2
            continue
        if item.startswith("-"):
            index += 1
            continue
        spec = item
        break
    if spec is None:
        return False
    if URL.match(spec) or spec.startswith(("git+", "file:", "./", "../", "/", "~")):
        # A URL is the fetch rule's; a path is a local program the person owns.
        return False
    return not _spec_pinned(spec, program)


def _fetch_execute(program: str, args: list[str]) -> bool:
    if program in ("curl", "wget"):
        return True
    joined = " ".join(args)
    if PIPE_TO_INTERPRETER.search(joined) and FETCHER.search(joined):
        return True
    if program in INTERPRETERS:
        if any(URL.match(a) for a in args):
            return True                              # `node https://…`, `deno run https://…`
        if "-c" in args or "-e" in args or "-lc" in args:
            return bool(FETCHER.search(joined)) and ("$(" in joined or "`" in joined)
    return False


def _outside(command: str, home: str) -> bool:
    text = command.strip()
    if not text:
        return False
    if text.startswith("~/"):
        return False
    if text.startswith(("./", "../", ".\\", "..\\")):
        return True
    absolute = text.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", text) is not None
    if not absolute:
        return False                                 # a bare name is looked up on the PATH, an installer's place
    if any(text.startswith(p) for p in TEMPORARY) or any(p in text for p in DOWNLOAD_PARTS):
        return True
    if any(text.startswith(p) for p in KNOWN_ROOTS):
        return False
    base = home.rstrip("/\\") if home else ""
    if base and text.startswith(base) and text[len(base):len(base) + 1] in ("/", "\\"):
        # Either separator: a config written on one system is read on another (a POSIX path read on Windows).
        rel = text[len(base) + 1:].replace("\\", "/")
        return not any(rel.startswith(p) for p in KNOWN_HOME)
    return True


def judge(command: Any, args: Any = (), home: str | None = None) -> list[str]:
    """The rule ids a command line trips, in a fixed order. Never the command line, never an argument."""
    if isinstance(command, list):
        args = list(command[1:]) + list(args or ())
        command = command[0] if command else ""
    if not isinstance(command, str) or not command.strip():
        return []
    argv = [str(a) for a in (args or ())]
    program = _program(command)
    home = home if home is not None else os.path.expanduser("~")
    found = []
    if _unpinned_runner(program, argv):
        found.append("unpinned-runner")
    if _fetch_execute(program, argv):
        found.append("fetch-execute")
    if _outside(command, home):
        found.append("outside-install-places")
    return found


# --- tool definitions and their pins ------------------------------------------------------------------------------

def tool_digest(tool: dict) -> str:
    """SHA-256 of the whole canonical definition: name, description and input schema (and output schema when present),
    so a changed parameter description counts, not only the top one."""
    body = {k: tool.get(k) for k in ("name", "description", "inputSchema", "outputSchema") if k in tool}
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def digest_tools(tools: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for tool in tools if isinstance(tools, list) else []:
        if isinstance(tool, dict) and isinstance(tool.get("name"), str) and tool["name"]:
            out[tool["name"]] = tool_digest(tool)
    return out


def pins_path() -> pathlib.Path:
    import paths
    return paths.STATE / "state" / "mcp-pins.json"


def load(path: pathlib.Path | None = None) -> dict:
    path = path or pins_path()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict) or not isinstance(doc.get("servers"), dict):
        doc = {"schema": SCHEMA, "servers": {}}
    return doc


def save(doc: dict, path: pathlib.Path | None = None) -> None:
    import atomic
    path = path or pins_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(path, doc)


def key_of(agent: str, name: str) -> str:
    return f"{agent}:{name}"


def observe(doc: dict, key: str, current: dict[str, str], stamp: str | None = None) -> list[dict]:
    """Record one probe of one server. The first sight pins every tool; a later one compares with the accepted pins
    and stores what differs as `pending` (replacing the last probe's, so a change put back clears). Returns the
    pending changes as rows {tool, change, old, new}."""
    stamp = stamp or now()
    servers = doc.setdefault("servers", {})
    entry = servers.get(key)
    if not isinstance(entry, dict) or not isinstance(entry.get("tools"), dict):
        servers[key] = {"pinned_at": stamp, "checked_at": stamp, "tools": dict(sorted(current.items())),
                        "pending": {}}
        return []
    pinned: dict = entry["tools"]
    before = entry.get("pending") if isinstance(entry.get("pending"), dict) else {}
    pending: dict[str, dict] = {}
    for name in sorted(set(pinned) | set(current)):
        old, new = pinned.get(name), current.get(name)
        if old == new:
            continue
        change = "added" if old is None else "removed" if new is None else "changed"
        seen = before.get(name) if isinstance(before.get(name), dict) and before[name].get("new") == new else None
        pending[name] = {"change": change, "old": old, "new": new,
                         "seen_at": (seen or {}).get("seen_at") or stamp}
    entry["pending"], entry["checked_at"] = pending, stamp
    return changes_of(entry)


def changes_of(entry: dict) -> list[dict]:
    pending = entry.get("pending") if isinstance(entry, dict) and isinstance(entry.get("pending"), dict) else {}
    return [{"tool": t, **{k: v.get(k) for k in ("change", "old", "new", "seen_at")}}
            for t, v in sorted(pending.items()) if isinstance(v, dict)]


def accept(doc: dict, key: str | None = None, tool: str | None = None, stamp: str | None = None) -> dict:
    """Make the pending definitions the pins: for one server (and one tool), or every server with a pending change.
    Returns {accepted: n, servers: [...]}; nothing pending accepts nothing, which is an answer, not an error."""
    stamp = stamp or now()
    done, touched = 0, []
    for k, entry in sorted((doc.get("servers") or {}).items()):
        if key is not None and k != key:
            continue
        pending = entry.get("pending") if isinstance(entry, dict) else None
        if not isinstance(pending, dict) or not pending:
            continue
        for name in list(pending):
            if tool is not None and name != tool:
                continue
            row = pending.pop(name)
            if row.get("new") is None:
                entry["tools"].pop(name, None)
            else:
                entry["tools"][name] = row["new"]
            done += 1
            if k not in touched:
                touched.append(k)
        entry["accepted_at"] = stamp
    return {"accepted": done, "servers": touched}


def accept_pending(key: str | None = None, tool: str | None = None, path: pathlib.Path | None = None) -> dict:
    """`accept` on the pin file: load, accept, save. The one function the page's button, the agent's tool and the CLI call."""
    doc = load(path)
    answer = accept(doc, key or None, tool or None)
    if answer["accepted"]:
        save(doc, path)
    return {**answer, "degraded": []}


def record_probe(per_server: dict[str, dict[str, str] | None], path: pathlib.Path | None = None,
                 stamp: str | None = None) -> dict[str, list[dict]]:
    """Fold one probe's listings into the pin file. `None` for a server means it was not listed this time: its pins
    are left as they are. Returns the pending changes by server key."""
    doc = load(path)
    found: dict[str, list[dict]] = {}
    for key, current in per_server.items():
        if current is None:
            continue
        rows = observe(doc, key, current, stamp)
        if rows:
            found[key] = rows
    doc["schema"] = SCHEMA
    save(doc, path)
    return found


# --- listing tools from a stdio server (on request only) ------------------------------------------------------------

def stdio_tools(command: str, args: list[str], timeout: float = LIST_TIMEOUT) -> list[dict]:
    """Start one stdio server, ask it for its tools (initialize, then tools/list) and stop it. Raises RuntimeError with a
    reason that names no argument. The declared environment is not passed: its values are never read."""
    import osproc
    argv = [command, *args]
    proc = subprocess.Popen(osproc.program(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, **osproc.new_group())
    answers: dict[int, dict] = {}
    ready = threading.Event()

    def read() -> None:
        for raw in proc.stdout:                       # one JSON message per line
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if isinstance(message, dict) and isinstance(message.get("id"), int):
                answers[message["id"]] = message
                ready.set()

    threading.Thread(target=read, daemon=True).start()

    def send(message: dict) -> None:
        proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        proc.stdin.flush()

    def wait(ident: int, until: float) -> dict:
        while ident not in answers:
            if time.monotonic() > until or proc.poll() is not None and ident not in answers:
                raise RuntimeError("the server did not answer")
            ready.wait(0.05)
            ready.clear()
        return answers[ident]

    try:
        until = time.monotonic() + timeout
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "project-observatory", "version": "0"}}})
        wait(1, until)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools: list[dict] = []
        cursor, ident = None, 2
        while True:
            send({"jsonrpc": "2.0", "id": ident, "method": "tools/list",
                  "params": {"cursor": cursor} if cursor else {}})
            answer = wait(ident, until)
            result = answer.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("the server refused to list its tools")
            tools += [t for t in result.get("tools") or [] if isinstance(t, dict)]
            cursor = result.get("nextCursor")
            if not cursor or ident > 20:
                return tools
            ident += 1
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"the server could not be asked ({type(exc).__name__})") from None
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        osproc.stop_group(proc.pid, 2.0)
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def probe_tools(specs: dict[str, tuple[str, list[str]]], lister: Callable[[str, list[str]], list[dict]],
                risky: dict[str, list[str]] | None = None, budget: float = LIST_BUDGET,
                path: pathlib.Path | None = None) -> tuple[dict[str, list[dict]], list[dict]]:
    """List and pin the tools of each stdio server in `specs` ({key: (command, args)}); returns (pending changes by
    server, degraded). A server judged `fetch-execute` is not started; one that cannot be asked is named, not pinned."""
    degraded: list[dict] = []
    listed: dict[str, dict[str, str] | None] = {}
    started = time.monotonic()
    for key, (command, args) in sorted(specs.items()):
        if "fetch-execute" in (risky or {}).get(key, ()):
            degraded.append({"source": f"mcp-tools:{key}", "reason": "not started: its command fetches and runs code"})
            continue
        if time.monotonic() - started > budget:
            degraded.append({"source": f"mcp-tools:{key}", "reason": "not listed: the refresh ran out of its time budget"})
            continue
        try:
            listed[key] = digest_tools(lister(command, args))
        except RuntimeError as exc:
            degraded.append({"source": f"mcp-tools:{key}", "reason": str(exc)[:120]})
        except Exception as exc:                      # noqa: BLE001: one server never stops the refresh
            degraded.append({"source": f"mcp-tools:{key}", "reason": f"could not be asked ({type(exc).__name__})"})
    return record_probe(listed, path), degraded


# --- the third-party scanners switch --------------------------------------------------------------------------------

def remote_scanners(base: pathlib.Path | None = None) -> dict:
    """{enabled, reason, source}: whether sending tool definitions to a third-party scanner is allowed. Off unless the
    person turned `features.mcp_remote_scanners` on; never sends anything itself."""
    reason = ("a third-party remote scanner receives each tool's name, description and schema, which is content from "
              "your servers: it stays off until you turn it on")
    try:
        import configuration
        on = configuration.enabled("mcp_remote_scanners", "features",
                                   settings=None if base is None else configuration.load(base))
    except Exception as exc:                          # noqa: BLE001: an unreadable setting is off, said so
        return {"enabled": False, "reason": reason, "source": SWITCH, "degraded": [{"source": SWITCH,
                "reason": f"the setting could not be read ({type(exc).__name__}); treated as off"}]}
    return {"enabled": bool(on), "reason": reason, "source": SWITCH, "degraded": []}


# --- the answer for the page, the tool and the findings -------------------------------------------------------------

def summary(servers: list[dict] | None = None, path: pathlib.Path | None = None, base: pathlib.Path | None = None) -> dict:
    """{risky, changes, pins, scanners, degraded}: `risky` rows {server, agent, rules} from the scan's declarations,
    `changes` rows {server, tool, change, old, new} still waiting for acceptance, `pins` the count of pinned tools."""
    degraded: list[dict] = []
    risky = []
    if servers is None:
        try:
            import mcp_inventory
            doc = json.loads(mcp_inventory.scan_file().read_text(encoding="utf-8"))
            servers = doc.get("servers") or []
        except (OSError, ValueError):
            servers = []
            degraded.append({"source": "mcp.json", "reason": "no MCP scan yet, so commands were not judged"})
    seen = set()
    for row in servers:
        rules = [r for r in (row.get("risky") or []) if r in RULES] if isinstance(row, dict) else []
        if rules and row.get("name") and (row.get("agent"), row.get("name"), tuple(rules)) not in seen:
            seen.add((row.get("agent"), row.get("name"), tuple(rules)))
            risky.append({"server": row["name"], "agent": row.get("agent"), "rules": rules})
    risky.sort(key=lambda r: (str(r["agent"]), r["server"]))
    doc = load(path)
    changes, pinned = [], 0
    for key, entry in sorted(doc["servers"].items()):
        if not isinstance(entry, dict):
            continue
        pinned += len(entry.get("tools") or {})
        agent, _, name = key.partition(":")
        changes += [{"server": name, "agent": agent, **row} for row in changes_of(entry)]
    scanners = remote_scanners(base)
    degraded += scanners.pop("degraded", [])
    return {"risky": risky, "changes": changes, "pins": pinned, "pinned_servers": len(doc["servers"]),
            "scanners": scanners, "rules": dict(RULES), "degraded": degraded}
