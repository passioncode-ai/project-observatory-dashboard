#!/usr/bin/env python3
"""Declared tracking: repositories and projects a person or an agent adds by name.

WHY. Discovery finds what is on this disk (the projects folder), what a
connected GitHub account lists and what local clones point at. A repository
nobody cloned here and no connected account lists is invisible — a partner's
Bitbucket workspace without a credential, a GitLab group, a self-hosted forge,
a department's own repositories. The people who own them know they exist, and
so do the agents working for them; this is where they say so.

ONE DOOR, TWO HANDS. The MCP tools `observatory_track` / `observatory_untrack`
and the CLI `full track` / `full untrack` / `full tracked` call the same
functions below, so a declaration made by hand and one made by an agent are
validated by one rule and land in one file. Adding one's own is applied at
once. Changing a fact something already MEASURED stays a proposal
(`observatory_propose`): a declaration never overwrites a measurement, and the
functions here refuse to pretend otherwise (`already-measured`).

THE FILE, `config/tracked.json` (absent: nothing declared, and no file is
created until the first declaration):

    {"schema_version": 1,
     "repositories": [{"key", "host", "hostname", "owner", "name", "url",
                       "project"?, "organization"?, "note"?,
                       "added_by", "added_at", "updated_by"?, "updated_at"?}],
     "projects":     [{"slug", "name", "description"?, "organization"?,
                       "added_by", "added_at", "updated_by"?, "updated_at"?}],
     "measurement":  {"refresh_hours": 6, "fetch_cache_mb": 256}}

Unknown top-level keys and unknown entry fields are preserved on every write,
so a newer release's field survives an older writer. A future
`schema_version` is refused before anything is written.

NO CREDENTIAL IS EVER STORED. A URL carrying `user:password@`, a token as the
user part of an https URL, a query string or a fragment is refused, and the
refusal never repeats the value. Measurement uses the person's own git
credentials — SSH agent and keys, the credential helper — through `git`
itself (`collectors/scan_tracked.py`); Observatory holds none of them.

WHO MAY REMOVE WHAT. `untrack` removes a DECLARATION, never a measured
repository or project: those are listed by a collector and would come back on
the next scan, so removing them here would be a lie about what exists (retire
one with `repo_status.json`, or propose). Anyone who may declare may also
remove a declaration somebody else made, because removing a declaration
changes no measured fact; the removal is journalled with the actor, the
entry's owner and the entry itself (`store/tracked-journal.jsonl`), so it can
be read back and re-declared.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.parse import urlsplit

import configuration

SCHEMA_VERSION = 1
FILE = "tracked.json"
JOURNAL = "tracked-journal.jsonl"
KINDS = ("repository", "project")

#: Measurement defaults, overridable under `measurement` in the file. Six hours
#: matches the remote probe's own age gate in the tick: whether a remote moved
#: does not change minute to minute, and every probe is a network round trip.
DEFAULT_REFRESH_HOURS = 6
#: The bounded fetch cache (`features.tracked_fetch`). A depth-1, commit-only
#: fetch is a few kilobytes per repository on a forge that supports partial
#: fetch and the head commit's tree on one that does not; 256 MB holds hundreds
#: of the first kind and still bounds the second.
DEFAULT_FETCH_CACHE_MB = 256

#: Forges with a fixed two-level `owner/name` namespace, and the host word the
#: registry already uses for them. Their keys stay `owner/name` so a declared
#: repository and the same repository found later by discovery are ONE row,
#: not two.
TWO_LEVEL = {"github.com": "github", "bitbucket.org": "bitbucket"}
#: GitLab nests groups (`group/subgroup/name`), so its key carries the host.
NESTED = {"gitlab.com": "gitlab"}
MAX_SEGMENTS = 20

#: An identity a caller may claim. The SAME rule as `mcp/server.py:CALLER_ID`
#: (a test compares the two patterns), repeated rather than imported because
#: the server module builds an MCP server on import and this module is read by
#: collectors and the CLI that must not need the SDK.
CALLER_ID = re.compile(r"^(agent|service):[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
OPERATOR = "operator"

SLUG = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}$")
SEGMENT = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]{0,99}$")
HOSTNAME = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                      r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")
SSH_USER = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
#: `user@host:path`, the scp-like form git accepts for SSH.
SCP_LIKE = re.compile(r"^(?P<user>[^@/\s]+)@(?P<host>[^:/\s]+):(?P<path>[^\s]+)$")
#: Credential shapes a note or description must not carry. The same prefixes the
#: profile exporter refuses (`workspace_profile._PREFIXED`), read from there so a
#: shape added once is refused in both places.
NOTE_MAX = 500


class TrackError(ValueError):
    """A refusal with a stable code, a sentence and a remedy — a typed answer.

    Raised inside this module and turned into `{"error", "detail", "hint"}` at
    both doors, so the MCP tool and the CLI refuse in the same words."""

    def __init__(self, code: str, detail: str, hint: str = ""):
        super().__init__(detail)
        self.code, self.detail, self.hint = code, detail, hint

    def answer(self) -> dict:
        return {"error": self.code, "detail": self.detail, "hint": self.hint, "degraded": []}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _paths():
    # Imported late: `paths` resolves the workspace on import, and the URL rules
    # below are useful (and tested) without one.
    import paths
    return paths


def file_path() -> Path:
    return _paths().config_file(FILE)


def journal_path() -> Path:
    return _paths().STATE / JOURNAL


# ─────────────────────────── validation ─────────────────────────────────────

def _token_shaped(value: str) -> bool:
    import workspace_profile
    return bool(workspace_profile._PREFIXED.search(value))


def _text(value: Any, field: str, *, required: bool = False, limit: int = NOTE_MAX) -> str | None:
    """A free-text field: a single line, bounded, and never a credential."""
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise TrackError("invalid-field", f"`{field}` is required")
        return None
    if not isinstance(value, str):
        raise TrackError("invalid-field", f"`{field}` must be a string")
    value = value.strip()
    if len(value) > limit:
        raise TrackError("invalid-field", f"`{field}` is longer than {limit} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise TrackError("invalid-field", f"`{field}` must be one line of text without control characters")
    if _token_shaped(value):
        raise TrackError("credential-refused",
                         f"`{field}` holds something shaped like a credential; it was not stored",
                         "Observatory never stores a credential. Keep keys in the vault "
                         "(`tools/vault.py put`, value on stdin) and describe them by name.")
    return value


def slug_of(value: Any) -> str:
    """A project slug, with or without the `project:` prefix."""
    if not isinstance(value, str) or not value.strip():
        raise TrackError("invalid-slug", "a project slug is required",
                         "use lower-case letters, digits, dots and hyphens, e.g. `alpha-web`")
    s = value.strip()
    s = s[len("project:"):] if s.startswith("project:") else s
    if not SLUG.match(s):
        raise TrackError("invalid-slug", f"{s[:70]!r} is not a project slug",
                         "use lower-case letters, digits, dots and hyphens (at most 64), "
                         "starting with a letter or digit, e.g. `alpha-web`")
    return s


def _credential_refusal(scheme: str, host: str, path: str) -> TrackError:
    """The refusal for a URL carrying credentials — WITHOUT the credential.

    The detail names what was wrong and hands back the same address with the
    secret part removed, so the remedy is one copy-paste and the value itself
    appears in no answer, log or transcript."""
    clean = f"{scheme}://{host}{path}"
    return TrackError(
        "credential-refused",
        "the URL carries credentials (a user:password@ or token@ part, or a query "
        "string); nothing was stored and the value is not repeated here",
        f"declare {clean} instead; git authenticates with your own SSH key or "
        f"credential helper when the next tick measures it. If the value was a "
        f"real token, treat it as exposed and rotate it.")


def _segments(path: str) -> list[str]:
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [p for p in path.split("/")] if path else []
    if not parts or any(not p for p in parts):
        raise TrackError("invalid-url", "the repository path is empty or has an empty segment",
                         "give owner/name, e.g. https://github.com/example-org/alpha-web.git")
    for p in parts:
        if p in (".", "..") or not SEGMENT.match(p):
            raise TrackError("invalid-url", f"{p[:40]!r} is not a valid repository path segment",
                             "segments use letters, digits, `.`, `_` and `-`")
    return parts


def _identity(hostname: str, parts: list[str]) -> dict:
    """Key, host word, owner and name for a validated host and path."""
    if hostname in TWO_LEVEL:
        if len(parts) != 2:
            raise TrackError("invalid-url", f"{hostname} repositories are exactly owner/name; got "
                             f"{len(parts)} path segment(s)",
                             f"e.g. https://{hostname}/example-org/alpha-web.git")
        owner, name = parts
        return {"key": f"{owner}/{name}", "host": TWO_LEVEL[hostname], "hostname": hostname,
                "owner": owner, "name": name}
    if len(parts) < 2 or len(parts) > MAX_SEGMENTS:
        raise TrackError("invalid-url", f"a repository on {hostname} needs a namespace and a name "
                         f"(2 to {MAX_SEGMENTS} path segments)",
                         f"e.g. https://{hostname}/group/alpha-web.git")
    owner, name = "/".join(parts[:-1]), parts[-1]
    return {"key": f"{hostname}/{owner}/{name}", "host": NESTED.get(hostname, hostname),
            "hostname": hostname, "owner": owner, "name": name}


def _host(raw: str) -> tuple[str, int | None]:
    host, port = raw, None
    if raw.startswith("["):
        raise TrackError("invalid-url", "IPv6 literal hosts are not supported for tracking",
                         "use the forge's host name")
    if ":" in raw:
        host, _, p = raw.rpartition(":")
        if not p.isdigit() or not 0 < int(p) < 65536:
            raise TrackError("invalid-url", "the port is not a number between 1 and 65535")
        port = int(p)
    host = host.lower().rstrip(".")
    if not HOSTNAME.match(host):
        raise TrackError("invalid-url", f"{host[:60]!r} is not a host name",
                         "e.g. github.com, bitbucket.org, gitlab.com or git.example.com")
    return host, port


def parse_repository(url: Any = None, *, host: Any = None, owner: Any = None,
                     name: Any = None) -> dict:
    """Validate a repository address and return its identity and a clone URL.

    Accepts the forms git itself accepts for a remote forge: `https://host/path`
    (and `http://` for a self-hosted forge only — github.com, bitbucket.org and
    gitlab.com serve HTTPS), `ssh://[user@]host[:port]/path`, and the scp-like
    `user@host:path`. Or `host` + `owner` + `name` without a URL, which becomes
    the HTTPS address.

    Refused: credentials in the URL, a query or fragment, `file://` and local
    paths (a local clone is discovered from the projects folder, not declared),
    `git://` (unauthenticated and unencrypted), and a path the host's namespace
    rules cannot hold.
    """
    if url is not None and any(v is not None for v in (host, owner, name)):
        raise TrackError("invalid-url", "give either a URL or host + owner + name, not both")
    if url is None:
        if not all(isinstance(v, str) and v.strip() for v in (host, owner, name)):
            raise TrackError("invalid-url", "a repository needs a URL, or host + owner + name",
                             "e.g. https://github.com/example-org/alpha-web.git")
        hostname, port = _host(host.strip())
        if "@" in host or "@" in owner or "@" in name:
            raise _credential_refusal("https", hostname, "/…")
        parts = _segments(f"{owner.strip().strip('/')}/{name.strip()}")
        ident = _identity(hostname, parts)
        netloc = hostname + (f":{port}" if port else "")
        return {**ident, "url": f"https://{netloc}/{'/'.join(parts)}.git"}
    if not isinstance(url, str) or not url.strip():
        raise TrackError("invalid-url", "the URL is empty")
    raw = url.strip()
    if any(c.isspace() for c in raw):
        raise TrackError("invalid-url", "a URL cannot contain spaces")
    lowered = raw.lower()
    if lowered.startswith("file:") or raw.startswith(("/", "./", "../", "~")):
        raise TrackError("invalid-url", "a local path is not a tracked repository",
                         "a clone on this disk is discovered from the projects folder "
                         "(`full configure sources projects DIR`); declare the forge's address instead")
    if "://" in raw:
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
        if scheme == "git":
            raise TrackError("invalid-url", "git:// is unauthenticated and unencrypted",
                             "use the https:// or ssh:// address of the repository")
        if scheme not in ("https", "http", "ssh"):
            raise TrackError("invalid-url", f"the scheme {scheme[:12]!r} is not a git forge address",
                             "use https://, ssh:// or git@host:owner/name.git")
        netloc = parts.netloc
        userinfo, _, hostport = netloc.rpartition("@")
        hostname, port = _host(hostport)
        if parts.query or parts.fragment or "?" in raw or "#" in raw:
            raise _credential_refusal(scheme, hostport.lower(), parts.path)
        if userinfo:
            # https: ANY user part is refused. `https://<token>@host/…` is how a
            # token rides in a URL, and a bare user name there buys nothing a
            # credential helper does not already do. ssh: a user is normal
            # (`git@`), a password never is.
            if scheme in ("https", "http") or ":" in userinfo or not SSH_USER.match(userinfo):
                raise _credential_refusal(scheme, hostport.lower(), parts.path)
        if scheme == "http" and (hostname in TWO_LEVEL or hostname in NESTED):
            raise TrackError("invalid-url", f"{hostname} serves HTTPS; http:// would send traffic in clear",
                             f"use https://{hostname}{parts.path}")
        ident = _identity(hostname, _segments(parts.path))
        clean_netloc = (f"{userinfo}@" if userinfo else "") + hostname + (f":{port}" if port else "")
        path = parts.path.rstrip("/")
        return {**ident, "url": f"{scheme}://{clean_netloc}{path}"}
    m = SCP_LIKE.match(raw)
    if not m:
        raise TrackError("invalid-url", "not a repository address git understands",
                         "use https://host/owner/name.git, ssh://git@host/owner/name.git "
                         "or git@host:owner/name.git")
    user = m.group("user")
    hostname, _ = _host(m.group("host"))
    if ":" in user or not SSH_USER.match(user):
        raise _credential_refusal("ssh", hostname, "/" + m.group("path"))
    ident = _identity(hostname, _segments(m.group("path")))
    return {**ident, "url": f"{user}@{hostname}:{m.group('path').rstrip('/')}"}


def same_key(a: str, b: str, host_word: str) -> bool:
    """Two keys name one repository. GitHub and Bitbucket names are
    case-insensitive; a self-hosted forge's may not be, so only the two-level
    forges fold case."""
    if host_word in TWO_LEVEL.values():
        return a.lower() == b.lower()
    return a == b


def repository_key(target: Any) -> str:
    """A repository named by URL, by `repository:<key>` or by its bare key."""
    if not isinstance(target, str) or not target.strip():
        raise TrackError("invalid-target", "name the repository: its URL or its key")
    t = target.strip()
    if t.startswith("repository:"):
        return t[len("repository:"):]
    if "://" in t or SCP_LIKE.match(t):
        return parse_repository(t)["key"]
    return t


def owner_problem(owner: Any, *, operator_allowed: bool) -> TrackError | None:
    """Who is declaring. `operator` only where a person can be checked for.

    Over MCP there is no channel identity, so only `agent:` and `service:`
    are accepted, exactly as for `observatory_record` and `observatory_propose`.
    At a terminal the CLI writes as the operator, the same standard
    `tools/review.py` applies before it writes with that authority."""
    if isinstance(owner, str) and CALLER_ID.match(owner):
        return None
    if operator_allowed and owner == OPERATOR:
        return None
    return TrackError("owner-refused", f"{str(owner)[:70]!r} is not an identity this door accepts",
                      "use `agent:<name>` or `service:<name>`, e.g. 'agent:claude-code'. "
                      "`operator` is written only from a terminal, by the CLI.")


def _organization(value: Any) -> str | None:
    org = _text(value, "organization", limit=64)
    if org is None:
        return None
    import organizations
    names = sorted((organizations.load().get("organizations") or {}))
    if not names:
        raise TrackError("unknown-organization",
                         "no organizations are configured in this workspace, so none can be named",
                         "describe them in config/organizations.json first "
                         "(docs/ONBOARDING.md, Organizations and resources), or omit it")
    if org not in names:
        raise TrackError("unknown-organization", f"{org!r} is not a configured organization",
                         "one of: " + ", ".join(names))
    return org


# ─────────────────────────── the file ───────────────────────────────────────

def empty() -> dict:
    return {"schema_version": SCHEMA_VERSION, "repositories": [], "projects": []}


def validate(doc: Any) -> list[str]:
    """Problems that make the file unusable as a whole; an empty list is usable.

    Entry-level problems (a row with no key) are NOT here: the merge skips such
    a row and names it in `degraded`, because one hand-edited row must not hide
    every other declaration."""
    if not isinstance(doc, dict):
        return ["tracked.json must be an object"]
    version = doc.get("schema_version", SCHEMA_VERSION)
    if type(version) is not int or version < 1:
        return ["schema_version must be a positive integer"]
    if version > SCHEMA_VERSION:
        return [f"tracked.json schema {version} is newer than this release reads "
                f"({SCHEMA_VERSION}); use a compatible release"]
    out = []
    for field in ("repositories", "projects"):
        rows = doc.get(field, [])
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            out.append(f"{field} must be a list of objects")
    measurement = doc.get("measurement", {})
    if not isinstance(measurement, dict):
        out.append("measurement must be an object")
    return out


def load(path: Path | None = None) -> dict:
    """The declarations, or an empty document when there is no file.

    A file that cannot be read or is invalid RAISES rather than returning
    empty: a writer that treated an unreadable file as empty would replace
    every declaration with one."""
    path = path or file_path()
    if path.is_symlink():
        raise TrackError("unreadable", f"{path.name} must not be a symbolic link")
    if not path.exists():
        return empty()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TrackError("unreadable", f"{path.name} cannot be read: {type(exc).__name__}",
                         "fix or restore the file; nothing was changed") from None
    problems = validate(doc)
    if problems:
        raise TrackError("unreadable", "; ".join(problems), "nothing was changed")
    doc.setdefault("schema_version", SCHEMA_VERSION)
    doc.setdefault("repositories", [])
    doc.setdefault("projects", [])
    return doc


def measurement_settings(doc: dict) -> dict:
    m = doc.get("measurement") if isinstance(doc.get("measurement"), dict) else {}
    def number(name, default, low, high):
        v = m.get(name, default)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) and low <= v <= high else default
    return {"refresh_hours": number("refresh_hours", DEFAULT_REFRESH_HOURS, 0, 24 * 30),
            "fetch_cache_mb": number("fetch_cache_mb", DEFAULT_FETCH_CACHE_MB, 0, 1024 * 64)}


@contextlib.contextmanager
def _locked(timeout: float = 10.0):
    """One writer at a time: an agent's MCP call and a person's CLI call can
    race, and a read-modify-write without a lock loses the first one's entry."""
    path = file_path().parent / f".{FILE}.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TrackError("busy", "another declaration is being written",
                                     "retry in a moment") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def _save(doc: dict) -> None:
    import atomic
    atomic.write_json(file_path(), doc, indent=2)


