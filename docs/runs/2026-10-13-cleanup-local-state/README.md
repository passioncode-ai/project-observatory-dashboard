# Cleanup: preserve ignored local state

## Objective and scope

Prevent worktree cleanup from deleting ignored data that its manual archive does
not contain, and prevent `.wrangler` local state from being treated as disposable
build output. Base: `ad93c46369976e7ad9c10d11d9da00afb6663d95`.

## Evidence and change

Two synthetic regressions in `GitTierSafety` first failed: manual cleanup returned
`removed` for a dirty worktree with an ignored local file; the configured artifact
list contained `.wrangler`. The dirty worktree archive contained only its tracked
patch and status, so it could not restore the ignored file.

Manual removal now skips any ignored file, both before archiving and immediately
before force removal. The recovery is to preserve or move those files and rerun
`full cleanup --apply --include manual`. The shared artifact filter excludes
`.wrangler` from old copied configurations, both planners and ignored-file checks.
This is deliberately conservative: manual cleanup with ignored dependency output
also waits for that output to be removed or preserved separately.

The CLI help, MCP machine-tool description, onboarding and existing portable
scenario document describe the same refusal. No live cleanup was performed.

## Checks actually run

- Initial focused `GitTierSafety`: 4 tests, 2 failed on the unfixed code.
- Focused `GitTierSafety` after the additional archive-time regression: 5 tests,
  all passed. Includes direct planner calls with an older artifact list.
- Disk-guard suite before that additional regression: 72 tests, passed, 1 skipped.
- `test_engine_doc_copies.py`: 6 tests, passed.
- `python3 tools/sync_engine_docs.py --check`: `stale: none`.
- `python3 tools/update_inventory.py --check`: passed, no differences.
- `python3 tools/check_public_release.py`: passed, zero findings (tree only;
  no private denylist supplied).
- `git diff --check`: passed.

## Open work and exact next task

Independent review and integration remain. No release, installed upgrade, whole
engine gate or live acceptance is claimed. The next developer slice must inspect
generic temporary-directory deletion and act-time revalidation of build-output
candidates; those paths are outside this narrowly scoped change. Preserve this
branch while that slice stacks on it. The parent run owns backlog reconciliation.
