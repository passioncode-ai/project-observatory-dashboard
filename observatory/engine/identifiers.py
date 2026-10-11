#!/usr/bin/env python3
"""One project under many names: curated aliases, `known_as` edges, and `resolve`.

Agents name the same thing differently. A store console knows an app by its
numeric id, a finance agent by a slug of its own, a hosting platform by an app
name, a bot by the namespace it files knowledge under. None of them is wrong,
and renaming any of them to the registry's id would break the system that owns
the name. So the registry keeps ONE canonical id (`project:` or `product:`) and
a table of the other names, each with the agent that owns it.

Three rules carry the design:

- **One alias, one target.** A name written twice with two targets — in the
  same file, or in two spellings that differ only in case — is a CONFLICT: both
  rows are withheld and the conflict is published, never resolved by "the last
  row wins". That is what a JSON object would do silently, which is why the file
  is read with a duplicate-key hook.
- **The operator's file is the decision; the emit is the measurement.** A
  curated alias that names a repository, deployment or domain the registry has
  MEASURED as belonging to another project contradicts the measurement and is a
  conflict too. Naming the PRODUCT the measured project belongs to does not.
- **`resolve` never guesses.** It answers a canonical id, an alias, or a name
  the registry already derives (`repository:`, a deployment id, `domain:`), by
  exact match only. Two candidates are `ambiguous`; a conflicted name is
  `conflict`; anything else is `not-found`. A near match is not offered: an
  agent that acts on the wrong project is worse off than one told "no".

The canonical id is always an Observatory id. Which project a folder or a
repository belongs to in ANOTHER system (a host's own project model) is that
system's decision; this module answers only what the registry holds.

This module does no I/O except reading the curated file it is handed.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

import credential_shape

SCHEMA_VERSION = 1
SRC = ["SRC-0016"]
#: Canonical namespaces. An alias may not use them: `project:x` as a name for
#: `project:y` would make the registry's own ids ambiguous.
CANONICAL = ("project:", "product:")
#: Names the emitter already derives, by the relation that carries them and the
#: side of that relation the name sits on. Answered without curation.
DERIVED = (("implemented_by", "to", "repository"),
           ("deployed_to", "to", "deployment"),
           ("public_domain_of", "from", "domain"))
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}:\S{1,240}\Z")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")
ROW_FIELDS = {"resolves_to", "owner", "source", "since"}
#: Fields an accepted proposal writes beside the row: provenance, never data.
PROVENANCE = {"why", "proposed_by", "evidence", "accepted_on"}
PROPOSAL_FIELDS = {"id", "owner", "source"}


class AliasConflict(ValueError):
    """An accepted alias would give an existing name a second target."""


def empty_document(obs_date: str) -> dict:
    return {"schema_version": SCHEMA_VERSION, "updated_on": obs_date, "note": NOTE,
            "aliases": [], "conflicts": [], "degraded": [],
            "totals": {"aliases": 0, "conflicts": 0, "targets": 0}, "source_refs": SRC}


NOTE = ("Other names of the registry's projects and products, each owned by the system that "
        "uses it. Curated by the operator in config/identifiers.json and published as `known_as` "
        "relations; a name with two targets is a conflict and is withheld, never resolved by the "
        "last row.")


def _shown(value: Any) -> str:
    return credential_shape.shown(value)


def id_problem(alias: Any) -> str:
    """Why `alias` cannot be a name, or "". Quoted only when it is safe to quote."""
    if not isinstance(alias, str):
        return "an alias id must be a string"
    if credential_shape.shaped(alias, uuids=False):
        return f"{_shown(alias)} looks like a credential, not a name"
    if alias.casefold().startswith(CANONICAL):
        return f"{alias} is in a canonical namespace ({', '.join(CANONICAL)}): it is an id, not an alias"
    if not _ID.match(alias):
        return (f"{_shown(alias)} is not `<system>:<value>` — a system prefix, a colon, and no "
                "whitespace (a `urn:` prefix is fine)")
    return ""


def alias_problem(row: Any) -> str:
    """The shape of one alias in a proposal patch: {id, owner, source}, or why not."""
    if not isinstance(row, dict):
        return "each alias must be an object {id, owner, source}"
    extra = sorted(set(row) - PROPOSAL_FIELDS)
    if extra:
        return f"an alias carries only id, owner and source; extra: {', '.join(extra)}"
    problem = id_problem(row.get("id"))
    if problem:
        return problem
    for field in ("owner", "source"):
        value = row.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 300:
            return f"an alias needs `{field}`: who owns the name and where it is used"
        if credential_shape.shaped(value):
            return f"`{field}` looks like a credential"
    return ""


def _read(path: Path) -> tuple[list[tuple[str, Any]] | None, dict, list[str]]:
    """(alias pairs in file order, the document, errors). Duplicate keys survive."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], {}, []
    except OSError as exc:
        return None, {}, [f"identifiers.json could not be read: {type(exc).__name__}"]
    pairs: list[tuple[str, Any]] = []

    def hook(items: list[tuple[str, Any]]) -> dict:
        return dict(items) | {"__pairs__": items}

    try:
        doc = json.loads(text, object_pairs_hook=hook)
    except ValueError as exc:
        return None, {}, [f"identifiers.json could not be read as JSON: {exc.msg} at line {exc.lineno}"]
    if not isinstance(doc, dict):
        return None, {}, ["identifiers.json must hold an object"]
    aliases = doc.get("aliases", {})
    if not isinstance(aliases, dict):
        return None, {}, ["identifiers.json: `aliases` must be an object of alias -> row"]
    pairs = list(aliases.get("__pairs__") or [])
    return pairs, doc, []


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items() if k != "__pairs__"}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def load_table(path: Path) -> dict:
    """The curated table for a WRITE: read with the duplicate-key hook, refused when a
    name is written twice. A plain `json.loads` keeps the last of two rows and the
    write would persist that silently, turning a conflict into no conflict."""
    pairs, raw, errors = _read(path)
    if pairs is None:
        raise ValueError("; ".join(errors))
    names = [a for a, _r in pairs]
    twice = sorted({a for a in names if names.count(a) > 1})
    if twice:
        raise AliasConflict(f"{', '.join(_shown(a) for a in twice)} written more than once in "
                            "config/identifiers.json; the operator resolves that before anything is accepted")
    return _plain(raw)


