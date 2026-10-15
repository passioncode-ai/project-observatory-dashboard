#!/usr/bin/env python3
"""The agents installed on this machine, one row per Fabric service, generated.

    scan_agents.py store/raw/agents.json        # the tick's collector: descriptors + launchd

WHY. The estate map says the engine generates the list of agents on a machine, and until
OBS-83 nothing did: the wiki's `inventory/` had no `agents.json` and no code named one. The
list a person or an agent wants — which agents exist, where each answers, which contract it
pins, who supervises it, whether it is running, which MCP declarations reach it — was spread
over three places this engine already reads for other reasons: the Fabric service descriptors
(`fabric_service.services_dir()`), the MCP inventory (`store/raw/mcp.json`, written by
`collectors/scan_mcp.py`), and launchd's own map.

TWO HALVES, because the tick runs its collectors in rotation (`tools/tick.py`, `Rotation`) and
so cannot promise this one runs after `scan-mcp`:

* `collect()` — this collector, run by the tick — reads the descriptors, each one's Fabric
  manifest for the contract pins, and launchd, and writes `store/raw/agents.json`;
* `document()` — called by `collectors/emit_registry.py`, after every collector — joins the MCP
  inventory, carries first-seen and last-running dates from the previous registry document and
  writes `registry/agents.json`. `tools/project_into_vault.py` mirrors every `registry/*.json`,
  so the wiki gets `inventory/agents.json` with no code of its own.

A ROW IS NEVER INFERRED FROM A NAME. Rows come from descriptors only. The MCP inventory and
launchd are joined to a row by a key the descriptor states — the origin's host and port, the
launchd label — so an MCP server or a launchd job no descriptor claims stays unattributed and is
counted, never guessed onto an agent whose name looks similar. A port two descriptors claim is
ambiguous: its declarations are attributed to neither, counted, and named in `degraded`.

WHAT IS NEVER WRITTEN. Nothing of `auth`: not the token, not where the token file lives — the
location is not a fact about the agent and the file is a credential. Every string the document
carries passes ONE sanitiser (`_text`): any URL in it — with `//`, one slash, a backslash or none,
since a browser finds the user-info in each — loses its user-info, query, `;params` and fragment,
as `scan_mcp.safe_url` does for MCP targets, and the row's `key_in_url` says the origin or the
repository had one. `problems()` refuses the same token shape, so a harmless `?lang=en` in a
summary is rewritten rather than left to turn the validator red and stop the tick. The emit runs
the sanitiser again over the whole document, so a scan written by an older collector is cleaned too.
Values a descriptor quotes back into a problem are dropped, not echoed. Commands are listed by
name (`doctor`, `update`), never their argument vectors. A path under the home directory is
written `~/…` wherever it occurs in a string (either separator, so a Windows home too). Every string is bounded and encodable
as UTF-8. No process id and no instant: the document changes when the estate changes, not on
every tick, so the registry is not committed every thirty minutes for nothing.

WHAT IS NEVER TRUSTED. A descriptor or manifest is opened without blocking, must be a regular
file, and is read up to a size cap; a link pointing outside the services directory is not
followed; a file name that is not UTF-8, JSON nested past the parser's depth, or any other
surprise in one descriptor degrades that descriptor and never the scan.

EVERY SOURCE IS ACCOUNTED FOR (`sources`, `degraded`):

* the services directory `read`, `absent` (no Fabric here: an empty machine, not a partial
  scan) or `unreadable` (degraded; the previous rows are kept, each marked `carried` with
  `carried_since`, and none counted in the supervisor totals, because an empty list in place of
  a partial one would be a false claim and a carried "running" is not a measurement);
* a descriptor that will not parse, or is not an object, is named in `degraded` by file and the
  kind of error, never its message; one that parses but breaks the contract is LISTED with
  `valid: false` and its problems (`fabric_service.validate_descriptor`), since an agent with a
  broken descriptor still exists;
* a manifest that cannot be read: `manifest.state: unreadable` and degraded; none declared:
  `absent`, not degraded;
* launchd `read`, `failed` (degraded: `launchctl list` did not answer on macOS),
  `not-applicable` (another platform), `skipped` (`OBSERVATORY_AGENTS_LAUNCHD=skip`) or
  `not-needed` (no descriptor names launchd). systemd and Task Scheduler are not read yet: their
  rows say `not-measured` and why;
* no MCP inventory: `mcp_declarations: null` per row (unmeasured, not "none") and degraded.

DATES ARE THE SCAN'S. `first_seen_on` and `last_seen_running_on` are the day the collector
looked, never the day the emit ran. The tick runs this collector under `features.machine_watch`,
the switch of the lifecycle watch that reads the same descriptors; with it off, or with the step
starved by the rotation, the emit keeps re-reading the last scan. A scan older than
`STALE_SECONDS`, or dated after the emit (`clock-skew`), makes the document `stale: true` with the
scan's date in `degraded`, and since it
dates nothing after the scan, re-emitting the same old scan changes nothing.

It starts nothing and signals nothing: `launchctl list` is the only command, run once.
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCHEMA_VERSION = 1
#: The source this document is evidence of (`registry/sources.json`, declared by the emit).
SOURCE_ID = "SRC-0020"
#: The MCP inventory's own source, cited when its declarations were joined.
MCP_SOURCE_ID = "SRC-0017"
LAUNCHD_ENV = "OBSERVATORY_AGENTS_LAUNCHD"
SUPERVISOR_STATES = ("running", "loaded", "not-loaded", "not-measured", "platform", "unsupervised")
MANIFEST_STATES = ("pinned", "absent", "unreadable")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
#: A scan older than this is stale: the tick's interval (1800 s) plus its own ceiling (1500 s),
#: so a collector that ran in the previous tick is still fresh at the end of this one.
STALE_SECONDS = 1800 + 1500
#: A scan dated this far after the emit came from a clock that is wrong; it is not trusted as fresh.
CLOCK_SKEW_SECONDS = 300
#: Read caps. A real descriptor is about 1 KB and a real manifest tens of KB.
MAX_DESCRIPTOR_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_PROBLEMS = 20
#: String bounds, the descriptor schema's own where it has one.
LIMITS = {"name": 80, "summary": 200, "default": 300, "problem": 240, "file": 160}
_ID = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
_INSTANCE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
#: `agent:<id>.<instance>`, or `agent:file:<name>` for a descriptor whose own id is unusable.
AGENT_ID = re.compile(r"^agent:([a-z][a-z0-9-]{1,62}\.[a-z][a-z0-9-]{0,31}|file:[A-Za-z0-9._-]{1,160})$")
#: A URL-shaped token: a scheme of two or more characters (so `C:\…` is a drive, not a scheme),
#: then anything up to whitespace or a quote.
_TOKEN = re.compile(r"(?<![A-Za-z0-9+.-])([A-Za-z][A-Za-z0-9+.-]+):([^\s'\"<>]*)")
#: After the scheme: an authority (`//` with either slash), or user-info with no slashes at all —
#: `https:u:S@h/`, `http:/u:S@h/` and `http:\\u:S@h/` all reach a host in a WHATWG parser.
_AUTHORITY = re.compile(r"^[\\/]{2}|^[\\/]*[^\s/\\?#@]*@")
_QUOTED = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
#: `fabric_service.validate_descriptor` names a command key unquoted; the key is the descriptor's.
_COMMAND_NAME = re.compile(r"\b(unknown command|command) \S+")


# --- small helpers -------------------------------------------------------------

def _home() -> str:
    return str(pathlib.Path.home()).rstrip("/\\")


def _home_pattern() -> re.Pattern:
    """The home directory wherever it appears: as a prefix, mid-string, or at the end."""
    return re.compile(re.escape(_home()) + r"(?=[/\\]|$|[\s'\"])")


def tilde(value):
    """A string with the home directory written `~` wherever it occurs, either separator."""
    if not isinstance(value, str) or not _home():
        return value
    return _home_pattern().sub("~", value)


def _clean(value: str) -> str:
    """Encodable as UTF-8: a lone surrogate (an undecodable file name, a `\\ud800` in JSON) escaped."""
    return value.encode("utf-8", "backslashreplace").decode("utf-8")


def _normal_url(url: str) -> str:
    """`scheme:<anything>` as `scheme://<authority>…`: backslashes become slashes and a missing or
    single slash is made double, so `urlsplit` sees the authority a browser would."""
    scheme, _, rest = url.partition(":")
    rest = rest.replace("\\", "/")
    if rest.startswith("//"):
        return f"{scheme}:{rest}"                       # already an authority; `file:///…` stays a path
    return f"{scheme}://{rest.lstrip('/')}"


def safe_url(url: str) -> tuple[str, bool]:
    """(url without user-info, query, `;params` and fragment, whether user-info or a query was
    there) — `scan_mcp.safe_url`'s rule, after `_normal_url`, with the host lowercased so an
    uppercase origin still joins."""
    if ":" in url and _AUTHORITY.match(url.partition(":")[2]):
        url = _normal_url(url)
    try:
        s = urlsplit(url)
        host = s.hostname or ""                         # `hostname` is already lowercase
    except ValueError:
        scheme = url.split(":", 1)[0] if ":" in url else ""
        return (scheme + "://…" if scheme.isalpha() else "…"), True
    port = _port(s)
    netloc = f"[{host}]" if ":" in host else host
    if port:
        netloc += f":{port}"
    keyed = bool(s.query) or s.username is not None or s.password is not None
    return urlunsplit((s.scheme.lower(), netloc, s.path.split(";", 1)[0], "", "")), keyed


def _port(parts) -> int | None:
    """The URL's port, or None when it has none or an unusable one. `urlsplit().port` raises
    ValueError above 65535 and returns 0 for `:0`; neither is a port anything answers on, and
    neither is a credential, so the port is dropped and the URL is not counted as keyed."""
    try:
        port = parts.port
    except ValueError:
        return None
    return port or None


def _url_tokens(text: str):
    """Each URL-shaped token in `text` that reaches an authority: (match, its safe form, keyed)."""
    for m in _TOKEN.finditer(text):
        if _AUTHORITY.match(m.group(2)):
            shown, keyed = safe_url(m.group(0))
            yield m, shown, keyed


def _scrub(text: str) -> str:
    """Every URL in `text` made safe; every other character kept."""
    out, last = [], 0
    for m, shown, _keyed in _url_tokens(text):
        out.append(text[last:m.start()])
        out.append(shown)
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def _text(value, limit: str = "default") -> str | None:
    """THE one sanitiser for every string this document carries: UTF-8-safe, every URL without
    user-info, query or params, the home directory written `~` wherever it occurs, then bounded.
    None for anything that is not a string."""
    if not isinstance(value, str):
        return None
    cap = LIMITS[limit]
    value = tilde(_scrub(_clean(value)))
    return value if len(value) <= cap else value[:cap - 1] + "…"


def _safe_problem(text) -> str:
    """A problem's message without the values it quoted back (or a command key it names bare)."""
    text = _QUOTED.sub("…", _clean(str(text)))
    return _text(_COMMAND_NAME.sub(lambda m: m.group(1) + " …", text), "problem")


