# Run 2026-10-17 — automatic project registration (Project Observatory)

Objective (REQ-003/004/005): a SessionStart-seen eligible Git repository or
worktree outside the configured projects root is declared locally and then
materialized by the ordinary collector/registry pipeline. Registration is
idempotent and reversible; it grants no credential or memory binding and changes
no organization ownership.

Base: `33c04a5f` on branch `agent/auto-project-registration-20261017`.
Commit: this document is committed with the implementation; resolve the final
object id with `git log -1 --format=%H` on that branch.

## Decisions

- **One exact declaration, never a broader source.** `config/tracked.json` holds
  absolute `local_folders` paths. SessionStart never broadens `sources.projects`
  and never writes `registry/projects.json`.
- **The existing pipeline owns measurement.** The next local/tick pass sends
  declarations through `scan_folder` → `local.json` → merge → emit → validate.
  Absolute folder keys retain the registry's existing path behavior; a
  `display_name` keeps external local-only projects readable.
- **Identity before automation.** Existing local and remote identities resolve
  first. A new clone of a known project is only another checkout; ambiguous
  identities are reported and never guessed.
- **Removal is durable.** `full untrack local-folder PATH` removes the active
  row and leaves a removal marker. Later SessionStart calls do not restore it;
  an explicit `full track local-folder PATH` opts back in. `full tracked` and
  `full doctor` expose active, removed and degraded states.
- **Existing boundaries remain.** Folder exclusions, worktree dedupe, atomic
  writes, missing/malformed-input degradation, legacy `core.discover` preview,
  and proposal review authority are unchanged. No new provider, credential or
  memory authority was added.

## What shipped

- `observatory/engine/tracked.py`: locked, atomic declaration storage with
  unknown-field preservation, per-row degradation, a raw-store journal, and
  idempotent declare/remove operations.
- SessionStart local/remote resolution, ambiguity reporting, exact-checkout
  detection, declared/pending/refused states, and session finding filtering.
- Filesystem scanning of declared paths with exclusion, missing-input and Git
  preflight handling; merge display names and exclusion reuse; unchanged emit
  registry authority.
- `full track`, `full untrack`, `full tracked`, help and doctor output.
- Agent and person documentation, MCP instructions, CLI compatibility, portable
  scenario OSS-58, Russian hook translations, generated engine doc copies, and
  `SOURCE-INVENTORY.json`.

## Evidence

Test-first red runs before implementation:

- `tests/test_session_start.py`: exit 1 — no declaration was written.
- `tests/test_local_folders.py`: exit 1 — declared paths were absent from
  `local.json` and the registry.
- `test_workspace.WorkspaceTests.test_local_folder_track_untrack_is_atomic_idempotent_and_reversible`:
  exit 1 — `track` was an unknown step.

Final focused runs (all exit 0):

- `tests/test_session_start.py`: declaration, idempotency, existing identity,
  ambiguity, exclusion, removal and broad-folder cases.
- `tests/test_local_folders.py`: real SessionStart → scan → merge → emit →
  validate, worktree dedupe, exclusions/removals, and degraded inputs.
- `tests/test_workspace.py`: 23 cases; includes atomic/idempotent track/untrack
  and doctor state.
- `tests/test_mcp_wire.py`: 128 assertions; instructions remain below 1,800
  characters (measured 1,770).
- `tests/test_wire_contract.py`: 54 assertions; the credential-reader contract is
  preserved alongside the new SessionStart instruction.
- `tests/test_api_listings.py`: 62 assertions; all test paths remain redirectable.
- `tests/test_recall.py`: 19 assertions; the degraded-answer contract is intact.
- `tests/test_handed_commands.py`: 9 cases; every new command and help route.
- `tests/test_i18n.py`: 44 cases; dynamic hook ids and Russian translations.
- `python -m unittest discover -s tests -v`: 214 tests, exit 0 on two clean runs.
- Two-round synthetic SessionStart/local scenario: declared → materialized →
  known, stable declaration bytes and `sources.projects`, exit 0.
- Clean-archive `tools/check_public_release.py`: 956 files, zero findings.
- `docs/site/check.py --self-test`: 15 static checks, exit 0;
  `node tools/check_site_interactions.cjs`: exit 0.
- `tools/check_backlog.py`: zero shape problems; no local private denylist was
  supplied, so that run does not claim a private-name check.
- `python tools/update_inventory.py --check`: exit 0; `tracked.py` included.
- `python tools/sync_engine_docs.py`: generated copies refreshed; engine doc
  copy tests pass in the root gate.

Complete engine gate:

- First `python -m observatory full check`: exit 1, 249 suite results. Five
  failures (`wire_contract`, `api_listings`, `recall`, `skill`, `osprivacy`)
  identified interface and path-contract violations; each is fixed and has a
  focused green rerun. Three suites timed out under parallel load
  (`credential_doors`, `disk_guard`, `digitalocean_door`) and were not counted
  as passes.
- Corrected rerun `python -m observatory full check --timeout 900 --jobs 2`:
  exit 0, all 249 suites `PASS`, including the three suites that had timed out.
  The receipt also discloses 10 `skip_assertions` and six skipped unittest cases
  (one each in `tick`, `gate_purity`, `osproc`, `osschedule`, `maintenance`,
  `maintain_app`, two in `disk_guard`, three in `tick_repo`, five in
  `trap_efficacy`). Three external scopes were deliberately `NOT_RUN`: live
  provider acceptance, external MCP host admission, and live secret
  rotation/delivery. They need separately configured accounts or authorization
  and are not claimed by this offline run.

## Open work and exact next task

Commit the reviewed change and hand it to the cross-repository coordinator for
integration. No fetch, push, release, version bump, installation,
operational-workspace mutation, or live-provider acceptance is part of this run.
