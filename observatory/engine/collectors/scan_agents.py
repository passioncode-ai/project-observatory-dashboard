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
location is not a fact about the agent and the file is a credential.

URLS FAIL CLOSED; NOTHING IS HALF-CLEANED. Three rounds of review found a credential that a URL
parser missed, so no URL is parsed and repaired. `origin` and `source.repository` are kept only when
they are strictly `scheme://host[:port][/path]` (`strict_url`: no `@ ? # ; \\`, exactly two
slashes, a known scheme, a port in 1..65535); anything else is null, `key_in_url` says whether it
carried `@` or `?`, and `degraded` says "origin unreadable" without the value. Host, port and the
MCP join come only from that parse. Every other string (`_text`) is bounded, NFC-normalised, its
home directory written `~` (a whole path segment, either separator, case-insensitive on macOS and
Windows), then split on whitespace: a token that is a strict URL is kept, a token with `@`, with
`:/` or `:\\`, or with `?` or `#` after a `scheme:` is replaced WHOLE by `[url]`, and every other
token is kept. The sanitiser is a fixed point, and `problems()` applies the same rule, so it can
only fire on a document the sanitiser never touched; the emit runs it over the whole document, so
a scan written by an older collector is cleaned too. A problem's text stops at its first quote and
a command key it names is dropped. Commands are listed by name (`doctor`, `update`), never their
argument vectors. No regular expression here can backtrack over unbounded input. What this covers
is scheme URLs with user-info, a query or a fragment; a schemeless `token=…` or a percent-encoded
form is not recognised as a URL; a credential carried in a URL's PATH (a webhook or bot-token
path) is not recognised either, and a C0 or line separator can split a URL from its query; a
Windows drive path outside the home (`C:\\work\\x`) holds
`:\\` and is written `[url]`. A descriptor whose FILE NAME is URL-shaped is not read at all: its id
would carry the name. When the scan is missing or cannot be used, the emit rewrites the previous
document with `stale: true` and one degraded line naming the error (`unusable`).

ONE INVARIANT AT THE EXIT. `document()` never returns a document `problems()` refuses: it rebuilds
every row from the schema's keys (`SCAN_ROW_KEYS`, `ROW_KEYS`), raises on a scan row without a
valid agent id (a partial scan is not a smaller estate), and checks the finished document with
`problems()` before returning it; a refusal raises, and the emit takes `unusable`. No process id and no instant: the document changes when the estate changes, not on
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
import functools
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import hashlib
import string
import unicodedata
import urllib.parse
from datetime import datetime, timezone

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
LOOPBACK = {"127.0.0.1", "localhost", "[::1]"}
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
#: The keys a row of the registry document may carry (`schemas/agents.schema.json`, `$defs.agent`),
#: and those the collector itself writes. A row is REBUILT from these, never copied whole, so a key
#: no one declared (an `auth` block among them) cannot travel through.
SCAN_ROW_KEYS = ("id", "service_id", "instance", "name", "summary", "placement", "origin", "host", "port",
                 "protocol", "descriptor", "installed_at", "source_repository", "key_in_url", "commands",
                 "supervisor", "manifest", "valid", "problems")
ROW_KEYS = SCAN_ROW_KEYS + ("carried", "carried_since", "first_seen_on", "last_seen_running_on",
                            "mcp_declarations", "source_refs")
_SUPERVISOR_KEYS = {"manager", "label", "unit", "task", "plist", "plist_present", "state", "last_exit_status", "why"}
_MANIFEST_KEYS = {"state", "path", "contract_version", "provider_id", "provider_revision", "content_hash",
                  "supported_contract_versions"}
#: `agent:<id>.<instance>`, or `agent:file:<name>` for a descriptor whose own id is unusable.
AGENT_ID = re.compile(r"^agent:([a-z][a-z0-9-]{1,62}\.[a-z][a-z0-9-]{0,31}|file:[A-Za-z0-9._-]{1,160})$")
#: THE ONE URL SHAPE THIS DOCUMENT KEEPS: `scheme://host[:port][/path]` and nothing else — no
#: user-info, query, fragment, `;params`, backslash, or any count of slashes but two. Each class
#: excludes the character that ends it, so `fullmatch` never backtracks.
_STRICT_URL = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9][A-Za-z0-9.-]*)"
                         r"(?::([0-9]{1,5}))?(/[^\s@?#;\\]*)?")
