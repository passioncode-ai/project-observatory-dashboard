# Run 2026-11-03: release 0.31.0

Objective: release what merged since 0.30.1, the interim relay for the operator's own estate (OBS-287) first among
it, by 2026-11-04 as the fabric-workspace coordinator relayed; then tag, verify and install it.

Base: `d0f033d2` (origin/main, #407 merged) on branch `release/0.31.0`. This record is committed with the version bump;
resolve the final object id with `git log -1 --format=%H` on that branch.

## What the release carries

Everything merged since `v0.30.1` (`git log v0.30.1..d0f033d2`, 6 commits). Each pull request with code passed an
independent adversarial review and every confirming round its findings required:

- #400: your own relay for your machines, `full relay serve|enable|disable|status`, `fabric-relay/0.1` for the
  operator's own estate until the relay project's RLY-05 is deployed (OBS-287, part: blobs, the hub delta, heartbeat
  frames and the Cloudflare deployment remain).
- #401: the Sentry door `tools/sentry.py`: a token scoped to `org:ci` from a broad one, verified and recorded, and
  the broad one retired only after the narrow one verifies (OBS-292).
- #406: a partial machine restore (`--only`, `--skip`, `--projects`) and `full machine-move status`, the checklist
  of a move (OBS-282, OBS-286).
- #405: `full ssh-keys rekey`, a second machine's own SSH key wherever the other machine's key is registered
  (OBS-285).
- #407: the contract's DEC-0147 adopted for shares: the export-day rule, the recipient's 36-day restatement, the
  sender's per-day replacement with its guards, and the two agent twins on the node's MCP (OBS-291).
- The documentation pull request #404 (the 0.30.1 receipts).

## Decisions

- **0.31.0, a minor release**: new commands (`full relay`, `full machine-move status` and `facts`, `full ssh-keys
  rekey`, `tools/sentry.py`) and a share report body that follows the contract's DEC-0147.
- **The plugin stays 0.30.0**: `git diff --stat v0.30.1..d0f033d2 -- observatory/engine/skill` prints nothing.
- **CHANGELOG**: `## Unreleased` becomes `## 0.31.0 (2026-11-03)` with a summary.
- **Rows**: OBS-282, OBS-285, OBS-286, OBS-291 and OBS-292 read `done in 0.31.0`; OBS-287 reads `partly done in
  0.31.0 (#400)` with what remains; OBS-295 reads `done in 0.30.1 (#403)`, where it shipped. OBS-288, OBS-289,
  OBS-290 and OBS-294 were planned for 0.31.0 and are not in it: they now read `planned for 0.31.x`. Row OBS-300
  (the second-machine plan hands out `rekey`) is added, and OBS-301 to OBS-303 file the three LOW findings of #407's
  confirming reviews (none blocks: rounds 3 and 4 found nothing blocking).
- **Not in this release**: the live Sentry steps for the operator's slot wait on two human gates (an organization
  token made in Sentry's page, and the personal token deleted there); they run after the release, on board thread
  `dafb49ce`.

## Checks run

On this branch, with `~/.local/share/project-observatory-venv/bin/python`:

- `python -m unittest tests.test_version_consistency tests.test_desktop_app tests.test_plugin_manifests
  tests.test_changelog_releases tests.test_backlog_board tests.test_docs_drift tests.test_engine_doc_copies
  tests.test_release_boundaries tests.test_licensing`: 165 tests OK.
- `python -m unittest discover -s tests` from the repository root: 333 tests OK (1 skipped).
- `tools/check_backlog.py`: 0 problems. `tools/update_inventory.py`: exit 0. `tools/sync_engine_docs.py --check`:
  exit 0. `python -m compileall -q observatory`: exit 0. `observatory/engine/tools/check_docs.py`: exit 0.
- Each pull request's own CI ran the engine suite on macOS, Linux and Windows (#407 green on all eight checks at
  `444431de`); the release pull request runs it again.

## Receipts

To be filled in after the tag.

## Next

The live Sentry steps (OBS-292) once the human gates clear, then OBS-300, OBS-293, OBS-288's rest and OBS-294.
