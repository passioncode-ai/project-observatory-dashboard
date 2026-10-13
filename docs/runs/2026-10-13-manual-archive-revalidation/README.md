# Manual cleanup archive revalidation

## Objective and source

Preserve changes made while a manual worktree archive is being written, including
an index version different from both HEAD and the working file. Base:
`c7772f1c2c25bbd956bbc523b092b3b630da8baf`. The independent reproduction changed
a tracked file after `export_tracked` returned: cleanup removed the newer data,
while its archive held only the older patch.

## Change and evidence

`observatory/engine/tools/cleanup.py` now saves separate working/staged patches,
verifies each against HEAD, writes and verifies a regular-file tarball plus its
SHA-256 manifest, and revalidates the archive against current Git and filesystem
state before removal. A bounded filesystem walk catches special files Git omits;
nested repositories, submodules, symlinks and unknown/unreadable state refuse
removal. Partial attempts remain named in the result and retries use a new path.
The legacy build-artifact action refuses stale plans and redirects to disk-guard.

The regression tests first ran against the base: 11 tests, 6 failures and 1 error.
After the initial implementation, the FIFO case still failed because Git omits
special files; the bounded tree inspection closed it. All fixtures are synthetic.
No live cleanup, installation, release or full engine gate was performed.

## Checks actually run

- `observatory/engine/tests/test_git_hardening.py -q`: 37 tests passed.
- `observatory/engine/tests/test_disk_guard.py GitTierSafety -q`: 8 tests passed.
- `observatory/engine/tests/test_machine.py Estate -q`: 10 tests passed.
- `python -m unittest discover -s tests -p test_engine_doc_copies.py -q`: 6 tests passed.
- `python3 tools/sync_engine_docs.py --check`: `stale: none`.
- `python3 tools/update_inventory.py --check`: passed, no differences.
- `git diff --check`: passed.
- Public-release tree scanner with the locally held private denylist: failed on
  three identifier matches in two files unchanged from the base,
  `observatory/engine/collectors/scan_harness.py` and
  `observatory/engine/tests/test_fabric_service.py`. The same scanner's path/text
  rules over all eight changed files: zero findings. No whitelist changed;
  full-tree privacy is not claimed green and history was not scanned here.

Tests use a temporary HOME and engine workspace. Positive restoration proves
working edits, separate staged bytes and regular untracked content; negative
cases cover tracked/untracked/HEAD drift, added files, nested/bare repositories,
symlinks, FIFO, read failures, corrupt archive reads and exhausted budgets.

## Limits and exact next task

The final observations and Git removal are not atomic. This protects against
observed archive-time changes, not an adversarial concurrent writer after its
last observation. Stop writers before explicit manual cleanup. Untracked hashing
uses bounded chunks and byte/file/time budgets; Git patch capture can allocate
its output before the size check, and a filesystem call can exceed the deadline.
The next task is independent exact-commit review and integration, followed by
final integrated checks. Disk-guard apply-time safety belongs to the parallel
change; this branch changes only the legacy action's refusal.
