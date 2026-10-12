# OBS-65 — one project under many names

Date: 2026-10-11. Branch `agent/obs-65-identity-aliases`. Not a release: no version bump, no
CHANGELOG entry. The new capability's schemas are pinned to the release that will publish them,
`v0.24.0`.

## What shipped

- `observatory/engine/identifiers.py` — the curated table `config/identifiers.json` (alias →
  `project:`/`product:` id, with the name's owner, source and date), its document
  `registry/identifiers.json`, `known_as` edges, and `resolve`. One alias, one target: a duplicate
  key, a case variant with another target, or an alias contradicting a measured repository,
  deployment or domain is a published conflict and is withheld. A malformed table is refused whole;
  a target the registry lacks is withheld and named in `degraded`.
- `collectors/emit_registry.py` — computes the document last, over every relation; `known_as` is
  in `DERIVED_TYPES` (rebuilt each emit); SRC-0016 now names the table.
- `tools/validate_registry.py` — seven alias rules; `known_as` endpoints resolve to published aliases.
- `proposals.py`, `tools/review.py` — `aliases` is a structured proposal field for `project:` and
  (alone) for `product:`; an accepted alias lands in `config/identifiers.json` with the decision's
  provenance; a name that already names something else is refused at the decision.
- `mcp/server.py` — tool `observatory_resolve` and capability `project.resolve`; the propose
  tool's `patch` description names `aliases`; the instructions list the capability.
- `fabric-agent.json` revision 8 (contentHash restamped), lock release `v0.24.0` with two schemas
  and one fixture, probe `resolve-names-one-id-or-refuses` with its assessor in `tools/run_probes.py`.
- Docs: [IDENTITY.md](../../design/IDENTITY.md#other-names-aliases-and-projectresolve-obs-65),
  [ONBOARDING.md](../../ONBOARDING.md), scenario OSS-56, `fabric/FABRIC-CONFORMANCE.md`
  (the tool count now defers to `GOLDEN_TOOLS`), backlog OBS-65.

## Checks actually run (macOS, Python 3.14.7)

| Check | Result |
|---|---|
| `project-observatory full check --jobs 6` | PASS — 233 suites, 6613 assertions, 1332 unittest cases, 0 failed (7 min 05 s) |
| `python -m unittest discover -s tests` | OK — 146 tests |
| `python tools/update_inventory.py --check` | passed, no differences |
| `python tools/check_public_release.py --history --history-ref HEAD` | passed, 0 findings |
| `python tools/check_public_release.py --history` (all refs of a fresh clone) | 2 `history:credential-url` findings in blobs outside HEAD's history; present before this branch, not introduced by it |
| `python3.11 -m compileall` on the changed modules | ok |
| `tools/fabric_hash.py --check` | contentHash OK |

Not run here: `swift test`, the app build, `claude plugin validate --strict`, the wheel build and
the site checks — this change touches none of their inputs; CI runs them.

## Open

- A release (`v0.24.0`) publishes the `project.resolve` schemas at their pinned address; until
  then a host that fetches the URI gets 404 while the bundled bytes serve locally.
- The dashboard does not show aliases or conflicts yet; `registry/identifiers.json` and the
  emit's printed line are the surfaces. Next task: a row per product on the products page listing
  its aliases and owners, and a Health row for open alias conflicts.