ORIGIN_SCHEMES = frozenset({"http", "https", "ws", "wss"})
REPOSITORY_SCHEMES = frozenset({"http", "https", "ssh", "git"})
TEXT_SCHEMES = ORIGIN_SCHEMES | REPOSITORY_SCHEMES
_SCHEME_CHARS = frozenset(string.ascii_letters + string.digits + "+.-")
_WHITESPACE = re.compile(r"(\s+)")
#: A string holding none of these cannot carry a URL-shaped token (`_fix`'s fast path).
_URL_CHARS = re.compile(r"[:@?#\\]")
#: What replaces a URL-shaped token that is not a strict URL: the whole token, never a part of it.
URL_MARK = "[url]"


class StrictURL:
    __slots__ = ("scheme", "host", "port", "path")

    def __init__(self, scheme: str, host: str, port: int | None, path: str):
        self.scheme, self.host, self.port, self.path = scheme, host, port, path

    @property
    def text(self) -> str:
        return f"{self.scheme}://{self.host}" + (f":{self.port}" if self.port else "") + self.path


# --- small helpers -------------------------------------------------------------

def _home() -> str:
    return unicodedata.normalize("NFC", str(pathlib.Path.home())).rstrip("/\\")


#: Characters of a name: the home directory followed by one of these is a longer name, not home.
_NAME_CHARS = frozenset(string.ascii_letters + string.digits + "_.-")
#: The macOS data-volume firmlink: `/System/Volumes/Data/Users/x` is `/Users/x`.
_FIRMLINK = "/system/volumes/data"


def _case_insensitive(platform: str) -> bool:
    return platform in ("darwin", "win32")


@functools.lru_cache(maxsize=8)
def _home_regex(home: str, platform: str) -> re.Pattern:
    """The home directory as a literal (either separator), not followed by a name character."""
    body = r"[/\\]".join(re.escape(part) for part in re.split(r"[/\\]", home))
    return re.compile(body + r"(?![A-Za-z0-9_.-])", re.IGNORECASE if _case_insensitive(platform) else 0)


def _home_pattern() -> re.Pattern | None:
    home = _home()
    return _home_regex(home, sys.platform) if home else None


def _home_spans(text: str) -> list[tuple[int, int]]:
    """Each place the home directory is written as a path in `text`, as (start, end) to replace by `~`.

    The span takes in what a path can carry before the home without leaving it: a run of separators
    and `.` or `..` segments (`//HOME`, `/./HOME`, `/../HOME`) and, on macOS, the data-volume
    firmlink (`/System/Volumes/Data/HOME`). Before the span there must be the start of the text or
    a character that is not a letter, digit, `~` or `:`: `_HOME`, `-HOME` and any other punctuation
    are boundaries; `x/srv/alpha/y` is another path (which `home_leaks` still refuses); after `:`
    the path is inside a URL (`file:///HOME`), which the token rule replaces whole. After it,
    anything but a name character. Linear: each walk to the left stops at the previous span."""
    home = _home()
    if not home:
        return []
    spans: list[tuple[int, int]] = []
    floor = 0
    for m in _home_regex(home, sys.platform).finditer(text):
        j = m.start()
        while True:
            while j > floor and text[j - 1] in "/\\.":
                j -= 1
            head = text[max(floor, j - len(_FIRMLINK)):j].replace("\\", "/")
            if sys.platform == "darwin" and len(head) == len(_FIRMLINK) and head.lower() == _FIRMLINK:
                j -= len(_FIRMLINK)
                continue
            break
        if j > 0 and (text[j - 1].isalnum() or text[j - 1] in "~:"):
            # After a letter, a digit or `~` it is another path; after `:` it is inside a URL
            # (`file:///HOME`), which the token rule replaces whole.
            continue
        spans.append((j, m.end()))
        floor = m.end()
    return spans


