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

## Second independent verification wave

The verifier reproduced three further paths on the integrated tree: index flags
could hide unarchived bytes from both worktree tiers (IV08); a conflicting or
failed detached archive ref left its commit unretained (IV09); and a branch tip
advanced during bundle verification could be deleted without being archived
(IV10). New regressions against the previous implementation reported seven
failures for hidden-index/ref cases and three branch-proof failures. A positive
fresh detached-ref test also caught a first-draft implementation that treated
Git's missing-ref status as an inspection failure; using a quiet existence check
closed that false refusal.

The corrective change reads index flags before status and at removal, verifies
the live HEAD attachment and archive ref by exact commit, binds branch eligibility
and bundle headers to the captured tip, rechecks attachment/eligibility and uses
`update-ref -d <ref> <expected-tip>` instead of unconditional branch deletion.
Unique bundles use new directories; unknown observations preserve their sources.
The expected-tip CAS prevents a concurrent newer tip from being deleted. It does
not atomically lock another process's worktree checkout; that final attachment
race remains explicit. Branch configuration is intentionally retained.

Second-wave checks, under a temporary HOME: 53 git-hardening tests, 8
GitTierSafety tests, 10 Estate tests and 6 document-copy tests passed (77 total).
Document-copy sync, source-inventory check and `git diff --check` passed. The
privacy scanner's path/text rules over all seven follow-up files found zero
matches. The previously recorded baseline full-tree privacy findings remain;
this follow-up did not rescan history. Independent review on the exact follow-up
commit remains the next task; no live removal ran.

## Symbolic ref follow-up (IV12)

Independent review found that plain expected-tip `update-ref -d` follows a
symbolic branch ref. A synthetic alias to the checked-out main branch therefore
removed its referent, both when present before eligibility and when injected at
the deletion boundary. Tests against the previous commit reproduced one failure
and one error (the main ref was missing).

Eligibility now refuses symbolic targets and unknown ref types, validates literal
branch names through `check-ref-format --branch` without accepting expanded
shortcuts, and accepts only known cleanup classes. Deletion adds `--no-deref`;
the real-Git late-alias test proves the referent survives. The existing expected-
tip race regression still covers ordinary branch movement. These changes do not
claim an atomic lock against another process attaching a worktree.

IV12 verification under a temporary HOME: 56 git-hardening, 8 GitTierSafety,
10 Estate and 6 document-copy tests passed (80 total). Document-copy sync,
source-inventory and diff-whitespace checks passed. Scoped privacy path/text
checks over seven changed files reported zero findings. No live cleanup or
history scan ran; the previously recorded full-tree findings remain unchanged.
Independent review of the follow-up commit is the next task.