def _journal(event: dict) -> None:
    """Append one line; a journal that cannot be written is reported, not fatal.

    The declaration itself is already saved by then, so failing the whole call
    would tell the caller the change did not happen when it did."""
    path = journal_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _journal_safely(event: dict, out: dict) -> None:
    try:
        _journal(event)
    except OSError as exc:
        out.setdefault("degraded", []).append(
            {"source": "journal", "reason": f"the change was saved but not journalled: {exc.strerror or exc}"})


# ─────────────────────────── registry context ───────────────────────────────

def _registry(name: str, key: str) -> list[dict]:
    try:
        doc = json.loads((_paths().REGISTRY / name).read_text(encoding="utf-8"))
        rows = doc.get(key) or []
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError, AttributeError):
        return []


def _measured_repository(key: str, host_word: str) -> dict | None:
    for r in _registry("repositories.json", "repositories"):
        rk = str(r.get("name_with_owner") or str(r.get("id", "")).removeprefix("repository:"))
        if same_key(rk, key, host_word) and r.get("discovered_by") != "declared":
            return r
    return None


def _measured_project(slug: str) -> dict | None:
    for p in _registry("projects.json", "projects"):
        if p.get("id") == f"project:{slug}" and p.get("anchor") != "declared":
            return p
    return None