def tilde(value):
    """A string with each home path `_home_spans` finds written `~`."""
    if not isinstance(value, str):
        return value
    out, last = [], 0
    for a, b in _home_spans(value):
        out.append(value[last:a])
        out.append("~")
        last = b
    out.append(value[last:])
    return "".join(out)


#: A JSON `\uXXXX` escape, decoded by `_lexical` before the separators are read.
_JSON_U = re.compile(r"\\u([0-9A-Fa-f]{4})")
#: How many rounds of percent-decoding `_lexical` applies: `%252F` is two, three bounds the work.
_PCT_ROUNDS = 3


def _segments(text: str, resolve: bool) -> str:
    """`text` split on `/`, empty and `.` segments dropped, `seg/..` resolved when `resolve`.
    One pass over the segments with a stack: linear, whatever the nesting."""
    lead, parts = text.startswith("/"), text.split("/")
    out: list[str] = []
    for seg in parts:
        if seg in ("", "."):
            continue
        if resolve and seg == "..":
            if out and out[-1] != "..":
                out.pop()
            elif not lead:
                out.append(seg)
            continue                            # `/..` at the root is the root
        out.append(seg)
    tail = "/" if text.endswith("/") and out else ""
    return ("/" if lead else "") + "/".join(out) + tail


def _lexical(text: str, fold: bool) -> list[str]:
    """The forms of `text` `home_leaks` searches, each a lexical normalisation that a path
    resolver, a JSON reader or a URL decoder would apply before it reached the file system:
    NFC; then NFKC (fullwidth `／` and `～`), JSON `\\uXXXX` and `\\/` escapes, bounded
    percent-decoding, backslashes as separators and separator runs collapsed; then `.` segments
    dropped; then `seg/..` resolved. `…`, the mark of a cut string, stays a boundary. Every stage is searched, so a home written plainly and then
    left by `..` (`HOME/../x`) is still found. Case-folded where the file system folds case."""
    base = unicodedata.normalize("NFC", text)
    # `…` ends a cut string (`_text`) and NFKC would spell it `...`, three name characters, so a
    # home cut right after its last part would stop matching: it stays a boundary instead.
    t = unicodedata.normalize("NFKC", base.replace("\u2026", "\n"))
    t = _JSON_U.sub(lambda m: chr(int(m.group(1), 16)), t)
    for _ in range(_PCT_ROUNDS):
        if "%" not in t:
            break
        decoded = urllib.parse.unquote(t, errors="replace")
        if decoded == t:
            break
        t = unicodedata.normalize("NFKC", decoded)
    t = re.sub(r"/{2,}", "/", t.replace("\\", "/"))
    forms = [base.replace("\\", "/"), t, _segments(t, resolve=False), _segments(t, resolve=True)]
    return [f.lower() if fold else f for f in forms]


@functools.lru_cache(maxsize=8)
def _home_needles(home: str, fold: bool) -> tuple[str, ...]:
    """What `home_leaks` looks for: home in its resolved lexical form, and `~<user>`."""
    needle = _lexical(home, fold)[-1].rstrip("/")
    if not needle:
        return ()
    user = needle.rsplit("/", 1)[-1]
    return (needle,) + (("~" + user,) if user and not user.endswith(":") else ())


def home_leaks(value: str) -> bool:
    """THE INDEPENDENT CHECK `problems()` runs over every string: the home directory anywhere,
    followed by the end, a separator or any character that is not a name character, whatever
    precedes it, in any lexical form `_lexical` folds back to it (separator runs, `.` segments,
    `x/..` detours, backslashes, JSON escapes, percent-encoding, fullwidth forms; case-folded on
    macOS and Windows), and `~<user>` naming the same home. Stricter than `tilde`, which rewrites
    only the plain forms; a string this finds makes the document refused, so a home form the
    sanitiser does not rewrite turns the scan stale instead of reaching the registry."""
    home = _home()
    if not home:
        return False
    needles = _home_needles(home, _case_insensitive(sys.platform))
    if not needles:
        return False
    # Every form above needs a separator, `\\`, `%`, `~` or a non-ASCII character: a string with
    # none of them (an id, a state, a date) cannot spell home, and most strings are such.
    if value.isascii() and "/" in needles[0] and not any(c in value for c in "/\\%~"):
        return False
    for hay in dict.fromkeys(_lexical(value, _case_insensitive(sys.platform))):
        for n in needles:
            i = hay.find(n)
            while i >= 0:
                after = i + len(n)
                if after == len(hay) or hay[after] not in _NAME_CHARS:
                    return True
                i = hay.find(n, i + 1)
    return False


