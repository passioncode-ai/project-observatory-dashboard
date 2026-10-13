# Disposable-target revalidation

## Scope and requirements

This developer wave hardens disk-guard deletion decisions. It is stacked on the
ignored-worktree/local-state preservation change and includes its follow-up
shared artifact inspector. No operational cleanup, service restart, installation
or release was performed.

| Requirement | Implementation and verification |
|---|---|
| Preserve old repositories/worktrees under temporary roots, including nested and parent Git ancestry | `disk_guard.git_ancestry`, shared `cleanup_safety.protected_entry_reason`, `TargetRevalidation` temporary-path cases |
| Revalidate build eligibility when applying a plan | `disk_guard.act`: checkout idle state, ignored status, tracked files and new subtree writes; `TargetRevalidation` build cases |
| Refresh process-holder evidence per action within a bounded budget | `disk_guard.fresh_busy`; `TargetRevalidation.test_each_action_uses_fresh_holders_and_unknown_is_a_skip`, `FreshTargetProbe` |
| Keep nested local state and uncertain artifacts | Shared `cleanup_safety`; `GitTierSafety` and `TargetRevalidation.test_apply_build_preserves_nested_local_state` |
| Include the shared helper in the portable source sandbox | `tests/run_portable.py::ROOT_FILES`; the copied-source `disk_guard` suite is the import/integration check |

Source ledger: `AGENTS.md`, `CONTRIBUTING.md`, `docs/design/DISK-GUARD.md`,
`docs/ONBOARDING.md`, `docs/AGENT-ONBOARDING.md`,
`observatory/engine/docs/ux/portable-scenarios.md`, `holders.py`,
`tools/disk_guard.py`, `tests/test_disk_guard.py`. Contradiction found: the
documented immediate holder recheck previously reused a class-level listing;
this wave adds a target-scoped observation at the action boundary.

## Evidence

- Test-first selection: `test_disk_guard.py TargetRevalidation FreshTargetProbe`
  ran 18 tests and failed with 14 assertion failures before implementation. The
  failures included actual removal of synthetic targets that should be kept.
- The same selection passed 18 tests after the first implementation.
- After integrating shared marker predicates, the direct synthetic file ran
  97 tests with one skip and exit 0.
- `python tools/sync_engine_docs.py --check`: exit 0, `stale: none`.
- `python tools/check_public_release.py`: exit 0, zero pattern findings in 884
  files before this run record was added; no private denylist or history scan.
- Changed Python sources parsed with Python 3.11 grammar. Runtime execution
  used Python 3.14; this is not Python 3.11 runtime acceptance.

Portable acceptance: `python observatory/engine/tests/run_portable.py --suite
disk_guard --jobs 1 --timeout 180` selected exactly one suite, ran 97 unittest
cases with two environment/platform skips and exited 0 (`PASS`, Python 3.14.7,
38.24 seconds). It copied the new helper through `ROOT_FILES` and imported it
in the isolated sandbox. The JSON receipt is [verification.json](verification.json).
No full-engine suite or live cleanup was run.

## Limits and exact next task

This is a source candidate, not installed cleanup or a release receipt. POSIX
path actions now require a complete target-scoped `lsof` result and fresh process
arguments sharing a five-second deadline; missing `lsof`, warnings, partial
reads or timeouts preserve the target. Build actions probe the checkout so a
new process working elsewhere in that checkout also holds its output. Windows
retains the rename probe. Shared inspection refuses symlinks and special files;
ordinary dependency trees with links can therefore remain in place.

The cleanup lock serializes Observatory writers only. Filesystem and holder
checks are point-in-time observations, not a lock against arbitrary concurrent
writers. Generic temporary-file cleanup remains a configured age policy; the
new repository/state exclusions do not prove that every other old file is
reproducible. No live target has been declared abandoned or reclaimable.

Next task: independently review the combined ignored-worktree and disk-guard
diff; integrate the portable runner argument-forwarding fix; run the selected
copied-source suites on the combined commit; then follow the repository's PR,
release and installation gates. The parent wave owns board updates and the
central handoff entry.

Skills used: task-pipeline (bounded development wave and test-first evidence),
evidence-docs (source-linked claims and explicit acceptance limits).