def _holder(repo_id: str) -> str | None:
    """The project a measured membership rule gave this repository, if any."""
    for rel in _registry("relations.json", "relations"):
        if (rel.get("type") == "implemented_by" and rel.get("to") == repo_id
                and not str(rel.get("rule", "")).startswith("declared by")):
            return rel.get("from")
    return None


def next_measurement(entry: dict, kind: str) -> dict:
    """What the next tick will measure for this entry, and what it will not.

    Read from the workspace's own settings rather than stated generically, so
    an agent that just declared something can tell its person the truth: with
    the remote probe switched off, nothing will be asked of the remote at all."""
    try:
        doc = configuration.load()
    except configuration.ConfigurationError:
        doc = {}
    integrations = doc.get("integrations") or {}
    features = doc.get("features") or {}
    when = ("the next scheduled tick" if features.get("scheduler") is True else
            "`project-observatory full scan-tracked`, then `project-observatory full local` "
            "(the scheduler is off in this workspace)")
    if kind == "project":
        return {"when": "`project-observatory full local` or the next tick",
                "measures": ["the project row, from this declaration; its activity comes from "
                             "the repositories declared or discovered for it"],
                "notMeasured": []}
    measures, skipped = [], []
    if integrations.get("git_remotes") is True:
        measures.append(f"the default branch and its head commit: `git ls-remote {entry['url']}` "
                        "with your own git credentials (SSH agent or keys, credential helper); "
                        "no token is stored by Observatory")
        if features.get("tracked_fetch") is True:
            measures.append("the head commit's date and author, from a depth-1 fetch into the "
                            "bounded cache under the workspace store")
        else:
            skipped.append("the last commit's date and author: `features.tracked_fetch` is off "
                           "(`project-observatory full configure features tracked_fetch true`)")
    else:
        skipped.append("anything on the remote: `integrations.git_remotes` is off, so the "
                       "repository is listed as declared and unmeasured "
                       "(`project-observatory full configure integrations git_remotes true`)")
    return {"when": when, "measures": measures, "notMeasured": skipped}