def _row_problems(alias: str, row: Any) -> list[str]:
    problem = id_problem(alias)
    if problem:
        return [problem]
    shown = _shown(alias)
    if not isinstance(row, dict):
        return [f"{shown}: a row must be an object"]
    row = {k: v for k, v in row.items() if k != "__pairs__"}
    out = []
    extra = sorted(set(row) - ROW_FIELDS - PROVENANCE)
    if extra:
        out.append(f"{shown}: unknown field(s) {', '.join(extra)}")
    missing = sorted(f for f in ROW_FIELDS if not row.get(f))
    if missing:
        out.append(f"{shown}: missing {', '.join(missing)}")
    target = row.get("resolves_to")
    if target and not (isinstance(target, str) and target.startswith(CANONICAL)):
        out.append(f"{shown}: resolves_to must be a project: or product: id, not {_shown(target)}")
    since = row.get("since")
    if since and not (isinstance(since, str) and _DATE.match(since)):
        out.append(f"{shown}: since must be a date, YYYY-MM-DD")
    return out


def derived_names(relations: Iterable[dict]) -> dict[str, tuple[str, set[str]]]:
    """name -> (via, projects that relation ties it to)."""
    out: dict[str, tuple[str, set[str]]] = {}
    for r in relations:
        for typ, side, via in DERIVED:
            if r.get("type") != typ:
                continue
            name, project = (r["to"], r["from"]) if side == "to" else (r["from"], r["to"])
            if not str(project).startswith("project:"):
                continue
            out.setdefault(name, (via, set()))[1].add(project)
    return out