def _clean(value: str) -> str:
    """Encodable as UTF-8: a lone surrogate (an undecodable file name, a `\\ud800` in JSON) escaped."""
    return value.encode("utf-8", "backslashreplace").decode("utf-8")


def strict_url(value, schemes=TEXT_SCHEMES) -> StrictURL | None:
    """The strict parse, or None. Refused whole: any of `@ ? # ; \\`, a scheme outside `schemes`,
    a count of slashes other than two, anything after the host but `:port` and a `/path`, and a
    port outside 1..65535. The scheme and host come back lowercase."""
    if not isinstance(value, str):
        return None
    m = _STRICT_URL.fullmatch(value)            # its classes exclude `@ ? # ; \\` and whitespace
    if not m or m.group(1).lower() not in schemes:
        return None
    port = int(m.group(3)) if m.group(3) else None
    if port is not None and not 1 <= port <= 65535:
        return None
    return StrictURL(m.group(1).lower(), m.group(2).lower(), port, m.group(4) or "")


def _suspect(token: str) -> bool:
    """A token that is URL-shaped but not a strict URL: `@` anywhere; `:/` or `:\\`; or `?` or `#`
    after a `scheme:` (a run of `[A-Za-z0-9+.-]` holding a letter, then `:`). One linear pass."""
    if "@" in token or ":/" in token or ":\\" in token:
        return True
    letter = scheme = False
    for c in token:
        if c in "?#":
            if scheme:
                return True
            letter = False
        elif c == ":":
            scheme = scheme or letter
            letter = False
        elif c in _SCHEME_CHARS:
            letter = letter or c.isalpha()
        else:
            letter = False
    return False


def _token(token: str) -> str:
    """A whitespace-free token: kept, kept as its strict URL, or replaced whole by `[url]`."""
    url = strict_url(token)
    if url is not None:
        return url.text
    return URL_MARK if _suspect(token) else token


def _fix(value: str) -> str:
    """NFC, the home directory as `~`, then every token through `_token`. A fixed point: applying
    it to its own output changes nothing, which is what lets `problems()` use it as its rule."""
    if value.isascii() and not _URL_CHARS.search(value):
        return tilde(value)                     # no `: @ ? # \\`: no token can be URL-shaped
    value = unicodedata.normalize("NFC", _clean(value))
    value = tilde(value)
    parts = _WHITESPACE.split(value)
    return "".join(_token(part) if i % 2 == 0 else part for i, part in enumerate(parts))


def _text(value, limit: str = "default") -> str | None:
    """THE one sanitiser for every string this document carries: bounded first (`4 × limit`), then
    `_fix`, then cut to the limit and fixed again (a cut can leave half a URL, which the second pass
    replaces whole). None for anything that is not a string."""
    if not isinstance(value, str):
        return None
    cap = LIMITS[limit]
    if len(value) > 4 * cap:
        # The first bound may cut a token in half, and half a keyed token (`u:SECR` of `u:SECRET@h`)
        # no longer looks keyed: the partial last token goes, never a piece of it.
        head = value[:4 * cap]
        ws = max(head.rfind(c) for c in " \t\n\r\f\v")
        value = (head[:ws].rstrip() + " …") if ws > 0 else "…"
    value = _fix(value)
    for _ in range(8):
        if len(value) <= cap:
            return value
        value = _fix(value[:cap - 1] + "…")
    return "…"


