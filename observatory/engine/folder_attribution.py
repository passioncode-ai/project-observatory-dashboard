"""Folder attribution: which project a folder belongs to, by the Fabric contract's rules.

A port of `src/folder-attribution.ts` of passioncode-ai/fabric-agent-contract (DEC-0089,
rule FAC-SEM-121, docs/specification/folder-attribution.md), pinned in
`fabric/attribution-vectors/SOURCE.json`. Three products attribute a coding agent's folder to a
project (Fabric, Fabric Switchboard and this engine); the contract is the one rule set, and
every product runs its vectors in its own gate (`tests/test_folder_attribution.py` runs them
here). Observatory keeps no rule of its own beside them.

Why a port and not a call: Fabric owns the canonical project id and serves the attribution
(`fabric.project.resolve`, D4). This module is the LOCAL fallback the contract asks for while
Fabric is not running, and the reader of what Observatory itself knows (its own projects,
their folders, its declared containers). It reads no file system: `fabric_attribution.py`
gathers the facts into a snapshot, and this answers from them, so the same facts give every
product the same answer.

Every function answers and never raises: a malformed snapshot is `{"fault": "input-invalid"}`.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlsplit

_PATH = re.compile(r"^(?:/|[A-Z]:/)(?:[^/]+(?:/[^/]+)*)?$")
MAX_PATH = 4096
MAX_LIST = 10_000
SCHEMES = ("http", "https", "ssh", "git", "git+ssh", "git+https")


def _within(value: str, limit: int) -> bool:
    """At most `limit` code points (Python strings are code points; JSON Schema counts the same)."""
    return len(value) <= limit


def _id_ok(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 256


def _origin_ok(value: Any) -> bool:
    return value is None or value is _ABSENT or (isinstance(value, str) and _within(value, MAX_PATH))


_ABSENT = object()


def _own(value: Any, key: str) -> Any:
    return value.get(key, _ABSENT) if isinstance(value, dict) else _ABSENT


def _list(value: Any) -> list | None:
    return value if isinstance(value, list) and len(value) <= MAX_LIST else None


def _ascii_lower(value: str) -> str:
    return re.sub(r"[A-Z]", lambda m: m.group(0).lower(), value)


def _trim_slashes(value: str, leading: bool) -> str:
    start, end = 0, len(value)
    while leading and start < end and value[start] == "/":
        start += 1
    while end > start and value[end - 1] == "/":
        end -= 1
    return value[start:end]


def is_path(value: Any) -> bool:
    return (isinstance(value, str) and _within(value, MAX_PATH) and bool(_PATH.match(value))
            and not any(s in (".", "..") for s in value.split("/")))


def _is_root(path: str) -> bool:
    return path == "/" or bool(re.fullmatch(r"[A-Z]:/", path))


def under(path: str, base: str) -> bool:
    """`path` is `base` or lies under it, segment by segment (`/a/bc` is not under `/a/b`)."""
    if path == base:
        return True
    return path.startswith(base if _is_root(base) else base + "/")


def _parent_of(path: str) -> str:
    parent = path[: path.rfind("/")]
    if parent == "":
        return "/"
    return parent + "/" if re.fullmatch(r"[A-Z]:", parent) else parent


def normalize_origin(raw: Any) -> str | None:
    """A remote's repository identity, `host/path`, A to Z lower-cased; None when it has none."""
    if not isinstance(raw, str) or not _within(raw, MAX_PATH):
        return None
    value = raw.strip()
    scheme = re.match(r"^([a-z][a-z0-9+.-]*)://", value, re.I)
    if scheme:
        if scheme.group(1).lower() not in SCHEMES:
            return None
        rest = re.split(r"[?#]", value[scheme.end():])[0]
        slash = rest.find("/")
        if slash < 0:
            return None
        host, path = rest[:slash], rest[slash + 1:]
    else:
        if value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("."):
            return None
        scp = re.fullmatch(r"(?:[^@/\s]+@)?([^:/\s]+):(\S+)", value)
        if not scp:
            return None
        host, path = scp.group(1), re.split(r"[?#]", scp.group(2))[0]
    host = _ascii_lower(re.sub(r":[0-9]*$", "", host[host.rfind("@") + 1:]))
    path = _ascii_lower(_trim_slashes(path, True))
    if path.endswith(".git"):
        path = path[:-4]
    segments = path.split("/")
    if (not re.fullmatch(r"[a-z0-9.-]+", host) or "." not in host or len(segments) < 2
            or any(s == "" or re.search(r"\s", s) for s in segments)):
        return None
    return f"{host}/{'/'.join(segments)}"


