"""embedding-policy/1: which memory text may leave the machine for an embedding model.

PB-137 N-002, operator decision D-3 (2026-10-03): embeddings are computed by a local
model by default; a remote model is used for a project only after an explicit,
recorded consent that names the provider; `confidential` text (the plan's `private`
and `secret-adjacent`) never leaves, consent or not. The full decision, with the
reasons for each rule, is docs/design/EMBEDDING-POLICY.md; the accepted cases are
tests/embedding_policy_cases.json and the published shape is
defaults/embedding-policy.schema.json.

This module DECIDES and never sends. It answers `remote` or `local` with a stable
reason code; the callers that embed (the indexer, the query path, the worker —
N-003) ask it before they read a key, spend a budget or open a connection.

Three properties carry the design:

- **Deny by default.** No policy file, an unknown classification, a record with no
  project, a global scope, an untrusted query context: each answers `local`. Only one
  path answers `remote`, and it needs every condition at once.
- **Authority is not an argument.** A query's classification counts only when the
  server derived it (`binding`, `operator-cli`). What a caller writes into a tool
  argument is `caller-argument`, and that authorizes nothing.
- **A broken policy is refused whole.** `parse` raises `PolicyError` on any defect;
  it never returns the part it could read. A caller that gets the error treats the
  workspace as consenting to nothing and says so in `degraded`.
"""
from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from store import ledger

SCHEMA = "embedding-policy/1"
CLASSES = ("public", "project-internal", "confidential")
EXPORTABLE = frozenset({"public", "project-internal"})
#: The accepted plan (§2, §6) names data classes `private` / `secret-adjacent` /
#: `internal`; the store keeps `classification` (plan §13: one column, not two).
ALIASES = {"private": "confidential", "secret-adjacent": "confidential",
           "internal": "project-internal"}
REMOTE_PROVIDERS = frozenset({"openai"})
LOCAL_PROVIDER = "local"
TRUSTED_AUTHORITIES = frozenset({"binding", "operator-cli"})
#: Work in progress never leaves: a checkpoint is written after every step and a
#: handoff pack carries the constraints of the next one; a refused step result is
#: a checkpoint's content kept as an episode.
WORK_IN_PROGRESS_KINDS = frozenset(ledger.WORKFLOW_KINDS) | {"step_result"}
PROJECT_ID = re.compile(r"^project:[a-z0-9][a-z0-9._-]{0,127}$")
CONSENT_VIAS = frozenset({"terminal"})
#: RFC 3339 with seconds and a zone — the same pattern the published schema carries,
#: so a validator without a format checker refuses what this module refuses.
TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$")

_TOP = {"schema", "policyRevision", "projects"}
_REMOTE = {"provider", "model", "classes", "consent", "revokedAt"}
_CONSENT = {"statement", "by", "at", "via"}


class PolicyError(ValueError):
    """The policy document is not a valid embedding-policy/1; nothing in it applies."""


@dataclass(frozen=True)
class Consent:
    provider: str
    model: str
    classes: frozenset
    statement: str
    by: str
    at: str
    revoked_at: str | None


@dataclass(frozen=True)
class Policy:
    revision: int
    projects: Mapping[str, Consent] = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    route: str                      # "remote" | "local"
    reason: str                     # a stable code, see docs/design/EMBEDDING-POLICY.md
    policy_revision: int
    provider: str | None = None     # set only on a remote verdict
    model: str | None = None

    @property
    def remote(self) -> bool:
        return self.route == "remote"


def empty() -> Policy:
    """The policy of a workspace that has none: every project local. A workspace
    that embedded through OpenAI before this contract existed starts here too —
    an embedding key in the configuration is not a consent."""
    return Policy(revision=0, projects={})