def _remove_hint(kind: str, ident: str) -> dict:
    target = ident if kind == "project" else ident
    return {"cli": f"project-observatory full untrack {kind} {target}",
            "mcp": {"tool": "observatory_untrack", "arguments": {"kind": kind, "target": target}}}


# ─────────────────────────── the actions ────────────────────────────────────

def _find(rows: list[dict], kind: str, ident: str, host_word: str = "") -> int | None:
    for i, row in enumerate(rows):
        if kind == "project" and row.get("slug") == ident:
            return i
        if kind == "repository" and isinstance(row.get("key"), str) and same_key(
                row["key"], ident, row.get("host") or host_word):
            return i
    return None


def track_repository(fields: dict, owner: str, *, operator_allowed: bool = False) -> dict:
    """Declare a repository. Idempotent by key.

    Re-declaring by the SAME owner updates the note, project link, organization
    and URL form (a transport preference such as https → ssh); by anyone else it
    returns the existing entry unchanged, because one declarer must not rewrite
    another's words."""
    bad = owner_problem(owner, operator_allowed=operator_allowed)
    if bad:
        raise bad
    ident = parse_repository(fields.get("url"), host=fields.get("host"),
                             owner=fields.get("owner"), name=fields.get("name"))
    note = _text(fields.get("note"), "note")
    project = slug_of(fields["project"]) if fields.get("project") not in (None, "") else None
    organization = _organization(fields.get("organization"))
    now = _now()
    out: dict[str, Any] = {"kind": "repository", "degraded": [], "warnings": []}
    with _locked():
        doc = load()
        rows = doc["repositories"]
        at = _find(rows, "repository", ident["key"], ident["host"])
        if at is None:
            entry = {**ident, "added_by": owner, "added_at": now}
            for k, v in (("project", project), ("organization", organization), ("note", note)):
                if v is not None:
                    entry[k] = v
            rows.append(entry)
            rows.sort(key=lambda r: str(r.get("key", "")).lower())
            _save(doc)
            out["status"] = "added"
            action = "add"
        else:
            entry = rows[at]
            if entry.get("added_by") != owner:
                out.update(status="exists", entry=dict(entry))
                out["warnings"].append(f"declared by {entry.get('added_by')}; only that owner "
                                       f"changes its note or project link")
                action = None
            else:
                changed = False
                for k, v in (("project", project), ("organization", organization), ("note", note),
                             ("url", ident["url"])):
                    if v is not None and entry.get(k) != v:
                        entry[k] = v
                        changed = True
                if changed:
                    entry["updated_by"], entry["updated_at"] = owner, now
                    _save(doc)
                out["status"] = "updated" if changed else "unchanged"
                action = "update" if changed else None
        entry = dict(rows[at] if at is not None else entry)
    out["entry"] = entry
    if action:
        _journal_safely({"at": now, "actor": owner, "action": action, "kind": "repository",
                         "key": entry["key"], "entry": entry}, out)
    measured = _measured_repository(entry["key"], entry["host"])
    out["alreadyMeasured"] = bool(measured)
    if measured:
        out["measuredBy"] = measured.get("discovered_by")
        out["warnings"].append(
            f"already measured ({measured.get('discovered_by')}); the declaration adds who "
            f"declared it and never overrides what was measured")
        holder = _holder(measured.get("id", ""))
        if entry.get("project") and holder and holder != f"project:{entry['project']}":
            out["warnings"].append(
                f"{holder} holds this repository by a measured rule, so the declared link to "
                f"project:{entry['project']} will not apply. Moving a measured repository is a "
                f"proposal: observatory_propose with the evidence")
    out["nextTick"] = next_measurement(entry, "repository")
    out["remove"] = _remove_hint("repository", entry["key"])
    return out


