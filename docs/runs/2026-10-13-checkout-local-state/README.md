# Saved local checkout state

## State now

The existing explicit-root checkout inventory can now distinguish an observed
clean Git status from changes, hidden index flags and incomplete reads. This is
the local-state slice of OBS-87, stacked on the open-unlinked observation branch.
Ownership, active use, allocated sizing, upstream durability and removal authority
remain separate work. Nothing here authorizes cleanup or changes installed services.

## Contract

The deadline-enforced inventory child uses the existing hardened Git command
builder. Status and index output are bounded to1MiB and10000 NUL records per
command before parsing. Status paths, content, stderr and argv are not retained
in the local-state result. Counters are Git records, not recursive file counts.
Ignored directories may represent many files.

Effective Git worktree root and inside-work-tree state, HEAD, root, Git directory,
marker and index metadata are checked before and after.
Changed or unreadable bindings refuse a clean observation. This is a point-in-time
observation, not a lock against concurrent writers or a filesystem-wide inventory.
Assume-unchanged and skip-worktree flags leave the state unknown. Bare repositories
are not applicable; unresolved/unborn HEAD and unsupported Windows pipe observation
stay unknown. Git optional locks remain off, and no fetch or lifecycle action runs.

The existing saved coverage reader projects the same counters and their age for
CLI/MCP. Stale, malformed, future-dated or inconsistent saved clean claims are not
promoted to current clean state. Path size and reclaimability remain unknown.

The format follows Git's [porcelain status](https://git-scm.com/docs/git-status)
and [index tag documentation](https://git-scm.com/docs/git-ls-files.html).
NUL-delimited rename source records and non-UTF8 filenames are parsed as bytes.

## Verification and errors retained

- New feature red baseline:9 tests failed with missing module/local-state field.
- Direct first implementation:9 tests passed.
- First portable invocation mistakenly executed zero new tests because the test
  module lacked its script entry point. That invocation is not acceptance.
- Corrected portable invocation:11 local-state,12 inventory,8 coverage and12
  open-unlinked tests passed under Python3.14.7 in isolated offline fixtures.
- Independent review of173933c rejected a false-clean claim after core.worktree
  redirected Git to another clean directory. Both binding checks now revalidate
  the effective root. A real Git before/during-redirection regression was red
  before the correction;44 portable cases pass after it. Independent correction
  review and hosted Windows/Linux/macOS checks remain pending.

Tests: `observatory/engine/tests/test_checkout_state.py`,
`test_checkout_inventory.py`, `test_coverage_view.py`, `test_open_unlinked.py`.
Run with `python observatory/engine/tests/run_portable.py --jobs 1 --suite checkout_state
--suite checkout_inventory --suite coverage_view --suite open_unlinked --report-dir <owned-output>`.

Next task: independently probe metadata replacement, hidden index state, output
limits, saved-reader tampering and positive real Git fixtures on the exact commit;
then publish a draft stack and retain installed acceptance as open.