def _products_of(relations: Iterable[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in relations:
        if r.get("type") == "part_of":
            out.setdefault(r["from"], []).append({"id": r["to"], "role": r.get("role")})
    return {k: sorted(v, key=lambda x: x["id"]) for k, v in out.items()}


def document(projects: list[dict], products: list[dict], relations: list[dict], obs_date: str,
             *, path: Path) -> tuple[dict, list[dict], list[str]]:
    """(registry/identifiers.json document, known_as edges, shape errors).

    A shape error refuses the WHOLE file, as for products.json: publishing half of
    an operator's table is a table nobody wrote. A target the registry does not
    hold today is dropped and named in `degraded`, because a renamed project must
    not stop the tick. A conflict is published and its rows withheld."""
    doc = empty_document(obs_date)
    pairs, raw, errors = _read(path)
    if pairs is None:
        return doc, [], errors
    for alias, row in pairs:
        errors += _row_problems(alias, row)
    if errors:
        return doc, [], errors

    targets = {p["id"] for p in projects} | {p["id"] for p in products}
    measured = derived_names(relations)
    # A measured name is compared without case: `domain:Alpha.example` measured on one
    # project must stop `domain:alpha.example` being published for another.
    measured_folded: dict[str, tuple[str, set[str]]] = {}
    for name, (via, owners) in measured.items():
        measured_folded.setdefault(name.casefold(), (via, set()))[1].update(owners)
    member_of = _products_of(relations)
    # Group every spelling by its case-folded form: one name, however it is typed.
    groups: dict[str, list[tuple[str, dict]]] = {}
    for alias, row in pairs:
        clean = {k: v for k, v in row.items() if k != "__pairs__"}
        groups.setdefault(alias.casefold(), []).append((alias, clean))

    published, conflicts = [], []
    for _key, rows in sorted(groups.items()):
        claimed = sorted({r["resolves_to"] for _a, r in rows})
        spellings = sorted({a for a, _r in rows})
        if len(claimed) > 1:
            conflicts.append({"id": spellings[0], "spellings": spellings,
                              "claims": [{"resolves_to": r["resolves_to"], "owner": r["owner"]}
                                         for _a, r in rows],
                              "reason": "one name, several targets in config/identifiers.json"})
            continue
        if len(rows) > 1:
            doc["degraded"].append({"source": "config/identifiers.json",
                                    "reason": f"{spellings[0]} is written {len(rows)} times; published once"})
        alias, row = rows[0]
        target = row["resolves_to"]
        if target not in targets:
            doc["degraded"].append({"source": "config/identifiers.json",
                                    "reason": f"{alias}: {target} is not in this registry; the alias is "
                                              "withheld until it is"})
            continue
        hit = measured_folded.get(alias.casefold())
        if hit:
            via, owners = hit
            allowed = set(owners) | {p["id"] for o in owners for p in member_of.get(o, [])}
            if target not in allowed:
                conflicts.append({"id": alias, "spellings": [alias],
                                  "claims": [{"resolves_to": target, "owner": row["owner"]}]
                                  + [{"resolves_to": o, "owner": f"measured:{via}"} for o in sorted(owners)],
                                  "reason": f"the curated alias contradicts the measured {via}"})
                continue
        published.append({"id": alias, "resolves_to": target, "owner": row["owner"],
                          "source": row["source"], "since": row["since"], "source_refs": SRC})

    edges = []
    for a in published:
        edges.append({"id": edge_id(a["resolves_to"], a["id"]),
                      "type": "known_as", "from": a["resolves_to"], "to": a["id"],
                      "owner": a["owner"], "source_refs": SRC})
    doc["aliases"] = published
    doc["conflicts"] = conflicts
    doc["totals"] = {"aliases": len(published), "conflicts": len(conflicts),
                     "targets": len({a["resolves_to"] for a in published})}
    return doc, edges, []


def edge_id(target: str, alias: str) -> str:
    """Readable AND collision-safe: `a_b` and `a-b` slug alike, so the exact alias's
    hash rides beside the slug — two names never share one edge id."""
    digest = hashlib.sha256(alias.encode("utf-8")).hexdigest()[:8]
    return f"relation:{_slug(target)}:known-as:{_slug(alias)}-{digest}"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9.-]+", "-", value.split(":", 1)[-1].lower() if value.startswith(CANONICAL)
                  else value.lower()).strip("-")


def merge_accepted(doc: dict, rows: list[dict], *, target: str, on: str, provenance: dict) -> dict:
    """The curated file with accepted aliases added. Refuses a second target."""
    out = copy.deepcopy(doc) if doc else {"schema_version": SCHEMA_VERSION, "aliases": {}}
    out.setdefault("schema_version", SCHEMA_VERSION)
    aliases = out.setdefault("aliases", {})
    folded = {k.casefold(): k for k in aliases}
    for r in rows:
        existing = folded.get(r["id"].casefold())
        if existing and aliases[existing].get("resolves_to") != target:
            raise AliasConflict(f"{existing} already names {aliases[existing].get('resolves_to')}; "
                                f"giving it {target} too would make it ambiguous")
        if existing:
            # Same name, same target: the row is already there. It is never rewritten —
            # owner, source, since and provenance stay what the first writer recorded,
            # so an accepted proposal cannot take over an operator-curated row.
            continue
        aliases[r["id"]] = {"resolves_to": target, "owner": r["owner"], "source": r["source"], "since": on,
                            **{k: v for k, v in provenance.items() if k in PROVENANCE}}
    return out