def _safe_problem(text) -> str:
    """A problem's message without what it quotes back: the text stops at the first quote, and a
    command key `fabric_service.validate_descriptor` names bare is dropped. String operations only."""
    text = str(text)[:4 * LIMITS["problem"]]
    for prefix in ("unknown command ", "command "):
        if text.startswith(prefix):
            _key, sep, tail = text[len(prefix):].rpartition(" must ")
            text = prefix + "…" + (sep + tail if sep else "")
            break
    cuts = [i for i in (text.find("'"), text.find('"')) if i >= 0]
    if cuts:
        text = text[:min(cuts)].rstrip() + " …"
    return _text(text, "problem")


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


_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _day(scanned_at) -> str | None:
    """`YYYY-MM-DD` of a scan instant, or None when it does not start with a date."""
    day = scanned_at[:10] if isinstance(scanned_at, str) else ""
    return day if _DATE.fullmatch(day) else None


DEGRADED_SOURCES = ("fabric-services", "fabric-manifest", "launchd", "mcp-inventory")


def _degraded_entry(d: dict) -> dict:
    """A degraded line in the schema's shape, whatever an older scan wrote."""
    entry = {"source": d.get("source") if d.get("source") in DEGRADED_SOURCES else "fabric-services",
             "reason": d.get("reason") if isinstance(d.get("reason"), str) else "an unreadable degraded line"}
    if isinstance(d.get("agent"), str) and AGENT_ID.match(d["agent"]):
        entry["agent"] = d["agent"]
    return entry


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