def track_project(fields: dict, owner: str, *, operator_allowed: bool = False) -> dict:
    """Declare a project. A project something already measured is not declared
    again: its name and description are measured facts, and changing them is a
    proposal."""
    bad = owner_problem(owner, operator_allowed=operator_allowed)
    if bad:
        raise bad
    slug = slug_of(fields.get("slug"))
    name = _text(fields.get("name"), "name", limit=120)
    description = _text(fields.get("description"), "description")
    organization = _organization(fields.get("organization"))
    now = _now()
    out: dict[str, Any] = {"kind": "project", "degraded": [], "warnings": []}
    with _locked():
        doc = load()
        rows = doc["projects"]
        at = _find(rows, "project", slug)
        if at is None:
            measured = _measured_project(slug)
            if measured:
                raise TrackError(
                    "already-measured",
                    f"project:{slug} is already in the registry ({measured.get('anchor')} anchor); "
                    f"a declaration would change measured facts",
                    f"propose the change instead: observatory_propose with targetId "
                    f"'project:{slug}' and the fields as a patch, or attach a repository to it "
                    f"with observatory_track kind=repository project={slug}")
            entry = {"slug": slug, "name": name or slug, "added_by": owner, "added_at": now}
            for k, v in (("description", description), ("organization", organization)):
                if v is not None:
                    entry[k] = v
            rows.append(entry)
            rows.sort(key=lambda r: str(r.get("slug", "")))
            _save(doc)
            out["status"], action = "added", "add"
        else:
            entry = rows[at]
            if entry.get("added_by") != owner:
                out["status"], action = "exists", None
                out["warnings"].append(f"declared by {entry.get('added_by')}; only that owner "
                                       f"changes its name or description")
            else:
                changed = False
                for k, v in (("name", name), ("description", description), ("organization", organization)):
                    if v is not None and entry.get(k) != v:
                        entry[k] = v
                        changed = True
                if changed:
                    entry["updated_by"], entry["updated_at"] = owner, now
                    _save(doc)
                out["status"] = "updated" if changed else "unchanged"
                action = "update" if changed else None
        entry = dict(entry)
    out["entry"] = entry
    if action:
        _journal_safely({"at": now, "actor": owner, "action": action, "kind": "project",
                         "key": slug, "entry": entry}, out)
    out["alreadyMeasured"] = False
    out["nextTick"] = next_measurement(entry, "project")
    out["remove"] = _remove_hint("project", slug)
    return out


