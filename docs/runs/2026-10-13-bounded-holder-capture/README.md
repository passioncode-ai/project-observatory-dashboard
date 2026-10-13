# Bounded explicit holder capture

## State now

This bounded OBS-86/OBS-87 continuation stacks on cheap admission
`25c375d5b9818fdcabbb1258814ae69116ef2d2e`. It caps the retained explicit holder
observations without changing their incomplete coverage or granting cleanup.
Independent source review passed at `ef3612954ffe0be6de9bb49e2f92bf8c58a6d386`;
draft delivery is next. No installed change is claimed.

`holders.py` now reads child pipes incrementally. Each subprocess has a 2 MiB
combined stdout/stderr cap and reads at most 64 KiB at a time. Stdout is retained
within the cap, stderr only counted. Parsers cap fields at 100000, retained hints
at 10000 and tokens/paths at 4096 bytes. The operation shares a monotonic deadline
across lsof and ps, with cleanup time reserved inside it. Only its newly created
POSIX process group is stopped; the direct child is reaped and pipes closed.
The old synchronous `/proc` fallback is retired, including its legacy entry point.
Windows still returns unknown without launching a collector.

This bounds captured payload and collections, not whole Python or child RSS.
Uninterruptible spawn/scheduling and descendants that escape the created group
remain outside the normal-execution bound. Raw command text, exception bodies
and stderr are not persisted. Complete file observations still cannot establish
complete argv/lifecycle coverage. No discovered holder is stopped; no live
cleanup, provider request, installed setting or automatic cleanup flag changed.
Unrelated open-unlinked and cache-command subprocess implementations are outside
this unit. See [contract and reader guidance](../../design/DISK-GUARD.md#bounded-explicit-holder-observations).

## Counterexamples and validation

Before implementation, three owned-child/fake-collector regressions failed:
output above 2 MiB was accepted, a 50 ms timeout admitted output after 200 ms,
and a missing lsof observation invoked the unbounded proc fallback. The expanded
contract initially had nine failures and twelve subtest/API errors; missing helper
errors were preparation evidence, not twelve independently demonstrated defects.

The first portable run passed capture16, Git-hardening61 and osprivacy17 cases.
Disk-guard ran154 cases with one failure and two skips: an older test still
expected proc hints. The successor asserts no fallback I/O and unknown preservation.
The original failed receipt is retained by the maintainer, not relabelled passing.
Final focused checks pass: capture17, disk-guard154 (152 passed, two skipped),
Git-hardening61 and osprivacy17. The latter two ran on identical runtime source
before the final fixture-only correction. Eleven root documentation-copy/inventory
cases and four Python3.11 syntax parses pass. Counts and source hashes are
recorded in [checks.json](checks.json).

Five actual owned script processes replayed absolute paths with spaces, both
relative forms, a symlink and a plain absolute path. Only their exact PIDs were
inspected; global responses were injected at the capture boundary. Four lossy
forms remained unknown and the plain absolute form stayed a positive hold.
No foreign command lines were collected or retained.

The new unittest suite is explicitly registered in the portable runner and has
`unittest.main()`. Source dependencies already include holders and osproc. Existing
holder fixtures now inject the capture boundary rather than accidentally invoking
host tools. Action/cache and archive refusals retain their assertions.

## Reproduce and next task

Run the portable subsets `holder_capture`, `disk_guard`, `git_hardening` and
`osprivacy`, then documentation-copy and source-inventory checks. These are
synthetic source checks; native Windows, whole-system and installed acceptance
remain distinct. Independent review should replay floods, deadline/descendant
cleanup, positive hints and actual action refusals on the exact candidate.

Primary semantics: Python's [subprocess documentation](https://docs.python.org/3.11/library/subprocess.html#subprocess.Popen.communicate)
explains in-memory capture and process-creation timeout limits;
[selectors](https://docs.python.org/3.11/library/selectors.html) documents POSIX pipe
support; [killpg](https://docs.python.org/3.11/library/os.html#os.killpg) defines
process-group signalling. These inform the mechanism, not a live-resource claim.

## Independent review and publication boundary

The independent reviewer accepted the exact implementation above after replaying
17 capture cases, six action/fresh-probe cases, four additional control groups
and five owned-script path forms. Controls included combined-pipe limits,
closed-pipe surviving descendants, an unrelated owned group that survives,
parser bounds and no-I/O modes. No new source finding remained. These overlapping
test groups are not added together into a fabricated unique-case total.

The implementation checkpoint's current-tree/delta privacy gate passed: 994 files,
13 history blobs and 4055 effective protected identifiers, zero findings. Final
publication checks bind the successor and its complete stacked range separately.
The parent cheap-admission source25c is independently accepted but is not yet a
published base branch, so draft delivery stacks on the published POSIX holder
branch and includes that prerequisite explicitly. It does not alter that branch.

Next: preserve the final candidate as a draft, observe hosted checks, and let
maintainer integration combine it with the separate observation lane. Do not
interpret a draft, this source verdict or a passing check as installed cleanup
or complete lifecycle coverage. Native Windows execution remains NOT_RUN here.