def _strict_field(value, schemes) -> tuple[StrictURL | None, bool, bool]:
    """(the strict parse, key_in_url, refused). Refused when a value is there and is not a strict
    URL; then nothing of it is kept, and key_in_url says it carried `@` (user-info) or `?`."""
    if value is None:
        return None, False, False
    url = strict_url(unicodedata.normalize("NFC", value), schemes) if isinstance(value, str) else None
    if url is not None:
        return url, False, False
    keyed = isinstance(value, str) and any(c in value for c in "@?#")
    return None, keyed, True


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
                         "reason": _text(f"{shown}: {_why(exc)}; the contract pins of "
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
    """`agent:file:<stem>`; a stem that had to be rewritten or cut gets a short hash of the original
    name, so `my agent.json`, `my-agent.json` and `my+agent.json` stay three rows."""
    stem = pathlib.Path(name).stem
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", stem)
    if safe == stem and 0 < len(safe) <= 160:
        return "agent:file:" + safe
    digest = hashlib.sha256(name.encode("utf-8", "surrogateescape")).hexdigest()[:8]
    return "agent:file:" + (safe[:150] or "unnamed") + "-" + digest


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
    origin, origin_keyed, origin_refused = _strict_field(doc.get("origin"), ORIGIN_SCHEMES)
    if origin_refused:
        degraded.append({"source": "fabric-services", "agent": agent_id,
                         "reason": "origin unreadable; not a strict scheme://host[:port] URL; nothing of it "
                                   "is kept"})
    source = doc.get("source") if isinstance(doc.get("source"), dict) else {}
    repository, repo_keyed, repo_refused = _strict_field(source.get("repository"), REPOSITORY_SCHEMES)
    if repo_refused:
        degraded.append({"source": "fabric-services", "agent": agent_id,
                         "reason": "source repository unreadable; not a strict scheme://host[:port]/path "
                                   "URL; nothing of it is kept"})
    commands = doc.get("commands") if isinstance(doc.get("commands"), dict) else {}
    return {"id": agent_id, "service_id": service_id, "instance": instance,
            "name": _text(doc.get("name"), "name"),
            "summary": _text(doc.get("summary"), "summary"),
            "placement": placement, "origin": origin.text if origin else None,
            "host": origin.host if origin else None, "port": origin.port if origin else None,
            "protocol": _text(doc.get("protocol")),
            "descriptor": _text(path.name, "file"),
            "installed_at": _text(doc.get("installedAt")),
            "source_repository": repository.text if repository else None,
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
                             "reason": _text(f"{_clean(path.name)}: a file name that is not UTF-8; "
                                             f"not read", "problem")})
            continue
        if _fix(path.stem) != path.stem or _suspect(path.stem):
            # Its id would carry the name, and the name may carry a credential; no hash of it either.
            degraded.append({"source": "fabric-services",
                             "reason": "a file name shaped like a URL; not read"})
            continue
        if path.is_symlink():
            try:
                target = path.resolve(strict=True)
            except (OSError, RuntimeError):
                target = None
            if target is None or root not in target.parents:
                degraded.append({"source": "fabric-services",
                                 "reason": _text(f"{path.name}: a link that does not resolve inside the "
                                                 f"services directory; not followed", "problem")})
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
                             "reason": _text(f"{path.name}: {_why(exc)}; this descriptor's agent is "
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
                             "reason": _text(f"{path.name}: {type(exc).__name__} while reading it; "
                                             f"this descriptor's agent is not listed", "problem")})
            continue
        if row["id"] in seen:
            degraded.append({"source": "fabric-services",
                             "reason": _text(f"{path.name}: claims {row['id']}, which an earlier "
                                             f"descriptor already claims; not listed twice", "problem")})
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
    url = strict_url(target, {"http", "https"})
    if url is None:
        return None
    port = url.port or (443 if url.scheme == "https" else 80)
    return ("loopback" if url.host in LOOPBACK else url.host), port


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
    fabric = (scan.get("sources") or {}).get("fabric_services") if isinstance(scan, dict) and isinstance(
        scan.get("sources"), dict) else None
    if (not isinstance(scan, dict) or not isinstance(scan.get("services"), list)
            or not isinstance(fabric, dict) or fabric.get("state") not in ("read", "absent", "unreadable")):
        # A scan without its services list or its fabric_services source is not a scan of an empty
        # machine: read as one, it would list every agent as removed and reset first-seen dates.
        raise TypeError("the agents scan lacks its services list or its fabric_services source")
    now = now or datetime.now(timezone.utc)
    degraded = [_degraded_entry(d) for d in scan.get("degraded") or [] if isinstance(d, dict)]
    if previous is not None and not isinstance(previous, dict):
        degraded.append({"source": "fabric-services",
                         "reason": "the previous agents document is not a JSON object; first-seen dates "
                                   "start again from this scan"})
        previous = None
    if mcp_scan is not None and not isinstance(mcp_scan, dict):
        degraded.append({"source": "mcp-inventory", "reason": "store/raw/mcp.json is not a JSON object"})
        mcp_scan = None
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
    previous_rows = {r["id"]: r for r in (previous or {}).get("agents") or []
                     if isinstance(r, dict) and isinstance(r.get("id"), str) and AGENT_ID.match(r["id"])}
    dir_state = ((scan.get("sources") or {}).get("fabric_services") or {}).get("state")
    if dir_state == "unreadable":
        rows = []
        for r in previous_rows.values():
            row = {k: copy.deepcopy(r[k]) for k in ROW_KEYS if k in r}
            row["carried_since"] = r.get("carried_since") or scan_day
            row["carried"] = True
            rows.append(row)
        removed: list[str] = []
    else:
        rows = []
        for r in scan.get("services") or []:
            if not isinstance(r, dict) or not isinstance(r.get("id"), str) or not AGENT_ID.match(r["id"]):
                # A partial scan is not a smaller estate: dropping the row would list its agent as
                # removed and reset its first-seen date on the next good scan.
                raise ValueError("a scan row is not an object with a valid agent id")
            row = {k: copy.deepcopy(r[k]) for k in SCAN_ROW_KEYS if k in r}
            row["carried"] = False
            before = previous_rows.get(row["id"]) or {}
            row["first_seen_on"] = min(filter(None, (before.get("first_seen_on"), scan_day)))
            running = _obj(row.get("supervisor")).get("state") == "running"
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
    scan_sources = scan.get("sources") if isinstance(scan.get("sources"), dict) else {}
    sources = {k: copy.deepcopy(scan_sources[k]) for k in ("fabric_services", "launchd")
               if isinstance(scan_sources.get(k), dict)}
    sources["mcp_inventory"] = ({"state": "absent"} if mcp_scan is None else
                                {"state": "read", "scanned_on": _day(mcp_scan.get("scanned_at"))})
    by_state: dict[str, int] = {}
    for row in rows:
        if row.get("carried"):
            continue
        state = _obj(row.get("supervisor")).get("state") or "not-measured"
        by_state[state] = by_state.get(state, 0) + 1
    rows = _sanitise(rows)
    degraded = _sanitise(degraded)
    sources = _sanitise(sources)
    doc = {
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
                   "manifests_pinned": sum(1 for r in rows if _obj(r.get("manifest")).get("state") == "pinned"),
                   "by_supervisor_state": dict(sorted(by_state.items())),
                   "mcp": mcp_totals},
        "degraded": degraded,
        "source_refs": [SOURCE_ID] + ([MCP_SOURCE_ID] if mcp_scan is not None else []),
    }
    # THE EXIT INVARIANT: never a document `problems()` refuses. Whatever shape a scan arrives in,
    # a document that would turn the validator red is not returned: the emit then keeps the
    # previous rows, stale, with the reason (`unusable`).
    refused = problems(doc)
    if refused:
        raise ValueError(f"the agents document would be refused: {refused[0]}")
    return doc


