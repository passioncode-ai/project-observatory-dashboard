# Identity: how projects and repositories keep their ids

Status: implemented in 0.3.0 (`observatory/engine/identity_map.py`, resolved in
`collectors/emit_registry.py`, read by `identity.project_id`). This document is the contract that
code comments point to.

## The problem

Ids are derived from names that change:

| Entity | Id today | Derived from | Changes when |
|---|---|---|---|
| project anchored by a wiki folder | `project:<slug(folder)>` | the wiki folder name | the folder is renamed |
| project anchored by an organisation | `project:<slug(owner)>` | the GitHub owner | the repositories move to another owner |
| project anchored by one repository | `project:<slug(owner-name)>` | the repository's owner/name | the repository is renamed or transferred |
| local project (no remote) | `project:local-<folder>` | the folder name | the folder is renamed |
| repository | `repository:<owner/name>` | owner/name | rename or transfer |

Nothing records which old id became which new one, except the single computed case of a local
folder that gains a remote or a wiki note (`identity.former_ids`). Events, ledger rows, weekly
rollups, metrics and curated files keep the old id, so a rename splits a project's history in two
and orphans its curation. `identity_overrides.json` can pin an id by hand. It is a flat
key-to-slug map, and nobody remembers to edit it before renaming.

## The contract

1. **An id is minted once and never reused.** The first time a project is seen, it gets
   `project:<slug(key)>`, or a numeric suffix if that id was ever used before.
2. **A rename keeps the id.** A project whose merge key changed keeps the id recorded for it when
   exactly one known project shares a *strong anchor* with it:
   - a repository it is implemented by (`owner/name`, or a former `owner/name` of that repository);
   - the root commit of a local Git history with no remote;
   - the absolute path of a local checkout.

   A wiki folder name, an organisation name or a local folder name alone is a *weak anchor*. It
   identifies the key, not a rename.
3. **Ambiguity is never resolved by guessing.** If a project shares strong anchors with two
   recorded projects, or two current projects claim one recorded id, a new id is minted and an
   `identity.ambiguous` finding names the candidates. The operator decides with
   `identity_overrides.json`, which always wins.
4. **A project that disappears is retired, not deleted.** Its id is marked retired with the date it
   was last seen. It is never minted again, and readers can still show its history.
5. **Repositories remember former names.** A transfer or rename the merge step followed
   (`transfers_followed`) is recorded as a former `owner/name` of the new repository id.
6. **History follows the id through readers, not rewrites.** Stored rows keep the id they were
   written with. Every reader that already widens queries with `identity.ids_for` also widens them
   with the recorded aliases, so a renamed project shows one timeline, one rollup and its notes.
   Stored data is not rewritten in place: an append-only store stays auditable.

## The map

`registry/identity.json`, written by the emit step and committed with the rest of the registry:

```json
{
  "schema_version": 1,
  "projects": {
    "project:example-app": {
      "minted_on": "2026-09-23",
      "keys": ["example-app", "old-example-app"],
      "anchors": {"repositories": ["example-org/example-app"], "root_commits": [], "checkouts": ["/srv/projects/example-app"]},
      "last_seen_on": "2026-09-30",
      "retired_on": null
    }
  },
  "repositories": {
    "repository:example-org/example-app": {"former": ["example-org/example-app-old"]}
  }
}
```

`keys` are aliases: every merge key that ever resolved to this id. `anchors` hold only strong
evidence and are unions over time. Names never appear as anchors.

## Resolution, per emit run

For each project in the merged model:

1. `identity_overrides.json` pins the id when it names the key.
2. Otherwise, if the map already lists the key, that id is used.
3. Otherwise, the project's strong anchors are compared with every non-retired entry. Exactly one
   match means the id is reused, the key is appended and a `renamed` delta is recorded. Two or more
   matches produce a new id and an `identity.ambiguous` finding.
4. Otherwise, a new id is minted.

Resolution runs in two passes, so the order of keys cannot decide a match: overrides and keys the map
already knows claim their ids first, and only then are new keys compared by anchors against the
recorded projects nobody has claimed. If two current projects would take one id (for example an
override pointing at an id another project holds), the first in key order keeps it, the other gets
a new id, and both cases produce an `identity.ambiguous` finding. An entry no project resolved to gets `retired_on`, unless the run was degraded
for the source that anchors it (a partial scan must not retire anything).

`merge.py` also asks `identity.project_id` for a key while matching domain claims. For a project
renamed in this very run it still sees the name-derived id; from the next run the map knows the key.
The emitted registry is correct in the same run.

## Migration