def track(kind: str, fields: dict, owner: str, *, operator_allowed: bool = False) -> dict:
    """The one entry point both doors call. Returns an answer, never raises
    for a caller's mistake: a refusal is `{"error", "detail", "hint"}`."""
    try:
        if kind == "repository":
            return track_repository(fields, owner, operator_allowed=operator_allowed)
        if kind == "project":
            return track_project(fields, owner, operator_allowed=operator_allowed)
        raise TrackError("invalid-kind", f"kind must be one of: {', '.join(KINDS)}")
    except TrackError as exc:
        return exc.answer()


def untrack(kind: str, target: str, actor: str, *, operator_allowed: bool = False) -> dict:
    """Remove a declaration. Never a measured row; see the module docstring."""
    try:
        bad = owner_problem(actor, operator_allowed=operator_allowed)
        if bad:
            raise bad
        if kind not in KINDS:
            raise TrackError("invalid-kind", f"kind must be one of: {', '.join(KINDS)}")
        ident = slug_of(target) if kind == "project" else repository_key(target)
        now = _now()
        out: dict[str, Any] = {"kind": kind, "degraded": [], "warnings": []}
        with _locked():
            doc = load()
            rows = doc["projects" if kind == "project" else "repositories"]
            at = _find(rows, kind, ident)
            if at is None:
                measured = (_measured_project(ident) if kind == "project"
                            else _measured_repository(ident, "github" if ident.count("/") == 1 else ""))
                if measured:
                    raise TrackError(
                        "measured-not-declared",
                        f"{measured.get('id')} is measured ({measured.get('discovered_by') or measured.get('anchor')}), "
                        f"not declared; removing it here would be undone by the next scan",
                        "retire a repository in config/repo_status.json (status inactive, with "
                        "evidence), or propose the change with observatory_propose")
                raise TrackError("not-tracked", f"no declared {kind} {ident!r}",
                                 "list the declarations with `project-observatory full tracked`")
            entry = rows.pop(at)
            _save(doc)
            linked = ([r.get("key") for r in doc["repositories"] if r.get("project") == ident]
                      if kind == "project" else [])
        out.update(status="removed", entry=entry, removedBy=actor)
        if entry.get("added_by") != actor:
            out["warnings"].append(f"this declaration was made by {entry.get('added_by')}; the "
                                   f"removal is journalled under {actor}")
        if linked:
            out["warnings"].append(f"{len(linked)} declared repository(ies) still name project "
                                   f"{ident}; they stand alone from the next emit: "
                                   + ", ".join(sorted(map(str, linked))[:10]))
        if kind == "repository" and _measured_repository(entry.get("key", ""), entry.get("host", "")):
            out["warnings"].append("the repository stays in the registry: a collector measures it; "
                                   "only the declaration was removed")
        _journal_safely({"at": now, "actor": actor, "action": "remove", "kind": kind,
                         "key": ident, "previous_owner": entry.get("added_by"), "entry": entry}, out)
        return out
    except TrackError as exc:
        return exc.answer()