# ---- the contract's credential detector, for `record_origin` (src/secret-shape.ts) ----------------
_CREDENTIAL_WORDS = {
    "token", "secret", "password", "passwd", "pass", "passphrase", "credential", "credentials", "cookie",
    "authorization", "bearer", "jwt", "otp", "apikey", "signature", "sig", "accesstoken", "idtoken", "authtoken",
    "refreshtoken", "sessionid", "sessiontoken", "logincode", "authcode", "privatekey", "clientsecret", "secretkey",
    "accesskey"}
_CREDENTIAL_PAIRS = {
    "key": ("private", "api", "access", "secret", "session", "auth", "signing", "encryption", "master", "client",
            "login", "refresh"),
    "auth": ("token", "code", "header"), "session": ("token", "id"), "code": ("login", "auth", "access", "session")}
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]*")
_SECRET_VALUE = re.compile(r"(^bearer\s.+|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abposr]-[A-Za-z0-9-]{10,}"
                           r"|AKIA[0-9A-Z]{16}|glpat-[A-Za-z0-9_-]{20,})", re.I)
_LONG_RUN = re.compile(r"[A-Za-z0-9+/=_-]{40,}")
_BEARER_INSIDE = re.compile(r"\bbearer\s+(?=[A-Za-z0-9._~+/-]*\d)[A-Za-z0-9._~+/-]{12,}=*", re.I)
_PATH_LIKE = re.compile(r"^(?:[/~.]|[A-Za-z]:[\\/]|[a-z][a-z0-9+.-]*://)", re.I)


def _name_words(name: str) -> list[str]:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", name)
    return [w.lower() for w in re.split(r"[\s._-]+", name) if w]


def _credential_name(name: str) -> bool:
    split = _name_words(name)
    words = split + [w[:-1] for w in split if len(w) > 2 and w.endswith("s")]
    if any(w in _CREDENTIAL_WORDS for w in words):
        return True
    if len(split) == 1 and any(w in _CREDENTIAL_PAIRS for w in words):
        return True
    return any(w in words and any(p in words for p in partners) for w, partners in _CREDENTIAL_PAIRS.items())


def _long_secret(value: str) -> bool:
    return any(re.search(r"[a-z]", r) and re.search(r"[A-Z]", r) and re.search(r"[0-9]", r)
               for r in _LONG_RUN.findall(value))


def _secret_value(value: str) -> bool:
    return bool(_JWT.search(value) or _SECRET_VALUE.search(value)) or _long_secret(value)


def _path_like(piece: str) -> bool:
    return bool(_PATH_LIKE.match(piece)) or (
        bool(re.search(r"[\\/]", piece)) and (bool(re.search(r"\.[A-Za-z0-9]{1,8}$", piece))
                                               or any(re.fullmatch(r"[a-z][a-z0-9_-]{2,}", s)
                                                      for s in re.split(r"[\\/]", piece))))


def _secret_argument(value: str) -> bool:
    if _JWT.search(value) or _SECRET_VALUE.search(value) or _BEARER_INSIDE.search(value):
        return True
    for piece in re.split(r"[\s=]", value):
        parts = re.split(r"[\\/]", piece) if _path_like(piece) else [piece]
        if any(_long_secret(p) for p in parts):
            return True
    return False


def _secret_url(value: str) -> bool:
    if _secret_argument(value):
        return True
    try:
        url = urlsplit(value)
        if not url.scheme or not url.netloc:
            return False
        password = url.password
    except ValueError:
        return False
    if password:
        return True
    for part in (url.query, url.fragment):
        for piece in part.split("&"):
            if not piece:
                continue
            name, _eq, raw = piece.partition("=")
            raw = unquote(raw)
            if (raw and _credential_name(unquote(name))) or _secret_value(raw):
                return True
    return False