The first run with the map seeds it from the current registry. Every existing id and key is
recorded as is, so no id changes on upgrade. `identity_overrides.json` keeps working and still takes
precedence. Existing `former_ids` (local folder to published) remain an additional alias source.

## Other names: aliases and `project.resolve` (OBS-65)

Status: implemented in `observatory/engine/identifiers.py`, emitted by
`collectors/emit_registry.py`, served as `project.resolve` (tool `observatory_resolve`).

The sections above keep ONE project's id stable across its own renames. This one answers a
different question: what the same project or product is called in OTHER systems. A store console
knows an app by a numeric id, a finance agent by a slug of its own, a hosting platform by an app
name, a bot by the namespace it files knowledge under. None of them is wrong, and renaming any of
them to the registry's id would break the system that owns the name.

**The table.** `config/identifiers.json` in the workspace, curated:

```json
{"schema_version": 1, "aliases": {
  "store:app:1000000001": {"resolves_to": "project:alpha-app", "owner": "store-agent",
                           "source": "store console", "since": "2026-10-11"},
  "urn:finance:project:alpha": {"resolves_to": "product:alpha", "owner": "finance-agent",
                                "source": "finance slug", "since": "2026-10-11"}}}
```

An alias is `<system>:<value>` with no whitespace (a `urn:` prefix is fine), never in a canonical
namespace (`project:`, `product:`), never credential-shaped. It names exactly one `project:` or
`product:` id and the agent or system that owns the name. A product groups several projects
(`config/products.json`, `part_of` edges); several repositories implement one project
(`implemented_by`). An alias may name either level.

**The rules.**

1. **One alias, one target.** A name written twice with two targets, in the same file or in two
   spellings that differ only in case, is a CONFLICT: both rows are withheld and
   `registry/identifiers.json` lists the conflict. The file is read with a duplicate-key hook,
   because a plain JSON object would silently keep the last row.
2. **The table is the decision, the emit is the measurement.** A curated alias that names a
   repository, deployment or domain the registry has measured on another project is a conflict.
   Naming the product the measured project belongs to is not.
3. **A malformed file is refused whole**, as `products.json` is; a target the registry does not
   hold today is withheld and named in `degraded`, so a renamed project does not stop the tick.
4. **`known_as` edges are rebuilt on every emit** (`DERIVED_TYPES`): an alias removed from the
   table loses its edge on the next run.

**Resolution.** `project.resolve {id}` answers a registry id, an alias, or a name the registry
already measures (`repository:`, a deployment id such as `heroku:<app>`, `domain:<name>`), by
exact match only. It returns the canonical id, the product's members with their role,
repositories, deployments and domains, the products a project is part of, and every alias with its
owner. A measured name on two projects is `ambiguous` with both candidates; a conflicted name is
`conflict`; anything else is `not-found`. A near match is never offered. The canonical id is
always an Observatory id: which project a folder or repository belongs to in a host's own project
model is that host's decision, and `project.resolve` does not make it.

**Intake.** The system that owns a name files it: `observatory_propose` (or `project.record`) with
`targetId` a `project:` or `product:` id and `patch: {"aliases": [{"id", "owner", "source"}]}`.
The wire refuses a malformed alias; `review.py accept-proposal` lands it in
`config/identifiers.json` with the decision's provenance and refuses a name that already names
something else. A product accepts no other field by proposal: its members are the operator's
grouping.

## Out of scope for this slice

- **Environments, accounts and deployments**: [DEPLOYMENTS.md](DEPLOYMENTS.md) (PB-004).

- **Checkouts as entities** (`checkout:<repository>/<folder>`): PB-003b.
- **Components**: several products in one repository, sub-directories of a monorepo. PB-003c; the
  product model stays N projects to 1 product until then.
- **Rewriting stored history**: not planned.

## Tests that define done

Aliases: `observatory/engine/tests/test_identifiers.py` (document, conflicts, resolve, the real
emit), `test_validator_rules.py` (one plant per alias rule), `test_write_surface.py`
`test_an_alias_proposal_is_checked_at_the_wire_and_lands_in_the_table_of_names`.

Ids:

- Renaming a wiki folder keeps the project id, the timeline shows both periods, and curation keyed
  by the id still applies.
- A repository transfer keeps the id of a project anchored by that repository, and the repository
  records its former name.
- Renaming a local Git folder without a remote keeps the id through its root commit. Renaming a
  local folder with no Git history mints a new id. This limit is documented, not guessed around.
- Two projects sharing one repository anchor with one recorded id produce `identity.ambiguous`, and
  both get new ids.
- A partial scan retires nothing. A project absent from a complete scan is retired, and its id is
  never minted again.
- The first run on an existing workspace changes no id.
