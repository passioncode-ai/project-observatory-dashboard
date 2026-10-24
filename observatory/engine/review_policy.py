#!/usr/bin/env python3
"""The standing review policy: decisions a person delegated once, applied by the tick (OBS-227).

WHY. Two queues wait for a decision. Registry proposals (`observatory_propose`, the `proposals`
table) had one decider, `review.py accept-proposal` at a terminal, one row at a time; measured on
one installed 0.29.1 on 2026-10-24, 112 were pending and 93 of them only appended `resources`,
which never replace what is recorded. Ledger conclusions waited by the hundred, a share of them
superseded readings of one day that `review.py reject-group` already knew how to fold. The operator
asked for the whole queue to be reviewed constantly, without a coding agent running, and for the
remainder to be resolved in one launch.

WHAT THIS MODULE IS. One evaluator for both queues. `review.py policy show` runs it as a dry run,
the tick's `review-policy` step runs it for real (`run`), `review.py batch prepare` turns what it
leaves into one file, and the doctor, the Health page and `review.queue_waiting` read its counts.
Nothing else decides, so the four cannot come to describe the queue four ways.

WHAT IT NEVER DOES. It never writes `owner = operator` or `decided_by = operator`: that authority
is a person's act at a terminal (`tools/review.py`), and the ledger refuses an automated writer
that tries (`store/ledger._check_owner`). A policy decision is `policy:<rule>@<hash12>`, journaled,
and reversible (`undo`). The rules are a closed vocabulary: a policy file naming a rule this module
does not ship is `invalid` and decides nothing, so editing the file cannot grant more than the
engine offers. And it does nothing at all until a person enables the exact rules shown
(`enable_document`, called only from `review.py policy enable` at a terminal); changing a rule
after that makes the policy `changed`, which again decides nothing.

The decision record: docs/design/REVIEW-POLICY.md.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paths

SCHEMA = "review-policy/1"
RUN_SCHEMA = "review-policy-run/1"
BATCH_SCHEMA = "review-batch/1"
FILE = "review-policy.json"

#: THE CLOSED VOCABULARY. A rule's `version` changes when what it decides changes, and the version
#: is part of the hash a person enabled, so an engine whose rule means something new stops applying
#: it until that person enables it again.
RULES: dict[str, dict] = {
    "registry.exact-duplicate": {
        "version": 1, "queue": "registry",
        "decides": "a pending proposal with the same target and patch as an older pending one is "
                   "superseded; the older one stays for its own decision"},
    "registry.resources-append": {
        "version": 1, "queue": "registry",
        "decides": "a resources-only proposal from an agent for a project in the registry is accepted: "
                   "the resources not yet recorded are appended to project_overrides.json"},
    "registry.aliases-clean": {
        "version": 1, "queue": "registry",
        "decides": "an aliases-only proposal for a target in the registry is accepted into identifiers.json "
                   "when no name conflicts with a curated or a measured one"},
    "ledger.fold-same-day": {
        "version": 1, "queue": "ledger",
        "decides": "superseded readings of one project's day leave the person's count (journal only); "
                   "the latest reading stays, and retention erases the rest as it would"},
}
#: When `review.queue_waiting` speaks: this many items wait for a person, or the oldest is this old.
REMIND_DEFAULT = {"count": 20, "age_days": 14}
#: The most decisions one run applies. The tick step is an optional tail step (tick_budget.py);
#: what is left is decided on the next tick.
RUN_LIMIT = 500
#: Who may have proposed something a rule accepts: an agent or a service identity, as the MCP wire
#: requires (`store/ledger._IDENTITY`). An owner that is not one is left for a person.
AGENT = re.compile(r"^(agent|service):[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
#: Fields a row of project_overrides.json carries about the decision rather than the project.
ROW_PROVENANCE = frozenset({"why", "proposed_by", "evidence"})


class PolicyError(ValueError):
    """The policy file cannot be read as review-policy/1."""


class Busy(RuntimeError):
    """Another review write holds the lock."""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canon(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# region the policy file

def policy_path() -> Path:
    return paths.config_file(FILE)


def default_rules() -> dict[str, dict]:
    return {rid: {"version": spec["version"], "on": True} for rid, spec in RULES.items()}


def default_document() -> dict:
    return {"schema": SCHEMA, "rules": default_rules(), "remind": dict(REMIND_DEFAULT),
            "enabled": None, "history": []}


def rules_hash(rules: dict) -> str:
    """The hash a person enables: every rule id, its version and whether it is on."""
    return _sha(_canon({"schema": SCHEMA, "rules": {rid: {"version": int(r["version"]), "on": bool(r["on"])}
                                                    for rid, r in sorted(rules.items())}}))


@dataclass
class Policy:
    #: off (no file, or not enabled), on, changed (the rules differ from what was enabled), invalid.
    state: str
    rules: dict[str, dict]
    hash: str
    remind: dict
    enabled: dict | None = None
    problem: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.state == "on"

    def rules_on(self) -> set[str]:
        """The rules this policy would apply, whether or not it is active."""
        if self.state == "invalid":
            return set()
        return {rid for rid, r in self.rules.items() if r.get("on") and rid in RULES}

    def decided_by(self, rule: str) -> str:
        return f"policy:{rule}@{self.hash.split(':', 1)[-1][:12]}"


def parse(doc) -> Policy:
    """A Policy from a document; raises PolicyError on anything that is not review-policy/1."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise PolicyError(f"not {SCHEMA}")
    rules = doc.get("rules")
    if not isinstance(rules, dict):
        raise PolicyError("`rules` must be an object of rule id -> {version, on}")
    notes: list[str] = []
    for rid, r in rules.items():
        if rid not in RULES:
            raise PolicyError(f"unknown rule {rid!r}: the rules are {', '.join(sorted(RULES))}")
        if not isinstance(r, dict) or set(r) - {"version", "on"} or not isinstance(r.get("on"), bool) \
                or isinstance(r.get("version"), bool) or not isinstance(r.get("version"), int):
            raise PolicyError(f"rule {rid!r} must be {{\"version\": <int>, \"on\": <bool>}}")
    remind = doc.get("remind", REMIND_DEFAULT)
    if not isinstance(remind, dict) or set(remind) - set(REMIND_DEFAULT) or any(
            isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in remind.values()):
        raise PolicyError("`remind` takes `count` and `age_days`, each a whole number of at least 1")
    remind = {**REMIND_DEFAULT, **remind}
    enabled = doc.get("enabled")
    if enabled is not None and not (isinstance(enabled, dict) and isinstance(enabled.get("hash"), str)):
        raise PolicyError("`enabled` must be null or the record of who enabled which hash")
    digest = rules_hash(rules)
    for rid in sorted(set(RULES) - set(rules)):
        notes.append(f"rule {rid} is new in this engine and is off until the policy is enabled again")
    for rid, r in sorted(rules.items()):
        if r["version"] != RULES[rid]["version"]:
            notes.append(f"rule {rid} is version {r['version']} in the file and {RULES[rid]['version']} "
                         f"in this engine: show the policy and enable it again")
    if enabled is None:
        state, problem = "off", "not enabled: `project-observatory full review policy enable` at a terminal"
    elif enabled["hash"] != digest:
        state, problem = "changed", ("the rules differ from the ones enabled; nothing is decided until "
                                     "`project-observatory full review policy enable` is run again")
    elif any(r["version"] != RULES[rid]["version"] for rid, r in rules.items()):
        state, problem = "changed", "a rule changed meaning in this engine; enable the policy again"
    else:
        state, problem = "on", ""
    return Policy(state, {rid: dict(r) for rid, r in rules.items()}, digest, remind, enabled, problem, notes)