def record_origin(origin: str | None) -> str | None:
    """The origin a record keeps: the raw remote without what can carry a credential, or None."""
    if origin is None:
        return None
    value = origin.strip()
    scheme = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://", value)
    if scheme:
        # A URL with no repository identity names nothing a rule could match, and its authority cannot be
        # read reliably (a password holding `/`, `?` or `#`): nothing of it is kept.
        if normalize_origin(value) is None:
            return None
        rest = re.split(r"[?#]", value[scheme.end():])[0]
        slash = rest.find("/")  # a URL with an identity has a path, so its authority ends here
        authority = rest[:slash]
        at = authority.rfind("@")
        user = "" if at < 0 or re.search("http", scheme.group(1), re.I) else authority[:at].split(":")[0]
        value = f"{scheme.group(0)}{user + '@' if user else ''}{authority[at + 1:]}{rest[slash:]}"
    elif not (value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith(".")):
        scp = re.fullmatch(r"(?:([^@/\s]+)@)?([^:/\s]+):(\S+)", value)
        # A remote that is not a local path and has no identity cannot be read reliably: nothing is kept.
        if not scp or normalize_origin(value) is None:
            return None
        host = scp.group(2)  # the host ends the user information at its last `@`, as normalize_origin reads it
        user = "" if scp.group(1) is None else scp.group(1).split(":")[0] + "@"
        value = f"{user}{host[host.rfind('@') + 1:]}:{re.split(r'[?#]', scp.group(3))[0]}"
    # The kept origin names the same repository as the raw one, by construction.
    return None if _secret_url(value) or normalize_origin(value) != normalize_origin(origin) else value


def owner_of(repository: str) -> str:
    return repository[: repository.rfind("/")]


def _relative_to(path: str, top: str) -> str:
    return "" if path == top else path[len(top) if _is_root(top) else len(top) + 1:]


def _rel_under(rel: str, base: str) -> bool:
    return base == "" or rel == base or rel.startswith(base + "/")


def _in_tool_folder(path: str, home: str) -> bool:
    if path == home or not under(path, home):
        return False
    first = path[len(home):].lstrip("/").split("/")[0]
    return first.startswith(".") or first in ("Library", "AppData")


class _Folder:
    __slots__ = ("path", "common_dir", "origin", "repository", "rel")

    def __init__(self, path: str, common_dir: str | None, origin: Any, rel: str) -> None:
        self.path, self.common_dir, self.origin, self.rel = path, common_dir, origin, rel
        self.repository: Any = _ABSENT


def _repository_of(entry: _Folder) -> str | None:
    if entry.repository is _ABSENT:
        entry.repository = normalize_origin(entry.origin)
    return entry.repository


def _projects_of(value: Any) -> list[tuple[str, list[_Folder], list[str]]] | None:
    raw = _list(value)
    if raw is None:
        return None
    out = []
    for entry in raw:
        pid, folders = _own(entry, "id"), _list(_own(entry, "folders"))
        sources = [] if _own(entry, "sources") is _ABSENT else _list(_own(entry, "sources"))
        if (not _id_ok(pid) or folders is None or sources is None
                or not all(isinstance(s, str) and s and _within(s, MAX_PATH) for s in sources)):
            return None
        parsed = []
        for folder in folders:
            path, common, origin, top = (_own(folder, k) for k in ("path", "commonDir", "origin", "toplevel"))
            if (not is_path(path) or (common not in (_ABSENT, None) and not is_path(common))
                    or not _origin_ok(origin)):
                return None
            if top not in (_ABSENT, None) and not (is_path(top) and under(path, top)):
                return None
            parsed.append(_Folder(path, common if isinstance(common, str) else None,
                                  None if origin is _ABSENT else origin,
                                  _relative_to(path, top) if isinstance(top, str) else ""))
        out.append((pid, parsed, [_trim_slashes(_ascii_lower(s), False) for s in sources]))
    return out if len({p[0] for p in out}) == len(out) else None


def _ids(projects: list) -> list[str]:
    seen: list[str] = []
    for p in projects:
        if p[0] not in seen:
            seen.append(p[0])
    return seen


def _nearest_in(projects: list, matches, rel: str) -> tuple[list, str]:
    nearest, place, owners = -1, "", []
    for project in projects:
        for entry in project[1]:
            if not matches(entry) or not _rel_under(rel, entry.rel):
                continue
            if len(entry.rel) > nearest:
                nearest, place, owners = len(entry.rel), entry.rel, [project]
            elif len(entry.rel) == nearest:
                owners.append(project)
    return owners, place