def listing() -> dict:
    """Every declaration with what the last measurement said about it."""
    try:
        doc = load()
    except TrackError as exc:
        return exc.answer()
    measured: dict = {}
    degraded: list[dict] = []
    try:
        scan = json.loads((_paths().SCRATCH / FILE).read_text(encoding="utf-8"))
        measured = scan.get("repositories") or {}
    except FileNotFoundError:
        if doc["repositories"]:
            degraded.append({"source": "scan-tracked", "reason": "no remote has been measured yet"})
    except (OSError, ValueError) as exc:
        degraded.append({"source": "scan-tracked", "reason": f"the last measurement is unreadable: {type(exc).__name__}"})
    repos = []
    for row in doc["repositories"]:
        m = measured.get(row.get("key")) or {}
        repos.append({**row, "measurement": {k: m[k] for k in (
            "reachable", "reason", "default_branch", "head", "checked_at", "last_commit_on",
            "last_commit_author", "fetch", "skipped") if k in m} or None})
    return {"schema_version": doc.get("schema_version", SCHEMA_VERSION), "file": str(file_path()),
            "repositories": repos, "projects": list(doc["projects"]),
            "measurement": measurement_settings(doc), "degraded": degraded}


# ─────────────────────────── merge support ──────────────────────────────────

def for_merge() -> tuple[dict, list[dict]]:
    """The declarations for the pipeline, and what could not be used.

    Never raises: an unreadable file is a degradation of the merge, not an
    outage — measured projects must still be written."""
    try:
        doc = load()
    except TrackError as exc:
        return empty(), [{"source": FILE, "reason": f"declarations not read: {exc.detail}"}]
    problems, repos, projects = [], [], []
    for row in doc["repositories"]:
        try:
            ident = parse_repository(row.get("url"))
            if not same_key(ident["key"], str(row.get("key", ident["key"])), ident["host"]):
                raise TrackError("invalid-url", "its key does not match its URL")
            repos.append({**row, **{k: ident[k] for k in ("key", "host", "hostname", "owner", "name")}})
        except TrackError as exc:
            problems.append({"source": FILE, "reason": f"a declared repository was skipped: {exc.detail}"})
    for row in doc["projects"]:
        try:
            projects.append({**row, "slug": slug_of(row.get("slug"))})
        except TrackError as exc:
            problems.append({"source": FILE, "reason": f"a declared project was skipped: {exc.detail}"})
    return {**doc, "repositories": repos, "projects": projects}, problems


