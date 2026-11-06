"""`ops.digest.record` (OBS-330): products send Observatory their daily operations digests.

A product folds its own operations log into one `ops.digest.daily@1` line for each closed UTC day (fabric-agent-contract
DEC-0153, release tag v0.1.0+2026-11-06.1; `ops_digest.py` reads the line). Fabric collects those lines on the machine
(OD-9) and sends each to Observatory with this capability. This module decides who is sending, for which project, keeps
the line and answers.

WHOSE LINE IT IS COMES FROM THE TRANSPORT, NEVER FROM AN ARGUMENT. The input has no project field, and a self-named
owner grants nothing:

- **Over stdio** the project is the one project of the access binding (access-bindings/1) the server runs under: the
  operator issues a binding with the scope `ops.digest.record` and the effect `propose` for one project
  (`project-observatory full access-binding issue service:<name> --project project:<slug> --scope ops.digest.record
  --effect propose --token-file PATH`), and the connection that starts the stdio server passes
  `--binding-token-file PATH` (or sets `OBSERVATORY_BINDING_TOKEN_FILE`). The server reads the owner-only file,
  hashes the bearer and resolves it against `config/access-bindings.json` exactly as the HTTP door does: the same
  audience, expiry and revocation. A server started without one answers `binding-required`: the operator's own local
  agent is not a product, and its stdio channel names no project.
- **Over the memory-http door** the project is the `project` claim of a delegation token Fabric's hub minted for
  `project-observatory` with the scope `ops.digest.record` and an effect ceiling that admits `draft`, verified by
  `delegation.verify_delegation` with the meter's trusted issuer and keys (`meter.Meter.verify_call`, DL-13). The
  claim must be a `project:<slug>` the registry holds or a `fabric:<uuid>` Fabric's own attribution records for one
  project; a curated alias is never taken (`resolve_project`). A request on that door without such a token answers
  `delegation-required`, a bearer binding included.

The optional `service {id, instance}` must be a service the registry attributes to that project: a curated name
`agent:<id>.<instance>` in `registry/identifiers.json`, or the agent row's source repository in
`registry/agents.json`, which the registry measures as implemented by the project. Anything else is
`service-not-of-project`.

WHAT IS KEPT: the line itself (counts and operation names, which `FAC-SEM-167` has already kept free of credentials and
home folders), the label, the service, the day and which channel sent it. Nothing else: no free text, no token, no
bearer, no log line. One record per (project, service, source, UTC day). The same body again changes nothing
(`repeated: true`); a different body is a new revision, and a reader reads the FIRST line of a day (OD-6), so a
product's restart or catch-up never rewrites a day already sent. What one project can make the store hold is bounded:
`MAX_REVISIONS` per record, `MAX_SOURCES` labels per day and `MONTH_FILE_MAX` bytes per month file, each refused
with its own code before anything is written. Files are owner-only compact JSON under
`<state>/ops-digests/<project slug>/<YYYY-MM>.json`, one per month of `day`; every write prunes days older than
`KEEP_DAYS`. A file that cannot be read is never overwritten: the call is refused with `store-unreadable`.

RW-4, for whoever reads these counts: the `ok` of a host's `retro-after-work` operation counts the retro requests
answered by a retro the host saw written, never the retros written; one retro may answer several asks.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import ops_digest as OD

CAPABILITY = "ops.digest.record"
#: The access-bindings/1 scope a stdio binding must hold, and the effect it must allow.
SCOPE = CAPABILITY
BINDING_EFFECT = "propose"
#: The effect class a delegation token's ceiling must admit (fabric-delegation/0.1): a record kept here, nothing
#: published outward.
DELEGATION_EFFECT = "draft"
#: The input's members; `tests/test_wire_contract.py` holds the published input schema to them.
REQUEST_FIELDS = ("digest", "service", "source")
DEFAULT_SOURCE = "app"
#: Days kept: a day older than this is refused on the way in and pruned on every write.
KEEP_DAYS = 400
#: Bounds on what one project can make the store hold (OBS-330 review M3): revisions kept per (day, service, source),
#: distinct `source` labels per (project, day), and the bytes of one month file. Past a bound the call is refused
#: with a typed code and nothing is written, so a sender looping on new bodies or labels cannot grow a month file
#: until it can no longer be read.
MAX_REVISIONS = 16
MAX_SOURCES = 8
MONTH_FILE_MAX = 64 * 1024 * 1024
TOKEN_FILE_ENV = "OBSERVATORY_BINDING_TOKEN_FILE"
TOKEN_FILE_MAX = 4096
STORE_DIR = "ops-digests"
STORE_SCHEMA = "observatory-ops-digests/1"
PROJECT_ID = re.compile(r"project:[a-z0-9][a-z0-9._-]{0,127}")
SERVICE_ID = re.compile(r"[a-z][a-z0-9-]{1,62}")
INSTANCE = re.compile(r"[a-z][a-z0-9-]{0,31}")

#: Every refusal code, with what to do about it. The published output schema lists the same codes.
HINTS = {
    "binding-required": "start the stdio server with --binding-token-file PATH, the file an operator's "
                        "`full access-binding issue ... --scope ops.digest.record --effect propose` wrote",
    "binding-unreadable": "the token file must be an owner-only regular file holding the bearer the operator issued",
    "registry-unreadable": "the operator must repair config/access-bindings.json "
                           "(`project-observatory full access-binding show` names the fault)",
    "binding-refused": "the detail names access-bindings/1's reason (no-binding, audience-mismatch, "
                       "binding-revoked, binding-expired, scope-not-bound, effect-above-ceiling, ...); the binding "
                       "must be live, of this workspace, with the scope ops.digest.record and the effect propose",
    "binding-not-one-project": "a binding that records digests names exactly one project, the one the lines are of",
    "delegation-required": "on the HTTP door, send the delegation token Fabric's hub minted for project-observatory "
                           "with the scope ops.digest.record (Authorization: Bearer or Fabric-Delegation)",
    "delegation-refused": "the delegation token was refused; the detail names the verifier's code (DL-13)",
    "project-required": "the delegation token names no project; the hub mints it for the project the lines are of",
    "project-unknown": "the token's project must be a project of this registry by its id (project:<slug>), or the "
                       "fabric:<uuid> Fabric's own attribution records for one; a curated other name is not taken",
    "service-not-of-project": "the registry attributes this service to no such project; name the service the "
                              "lines came from, or leave service out",
    "source-refused": "source is a lower-case label (`app` by default), never a value",
    "digest-refused": "the line fails the contract (DEC-0153); the findings name each place, never its value",
    "day-not-closed": "a daily line is of a closed UTC day: yesterday or earlier",
    "day-outside-retention": f"Observatory keeps {KEEP_DAYS} days of digests; an older day is not taken",
    "revisions-exhausted": f"a day's record of one service and source keeps at most {MAX_REVISIONS} revisions; "
                           "the first is the day's line, and later bodies change nothing a reader sees",
    "sources-exhausted": f"a project's day takes at most {MAX_SOURCES} distinct source labels",
    "store-full": "this project's month of digests reached its size bound; nothing was written",
    "store-unreadable": "the digest store cannot be read; nothing was written, and the file is kept as it is",
    "store-unwritable": "the digest store cannot be written; nothing was recorded",
}


class Refused(Exception):
    """A typed refusal: an answer, not an error."""

    def __init__(self, code: str, detail: str, findings: list[dict] | None = None) -> None:
        super().__init__(code)
        self.code, self.detail, self.findings = code, detail, findings

    def envelope(self) -> dict:
        out = {"error": "ops digest refused", "code": self.code, "detail": self.detail, "hint": HINTS[self.code]}
        if self.findings:
            out["findings"] = self.findings
        out["degraded"] = []
        return out


@dataclass(frozen=True)
class Caller:
    #: The project as the transport names it: a binding's `project:<slug>`, or a token's `project` claim.
    project: str
    #: Which channel sent it, kept with the line: `binding:<id>` or `delegation:<calling agent>`.
    via: str
    #: Whether `project` still has to be resolved to a registry id (a token's claim may be `fabric:<uuid>`).
    claimed: bool


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


# ── who is sending ────────────────────────────────────────────────────────────────────────────────────────────

def caller() -> Caller:
    """The sender, from the channel the transport observed (`memory_access.current()`); raises Refused."""
    import memory_access as MA
    observed = MA.current()
    kind = observed.get("channel") if isinstance(observed, dict) else None
    if kind == "delegation":
        return _delegated(observed.get("delegationToken"))
    if kind == "stdio":
        return _bound()
    if kind == "http":
        raise Refused("delegation-required", "a binding's bearer opens the memory tools; ops.digest.record on "
                                             "the HTTP door takes a delegation token")
    raise Refused("binding-required", "this channel names no binding")


def _token_file_check(fd: int) -> None:
    """Owner-only, on every OS: no group or world bit on POSIX, no access entry for another account on Windows
    (`osprivacy.private`), and owned by this account."""
    import osprivacy
    if not osprivacy.private(fd):
        raise ValueError("readable by others")
    if not osprivacy.owned_by_me(fd):
        raise ValueError("owned by another account")


def _bound() -> Caller:
    import access_binding as AB
    import memory_access as MA
    import osprivacy
    named = os.environ.get(TOKEN_FILE_ENV, "")
    if not named:
        raise Refused("binding-required", "this stdio server runs under no access binding; the operator's own "
                                          "local agent is not a product, and its channel names no project")
    path = Path(named)
    if not path.is_absolute():
        raise Refused("binding-unreadable", "the token file is not an absolute path")
    try:
        raw = osprivacy.read_regular(path, TOKEN_FILE_MAX, check=_token_file_check)
        bearer = raw.decode("utf-8").strip()
    except FileNotFoundError:
        raise Refused("binding-unreadable", "the token file does not exist") from None
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, UnicodeDecodeError) \
            else type(exc).__name__
        raise Refused("binding-unreadable", f"the token file cannot be read: {reason}") from None
    if not bearer or any(c.isspace() for c in bearer):
        raise Refused("binding-unreadable", "the token file holds no single bearer")
    digest = AB.bearer_digest(bearer)
    del bearer, raw
    try:
        registry = MA.registry()
    except AB.BindingError as exc:
        raise Refused("registry-unreadable", str(exc)) from None
    found = AB.resolve(registry, {"channel": "http", "audience": MA.own_audience(), "bearerDigest": digest},
                       now=MA._now())
    if found.binding is None:
        raise Refused("binding-refused", found.reason)
    binding = found.binding
    if binding.projects == "all" or len(binding.projects) != 1:
        raise Refused("binding-not-one-project", f"{binding.binding_id} names "
                                                 f"{'every project' if binding.projects == 'all' else len(binding.projects)} projects")
    decision = AB.authorize(binding, {"scope": SCOPE, "effect": BINDING_EFFECT, "projectId": binding.projects[0]})
    if not decision.allow:
        raise Refused("binding-refused", f"{binding.binding_id}: {decision.reason}")
    return Caller(project=binding.projects[0], via=f"binding:{binding.binding_id}", claimed=False)


def _delegated(token: Any) -> Caller:
    import meter
    try:
        verdict = meter.default_meter().verify_call(token, capability=CAPABILITY, effect=DELEGATION_EFFECT)
    except (OSError, ValueError) as exc:
        raise Refused("delegation-refused", f"the verifier could not run: {type(exc).__name__}") from None
    finally:
        del token
    if not verdict.ok:
        raise Refused("delegation-refused", f"{verdict.code}:{verdict.detail}")
    project = verdict.claims.get("project")
    if not isinstance(project, str) or not project:
        raise Refused("project-required", "the token carries no project claim")
    return Caller(project=project, via=f"delegation:{verdict.caller}", claimed=True)


# ── which project, and whose service ──────────────────────────────────────────────────────────────────────────

def resolve_project(caller_: Caller, registry: Callable[[], Any] | None) -> str:
    """The registry id of the caller's project.

    A binding's project is the operator's own grant at a terminal and is taken as written. A token's claim is taken
    only two ways, both checked against the registry: a `project:<slug>` the registry holds, or a `fabric:<uuid>`
    that Fabric's own attribution (`fabric_attribution`, the registry's `fabric` map) records for exactly one
    project. A curated other name (`config/identifiers.json`) is never taken here: an alias an agent proposed and a
    policy accepted could otherwise send one project's digests to another (OBS-330 review M2)."""
    if not caller_.claimed:
        if PROJECT_ID.fullmatch(caller_.project):
            return caller_.project
        raise Refused("project-unknown", "the binding's project is not a registry id")
    if registry is None:
        raise Refused("project-unknown", "no registry to resolve the token's project against")
    reg = registry()
    claim = caller_.project
    if PROJECT_ID.fullmatch(claim):
        if claim in reg.projects:
            return claim
        raise Refused("project-unknown", "the token's project is not in this registry")
    if claim.startswith("fabric:"):
        owners = sorted(pid for pid, alias in reg.fabric.items() if alias == claim and pid in reg.projects)
        if len(owners) == 1:
            return owners[0]
        raise Refused("project-unknown", "Fabric's attribution records this fabric: id for "
                                         f"{'several projects' if owners else 'no project'} of this registry")
    raise Refused("project-unknown", "the token's project is neither a project: id nor a fabric: id")