class Registry:
    """The four registry documents `resolve` reads, indexed once."""

    def __init__(self, projects: list[dict], products: list[dict], relations: list[dict],
                 identifiers_doc: dict, degraded: list[dict] | None = None) -> None:
        self.projects = {p["id"]: p for p in projects}
        self.products = {p["id"]: p for p in products}
        self.relations = relations
        self.aliases = {a["id"]: a for a in identifiers_doc.get("aliases") or []}
        self.folded = {k.casefold(): k for k in self.aliases}
        self.conflicts = {}
        for c in identifiers_doc.get("conflicts") or []:
            for s in c.get("spellings") or [c["id"]]:
                self.conflicts[s] = c
        self.measured = derived_names(relations)
        self.member_of = _products_of(relations)
        self.degraded = list(degraded or [])
        self.by_project: dict[str, dict[str, set[str]]] = {}
        for r in relations:
            t = r.get("type")
            if t == "implemented_by":
                self._add(r["from"], "repositories", r["to"])
            elif t == "deployed_to":
                self._add(r["from"], "deployments", r["to"])
            elif t == "public_domain_of":
                self._add(r["to"], "domains", r["from"].split(":", 1)[-1])

    def _add(self, project: str, key: str, value: str) -> None:
        self.by_project.setdefault(project, {}).setdefault(key, set()).add(value)

    def member(self, project: str, role: str | None) -> dict:
        got = self.by_project.get(project, {})
        return {"project": project, "role": role,
                "repositories": sorted(got.get("repositories", ())),
                "deployments": sorted(got.get("deployments", ())),
                "domains": sorted(got.get("domains", ()))}

    def aliases_of(self, canonical: str) -> list[dict]:
        return sorted(({"id": a["id"], "owner": a["owner"]} for a in self.aliases.values()
                       if a["resolves_to"] == canonical), key=lambda x: x["id"])


def _refusal(code: str, query: str, reg: Registry, **extra: Any) -> dict:
    hints = {"not-found": "names are matched exactly; estate.survey lists the registry's ids",
             "ambiguous": "the name is measured on several projects; ask by one of their ids",
             "conflict": "config/identifiers.json gives this name several targets; the operator decides"}
    return {"error": code, "query": _shown(query), "hint": hints[code], **extra,
            "degraded": list(reg.degraded)}


def resolve(query: str, reg: Registry) -> dict:
    """Any name -> the canonical project or product it names, or a typed refusal."""
    # The published input schema requires `^\\S+$`; the tool agrees and refuses
    # surrounding whitespace rather than quietly trimming it.
    if not isinstance(query, str) or not query or query != query.strip() or any(c.isspace() for c in query):
        return _refusal("not-found", str(query), reg)
    if query in reg.conflicts:
        c = reg.conflicts[query]
        return _refusal("conflict", query, reg, candidates=sorted({x["resolves_to"] for x in c["claims"]}))
    if query in reg.projects or query in reg.products:
        canonical, via = query, "canonical"
    elif query in reg.aliases:
        canonical, via = reg.aliases[query]["resolves_to"], "alias"
    elif (query.casefold() in reg.folded and not query.startswith(("repository:", "domain:"))
          and not query.casefold().startswith(CANONICAL)):
        # One name, however it is typed — but only for curated names. A measured
        # repository or domain keeps the case the registry wrote.
        canonical, via = reg.aliases[reg.folded[query.casefold()]]["resolves_to"], "alias"
    elif query in reg.measured:
        via, owners = reg.measured[query]
        if len(owners) > 1:
            return _refusal("ambiguous", query, reg, candidates=sorted(owners))
        canonical = next(iter(owners))
    else:
        return _refusal("not-found", query, reg)
    out: dict[str, Any] = {"canonical": canonical, "matched": {"query": _shown(query), "via": via},
                           "aliases": reg.aliases_of(canonical), "degraded": list(reg.degraded)}
    if canonical in reg.products:
        p = reg.products[canonical]
        out.update(kind="product", name=p.get("name"), domains=sorted(p.get("domains") or []),
                   members=[reg.member(m["project"], m.get("role")) for m in p.get("members") or []])
        if p.get("kind") == "suggested":
            out["degraded"].append({"source": "registry/products.json",
                                    "reason": "a suggested grouping, inferred from a shared domain — "
                                              "not the operator's decision"})
    else:
        p = reg.projects.get(canonical, {})
        out.update(kind="project", name=p.get("name"), domains=[],
                   members=[reg.member(canonical, None)],
                   products=reg.member_of.get(canonical, []))
    return out