def _sanitise(node):
    """`_text` over every string of a JSON value, keys included, never re-bounding below 300."""
    if isinstance(node, str):
        return _text(node)
    if isinstance(node, dict):
        return {_text(k): _sanitise(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_sanitise(v) for v in node]
    return node


def _expand(value: str) -> pathlib.Path:
    return pathlib.Path(os.path.expanduser(value))


def _is_file(path: pathlib.Path) -> bool:
    try:
        return path.is_file()
    except (OSError, ValueError):
        return False


def _day(scanned_at) -> str | None:
    return scanned_at[:10] if isinstance(scanned_at, str) and len(scanned_at) >= 10 else None


def _instant(scanned_at) -> datetime | None:
    try:
        return datetime.strptime(scanned_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


class Refused(Exception):
    """A file this scan will not read, with the reason a person reads."""


def read_json(path: pathlib.Path, cap: int):
    """JSON from a regular file, opened without blocking and read up to `cap` bytes."""
    import osprivacy                     # the engine's one door for open flags (binary on Windows)
    try:
        fd = osprivacy.open(path, os.O_RDONLY | osprivacy.NONBLOCK)
    except ValueError as exc:                            # an embedded NUL in a declared path
        raise Refused("not a usable path") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refused("not a regular file")
        if info.st_size > cap:
            raise Refused(f"larger than {cap} bytes")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(cap + 1)
    finally:
        os.close(fd)
    if len(data) > cap:
        raise Refused(f"larger than {cap} bytes")
    try:
        return json.loads(data.decode("utf-8"))
    except RecursionError as exc:
        raise Refused("nested deeper than the parser reads") from exc


def _why(exc: BaseException) -> str:
    return str(exc) if isinstance(exc, Refused) else type(exc).__name__


# --- launchd -------------------------------------------------------------------

def parse_launchctl(out: str) -> dict[str, dict]:
    """`launchctl list` (PID, Status, Label, tab-separated) as {label: {pid, status}}."""
    jobs: dict[str, dict] = {}
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) != 3 or cols[0] == "PID":
            continue
        pid, status, label = cols
        try:
            jobs[label] = {"pid": int(pid) if pid.strip().isdigit() else None,
                           "status": int(status) if status.strip().lstrip("-").isdigit() else None}
        except ValueError:
            continue
    return jobs


def read_launchd() -> dict[str, dict] | None:
    """launchd's map, or None when `launchctl list` did not answer."""
    try:
        p = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=15,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_launchctl(p.stdout) if p.returncode == 0 else None


# --- one descriptor --------------------------------------------------------------

def _origin(origin) -> tuple[str | None, str | None, int | None, bool]:
    """(safe origin, lowercase host, port, key_in_url). A port outside 1..65535 is None."""
    if not isinstance(origin, str):
        return None, None, None, False
    shown, keyed = safe_url(origin)
    try:
        parts = urlsplit(_normal_url(origin) if ":" in origin else origin)
    except ValueError:
        return _text(shown), None, None, keyed
    return _text(shown), _text(parts.hostname or None), _port(parts), keyed


def _manifest(declared, degraded: list, agent_id: str) -> dict:
    if not isinstance(declared, str) or not declared:
        return {"state": "absent"}
    shown = _text(tilde(declared))
    try:
        doc = read_json(_expand(declared), MAX_MANIFEST_BYTES)
        if not isinstance(doc, dict):
            raise Refused("not a JSON object")
    except (OSError, ValueError, Refused) as exc:
        degraded.append({"source": "fabric-manifest", "agent": agent_id,
                         "reason": _text(f"{shown}: {_why(exc)} — the contract pins of "
                                         f"{agent_id} were not read", "problem")})
        return {"state": "unreadable", "path": shown}
    provider = doc.get("provider") if isinstance(doc.get("provider"), dict) else {}
    revision = provider.get("revision")
    supported = provider.get("supportedContractVersions")
    pins = {"state": "pinned", "path": shown,
            "contract_version": _text(doc.get("contractVersion")),
            "provider_id": _text(provider.get("id")),
            "provider_revision": revision if isinstance(revision, int) and not isinstance(revision, bool) else None,
            "content_hash": _text(provider.get("contentHash")),
            "supported_contract_versions": ([_text(v) for v in supported[:16] if isinstance(v, str)]
                                            if isinstance(supported, list) else None)}
    return {k: v for k, v in pins.items() if v is not None}


def _supervisor(life, placement: str, launchd: dict | None, launchd_state: str) -> dict:
    life = life if isinstance(life, dict) else {}
    manager = _text(life.get("manager"))
    sup: dict = {"manager": manager}
    for key in ("label", "unit", "task"):
        if isinstance(life.get(key), str):
            sup[key] = _text(life[key])
    if isinstance(life.get("plist"), str):
        sup["plist"] = _text(tilde(life["plist"]))
        sup["plist_present"] = _is_file(_expand(life["plist"]))
    if manager == "launchd":
        if launchd is None:
            sup.update(state="not-measured", why=f"launchd {launchd_state}")
        elif life.get("label") in launchd:
            job = launchd[life["label"]]
            sup.update(state="running" if job.get("pid") else "loaded")
            if isinstance(job.get("status"), int):
                sup["last_exit_status"] = job["status"]
        else:
            sup.update(state="not-loaded")
    elif manager == "none":
        sup["state"] = "platform" if placement == "remote" else "unsupervised"
    else:
        sup.update(state="not-measured",
                   why=f"{manager or 'an unknown manager'} is not read by this scan; launchd is")
    return sup


def _file_id(name: str) -> str:
    return "agent:file:" + (re.sub(r"[^A-Za-z0-9._-]+", "-", pathlib.Path(name).stem)[:160] or "unnamed")


def _row(path: pathlib.Path, doc: dict, launchd: dict | None, launchd_state: str,
         degraded: list) -> dict:
    import fabric_service
    service_id = doc.get("id") if isinstance(doc.get("id"), str) and _ID.match(doc["id"]) else None
    instance = (doc.get("instance") if isinstance(doc.get("instance"), str) and _INSTANCE.match(doc["instance"])
                else None)
    agent_id = f"agent:{service_id}.{instance}" if service_id and instance else _file_id(path.name)
    try:
        raw = fabric_service.validate_descriptor(doc)
    except Exception as exc:                                        # noqa: BLE001 — any shape
        raw = [f"the descriptor could not be checked: {type(exc).__name__}"]
    problems = [_safe_problem(p) for p in raw[:MAX_PROBLEMS]]
    if len(raw) > MAX_PROBLEMS:
        problems.append(f"and {len(raw) - MAX_PROBLEMS} more")
    placement = "remote" if doc.get("placement") == "remote" else "local"
    origin, host, port, origin_keyed = _origin(doc.get("origin"))
    source = doc.get("source") if isinstance(doc.get("source"), dict) else {}
    repository, repo_keyed = (safe_url(source["repository"]) if isinstance(source.get("repository"), str)
                              else (None, False))
    commands = doc.get("commands") if isinstance(doc.get("commands"), dict) else {}
    return {"id": agent_id, "service_id": service_id, "instance": instance,
            "name": _text(doc.get("name"), "name"),
            "summary": _text(doc.get("summary"), "summary"),
            "placement": placement, "origin": origin, "host": host, "port": port,
            "protocol": _text(doc.get("protocol")),
            "descriptor": _text(path.name, "file"),
            "installed_at": _text(doc.get("installedAt")),
            "source_repository": _text(repository),
            "key_in_url": bool(origin_keyed or repo_keyed),
            "commands": sorted(k for k in commands if k in ("doctor", "update")),
            "supervisor": _supervisor(doc.get("lifecycle"), placement, launchd, launchd_state),
            "manifest": _manifest(doc.get("fabricManifest"), degraded, agent_id),
            "valid": not raw, "problems": problems}


# --- the collector ----------------------------------------------------------------

def _descriptor_files(directory: pathlib.Path, degraded: list) -> list[pathlib.Path]:
    """The `*.json` entries to read: each name UTF-8, each link resolving inside the directory."""
    root = directory.resolve()
    out = []
    for path in sorted(directory.iterdir()):
        if path.suffix != ".json":
            continue
        try:
            path.name.encode("utf-8")
        except UnicodeEncodeError:
            degraded.append({"source": "fabric-services",
                             "reason": _text(f"{_clean(path.name)}: a file name that is not UTF-8 — "
                                             f"not read", "problem")})
            continue
        if path.is_symlink():
            try:
                target = path.resolve(strict=True)
            except (OSError, RuntimeError):
                target = None
            if target is None or root not in target.parents:
                degraded.append({"source": "fabric-services",
                                 "reason": _text(f"{path.name}: a link that does not resolve inside the "
                                                 f"services directory — not followed", "problem")})
                continue
        out.append(path)
    return out


def collect(directory: pathlib.Path, *, launchd: dict | None = None, platform: str | None = None,
            launchd_failed: bool = False, now: datetime | None = None,
            read_launchd_now: bool = False) -> dict:
    """The scan: every descriptor in `directory`, its manifest, and launchd's view of it.

    `launchd` is the label map when the caller already has it (tests inject one); with
    `read_launchd_now` the collector asks `launchctl` itself, once, and only when a descriptor
    names launchd."""
    now = now or datetime.now(timezone.utc)
    platform = platform or sys.platform
    degraded: list[dict] = []
    files: list[pathlib.Path] = []
    if not directory.exists():
        dir_state = "absent"
    else:
        try:
            files = _descriptor_files(directory, degraded)
            dir_state = "read"
        except OSError as exc:
            dir_state = "unreadable"
            degraded.append({"source": "fabric-services",
                             "reason": f"the services directory could not be listed: "
                                       f"{type(exc).__name__}; the previous rows are kept"})
    docs: list[tuple[pathlib.Path, dict]] = []
    for path in files:
        try:
            doc = read_json(path, MAX_DESCRIPTOR_BYTES)
            if not isinstance(doc, dict):
                raise Refused("not a JSON object")
        except (OSError, ValueError, Refused) as exc:
            degraded.append({"source": "fabric-services",
                             "reason": _text(f"{path.name}: {_why(exc)} — this descriptor's agent is "
                                             f"not listed", "problem")})
            continue
        docs.append((path, doc))

    wants_launchd = any(isinstance(d.get("lifecycle"), dict) and d["lifecycle"].get("manager") == "launchd"
                        for _p, d in docs)
    if platform != "darwin":
        launchd_state, launchd = "not-applicable", None
    elif os.environ.get(LAUNCHD_ENV) == "skip":
        launchd_state, launchd = "skipped", None
    elif not wants_launchd:
        launchd_state, launchd = "not-needed", None
    else:
        if launchd is None and read_launchd_now and not launchd_failed:
            launchd = read_launchd()
            launchd_failed = launchd is None
        if launchd is None and launchd_failed:
            launchd_state = "failed"
            degraded.append({"source": "launchd",
                             "reason": "`launchctl list` did not answer; whether each launchd-supervised "
                                       "agent is loaded or running was not measured"})
        elif launchd is None:
            launchd_state = "not-measured"
        else:
            launchd_state = "read"

    rows: list[dict] = []
    seen: set[str] = set()
    for path, doc in docs:
        try:
            row = _row(path, doc, launchd, launchd_state, degraded)
        except Exception as exc:                                    # noqa: BLE001 — one descriptor
            degraded.append({"source": "fabric-services",
                             "reason": _text(f"{path.name}: {type(exc).__name__} while reading it — "
                                             f"this descriptor's agent is not listed", "problem")})
            continue
        if row["id"] in seen:
            degraded.append({"source": "fabric-services",
                             "reason": _text(f"{path.name}: claims {row['id']}, which an earlier "
                                             f"descriptor already claims — not listed twice", "problem")})
            continue
        seen.add(row["id"])
        rows.append(row)
    return {"schema_version": SCHEMA_VERSION,
            "scanned_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sources": {"fabric_services": {"state": dir_state, "directory": _text(tilde(_clean(str(directory)))),
                                            "descriptors": len(files)},
                        "launchd": {"state": launchd_state}},
            "services": rows, "degraded": degraded}


# --- the registry document (called by the emit) -----------------------------------------

def _mcp_key(target) -> tuple[str, int] | None:
    """(host, port) an HTTP(S) declaration reaches; None for anything else (a command basename)."""
    if not isinstance(target, str):
        return None
    try:
        parts = urlsplit(target)
    except ValueError:
        return None
    port = _port(parts)
    host = parts.hostname or ""                         # already lowercase
    if not host or parts.scheme.lower() not in ("http", "https"):
        return None
    port = port or (443 if parts.scheme.lower() == "https" else 80)
    return ("loopback" if host in LOOPBACK else host), port


def _agent_key(row: dict) -> tuple[str, int] | None:
    if not row.get("host"):
        return None
    if row.get("placement") == "remote":
        return row["host"], row.get("port") or 443
    return ("loopback", row["port"]) if row.get("port") else None


def _join_mcp(rows: list[dict], mcp_scan: dict | None, degraded: list) -> dict | None:
    """Each row's MCP declarations, by the origin both name. None when there is no inventory."""
    if mcp_scan is None:
        for row in rows:
            row["mcp_declarations"] = None
        return None
    import estate_surfaces
    by_key: dict[tuple, list[dict]] = {}
    for r in rows:
        key = _agent_key(r)
        if key:
            by_key.setdefault(key, []).append(r)
    for row in rows:
        row["mcp_declarations"] = []
    counts = {"attributed": 0, "ambiguous": 0, "unattributed_http": 0, "stdio": 0, "other": 0}
    ambiguous_keys: set[tuple] = set()
    for decl in estate_surfaces.mcp_rows(mcp_scan):
        if decl.get("transport") == "stdio":
            # What `scan_mcp` says, literally: its stdio target is the command's basename.
            counts["stdio"] += 1
            continue
        key = _mcp_key(decl.get("target"))
        if key is None:
            # Not stdio and not an HTTP(S) origin: `ws://`, a malformed target, no target at all.
            counts["other"] += 1
            continue
        owners = by_key.get(key) or []
        if len(owners) > 1:
            counts["ambiguous"] += 1
            ambiguous_keys.add(key)
        elif owners:
            owners[0]["mcp_declarations"].append(decl["id"])
            counts["attributed"] += 1
        else:
            counts["unattributed_http"] += 1
    for key in sorted(ambiguous_keys):
        degraded.append({"source": "mcp-inventory",
                         "reason": _text(f"{', '.join(sorted(r['id'] for r in by_key[key]))} answer at the "
                                         f"same origin, so the MCP declarations reaching it are attributed "
                                         f"to neither", "problem")})
    for row in rows:
        row["mcp_declarations"].sort()
    return counts


def document(scan: dict, mcp_scan: dict | None, previous: dict | None, obs_date: str,
             now: datetime | None = None) -> dict:
    """`registry/agents.json` from the collector's scan, the MCP inventory and the last document."""
    now = now or datetime.now(timezone.utc)
    degraded = [dict(d) for d in scan.get("degraded") or []]
    scan_day = _day(scan.get("scanned_at")) or obs_date
    scanned = _instant(scan.get("scanned_at"))
    skew = scanned is not None and (scanned - now).total_seconds() > CLOCK_SKEW_SECONDS
    stale = scanned is None or skew or (now - scanned).total_seconds() > STALE_SECONDS
    if skew:
        degraded.append({"source": "fabric-services",
                         "reason": _text(f"clock-skew: the agents scan is dated {scan.get('scanned_at')}, after "
                                         f"this emit; it is treated as stale", "problem")})
    elif stale:
        degraded.append({"source": "fabric-services",
                         "reason": _text(f"the agents scan is from {scan.get('scanned_at') or 'an unknown time'}, "
                                         f"older than one tick; nothing in this document is dated after it",
                                         "problem")})
    previous_rows = {r.get("id"): r for r in (previous or {}).get("agents") or [] if isinstance(r, dict)}
    dir_state = ((scan.get("sources") or {}).get("fabric_services") or {}).get("state")
    if dir_state == "unreadable":
        rows = []
        for r in previous_rows.values():
            row = copy.deepcopy(r)
            row["carried_since"] = r.get("carried_since") or scan_day
            row["carried"] = True
            rows.append(row)
        removed: list[str] = []
    else:
        rows = []
        for r in scan.get("services") or []:
            row = copy.deepcopy(r)
            row["carried"] = False
            before = previous_rows.get(row["id"]) or {}
            row["first_seen_on"] = min(filter(None, (before.get("first_seen_on"), scan_day)))
            running = (row.get("supervisor") or {}).get("state") == "running"
            row["last_seen_running_on"] = scan_day if running else before.get("last_seen_running_on")
            row["source_refs"] = [SOURCE_ID]
            rows.append(row)
        removed = sorted(set(previous_rows) - {r["id"] for r in rows})
    rows.sort(key=lambda r: r["id"])
    mcp_totals = _join_mcp(rows, mcp_scan, degraded)
    if mcp_scan is None:
        degraded.append({"source": "mcp-inventory",
                         "reason": "store/raw/mcp.json is absent, so which MCP declarations reach each "
                                   "agent was not measured; `full scan-mcp` writes it"})
    else:
        for row in rows:
            if row["mcp_declarations"]:
                row["source_refs"] = sorted(set(row.get("source_refs") or [SOURCE_ID]) | {MCP_SOURCE_ID})
    sources = copy.deepcopy(scan.get("sources") or {})
    sources["mcp_inventory"] = ({"state": "absent"} if mcp_scan is None else
                                {"state": "read", "scanned_on": _day(mcp_scan.get("scanned_at"))})
    by_state: dict[str, int] = {}
    for row in rows:
        if row.get("carried"):
            continue
        state = (row.get("supervisor") or {}).get("state") or "not-measured"
        by_state[state] = by_state.get(state, 0) + 1
    rows = _sanitise(rows)
    degraded = _sanitise(degraded)
    sources = _sanitise(sources)
    return {
        "schema_version": SCHEMA_VERSION, "updated_on": obs_date,
        "scanned_on": _day(scan.get("scanned_at")),
        "stale": stale,
        "note": ("Every agent installed on this machine as a Fabric service (fabric-service/0.1): "
                 "where it answers, the contract its manifest pins, who supervises it and whether "
                 "launchd has it running, and which MCP declarations reach it by its origin. "
                 "Generated by collectors/scan_agents.py; never edited by hand."),
        "sources": sources,
        "agents": rows,
        "removed": removed,
        "totals": {"agents": len(rows),
                   "local": sum(1 for r in rows if r.get("placement") == "local"),
                   "remote": sum(1 for r in rows if r.get("placement") == "remote"),
                   "valid": sum(1 for r in rows if r.get("valid")),
                   "invalid": sum(1 for r in rows if not r.get("valid")),
                   "carried": sum(1 for r in rows if r.get("carried")),
                   "manifests_pinned": sum(1 for r in rows if (r.get("manifest") or {}).get("state") == "pinned"),
                   "by_supervisor_state": dict(sorted(by_state.items())),
                   "mcp": mcp_totals},
        "degraded": degraded,
        "source_refs": [SOURCE_ID] + ([MCP_SOURCE_ID] if mcp_scan is not None else []),
    }


# --- the validator's rules ------------------------------------------------------------

def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def problems(doc) -> list[str]:
    """What `tools/validate_registry.py` refuses in `registry/agents.json`."""
    if not isinstance(doc, dict) or not isinstance(doc.get("agents"), list):
        return ["agents.json carries no `agents` list"]
    out: list[str] = []
    from collections import Counter
    dup = sorted(str(i) for i, n in Counter(r.get("id") for r in doc["agents"] if isinstance(r, dict)).items()
                 if n > 1)
    if dup:
        out.append(f"duplicate agent ids: {dup}")
    for row in doc["agents"]:
        if not isinstance(row, dict):
            out.append("an agent row is not an object")
            continue
        rid = row.get("id")
        if not isinstance(rid, str) or not AGENT_ID.match(rid):
            out.append(f"agent id is not agent:<id>.<instance> or agent:file:<name>: {rid}")
        if "auth" in row:
            out.append(f"{rid} carries auth: the token file is never part of the registry")
        port = row.get("port")
        if port is not None and (not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535):
            out.append(f"{rid} has a port outside 1..65535: {port}")
        if (row.get("supervisor") or {}).get("state") not in SUPERVISOR_STATES:
            out.append(f"{rid} has an unknown supervisor state: {(row.get('supervisor') or {}).get('state')}")
        manifest = row.get("manifest") or {}
        if manifest.get("state") not in MANIFEST_STATES:
            out.append(f"{rid} has an unknown manifest state: {manifest.get('state')}")
        for field, kind in (("contract_version", str), ("provider_id", str), ("content_hash", str),
                            ("provider_revision", int), ("path", str)):
            if field in manifest and (not isinstance(manifest[field], kind) or isinstance(manifest[field], bool)):
                out.append(f"{rid} manifest.{field} is not a {kind.__name__}")
    strings = list(_strings(doc))
    home = _home_pattern() if _home() else None
    if home and any(home.search(s) for s in strings):
        out.append("agents.json holds an absolute home path; paths under home are written ~/")
    if any(keyed for s in strings for _m, _shown, keyed in _url_tokens(s)):
        out.append("agents.json holds a URL with user-info or a query: a credential's shape, never kept")
    if (doc.get("totals") or {}).get("agents") != len(doc["agents"]):
        out.append("agents.json totals disagree with the list")
    if SOURCE_ID not in (doc.get("source_refs") or []):
        out.append(f"agents.json does not cite {SOURCE_ID}, the source that measures it")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    import atomic
    import fabric_service
    scan = collect(fabric_service.services_dir(), read_launchd_now=True)
    out = pathlib.Path(argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(out, scan)
    rows = scan["services"]
    print(f"agents: {len(rows)} agent(s) from {scan['sources']['fabric_services']['state']} descriptors, "
          f"{sum(1 for r in rows if not r['valid'])} invalid; launchd {scan['sources']['launchd']['state']}")
    for d in scan["degraded"]:
        print(f"  degraded {d['source']}: {d['reason'][:140]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
