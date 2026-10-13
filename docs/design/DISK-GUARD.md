# The disk guard — design (OBS-63, second iteration)

Status: adopted 2026-10-12. The first iteration shipped in 0.23.0 (#202) and was fixed twice
(#212, #222). An independent three-tier review of `main` at `a99a0f4` (unit, seam, product) then
found five places where the guard's own promises did not hold and a dozen risks. This document is
the design that replaces those patches with one set of rules. The run record is
[docs/runs/2026-10-11-disk-guard](../runs/2026-10-11-disk-guard/README.md).

## What the guard promises

1. **Nothing a process holds is removed** — open, mapped, executed from, or worked inside — and
   the check is fresh when the removal happens, not when the run began.
2. **Nothing that is somebody's only copy is removed.** Regenerable things only (caches, build
   output git ignores, temporary entries, old Trash), and the git tier that loses nothing (merged
   or pushed branches, clean worktrees whose branch stays).
3. **No privacy-guarded place is opened** (macOS TCC folders: Documents, Desktop, Downloads,
   other apps' containers…), by any class, from the scheduled job.
4. **It starts nothing and stops nothing.** Memory and swap are reported.
5. **It finishes inside its time and always says what it did**, truthfully: ran, not due, or
   failed — never "ran" with an earlier run's numbers.
6. **It runs by itself on every system**: the maintenance job (launchd, a systemd user timer,
   Task Scheduler) every 15 minutes; acting hourly while space is normal and on every pass below
   the `low` line.

## What broke them, and the rule that replaces each patch

| Found | Rule now |
|---|---|
| One `lsof` listing at the start, reused for minutes of removals | **Fresh holders per class** for planning, then a bounded target-scoped holder probe immediately before each removal; missing, partial or timed-out evidence skips the target |
| The git tier checked only working directories; "clean" worktree removal deleted ignored files (`.env.local`, local databases) | The git tier uses **the same holders** (open files, maps, executables) and a worktree with **ignored files outside the artifact directories is not clean** |
| Programs run from a cache folder (an `npx` MCP server) hold no file open | Holders include every running process's **executable and the absolute paths in its arguments** |
| `lsof` printed non-ASCII names escaped without a UTF-8 locale | `lsof` runs under a UTF-8 locale and `\xNN` escapes are decoded |
| `/proc` without `maps`/`exe`; Windows checkout not protected; a failed removal after the rename left `*.observatory-removing` | `/proc` reads fd, cwd, root, exe and maps; Windows renames aside and **renames back** when removal fails |
| Files of another account inside a folder this account owns | The walk refuses a tree with **any** entry another account owns |
| Force-tracked files inside an ignored folder | Build output is removed only when `git ls-files -- <dir>` is empty |
| The budget was not checked inside long walks; a SIGKILL repeated on every pass | Every walk, `lsof` and git call takes the **deadline**; the class order no longer lets one class starve the rest (**a fair share per class**) |
| One failed action marked the pass "failed"; cleanup failures were invisible | Exit 0 when the guard ran; **counts** of removed / skipped / failed in the record; non-zero only when the guard itself crashed |
| A not-due run was recorded as "ran" with old numbers | The JSON line carries `not_due`; the pass records `not-due` with the current free space |
| "Hourly" gate measured from the end of the previous run (≈75 min; 2 h on an old schedule) | Measured from the run's **start**, with a tolerance of half an interval |
| Workspaces created on 0.23.0 froze the guard section; pressure overrode a person's own larger thresholds | A workspace keeps **only the keys a person changed**: values equal to a shipped default of any release are read as unset; pressure lowers only unset keys |
| Two git-tier writers (the tick, the guard) and a person's `--apply` with no lock; torn plan file | **One cleanup lock** shared by the guard, `cleanup.py` and the tick; a busy run is a skip; the plan is written atomically and records its level |
| The guard's 2,000 rows a run pushed the git tier's undo rows out of every view and every log generation | Guard rows go to **`store/logs/disk-guard.jsonl`** with one summary row per run; `cleanup.jsonl` keeps the git tier's undo record; views count from the summaries |
| `uv cache clean` four times an hour below `low` | Per-command **`every_hours`** (whole-cache cleans at most daily) |
| Only the large-file walk honoured privacy-guarded places | `protected_place` filters checkouts, temporary roots and cache roots before any stat, and the skip is named in `degraded` |
| Windows killed the whole pass at 14 minutes; a failed backup stopped the tick and server every 15 minutes; macOS kept the hourly plist after an update | The Task Scheduler limit is **not tied to the interval**; a failed snapshot **backs off an hour**; a pass that finds a pending schedule reload **reloads it after it exits** |

## Order of a run

1. Take the cleanup lock (skip the run when another holds it); read free space and the level;
   decide whether the run is due.
2. For each class in turn — cache folders, build output, temporary entries, Trash, browser
   clones, the tools' commands — take a **fresh holders listing**, plan within the class's fair
   share, and act; every removal re-checks the target.
3. The git tier, with the same lock held, the same holders and the remaining time.
4. Survey refreshes and the large-file walk, only with the time left.
5. Write the summary row and the report — always, in `finally`.

## Local state preservation

`.wrangler` is a state container, not disposable build output. The artifact-name
filter in `tools/cleanup.py::artifact_names` excludes it from both cleanup paths,
including copied configurations that still list it. A manually removed dirty
worktree has only its tracked patch and untracked files archived; any ignored
files therefore cause a skip before archiving and again before force removal.
`tests/test_disk_guard.py::GitTierSafety` reproduces and pins these refusals.
Git can collapse an ignored artifact parent into one row. Cleanup therefore walks
that candidate with `cleanup_safety.protected_tree_reason`: repository and local
state markers, symlinks, special files, read errors and exhausted time/entry budgets
all retain it. The disk guard shares the entry predicate in its own bounded walk.
This observation does not lock out an adversarial writer between a check and removal.

## Not done here, and why

- A person's `full update --apply` started while a pass runs is refused by the snapshot's
  stillness check; the update names the running pass. Making it wait is the update's design
  (OBS-40 family), not the guard's.
- Case-insensitive APFS paths written in the wrong case in a person's own roots do not match
  `lsof` names; the shipped roots are cased correctly.

## Apply-time preservation (2026-10-13)

`tools/disk_guard.py::git_ancestry` keeps generic temporary cleanup out of Git
repositories and linked worktrees, including a target whose parent is a checkout.
The recursive inspection also refuses nested repository markers and local state;
old timestamps do not turn those paths into disposable temporary data. Structural
bare repositories (`HEAD`, `objects`, and `refs` or `config`) are preserved even
without a `.git` marker, including nested trees and configured paths beneath one.
The shared `cleanup_safety` inspection applies the same rule to artifact pruning.

Every path plan records the target device and inode. Apply requires that identity
and checks it before revalidation, after inspection and immediately before removal;
a replacement or missing identity is a skip requiring a new plan. Browser clones
also undergo subtree inspection when planned. `SnapshotSafety` covers bare
repositories, their ancestry, target replacement and identity on each path class.

Build plans carry their checkout, idle threshold and planning timestamp.
`act` repeats the checkout-idle, Git-ignore, tracked-content and subtree-freshness
checks before removal. Missing context requires a new plan; failed Git reads,
recent edits, newly written ignored files and new nested repositories preserve
the target. `tests/test_disk_guard.py::TargetRevalidation` covers these changes.

Every path action uses `fresh_busy` after its filesystem checks. On POSIX it
queries `lsof` for this target only, then refreshes command-line paths with `ps`,
sharing at most five seconds (and never beyond the action deadline). Warnings,
partial output, a missing tool or timeout are unknown, so nothing is removed.
There is deliberately no unbounded per-action `/proc` fallback: on a POSIX host
without `lsof`, planning remains available but path actions skip. Windows keeps
the rename probe. `FreshTargetProbe` pins the bounded and unknown outcomes.

These checks reduce the planning-to-action race; they are not an exclusive
filesystem lock against an arbitrary writer starting after the final probe.
The existing cleanup lock serializes Observatory writers only.

## Measurement coverage (OBS-87 subset)

`memory_report` preserves an unavailable swap measurement as `swap_gb: null`,
with `swap_reason` and the source measurement timestamp. A measured zero remains
zero. Invalid negative, nonnumeric or nonfinite values are unknown too.

`large_files` records `complete`, `errors` counts (`root_stat`,
`directory_read`, `entry_stat`) and `budget_exhausted`. Failure reasons are
fixed categories: OS error bodies are not returned. Successful observations
survive a later failed path; an empty partial scan never proves no large files.
Completeness applies to `home-with-policy-exclusions`, not every mounted volume
or privacy-protected folder. Reused reports retain their measurement time.

The bounded MCP overview keeps `largeFiles` as a list for compatibility and adds
`largeFilesCoverage`; its guard memory summary retains the nullable swap value
and reason. The dashboard labels incomplete coverage even when some files were
found. `test_disk_guard.py::CoverageTruth` covers these producer/consumer seams.
This subset does not complete worktree discovery or survey scheduling fairness.

## Registration refresh and build-root fairness (OBS-87 subset)

Before splitting the class budgets, `run` reserves up to 20 seconds (at most a
quarter of the time available after the run reserve) for the registration-only
collector. It writes `worktree-registration.json`, not `git-hygiene.json`; it
cannot refresh old cleanliness or branch evidence. `refresh` supplies its child
with a new deadline just inside its subprocess timeout, rather than inheriting
an expired step deadline.

`checkouts` accepts only successful, nonstale registration rows measured within
six hours, excluding bare, locked and prunable worktrees. If a registration
receipt exists, failed or old rows never fall back to hygiene. Only when the
receipt is absent may a dated, nonstale hygiene root measured within 24 hours be
used, with a degradation entry naming its original measurement time. Membership
alone never authorizes removal: checkout activity, ignored/tracked content,
subtree protection, identity and holder checks still apply.

`plan_builds` saves `disk-guard-build-cursor.json` before attempting each root.
The next pass rotates after that root, including after an interruption. A root
gets five seconds by default (`build_root_quantum_seconds`, capped at 30), bounded
by the phase budget. Git calls, activity inspection and lazy directory enumeration
share that deadline; enumeration also has the existing 50,000-entry limit.
Unknown activity preserves the root; incomplete traversal reports partial
coverage and only independently inspected candidates can be planned. This is fairness
between registered roots, not resumable progress within one oversized root; such
a root can remain incomplete. Individual blocked filesystem system calls cannot
be preempted by these cooperative deadlines.

`holders_by_phase` retains each phase's mode, known state and measurement time;
the cache-command phase's `none` does not erase a failed path-holder probe.
`RegistrationFairness` pins the refresh order, stale fallback, child deadline,
pre-attempt cursor, per-root quantum and bounded enumeration.

The registration consumer validates nested collections and row types before
reading fields. Invalid repository/worktree rows are excluded with explicit
counts in `degraded`, while valid peers remain available. Paths must be absolute
strings without NUL, timestamps must parse, and freshness and worktree protection
flags must be booleans. Unknown flags never authorize a root. The dated hygiene
fallback also rejects malformed nested rows. `RegistrationMalformed` exercises
null/scalar collections, mixed valid/invalid peers, malformed metadata and the
whole guard run continuing through later phases after a damaged receipt.