def _decide(matches: list, rule: str) -> dict | None:
    found = _ids(matches)
    if not found:
        return None
    if len(found) == 1:
        return {"outcome": "attached", "project": found[0], "rule": rule}
    return {"outcome": "ambiguous", "rule": rule, "projects": found}


def attribute_folder(snapshot: Any) -> dict:
    """The project of one folder, in the order of the contract's AT table."""
    return explain_folder(snapshot)["answer"]


def explain_folder(snapshot: Any) -> dict:
    """`attribute_folder`'s answer, the AT row that decided it, and the folder to record (or None)."""
    def at(row: int, answer: dict, record: dict | None = None) -> dict:
        return {"answer": answer, "row": row, "record": record}

    fault = at(0, {"fault": "input-invalid"})
    folder, home = _own(snapshot, "folder"), _own(snapshot, "home")
    temp_roots = _list(_own(snapshot, "tempRoots"))
    git = _own(snapshot, "git")
    projects = _projects_of(_own(snapshot, "projects"))

    def optional_list(key: str):
        v = _own(snapshot, key)
        return [] if v is _ABSENT else _list(v)

    work, tombstones, containers = optional_list("work"), optional_list("tombstones"), optional_list("containers")
    explicit = _own(snapshot, "explicit")
    if (not is_path(folder) or not is_path(home) or temp_roots is None or not all(is_path(t) for t in temp_roots)
            or projects is None or work is None or tombstones is None or not all(is_path(t) for t in tombstones)
            or containers is None or not all(is_path(c) for c in containers)):
        return fault
    if git is _ABSENT or (git is not None and not isinstance(git, dict)):
        return fault
    toplevel, common_dir, main_checkout, origin = (_own(git, k) for k in ("toplevel", "commonDir", "mainCheckout", "origin"))
    if git is not None and (not is_path(toplevel) or not is_path(common_dir) or not is_path(main_checkout)
                            or not under(folder, toplevel) or not _origin_ok(origin)):
        return fault
    records = []
    for record in work:
        project, repo, rorigin = _own(record, "project"), _own(record, "commonDir"), _own(record, "origin")
        if not _id_ok(project) or not is_path(repo) or not _origin_ok(rorigin):
            return fault
        records.append((project, repo, None if rorigin is _ABSENT else rorigin))
    declared = set(containers)

    def broad(path: str) -> bool:
        return _is_root(path) or under(home, path) or path in declared

    def cut_of(path: str) -> str | None:
        cut = None
        for container in declared:
            if under(path, container) and (cut is None or len(container) > len(cut)):
                cut = container
        return cut

    def reaches(path: str, cut: str | None, entry: str) -> bool:
        return under(path, entry) and not (cut is not None and cut != entry and under(cut, entry))

    kept_origin = record_origin(origin if isinstance(origin, str) else None)

    def git_record(path: str, top: str | None) -> dict | None:
        return None if broad(path) else {"path": path, "commonDir": common_dir, "origin": kept_origin, "toplevel": top}

    if explicit is not _ABSENT:
        target = next((p for p in projects if p[0] == explicit), None) if _id_ok(explicit) else None
        if target is None:
            return fault
        record = (None if broad(folder) else
                  {"path": folder, "commonDir": None, "origin": None, "toplevel": None} if git is None
                  else git_record(folder, toplevel))
        return at(1, {"outcome": "attached", "project": target[0], "rule": "explicit"}, record)

    root = folder if git is None else main_checkout

    def listed(path: str) -> bool:
        return any(e.path == path for p in projects for e in p[1])

    def loose_under_home(path: str) -> bool:
        return _parent_of(path) == home and not listed(path)

    def by_path(path: str) -> list:
        cut, nearest, owners = cut_of(path), -1, []
        for project in projects:
            for entry in project[1]:
                if not reaches(path, cut, entry.path) or broad(entry.path):
                    continue
                if len(entry.path) > nearest:
                    nearest, owners = len(entry.path), [project]
                elif len(entry.path) == nearest:
                    owners.append(project)
        return owners

    repository = None if git is None else normalize_origin(origin)
    place = "" if git is None else _relative_to(folder, toplevel)

    def same_common(e: _Folder) -> bool:
        return git is not None and not broad(e.path) and (e.common_dir == common_dir or e.path == main_checkout)

    def same_identity(e: _Folder) -> bool:
        return repository is not None and not broad(e.path) and _repository_of(e) == repository

    def same_repository(e: _Folder) -> bool:
        return same_common(e) or same_identity(e)

    def any_folder(test) -> bool:
        return any(test(e) for p in projects for e in p[1])

    deepest = None
    folder_cut = cut_of(folder)
    for project in projects:
        for entry in project[1]:
            if (not broad(entry.path) and reaches(folder, folder_cut, entry.path)
                    and (deepest is None or len(entry.path) > len(deepest))):
                deepest = entry.path
    if any(under(folder, s) and not (deepest is not None and deepest != s and under(deepest, s)) for s in tombstones):
        return at(2, {"outcome": "refused", "reason": "tombstoned"})
    if any_folder(lambda e: same_repository(e) and reaches(folder, folder_cut, e.path)):
        common = _nearest_in(projects, same_common, place)
        identity = _nearest_in(projects, same_identity, place)
        nearer = identity if (not common[0] or len(identity[1]) > len(common[1])) else common
        by_place = _decide(nearer[0], "folder")
        if by_place:
            return at(3, by_place)
    by_folder = _decide(by_path(folder), "folder")
    if by_folder:
        return at(3, by_folder)
    if any(under(folder, t) or under(root, t) for t in temp_roots):
        return at(4, {"outcome": "refused", "reason": "transient"})
    # AT-5: a checkout right under home is a container whatever folder of it the agent starts in.
    checkout_top = None if git is None else toplevel
    loose_root = loose_under_home(root) and not any_folder(
        lambda e: same_repository(e) and (checkout_top != root or under(e.path, root)))
    if (broad(folder) or broad(root) or loose_under_home(folder)
            or (checkout_top is not None and checkout_top != root
                and (loose_under_home(checkout_top) or checkout_top in declared))
            or loose_root):
        return at(5, {"outcome": "refused", "reason": "container"})
    if git is not None:
        top = toplevel

        def at_place(found: tuple[list, str], row: int, rule: str):
            answer = _decide(found[0], rule)
            if not answer:
                return None
            where = top if found[1] == "" else (top if _is_root(top) else top + "/") + found[1]
            return at(row, answer, git_record(where, top) if answer["outcome"] == "attached" else None)

        # AT-6 and AT-7 weigh the same places AT-3 does: the nearest across common dir and identity.
        common = _nearest_in(projects, same_common, place)
        identity = _nearest_in(projects, same_identity, place)
        identity_nearer = not common[0] or len(identity[1]) > len(common[1])
        hit = at_place(identity, 7, "repository") if identity_nearer else at_place(common, 6, "worktree")
        if hit:
            return hit

        def whole(row: int, answer: dict | None):
            if not answer:
                return None
            return at(row, answer, git_record(top, top) if answer["outcome"] == "attached" else None)

        hit = whole(8, _decide(by_path(root), "worktree"))
        if hit:
            return hit
        if any_folder(same_repository):
            return at(9, {"outcome": "refused", "reason": "container"})
        if repository is not None:
            hit = whole(10, _decide([p for p in projects if owner_of(repository) in p[2]], "owner"))
            if hit:
                return hit
        if _in_tool_folder(folder, home) or _in_tool_folder(root, home):
            return at(11, {"outcome": "refused", "reason": "tool"})
        workers = {r[0] for r in records
                   if r[1] == common_dir or (repository is not None and normalize_origin(r[2]) == repository)}
        hit = whole(12, _decide([p for p in projects if p[0] in workers], "work"))
        if hit:
            return hit
    elif _in_tool_folder(folder, home):
        return at(11, {"outcome": "refused", "reason": "tool"})
    if any(under(root, s) for s in tombstones):
        return at(13, {"outcome": "refused", "reason": "tombstoned"})
    if any_folder(lambda e: under(e.path, root)):
        return at(14, {"outcome": "refused", "reason": "container"})
    record = ({"path": root, "commonDir": None, "origin": None, "toplevel": None} if git is None
              else git_record(root, None if root == common_dir else root))
    return at(15, {"outcome": "new", "root": root}, record)