def load(path: Path | None = None) -> Policy:
    """The policy as it stands. Absent is `off` with the default rules; broken is `invalid`."""
    path = path or policy_path()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        doc = default_document()
        pol = parse(doc)
        pol.problem = ("no policy file: nothing is decided automatically. `project-observatory full review "
                       "policy show`, then `policy enable` at a terminal")
        return pol
    except (OSError, ValueError) as exc:
        return Policy("invalid", {}, "", dict(REMIND_DEFAULT), None,
                      f"{FILE} cannot be read: {type(exc).__name__}")
    try:
        return parse(doc)
    except PolicyError as exc:
        remind = doc.get("remind") if isinstance(doc, dict) else None
        ok = isinstance(remind, dict) and all(isinstance(v, int) and not isinstance(v, bool) and v >= 1
                                              for v in remind.values()) and not set(remind) - set(REMIND_DEFAULT)
        return Policy("invalid", {}, "", {**REMIND_DEFAULT, **(remind if ok else {})}, None,
                      f"{FILE} is refused: {exc}. Nothing is decided until it is repaired")


def read_document(path: Path | None = None) -> dict:
    """The file for a WRITE (enable, disable): the defaults when absent; refused when broken, because
    rewriting a broken file would drop whatever a person meant by it."""
    path = path or policy_path()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default_document()
    except (OSError, ValueError) as exc:
        raise PolicyError(f"{FILE} cannot be read: {type(exc).__name__}") from None
    parse(doc)
    return doc


def enable_document(doc: dict, *, only: set[str] | None, at: str, user: str) -> dict:
    """The document a person's `policy enable` writes: every rule at this engine's version, on unless
    `only` names others, and the record of who enabled which hash. Called from a terminal only."""
    out = copy.deepcopy(doc)
    rules = {}
    for rid, spec in RULES.items():
        was = (out.get("rules") or {}).get(rid) or {}
        on = (rid in only) if only is not None else bool(was.get("on", True))
        rules[rid] = {"version": spec["version"], "on": on}
    out["schema"] = SCHEMA
    out["rules"] = rules
    out.setdefault("remind", dict(REMIND_DEFAULT))
    digest = rules_hash(rules)
    out["enabled"] = {"hash": digest, "at": at, "by": "operator at a terminal", "user": user}
    out.setdefault("history", []).append({"event": "enabled", "hash": digest, "at": at, "user": user})
    return out


def disable_document(doc: dict, *, at: str, user: str) -> dict:
    out = copy.deepcopy(doc)
    out["enabled"] = None
    out.setdefault("history", []).append({"event": "disabled", "hash": rules_hash(out.get("rules") or {}),
                                          "at": at, "user": user})
    return out

# endregion


# region the journal and the lock

def folder() -> Path:
    return paths.STATE / "review-policy"


def journal_path() -> Path:
    return folder() / "decisions.jsonl"


def receipt_path() -> Path:
    return paths.SCRATCH / "review-policy.json"