def canonical_classification(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = ALIASES.get(value, value)
    return value if value in CLASSES else None


def _timestamp(value: Any, where: str) -> str:
    if not isinstance(value, str) or not TIMESTAMP.match(value):
        raise PolicyError(f"{where} must be an RFC 3339 timestamp with seconds and a zone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PolicyError(f"{where} must be an RFC 3339 timestamp") from None
    if parsed.tzinfo is None:
        raise PolicyError(f"{where} must carry a time zone")
    return value


def _text(value: Any, where: str, limit: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise PolicyError(f"{where} must be non-empty text of at most {limit} characters")
    return value


def _exact_keys(obj: Any, allowed: set, where: str) -> Mapping:
    if not isinstance(obj, dict):
        raise PolicyError(f"{where} must be an object")
    extra = set(obj) - allowed
    missing = allowed - set(obj)
    if extra:
        raise PolicyError(f"{where} has unknown field(s) {sorted(extra)}")
    if missing:
        raise PolicyError(f"{where} lacks field(s) {sorted(missing)}")
    return obj


def _consent(project: str, entry: Any) -> Consent:
    where = f"projects[{project}]"
    entry = _exact_keys(entry, {"remote"}, where)
    remote = _exact_keys(entry["remote"], _REMOTE, f"{where}.remote")
    provider = remote["provider"]
    if provider not in REMOTE_PROVIDERS:
        raise PolicyError(f"{where}.remote.provider must be one of {sorted(REMOTE_PROVIDERS)}")
    model = _text(remote["model"], f"{where}.remote.model", 200)
    classes = remote["classes"]
    if (not isinstance(classes, list) or not classes
            or len(set(classes)) != len(classes)
            or any(c not in EXPORTABLE for c in classes)):
        raise PolicyError(f"{where}.remote.classes must list distinct classes from "
                          f"{sorted(EXPORTABLE)}; confidential text never leaves")
    consent = _exact_keys(remote["consent"], _CONSENT, f"{where}.remote.consent")
    statement = _text(consent["statement"], f"{where}.remote.consent.statement")
    if provider.lower() not in statement.lower():
        raise PolicyError(f"{where}.remote.consent.statement must name the provider "
                          f"({provider}): a consent says where the texts go")
    by = _text(consent["by"], f"{where}.remote.consent.by", 200)
    if consent["via"] not in CONSENT_VIAS:
        raise PolicyError(f"{where}.remote.consent.via must be one of {sorted(CONSENT_VIAS)}: "
                          f"a consent is given by a person at a terminal, never through a tool call")
    at = _timestamp(consent["at"], f"{where}.remote.consent.at")
    revoked = remote["revokedAt"]
    if revoked is not None:
        revoked = _timestamp(revoked, f"{where}.remote.revokedAt")
    return Consent(provider=provider, model=model, classes=frozenset(classes),
                   statement=statement, by=by, at=at, revoked_at=revoked)


def parse(doc: Any) -> Policy:
    """Validate a whole embedding-policy/1 document, or raise PolicyError."""
    doc = _exact_keys(doc, _TOP, "policy")
    if doc["schema"] != SCHEMA:
        raise PolicyError(f"policy.schema must be {SCHEMA!r}")
    revision = doc["policyRevision"]
    if type(revision) is not int or revision < 1:
        raise PolicyError("policy.policyRevision must be an integer of at least 1")
    projects = doc["projects"]
    if not isinstance(projects, dict):
        raise PolicyError("policy.projects must be an object keyed by project id")
    parsed = {}
    for project, entry in projects.items():
        if not isinstance(project, str) or not PROJECT_ID.match(project):
            raise PolicyError(f"policy.projects key {project!r} is not a project id "
                              f"(project:<slug>); a consent names exactly one project")
        parsed[project] = _consent(project, entry)
    return Policy(revision=revision, projects=parsed)


def load(path: pathlib.Path) -> Policy:
    """The workspace's policy. A missing file is the empty policy; an unreadable or
    invalid one raises PolicyError — never a partial policy."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return empty()
    except OSError as exc:
        raise PolicyError(f"the policy file cannot be read ({type(exc).__name__})") from None
    try:
        doc = json.loads(raw)
    except ValueError:
        raise PolicyError("the policy file is not JSON") from None
    return parse(doc)


def _local(policy: Policy, reason: str) -> Verdict:
    return Verdict(route="local", reason=reason, policy_revision=policy.revision)


def _decide(policy: Policy, project: Any, classification: Any, configured: Any) -> Verdict:
    if not isinstance(configured, Mapping) or not isinstance(configured.get("provider"), str):
        return _local(policy, "no-configured-model")
    if configured["provider"] == LOCAL_PROVIDER:
        return _local(policy, "local-model")
    cls = canonical_classification(classification)
    if cls is None:
        return _local(policy, "unknown-classification")
    if cls == "confidential":
        return _local(policy, "confidential")
    if not isinstance(project, str) or not PROJECT_ID.match(project):
        return _local(policy, "no-project")
    consent = policy.projects.get(project)
    if consent is None:
        return _local(policy, "no-consent")
    if consent.revoked_at is not None:
        return _local(policy, "consent-revoked")
    if (configured.get("provider") != consent.provider
            or configured.get("model") != consent.model):
        return _local(policy, "provider-mismatch")
    if cls not in consent.classes:
        return _local(policy, "class-not-consented")
    return Verdict(route="remote", reason="remote-consented", policy_revision=policy.revision,
                   provider=consent.provider, model=consent.model)


def for_record(policy: Policy, record: Any, *, configured: Any) -> Verdict:
    """May this stored memory record be embedded by the configured model remotely?"""
    if not isinstance(record, Mapping):
        return _local(policy, "unknown-classification")
    if record.get("scope") == "global":
        return _local(policy, "global-context")
    if record.get("scope") == "agent-private":
        return _local(policy, "agent-private-scope")
    if record.get("kind") in WORK_IN_PROGRESS_KINDS:
        return _local(policy, "workflow-kind")
    return _decide(policy, record.get("project_id"), record.get("classification"), configured)


def for_query(policy: Policy, context: Any, *, configured: Any) -> Verdict:
    """May this query's text be embedded by the configured model remotely?

    `context` is built by the server: `authority` says where its classification came
    from. Only `binding` (the caller's authenticated binding) and `operator-cli` (a
    person at the operator's terminal) are trusted; anything a tool argument claims is
    `caller-argument` and decides nothing."""
    if not isinstance(context, Mapping):
        return _local(policy, "untrusted-authority")
    if context.get("authority") not in TRUSTED_AUTHORITIES:
        return _local(policy, "untrusted-authority")
    return _decide(policy, context.get("project_id"), context.get("classification"), configured)