# ─────────────────────────── CLI ────────────────────────────────────────────

def _cli_owner(value: str | None) -> tuple[str | None, bool]:
    """`--as` for an agent driving the CLI; otherwise the person at a terminal."""
    if value:
        return value, False
    return (OPERATOR, True) if sys.stdin.isatty() else (None, False)


def _print(result: dict, as_json: bool) -> int:
    if as_json or "error" in result:
        stream = sys.stderr if "error" in result and not as_json else sys.stdout
        print(json.dumps(result, indent=2, ensure_ascii=False), file=stream)
        return 2 if "error" in result else 0
    entry = result.get("entry") or {}
    ident = entry.get("key") or entry.get("slug")
    print(f"{result.get('status')}: {result.get('kind')} {ident} (added by {entry.get('added_by')})")
    for w in result.get("warnings") or []:
        print(f"  note: {w}")
    nt = result.get("nextTick") or {}
    for m in nt.get("measures") or []:
        print(f"  will measure: {m}")
    for m in nt.get("notMeasured") or []:
        print(f"  not measured: {m}")
    if nt.get("when"):
        print(f"  when: {nt['when']}")
    if result.get("remove"):
        print(f"  remove with: {result['remove']['cli']}")
    return 0


def main(argv: list[str]) -> int:
    """`track`, `untrack` and `tracked` — the CLI half of the one door."""
    command, rest = (argv[0], argv[1:]) if argv else ("tracked", [])
    ap = argparse.ArgumentParser(prog=f"project-observatory full {command}")
    ap.add_argument("--json", action="store_true", help="print the answer as JSON")
    if command in ("track", "untrack"):
        ap.add_argument("--as", dest="actor", metavar="IDENTITY",
                        help="agent:<name> or service:<name> when an agent runs this; "
                             "a person at a terminal is recorded as the operator")
        sub = ap.add_subparsers(dest="kind", required=True)
        if command == "track":
            r = sub.add_parser("repository")
            r.add_argument("url", nargs="?")
            r.add_argument("--host"); r.add_argument("--namespace", dest="owner")
            r.add_argument("--repo-name", dest="name")
            r.add_argument("--project"); r.add_argument("--organization"); r.add_argument("--note")
            p = sub.add_parser("project")
            p.add_argument("slug")
            p.add_argument("--name"); p.add_argument("--description"); p.add_argument("--organization")
        else:
            sub.add_parser("repository").add_argument("target")
            sub.add_parser("project").add_argument("target")
    elif command != "tracked":
        print(f"Observatory: unknown command {command}", file=sys.stderr)
        return 2
    a = ap.parse_args(rest)
    try:
        configuration.validate_workspace(required=True)
    except configuration.ConfigurationError as exc:
        print(f"Observatory: {exc}", file=sys.stderr)
        return 2
    if command == "tracked":
        result = listing()
        if a.json or "error" in result:
            return _print(result, True)
        if not result["repositories"] and not result["projects"]:
            print("nothing declared — add with `project-observatory full track repository URL` "
                  "or the MCP tool observatory_track")
            return 0
        for row in result["repositories"]:
            m = row.get("measurement") or {}
            state = ("reachable" if m.get("reachable") else
                     f"unreachable: {m.get('reason')}" if m.get("reachable") is False else "not measured yet")
            print(f"repository  {row['key']:<48} {state}  (added by {row.get('added_by')})")
        for row in result["projects"]:
            print(f"project     {row['slug']:<48} {row.get('name', '')}  (added by {row.get('added_by')})")
        return 0
    owner, operator = _cli_owner(a.actor)
    if owner is None:
        return _print(TrackError("owner-refused", "no terminal and no --as identity",
                                 "an agent passes --as agent:<name>; a person runs this at a terminal").answer(),
                      a.json)
    if command == "track":
        fields = ({k: getattr(a, k) for k in ("url", "host", "owner", "name", "project", "organization", "note")}
                  if a.kind == "repository" else
                  {k: getattr(a, k) for k in ("slug", "name", "description", "organization")})
        return _print(track(a.kind, fields, owner, operator_allowed=operator), a.json)
    return _print(untrack(a.kind, a.target, owner, operator_allowed=operator), a.json)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