def read_journal() -> tuple[list[dict], set[str]]:
    """(every decision, the ids undone). A line that does not parse is skipped, never fatal."""
    decisions, undone = [], set()
    try:
        text = journal_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], set()
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if row.get("undone"):
            undone.add(str(row["undone"]))
        elif row.get("id"):
            decisions.append(row)
    return decisions, undone


def active_decisions() -> list[dict]:
    decisions, undone = read_journal()
    return [d for d in decisions if d["id"] not in undone]


def append_journal(rows: list[dict]) -> None:
    if not rows:
        return
    folder().mkdir(parents=True, exist_ok=True, mode=0o700)
    import osprivacy
    fd = osprivacy.open(journal_path(), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(_canon(row) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


@contextlib.contextmanager
def lock(wait: bool = False):
    """One review writer at a time: the tick's run, an undo, a batch, an accept at a terminal."""
    import oslocks
    folder().mkdir(parents=True, exist_ok=True, mode=0o700)
    import osprivacy
    fd = osprivacy.open(folder() / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            oslocks.flock(fd, oslocks.LOCK_EX | (0 if wait else oslocks.LOCK_NB))
        except BlockingIOError:
            raise Busy("another review write is running (the tick's review-policy step, an undo or a batch)") \
                from None
        try:
            yield
        finally:
            oslocks.flock(fd, oslocks.LOCK_UN)
    finally:
        os.close(fd)

# endregion


# region reading the queues

def pending_proposals(conn) -> list[dict]:
    out = []
    for r in conn.execute("SELECT id, target_id, patch_json, evidence_json, status, created_at FROM proposals"
                          " WHERE status = 'proposed' ORDER BY created_at, id"):
        r = dict(r)
        try:
            patch = json.loads(r["patch_json"] or "{}")
        except ValueError:
            patch = None
        try:
            stored = json.loads(r["evidence_json"] or "[]")
        except ValueError:
            stored = []
        owner = stored.get("owner") if isinstance(stored, dict) else None
        evidence = (stored.get("evidence") if isinstance(stored, dict) else stored) or []
        out.append({**r, "patch": patch if isinstance(patch, dict) else None, "owner": owner,
                    "evidence": evidence if isinstance(evidence, list) else []})
    return out


def proposed_conclusions(conn) -> list[dict]:
    """The current revision of every `proposed` ledger record that is not tombstoned: the digest's query."""
    return [dict(r) for r in conn.execute(
        "SELECT l.memory_id, l.project_id, l.kind, l.owner, l.created_at, l.revision, l.state,"
        "       l.evidence_json, substr(l.statement, 1, 200) AS gist"
        " FROM ledger l JOIN (SELECT memory_id, MAX(revision) rev FROM ledger GROUP BY memory_id) m"
        "   ON l.memory_id = m.memory_id AND l.revision = m.rev"
        " LEFT JOIN tombstones t ON t.memory_id = l.memory_id"
        " WHERE t.memory_id IS NULL AND l.state = 'proposed'"
        " ORDER BY l.created_at, l.memory_id")]


@dataclass
class Context:
    """What the rules read besides the queues: the registry and the two curation files."""
    projects: dict[str, dict]
    products: set[str]
    relations: list[dict]
    overrides: dict
    identifiers: dict | None
    identifiers_problem: str = ""
    degraded: list[str] = field(default_factory=list)


def context() -> Context:
    degraded: list[str] = []

    def registry(name: str, key: str) -> list:
        try:
            rows = json.loads((paths.REGISTRY / name).read_text(encoding="utf-8"))[key]
            return rows if isinstance(rows, list) else []
        except FileNotFoundError:
            degraded.append(f"registry/{name} is absent: run `project-observatory full emit`")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            degraded.append(f"registry/{name} unreadable: {type(exc).__name__}")
        return []

    projects = {p["id"]: p for p in registry("projects.json", "projects") if isinstance(p, dict) and p.get("id")}
    products = {p["id"] for p in registry("products.json", "products") if isinstance(p, dict) and p.get("id")}
    relations = [r for r in registry("relations.json", "relations") if isinstance(r, dict)]
    try:
        overrides = json.loads(paths.config_file("project_overrides.json").read_text(encoding="utf-8"))
        if not isinstance(overrides.get("projects"), dict):
            raise ValueError("no `projects` object")
    except FileNotFoundError:
        overrides = {"projects": {}}
    except (OSError, ValueError, AttributeError) as exc:
        degraded.append(f"config/project_overrides.json unreadable: {type(exc).__name__}; no resource is accepted")
        overrides = None
    import identifiers
    try:
        table, problem = identifiers.load_table(paths.config_file("identifiers.json")), ""
    except (ValueError, identifiers.AliasConflict) as exc:
        table, problem = None, f"config/identifiers.json cannot be written safely: {exc}"
    return Context(projects, products, relations, overrides, table, problem, degraded)

# endregion


# region the evaluator

@dataclass
class Decision:
    rule: str
    queue: str
    item: str
    action: str            # accept | supersede | fold
    reason: str
    evidence: list
    effect: dict
    created_at: str = ""
    target: str = ""


@dataclass
class Waiting:
    queue: str
    item: str
    reason: str
    created_at: str = ""
    target: str = ""
    kind: str = ""
    #: The rule that would decide it if it were on (None: no rule decides it).
    rule: str | None = None


@dataclass
class Plan:
    decisions: list[Decision] = field(default_factory=list)
    #: Items no rule decides: they wait for a person.
    waiting: list[Waiting] = field(default_factory=list)
    #: Items a rule that is off would decide.
    undecided: list[Waiting] = field(default_factory=list)
    deferred: int = 0
    retention: int = 0
    corroboration: int = 0
    folded: int = 0
    degraded: list[str] = field(default_factory=list)
    #: The curation documents as the decisions above leave them.
    overrides: dict | None = None
    identifiers: dict | None = None


def _measured(relations: list[dict]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """(case-folded measured name -> the projects it is measured on, project -> its products)."""
    import identifiers
    measured: dict[str, set[str]] = {}
    for name, (_via, owners) in identifiers.derived_names(relations).items():
        measured.setdefault(name.casefold(), set()).update(owners)
    member_of: dict[str, set[str]] = {}
    for r in relations:
        if r.get("type") == "part_of" and r.get("from") and r.get("to"):
            member_of.setdefault(r["from"], set()).add(r["to"])
    return measured, member_of


def evaluate(conn, rules_on: set[str], ctx: Context | None = None, *, limit: int = RUN_LIMIT,
             journal: list[dict] | None = None, on: str | None = None) -> Plan:
    """What `rules_on` decide over both queues as they stand, and what is left.

    The curation documents are copied and changed as each decision would change them, so the second
    of two proposals sees the first one's effect, exactly as the run applies them in order."""
    ctx = ctx or context()
    plan = Plan(degraded=list(ctx.degraded))
    plan.overrides = copy.deepcopy(ctx.overrides)
    plan.identifiers = copy.deepcopy(ctx.identifiers) if ctx.identifiers is not None else None
    on = on or now()
    import proposals as P
    measured, member_of = _measured(ctx.relations)
    seen: dict[tuple, str] = {}

    def room() -> bool:
        if len(plan.decisions) < limit:
            return True
        plan.deferred += 1
        return False

    def decide(rule: str, p: dict, action: str, reason: str, effect: dict) -> None:
        plan.decisions.append(Decision(rule, "registry", p["id"], action, reason, p["evidence"], effect,
                                       p["created_at"], p["target_id"]))

    def leave(p: dict, reason: str, rule: str | None = None) -> None:
        row = Waiting("registry", p["id"], reason, p["created_at"], p["target_id"],
                      ",".join(sorted(p["patch"] or {})) or "?", rule)
        (plan.undecided if rule else plan.waiting).append(row)

    for p in pending_proposals(conn):
        patch, target = p["patch"], p["target_id"]
        if patch is None:
            leave(p, "the patch is not a JSON object")
            continue
        key = (target, _canon(patch))
        first = seen.setdefault(key, p["id"])
        if first != p["id"]:
            if "registry.exact-duplicate" in rules_on:
                if room():
                    decide("registry.exact-duplicate", p, "supersede",
                           f"exact duplicate of {first}, which stays pending", {"of": first})
                continue
            leave(p, f"exact duplicate of {first}", "registry.exact-duplicate")
            continue
        refused = P.refusal(target, patch, p["evidence"])
        if refused:
            leave(p, f"cannot be applied: {refused}")
            continue
        fields = sorted(patch)
        if fields == ["resources"]:
            _resources(p, rules_on, ctx, plan, decide, leave, room, on)
        elif fields == ["aliases"]:
            _aliases(p, rules_on, ctx, plan, decide, leave, room, on, measured, member_of)
        else:
            leave(p, f"changes {', '.join(fields)}: what the project is, which a person decides")

    _ledger(conn, rules_on, plan, room, journal)
    return plan


def _owner_problem(p: dict) -> str:
    if not AGENT.match(str(p.get("owner") or "")):
        return f"proposed by {p.get('owner')!r}, which is not an agent identity (`agent:` or `service:`)"
    return ""


def _resources(p, rules_on, ctx, plan, decide, leave, room, on) -> None:
    rule, target = "registry.resources-append", p["target_id"]
    if not target.startswith("project:"):
        return leave(p, "resources are recorded for a project only")
    if target not in ctx.projects:
        return leave(p, f"{target} is not in the registry")
    problem = _owner_problem(p)
    if problem:
        return leave(p, problem)
    if plan.overrides is None:
        return leave(p, "config/project_overrides.json is unreadable")
    row = plan.overrides["projects"].get(target) or {}
    recorded = {(r.get("kind"), r.get("identifier")): r for r in row.get("resources") or [] if isinstance(r, dict)}
    added, skipped = [], []
    for r in p["patch"]["resources"]:
        k = (r["kind"], r["identifier"])
        if k in recorded:
            if all(recorded[k].get(f) == v for f, v in r.items()):
                skipped.append(list(k))
                continue
            changed = sorted(f for f, v in r.items() if recorded[k].get(f) != v)
            return leave(p, f"would change the recorded {k[0]} {k[1]} ({', '.join(changed)}); "
                            "appending never replaces, so a person decides")
        if any((a["kind"], a["identifier"]) == k for a in added):
            continue
        added.append(dict(r))
    if rule not in rules_on:
        return leave(p, f"{len(added)} resource(s) to append, {len(skipped)} already recorded", rule)
    if not room():
        return
    created = target not in plan.overrides["projects"]
    provenance = None
    import organizations
    new_row = copy.deepcopy(row)
    if added:
        new_row["resources"] = organizations.merge_resources(row.get("resources") or [], added)
    if created:
        provenance = {"why": f"policy:{rule}: resources an agent reported (OBS-227)",
                      "proposed_by": f"{p['id']} ({p['created_at']})", "evidence": p["evidence"]}
        new_row.update(provenance)
    if added or created:
        plan.overrides["projects"][target] = new_row
    reason = (f"appended {len(added)} resource(s)" + (f", {len(skipped)} already recorded" if skipped else "")
              if added else "every resource was already recorded; nothing appended")
    decide(rule, p, "accept", reason, {"file": "project_overrides.json", "target": target, "added": added,
                                       "skipped": skipped, "row_created": bool(created and added),
                                       "provenance": provenance if (created and added) else None})


def _aliases(p, rules_on, ctx, plan, decide, leave, room, on, measured, member_of) -> None:
    rule, target = "registry.aliases-clean", p["target_id"]
    import identifiers
    if target not in ctx.projects and target not in ctx.products:
        return leave(p, f"{target} is not in the registry")
    problem = _owner_problem(p)
    if problem:
        return leave(p, problem)
    if plan.identifiers is None:
        return leave(p, ctx.identifiers_problem or "config/identifiers.json cannot be read")
    for a in p["patch"]["aliases"]:
        owners = measured.get(a["id"].casefold())
        if owners:
            allowed = set(owners) | {prod for o in owners for prod in member_of.get(o, ())}
            if target not in allowed:
                return leave(p, f"{a['id']} is measured on {', '.join(sorted(owners))}; naming {target} "
                                "would contradict the measurement")
    before = set((plan.identifiers.get("aliases") or {}))
    provenance = {"why": f"policy:{rule}: no conflict with a curated or measured name (OBS-227)",
                  "proposed_by": f"{p['id']} ({p['created_at']})", "evidence": p["evidence"]}
    try:
        table = identifiers.merge_accepted(plan.identifiers, p["patch"]["aliases"], target=target,
                                           on=on[:10], provenance=provenance)
    except identifiers.AliasConflict as exc:
        return leave(p, f"conflict: {exc}")
    added = sorted(set(table.get("aliases") or {}) - before)
    if rule not in rules_on:
        return leave(p, f"{len(added)} alias(es) without conflict", rule)
    if not room():
        return
    plan.identifiers = table
    decide(rule, p, "accept", f"recorded {len(added)} alias(es)" if added else "every alias was already recorded",
           {"file": "identifiers.json", "target": target, "added": added, "why": provenance["why"]})


def _ledger(conn, rules_on, plan, room, journal) -> None:
    import estate
    rule = "ledger.fold-same-day"
    rows = proposed_conclusions(conn)
    by_id = {r["memory_id"]: r for r in rows}
    folded = {d["item"]: d.get("effect", {}).get("revision")
              for d in (active_decisions() if journal is None else journal) if d.get("action") == "fold"}
    members = set()
    for g in estate.fold_groups(rows):
        for mid in g["fold"]:
            members.add(mid)
            r = by_id[mid]
            day = str(r["created_at"] or "")[:10]
            if folded.get(mid) == r["revision"]:
                plan.folded += 1
                continue
            reason = f"superseded reading of {r['project_id']} on {day}; the latest, {g['keep']}, stays"
            if rule in rules_on:
                if room():
                    plan.decisions.append(Decision(rule, "ledger", mid, "fold", reason, [], {
                        "revision": r["revision"], "keep": g["keep"], "project": r["project_id"], "day": day},
                        r["created_at"], r["project_id"] or ""))
                continue
            plan.undecided.append(Waiting("ledger", mid, reason, r["created_at"], r["project_id"] or "",
                                          r["kind"], rule))
    for r in rows:
        if r["memory_id"] in members:
            continue
        if r["kind"] == "lesson":
            plan.corroboration += 1
            continue
        klass = estate.conclusion_class(r["evidence_json"])
        if klass == "routine":
            plan.retention += 1
            continue
        plan.waiting.append(Waiting("ledger", r["memory_id"],
                                    "rests on a structural change" if klass == "structural"
                                    else "rests on evidence the engine cannot classify",
                                    r["created_at"], r["project_id"] or "", r["kind"]))


def for_person(plan: Plan, policy: Policy) -> list[Waiting]:
    """What waits for a person: what no rule decides, what a rule that is off would decide, and, while
    the policy is not in force, what its rules would decide. An enabled policy's own decisions are the
    next run's, never a person's."""
    pending = [] if policy.active else [
        Waiting(d.queue, d.item, d.reason, d.created_at, d.target,
                "observation" if d.queue == "ledger" else "", d.rule) for d in plan.decisions]
    return plan.waiting + plan.undecided + pending

# endregion


# region applying, the receipt and undo

def _counts(items, key) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        out[getattr(it, key)] = out.get(getattr(it, key), 0) + 1
    return dict(sorted(out.items()))


def waiting_summary(items: list[Waiting]) -> dict:
    stamps = sorted(w.created_at for w in items if w.created_at)
    return {"total": len(items), "registry": sum(w.queue == "registry" for w in items),
            "ledger": sum(w.queue == "ledger" for w in items), "oldest": stamps[0] if stamps else None}


def run(conn, *, apply: bool = True, limit: int = RUN_LIMIT, at: str | None = None) -> dict:
    """The tick's step: evaluate, apply what an enabled policy decides, write the receipt.

    Never raises for the queue's sake: a store or file it cannot read is named in `degraded`. A
    held lock skips the run and says so (`skipped`)."""
    at = at or now()
    policy = load()
    receipt = {"schema": RUN_SCHEMA, "ran_at": at, "state": policy.state, "hash": policy.hash or None,
               "problem": policy.problem, "notes": policy.notes, "applied": {}, "would": {}, "deferred": 0,
               "waiting": waiting_summary([]), "retention": 0, "corroboration": 0, "folded": 0,
               "remind": policy.remind, "degraded": []}
    try:
        with lock():
            plan = evaluate(conn, policy.rules_on(), limit=limit, on=at)
            receipt["degraded"] = plan.degraded
            if apply and policy.active and plan.decisions:
                done = apply_plan(conn, plan, policy, at=at)
                receipt["applied"] = _counts(done, "rule")
                plan.folded += sum(d.action == "fold" for d in done)
            else:
                receipt["would"] = _counts(plan.decisions, "rule")
            receipt.update(deferred=plan.deferred, waiting=waiting_summary(for_person(plan, policy)),
                           retention=plan.retention, corroboration=plan.corroboration, folded=plan.folded)
    except Busy as exc:
        receipt["skipped"] = str(exc)
    except sqlite3.Error as exc:
        receipt["degraded"].append(f"the store could not be read: {type(exc).__name__}")
    write_receipt(receipt)
    return receipt


def write_receipt(receipt: dict) -> None:
    import atomic
    try:
        receipt_path().parent.mkdir(parents=True, exist_ok=True)
        atomic.write_json(receipt_path(), receipt)
    except OSError:
        pass


def read_receipt() -> dict | None:
    try:
        doc = json.loads(receipt_path().read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def apply_plan(conn, plan: Plan, policy: Policy, *, at: str) -> list[Decision]:
    """Write `plan`'s decisions: the curation files first, then the journal, then the statuses.

    A crash after the files and before the statuses leaves the proposals pending; the next run
    decides them again and finds every resource and alias already recorded, so nothing is added
    twice. The caller holds `lock()`."""
    import atomic
    registry_accepts = [d for d in plan.decisions if d.queue == "registry" and d.action == "accept"]
    if any(d.effect.get("file") == "project_overrides.json" and (d.effect.get("added") or d.effect.get("row_created"))
           for d in registry_accepts):
        atomic.write_json(paths.config_file("project_overrides.json"), plan.overrides)
    if any(d.effect.get("file") == "identifiers.json" and d.effect.get("added") for d in registry_accepts):
        atomic.write_json(paths.config_file("identifiers.json"), plan.identifiers)
    rows = [{"id": f"rpd:{uuid.uuid4().hex[:16]}", "at": at, "rule": d.rule, "policy": policy.hash,
             "decided_by": policy.decided_by(d.rule), "queue": d.queue, "item": d.item, "target": d.target,
             "action": d.action, "reason": d.reason, "evidence": d.evidence, "effect": d.effect}
            for d in plan.decisions]
    append_journal(rows)
    status = {"accept": "accepted", "supersede": "superseded"}
    with conn:
        for d in plan.decisions:
            if d.queue == "registry":
                conn.execute("UPDATE proposals SET status = ?, decided_by = ?, decided_note = ?, decided_at = ?"
                             " WHERE id = ? AND status = 'proposed'",
                             (status[d.action], policy.decided_by(d.rule), d.reason, at, d.item))
    return plan.decisions


def select(entries: list[dict], *, ids: set[str] | None = None, since: str | None = None) -> list[dict]:
    return [d for d in entries if (ids is not None and d["id"] in ids)
            or (since is not None and str(d.get("at") or "") >= since)]


def undo(conn, entries: list[dict], *, at: str | None = None) -> list[dict]:
    """Revert policy decisions, newest first. A proposal decided again since (by a person or a later
    run) is left as it is and named; a resource or alias changed since is kept and named. The caller
    holds `lock()`."""
    import atomic
    at = at or now()
    results, journal = [], []
    for d in sorted(entries, key=lambda e: (str(e.get("at") or ""), e["id"]), reverse=True):
        result = {"id": d["id"], "item": d["item"], "rule": d["rule"], "action": d["action"]}
        if d["action"] in ("accept", "supersede"):
            row = conn.execute("SELECT status, decided_by FROM proposals WHERE id = ?", (d["item"],)).fetchone()
            want = {"accept": "accepted", "supersede": "superseded"}[d["action"]]
            if row is None or row[0] != want or row[1] != d.get("decided_by"):
                result["result"] = "left: decided again since"
                results.append(result)
                continue
            kept: list[str] = []
            effect = d.get("effect") or {}
            if d["action"] == "accept" and effect.get("file") == "project_overrides.json" and \
                    (effect.get("added") or effect.get("row_created")):
                kept = _revert_resources(effect, atomic)
            elif d["action"] == "accept" and effect.get("file") == "identifiers.json" and effect.get("added"):
                kept = _revert_aliases(effect, atomic)
            with conn:
                conn.execute("UPDATE proposals SET status = 'proposed', decided_by = NULL, decided_note = NULL,"
                             " decided_at = NULL WHERE id = ? AND status = ? AND decided_by = ?",
                             (d["item"], want, d.get("decided_by")))
            result["result"] = "reverted" + (f"; kept, changed since: {', '.join(kept)}" if kept else "")
        else:
            result["result"] = "reverted"
        journal.append({"undone": d["id"], "at": at, "result": result["result"]})
        results.append(result)
    append_journal(journal)
    return results


def _revert_resources(effect: dict, atomic) -> list[str]:
    f = paths.config_file("project_overrides.json")
    doc = json.loads(f.read_text(encoding="utf-8"))
    row = doc["projects"].get(effect["target"])
    if not isinstance(row, dict):
        return []
    kept = []
    resources = list(row.get("resources") or [])
    for added in effect.get("added") or []:
        key = (added.get("kind"), added.get("identifier"))
        for r in resources:
            if (r.get("kind"), r.get("identifier")) == key:
                if r == added:
                    resources.remove(r)
                else:
                    kept.append(f"{key[0]} {key[1]}")
                break
    if resources:
        row["resources"] = resources
    else:
        row.pop("resources", None)
    prov = effect.get("provenance") or {}
    if effect.get("row_created") and not row.get("resources") and set(row) <= ROW_PROVENANCE \
            and row.get("why") == prov.get("why"):
        del doc["projects"][effect["target"]]
    else:
        doc["projects"][effect["target"]] = row
    atomic.write_json(f, doc)
    return kept


def _revert_aliases(effect: dict, atomic) -> list[str]:
    f = paths.config_file("identifiers.json")
    doc = json.loads(f.read_text(encoding="utf-8"))
    aliases = doc.get("aliases") or {}
    kept = []
    for name in effect.get("added") or []:
        row = aliases.get(name)
        if isinstance(row, dict) and row.get("resolves_to") == effect["target"] and row.get("why") == effect.get("why"):
            del aliases[name]
        elif row is not None:
            kept.append(name)
    atomic.write_json(f, doc)
    return kept

# endregion


# region the batch: the remainder, resolved in one launch

def item_hash(queue: str, row: dict | None) -> str | None:
    """What a batch item rested on, so `batch apply` refuses one that changed after preparation."""
    if row is None:
        return None
    if queue == "registry":
        return _sha(_canon(["registry", row["id"], row["status"], row["target_id"], row["patch_json"],
                            row["evidence_json"]]))
    return _sha(_canon(["ledger", row["memory_id"], int(row["revision"]), row["state"]]))


def current_row(conn, queue: str, item: str) -> dict | None:
    if queue == "registry":
        r = conn.execute("SELECT id, target_id, patch_json, evidence_json, status, created_at FROM proposals"
                         " WHERE id = ?", (item,)).fetchone()
        return dict(r) if r else None
    r = conn.execute(
        "SELECT l.memory_id, l.revision, l.state FROM ledger l"
        " WHERE l.memory_id = ? AND l.revision = (SELECT MAX(revision) FROM ledger WHERE memory_id = ?)"
        " AND NOT EXISTS (SELECT 1 FROM tombstones t WHERE t.memory_id = l.memory_id)", (item, item)).fetchone()
    return dict(r) if r else None


def _recommend_registry(w: Waiting, p: dict, ctx: Context) -> tuple[str, str, str]:
    """(decision, reason, basis) for a registry item a person must decide. Deterministic."""
    if w.rule:
        if w.rule == "registry.exact-duplicate":
            return "reject", f"{w.reason}: superseded by the older one", f"rule:{w.rule}"
        return "accept", f"the rule {w.rule} accepts it ({w.reason}); the policy is not in force", f"rule:{w.rule}"
    if w.reason.startswith("cannot be applied") or w.reason.endswith("is not in the registry") \
            or w.reason.startswith("the patch is not"):
        return "reject", w.reason, "heuristic:not-appliable"
    if w.reason.startswith("conflict:") or "would contradict the measurement" in w.reason:
        return "reject", f"accepting it would fail: {w.reason}", "heuristic:alias-conflict"
    patch = p.get("patch") or {}
    if set(patch) == {"description"}:
        current = (ctx.projects.get(p["target_id"]) or {}).get("description")
        if not current:
            return "accept", "the project has no description yet", "heuristic:first-description"
        return "keep", f"replaces the current description: {str(current)[:120]}", "heuristic:replaces-description"
    return "keep", w.reason, "heuristic:a-person-reads"


def prepare_batch(conn, *, include_routine: bool = False, at: str | None = None) -> dict:
    """Everything the policy leaves for a person, and the folded readings, each with a recommendation.

    The file is the one launch's input: an agent may edit `decision` and `reason`; `batch apply`
    applies it at a terminal, refusing any item whose `hash` no longer matches."""
    at = at or now()
    policy = load()
    ctx = context()
    # With the policy in force its rules' candidates are the next run's; without it every rule is
    # evaluated as off, so what each would decide arrives here with the rule that would decide it.
    plan = evaluate(conn, policy.rules_on() if policy.active else set(), ctx, limit=10**9, on=at)
    props = {p["id"]: p for p in pending_proposals(conn)}
    items = []
    for w in plan.waiting + plan.undecided:
        if w.queue == "registry":
            p = props.get(w.item)
            if p is None:
                continue
            decision, reason, basis = _recommend_registry(w, p, ctx)
            summary = _canon(p["patch"])[:300]
            evidence = p["evidence"]
        else:
            if w.rule == "ledger.fold-same-day":
                decision, reason, basis = "reject", w.reason, f"rule:{w.rule}"
            else:
                decision, reason, basis = "keep", f"{w.reason}: the engine cannot judge it; read it", \
                                          "heuristic:a-person-reads"
            summary, evidence = "", []
        items.append(_batch_item(conn, w, decision, reason, basis, summary, evidence))
    # The readings the policy folded are still `proposed`: the one launch can make them rejections.
    folded = [d for d in active_decisions() if d.get("action") == "fold"]
    held = {it["id"] for it in items}
    for d in folded:
        if d["item"] in held:
            continue
        w = Waiting("ledger", d["item"], d["reason"], "", d.get("target") or "", "observation", d["rule"])
        row = current_row(conn, "ledger", d["item"])
        if row is None or row["state"] != "proposed" or row["revision"] != (d.get("effect") or {}).get("revision"):
            continue
        items.append(_batch_item(conn, w, "reject", f"{d['reason']} (folded by {d['decided_by']})",
                                 f"rule:{d['rule']}", "", []))
    not_included = {"retention": plan.retention, "corroboration": plan.corroboration}
    if include_routine:
        for r in proposed_conclusions(conn):
            if r["kind"] == "lesson" or r["memory_id"] in {it["id"] for it in items}:
                continue
            import estate
            if estate.conclusion_class(r["evidence_json"]) == "routine":
                w = Waiting("ledger", r["memory_id"], "routine", r["created_at"], r["project_id"] or "", r["kind"])
                items.append(_batch_item(conn, w, "keep", "rests on the working tree's own rhythm; retention "
                                         "retires it", "heuristic:routine", "", []))
        not_included["retention"] = 0
    gists = {r["memory_id"]: r["gist"] for r in proposed_conclusions(conn)}
    for it in items:
        if it["queue"] == "ledger":
            it["summary"] = gists.get(it["id"], "")
    return {"schema": BATCH_SCHEMA, "prepared_at": at,
            "policy": {"state": policy.state, "hash": policy.hash or None},
            "how": "Edit `decision` (accept, reject or keep) and `reason` of any item; then, at a terminal, "
                   "`project-observatory full review batch apply FILE`. An item whose `hash` no longer matches "
                   "the queue is skipped.",
            "counts": {d: sum(it["decision"] == d for it in items) for d in ("accept", "reject", "keep")},
            "not_included": not_included, "items": items}


def _batch_item(conn, w: Waiting, decision: str, reason: str, basis: str, summary: str, evidence: list) -> dict:
    return {"id": w.item, "queue": w.queue, "kind": w.kind, "target": w.target, "created_at": w.created_at,
            "summary": summary, "recommended": decision, "decision": decision, "reason": reason,
            "basis": basis, "evidence": evidence, "hash": item_hash(w.queue, current_row(conn, w.queue, w.item))}


def check_batch(doc) -> list[str]:
    """Why a batch file cannot be applied at all; [] when its shape is right."""
    if not isinstance(doc, dict) or doc.get("schema") != BATCH_SCHEMA:
        return [f"not a {BATCH_SCHEMA} file"]
    items = doc.get("items")
    if not isinstance(items, list):
        return ["`items` must be a list"]
    problems = []
    for n, it in enumerate(items):
        if not isinstance(it, dict):
            problems.append(f"item {n} is not an object")
            continue
        if it.get("queue") not in ("registry", "ledger") or not isinstance(it.get("id"), str):
            problems.append(f"item {n}: `queue` must be registry or ledger and `id` a string")
        if it.get("decision") not in ("accept", "reject", "keep"):
            problems.append(f"item {n} ({it.get('id')}): `decision` must be accept, reject or keep")
        if not isinstance(it.get("reason"), str) or not it["reason"].strip():
            problems.append(f"item {n} ({it.get('id')}): a decision needs its `reason`")
        if not isinstance(it.get("hash"), str):
            problems.append(f"item {n} ({it.get('id')}): no `hash`; prepare the file again")
    return problems

# endregion


# region what the doctor, Health and the finding read

def summary(conn=None) -> dict:
    """The queue as a person faces it now, read-only: the policy, the last run, what waits."""
    policy = load()
    out = {"policy": {"state": policy.state, "hash": policy.hash or None, "problem": policy.problem,
                      "enabled_at": (policy.enabled or {}).get("at")},
           "remind": policy.remind, "last_run": None, "waiting": waiting_summary([]),
           "retention": 0, "corroboration": 0, "folded": 0, "degraded": []}
    receipt = read_receipt()
    if receipt:
        out["last_run"] = {k: receipt.get(k) for k in ("ran_at", "state", "applied", "would", "skipped")}
    own = conn is None
    if own:
        if not paths.DB.exists():
            out["degraded"].append("no local store")
            return out
        conn = sqlite3.connect(paths.sqlite_uri(paths.DB), uri=True)
        conn.row_factory = sqlite3.Row
    try:
        plan = evaluate(conn, policy.rules_on(), limit=10**9)
        out.update(waiting=waiting_summary(for_person(plan, policy)), retention=plan.retention,
                   corroboration=plan.corroboration, folded=plan.folded, degraded=plan.degraded)
    except sqlite3.Error as exc:
        out["degraded"].append(f"the store could not be read: {type(exc).__name__}")
    finally:
        if own:
            conn.close()
    return out


def status() -> dict:
    """`full doctor`'s and Health's `review`: never an exception."""
    try:
        return summary()
    except Exception as exc:                                                       # noqa: BLE001
        return {"state": "unknown", "reason": type(exc).__name__}


def age_days(stamp: str | None, at: datetime | None = None) -> int | None:
    try:
        made = datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(((at or datetime.now(timezone.utc)) - made) / timedelta(days=1))

# endregion