def _repository_ids(url: str) -> set[str]:
    """`registry/repositories.json` ids whose URL is this one, compared without `.git`, a trailing slash or case."""
    import paths

    def norm(value: str) -> str:
        value = value.strip().rstrip("/")
        return (value[:-4] if value.endswith(".git") else value).casefold()
    try:
        rows = json.loads((Path(paths.REGISTRY) / "repositories.json").read_text(encoding="utf-8"))["repositories"]
    except (OSError, ValueError, KeyError, TypeError):
        return set()
    want = norm(url)
    return {r["id"] for r in rows if isinstance(r, dict) and isinstance(r.get("url"), str)
            and isinstance(r.get("id"), str) and norm(r["url"]) == want}


def service_projects(service: dict, registry: Callable[[], Any] | None) -> set[str]:
    """Every project the registry attributes this service to: its curated name `agent:<id>.<instance>`, and the
    projects measured as implementing the source repository its row in `registry/agents.json` names. Never a match
    by a name's look (rule 2 of the registry)."""
    import identifiers
    import paths
    if registry is None:
        return set()
    reg = registry()
    names = [f"agent:{service['id']}.{service['instance']}"]
    try:
        rows = json.loads((Path(paths.REGISTRY) / "agents.json").read_text(encoding="utf-8"))["agents"]
    except (OSError, ValueError, KeyError, TypeError):
        rows = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("service_id") == service["id"] \
                and row.get("instance") == service["instance"] and isinstance(row.get("source_repository"), str):
            names.extend(sorted(_repository_ids(row["source_repository"])))
    out: set[str] = set()
    for name in names:
        answer = identifiers.resolve(name, reg)
        if answer.get("kind") == "project" and isinstance(answer.get("canonical"), str):
            out.add(answer["canonical"])
        elif answer.get("error") == "ambiguous":
            out.update(c for c in answer.get("candidates") or [] if isinstance(c, str) and c.startswith("project:"))
    return out