UNUSABLE = "the agents scan could not be used"


def unusable(previous, why: str, obs_date: str) -> dict | None:
    """The previous document when this tick's scan is missing or broken: its rows kept, `stale`
    true, and one degraded line saying why and from when the rows are. Rewriting it the same way
    again changes nothing but `updated_on`. None when there is no usable previous document."""
    if not isinstance(previous, dict) or not isinstance(previous.get("agents"), list):
        return None
    doc = _sanitise(copy.deepcopy(previous))        # an older, dirtier document heals rather than turns validate red
    doc["updated_on"] = obs_date
    doc["stale"] = True
    lines = doc.get("degraded") if isinstance(doc.get("degraded"), list) else []
    kept = [d for d in lines if isinstance(d, dict) and not str(d.get("reason", "")).startswith(UNUSABLE)]
    day = doc.get("scanned_on") if isinstance(doc.get("scanned_on"), str) else None
    kept.append({"source": "fabric-services",
                 "reason": _text(f"{UNUSABLE} ({why}); rows are from {day or 'an unknown day'}", "problem")})
    doc["degraded"] = kept
    return doc


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


def _obj(v) -> dict:
    """`v` when it is an object, else an empty one: a reader of a hand-edited document never crashes."""
    return v if isinstance(v, dict) else {}


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _row_shape(row: dict) -> list[str]:
    """What a row carries that `schemas/agents.schema.json` would refuse: an undeclared key, a
    missing one, or a value of the wrong type. The schema's own rules, without the jsonschema
    dependency the base install does not have."""
    out = [f"carries an undeclared key {k!r}" for k in row if k not in ROW_KEYS and k != "auth"]
    out += [f"lacks {k}" for k in ROW_KEYS
            if k not in row and k not in ("summary", "installed_at", "source_repository", "carried_since")]
    text = lambda v: v is None or isinstance(v, str)                                     # noqa: E731
    for k in ("service_id", "instance", "name", "summary", "origin", "host", "protocol", "installed_at",
              "source_repository"):
        if k in row and not text(row[k]):
            out.append(f"{k} is not a string or null")
    if "placement" in row and row["placement"] not in ("local", "remote"):
        out.append("placement is not local or remote")
    if not isinstance(row.get("descriptor", ""), str):
        out.append("descriptor is not a string")
    for k in ("key_in_url", "valid", "carried"):
        if k in row and not isinstance(row[k], bool):
            out.append(f"{k} is not a boolean")
    if not (isinstance(row.get("commands", []), list) and set(row.get("commands", [])) <= {"doctor", "update"}):
        out.append("commands is not a list of doctor and update")
    if not (isinstance(row.get("problems", []), list) and all(isinstance(p, str) for p in row.get("problems", []))):
        out.append("problems is not a list of strings")
    for k in ("first_seen_on", "carried_since"):
        if k in row and not (isinstance(row[k], str) and _DATE.fullmatch(row[k])):
            out.append(f"{k} is not a date")
    if row.get("last_seen_running_on") is not None and not (
            isinstance(row["last_seen_running_on"], str) and _DATE.fullmatch(row["last_seen_running_on"])):
        out.append("last_seen_running_on is not a date or null")
    mcp = row.get("mcp_declarations")
    if mcp is not None and not (isinstance(mcp, list) and all(isinstance(m, str) and m.startswith("mcp:")
                                                              for m in mcp)):
        out.append("mcp_declarations is not null or a list of mcp: ids")
    if not (isinstance(row.get("source_refs", []), list) and all(isinstance(r, str) for r in row.get("source_refs", []))):
        out.append("source_refs is not a list of strings")
    sup = row.get("supervisor")
    if not isinstance(sup, dict):
        out.append("supervisor is not an object")
    else:
        out += [f"supervisor carries an undeclared key {k!r}" for k in sup if k not in _SUPERVISOR_KEYS]
        if "last_exit_status" in sup and not _is_int(sup["last_exit_status"]):
            out.append("supervisor.last_exit_status is not an integer")
        if "plist_present" in sup and not isinstance(sup["plist_present"], bool):
            out.append("supervisor.plist_present is not a boolean")
        for k in ("manager", "label", "unit", "task", "plist", "why"):
            if k in sup and not text(sup[k]):
                out.append(f"supervisor.{k} is not a string")
    man = row.get("manifest")
    if not isinstance(man, dict):
        out.append("manifest is not an object")
    else:
        out += [f"manifest carries an undeclared key {k!r}" for k in man if k not in _MANIFEST_KEYS]
        versions = man.get("supported_contract_versions", [])
        if not (isinstance(versions, list) and all(isinstance(v, str) for v in versions)):
            out.append("manifest.supported_contract_versions is not a list of strings")
    return out


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
        out.extend(f"{rid} {p}" for p in _row_shape(row))
        port = row.get("port")
        if port is not None and (not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535):
            out.append(f"{rid} has a port outside 1..65535: {port}")
        if _obj(row.get("supervisor")).get("state") not in SUPERVISOR_STATES:
            out.append(f"{rid} has an unknown supervisor state: {_obj(row.get('supervisor')).get('state')}")
        manifest = _obj(row.get("manifest"))
        if manifest.get("state") not in MANIFEST_STATES:
            out.append(f"{rid} has an unknown manifest state: {manifest.get('state')}")
        for field, kind in (("contract_version", str), ("provider_id", str), ("content_hash", str),
                            ("provider_revision", int), ("path", str)):
            if field in manifest and (not isinstance(manifest[field], kind) or isinstance(manifest[field], bool)):
                out.append(f"{rid} manifest.{field} is not a {kind.__name__}")
    scanned_on = doc.get("scanned_on")
    if scanned_on is not None and not (isinstance(scanned_on, str) and _DATE.fullmatch(scanned_on)):
        out.append("agents.json scanned_on is not a YYYY-MM-DD date")
    if any(not (isinstance(r, str) and AGENT_ID.match(r)) for r in doc.get("removed") or []):
        out.append("agents.json removed holds something that is not an agent id")
    if any(not isinstance(d, dict) or d.get("source") not in DEGRADED_SOURCES or not isinstance(d.get("reason"), str)
           for d in doc.get("degraded") or []):
        out.append("agents.json degraded holds a line without a known source and a text reason")
    # THE SANITISER'S OWN RULE: a string `_fix` would change is one it never touched. Sanitised
    # output is a fixed point of `_fix`, so these can only fire on a document that bypassed it.
    strings = list(_strings(doc))
    # The home directory: checked INDEPENDENTLY of the sanitiser, over every string, failing closed.
    if any(home_leaks(s) for s in strings):
        out.append("agents.json holds an absolute home path; paths under home are written ~/")
    dirty = [s for s in strings if _fix(s) != s]
    if any(_token(t) != t for s in dirty for t in _WHITESPACE.split(s)):
        out.append("agents.json holds a URL-shaped token the sanitiser replaces (user-info or a query: a "
                   "credential's shape), never kept")
    elif any(not _home_spans(s) for s in dirty):
        out.append("agents.json holds text the sanitiser would change (unnormalised Unicode or an escape)")
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