# ── the store ─────────────────────────────────────────────────────────────────────────────────────────────────

def store_root() -> Path:
    import paths
    return Path(paths.STATE) / STORE_DIR


def _project_dir(project: str) -> Path:
    return store_root() / project.split(":", 1)[1]


def _canonical(digest: dict) -> str:
    return json.dumps(digest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class StoreUnreadable(OSError):
    """A month file that is there and is not this store's document: refused, never replaced."""


def _read(path: Path) -> dict | None:
    """A month file, or None when absent. Anything else that is not this store's document raises OSError, so a
    damaged file is refused and kept rather than replaced by an empty one."""
    import osprivacy
    try:
        raw = osprivacy.read_regular(path, 256 * 1024 * 1024)
    except FileNotFoundError:
        return None
    except ValueError as exc:
        raise StoreUnreadable(f"{path.name}: {exc}") from None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise StoreUnreadable(f"{path.name} is not JSON") from None
    if not (isinstance(doc, dict) and doc.get("schema") == STORE_SCHEMA and isinstance(doc.get("lines"), list)
            and all(isinstance(x, dict) and isinstance(x.get("day"), str) and isinstance(x.get("revisions"), list)
                    and all(isinstance(r, dict) for r in x["revisions"]) for x in doc["lines"])):
        raise StoreUnreadable(f"{path.name} is not an {STORE_SCHEMA} document")
    return doc


def _write(path: Path, doc: dict) -> None:
    """Compact (no indent): the store is read by this module, not by a person in a diff."""
    import atomic
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic.write_json(path, doc, indent=None)


class _Lock:
    """One writer at a time across processes: several stdio servers and the HTTP door may record at once."""

    def __enter__(self):
        import oslocks
        import osprivacy
        root = store_root()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # NOFOLLOW: a link planted as the lock file is refused, never opened through.
        self.fd = osprivacy.open(root / ".lock", os.O_CREAT | os.O_RDWR | osprivacy.NOFOLLOW | osprivacy.NONBLOCK,
                                 0o600)
        oslocks.flock(self.fd, oslocks.LOCK_EX)
        return self

    def __exit__(self, *exc):
        os.close(self.fd)
        return False


def record(project: str, service: dict | None, source: str, digest: dict, via: str, *,
           today: date | None = None) -> dict:
    """Keep one daily line (already checked) under (project, service, source, day). The same body again changes
    nothing; a different body becomes the next revision; the first revision is the day's line (OD-6)."""
    day = digest["day"]
    body = _canonical(digest)
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    path = _project_dir(project) / f"{day[:7]}.json"
    with _Lock():
        doc = _read(path) or {"schema": STORE_SCHEMA, "project": project, "month": day[:7], "lines": []}
        line = next((x for x in doc["lines"] if x.get("day") == day and x.get("source") == source
                     and x.get("service") == service), None)
        if line is None:
            sources = {x.get("source") for x in doc["lines"] if x.get("day") == day}
            if source not in sources and len(sources) >= MAX_SOURCES:
                raise Refused("sources-exhausted", f"{day} already holds {len(sources)} source labels")
            line = {"day": day, "service": service, "source": source, "revisions": []}
            doc["lines"].append(line)
        same = next((r for r in line["revisions"] if r.get("sha256") == sha), None)
        if same is not None:
            revision, repeated = same["revision"], True
        else:
            if len(line["revisions"]) >= MAX_REVISIONS:
                raise Refused("revisions-exhausted", f"{day} of this service and source holds "
                                                     f"{len(line['revisions'])} revisions")
            revision, repeated = len(line["revisions"]) + 1, False
            line["revisions"].append({"revision": revision, "sha256": sha, "via": via,
                                      "receivedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                      "digest": json.loads(body)})
            doc["lines"].sort(key=lambda x: (x["day"], str(x.get("source")), _canonical(x.get("service") or {})))
            if len(json.dumps(doc, ensure_ascii=False).encode("utf-8")) > MONTH_FILE_MAX:
                raise Refused("store-full", f"{day[:7]} would pass {MONTH_FILE_MAX // (1024 * 1024)} MiB")
            _write(path, doc)
        # The line is kept: pruning other days is housekeeping, and its failure never turns an accepted line into
        # a refusal (OBS-330 review LOW 1). The next write prunes again.
        try:
            _prune_locked(today or today_utc())
        except Exception:  # noqa: BLE001
            pass
    return {"accepted": True, "repeated": repeated, "revision": revision, "project": project, "service": service,
            "source": source, "day": day}


def cutoff(today: date) -> str:
    return (today - timedelta(days=KEEP_DAYS)).isoformat()


def _prune_locked(today: date) -> int:
    """Drop every day older than the cutoff, in every project's folder. A file it cannot read is kept."""
    root, oldest, dropped = store_root(), cutoff(today), 0
    if not root.is_dir():
        return 0
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink()):
        for path in sorted(folder.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9].json")):
            if path.stem >= oldest[:7] and path.stem != oldest[:7]:
                continue
            try:
                doc = _read(path)
            except OSError:
                continue          # a damaged or unreadable month is kept for a person to look at
            if doc is None or not isinstance(doc.get("lines"), list):
                continue
            kept = [x for x in doc["lines"] if isinstance(x, dict) and isinstance(x.get("day"), str)
                    and x["day"] >= oldest]
            dropped += len(doc["lines"]) - len(kept)
            if not kept:
                path.unlink()
            elif len(kept) != len(doc["lines"]):
                _write(path, {**doc, "lines": kept})
    return dropped


def prune(today: date | None = None) -> int:
    """Days dropped across every project (each write also prunes)."""
    with _Lock():
        return _prune_locked(today or today_utc())


def first_lines(project: str) -> list[dict]:
    """The line of each (day, service, source) a reader takes: its FIRST revision (OD-6), with how many came."""
    folder = _project_dir(project)
    out = []
    for path in sorted(folder.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9].json")) if folder.is_dir() else []:
        doc = _read(path) or {"lines": []}
        for x in doc["lines"]:
            if x.get("revisions"):
                out.append({"day": x["day"], "service": x["service"], "source": x["source"],
                            "revisions": len(x["revisions"]), "digest": x["revisions"][0]["digest"]})
    return out


# ── the capability ────────────────────────────────────────────────────────────────────────────────────────────

def _service(value: Any) -> dict | None:
    if value is None:
        return None
    if not (isinstance(value, dict) and set(value) == {"id", "instance"} and isinstance(value["id"], str)
            and isinstance(value["instance"], str) and SERVICE_ID.fullmatch(value["id"])
            and INSTANCE.fullmatch(value["instance"])):
        raise Refused("service-not-of-project", "service is {id, instance} as a Fabric service descriptor names them")
    return {"id": value["id"], "instance": value["instance"]}


def record_call(arguments: dict, *, registry: Callable[[], Any] | None = None, today: date | None = None) -> dict:
    """One `ops.digest.record` call: the answer, or a refusal envelope. The input schema has already been checked."""
    today = today or today_utc()
    try:
        who = caller()
        source = arguments.get("source", DEFAULT_SOURCE)
        refused = OD.source_refused(source)
        if refused is not None:
            raise Refused("source-refused", refused)
        digest = arguments.get("digest")
        findings = OD.check("ops-digest-daily", digest)
        if findings:
            raise Refused("digest-refused", f"{len(findings)} finding(s) against DEC-0153", findings[:50])
        digest = OD.normalized(digest)
        day = digest["day"]
        if day >= today.isoformat():
            raise Refused("day-not-closed", f"{day} is not a closed UTC day")
        if day < cutoff(today):
            raise Refused("day-outside-retention", f"{day} is older than {KEEP_DAYS} days")
        project = resolve_project(who, registry)
        service = _service(arguments.get("service"))
        if service is not None and project not in service_projects(service, registry):
            raise Refused("service-not-of-project",
                          f"the registry attributes {service['id']}.{service['instance']} to no project {project}")
        try:
            return record(project, service, source, digest, who.via, today=today)
        except StoreUnreadable as exc:
            raise Refused("store-unreadable", str(exc)) from None
        except (OSError, ValueError) as exc:
            raise Refused("store-unwritable", type(exc).__name__) from None
    except Refused as exc:
        return exc.envelope()
