# The disk guard — design (OBS-63, second iteration)

Status: adopted 2026-10-12. The first iteration shipped in 0.23.0 (#202) and was fixed twice
(#212, #222). An independent three-tier review of `main` at `a99a0f4` (unit, seam, product) then
found five places where the guard's own promises did not hold and a dozen risks. This document is
the design that replaces those patches with one set of rules. The run record is
[docs/runs/2026-10-11-disk-guard](../runs/2026-10-11-disk-guard/README.md).

## What the guard promises

1. **Observed holders refuse removal** — open, mapped, executed from, or worked inside — and
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
| `/proc` without `maps`/`exe`; Windows checkout not protected; a failed removal after the rename left `*.observatory-removing` | Historical `/proc` read fd, cwd, root, exe and maps (retired below); historical rename rollback preserved a failed staged removal; superseded for Windows authorization by the unknown-holder refusal below |
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

## Live state under cache roots (OBS-203, 2026-10-22)

`~/Library/Caches` and `~/.cache` are cache roots, so a folder in them was removable once nobody
had touched or held it for `cache_days`. An agent service may keep in-flight jobs there, and on
2026-10-20 one service's job workspaces and runner permissions in its own folder there were lost
(this engine's journals hold no removal in that window, but the guard would have planned one).
Until then only a person's `guard.cache_keep` list protected such a folder. Now a service says it
itself, in one of two ways:

- **A marker file**, `.observatory-keep` (`cleanup_safety.KEEP_MARKER`). At the top of a folder,
  `disk_guard.keep_reason` keeps it before any walk. Anywhere deeper, the shared entry predicate
  refuses the tree, so the guard's walk and `cleanup.py`'s build-output walk
  (`protected_tree_reason`) keep it too.
- **A Fabric service descriptor.** The contract's descriptor already declares `paths.data`,
  `paths.config`, `paths.cache` and `paths.logs`; no new field was added.
  `disk_guard.service_state` reads the local descriptors (`fabric_service.services_dir()`,
  remote ones skipped) and treats `data`, `config` and `logs` as state: a folder that is such a
  path, holds one or lies inside one is kept. `paths.cache` stays removable, because the contract
  calls it a cache; a service that keeps live work there writes the marker. Paths compare without
  case on macOS and Windows, lexically and resolved; a declared path in a privacy-guarded place is
  not resolved.

**Which classes.** The planners of cache folders, build output, temporary entries and Trash consult
both, and list what they keep in the report. `act` checks the marker again right before every path
removal, browser clones included, against the descriptors as last read (below). The tools' own
cache commands (`uv cache prune` and the like) remove nothing by path and are not covered.

**When the descriptors are read.** Once after a run takes the lock, for planning, with a deadline
of a tenth of the run's time (at most 30 seconds); the time is in the report's
`phases.service-state`. Then once more at the start of each class's removal phase, when that class
has something to remove (deadline a twentieth of what is left, at most 10 seconds), so a service
installed while the run planned is honoured before anything of that class goes. They are NOT read
again before each single removal: a service installed during one class's removals is honoured from
the next class or run. A marker is honoured at once. `act` called on its own reads them itself. The
report's `service_state` gives `complete`, the number of paths, of refused ones, and
`unknown_services`. At most 256 declared paths are resolved; past that the reading is incomplete.

**When the descriptors folder cannot be read.** No services folder means no Fabric on the machine:
nothing is degraded. When the folder itself cannot be read (it or its parent unreadable, the path
not a folder, or the read's deadline reached), that is named in `degraded` (`fabric-descriptors`),
and the cache-folder and temporary classes remove nothing that run, each saying so in `degraded`;
`act` refuses those two classes too. The folder is listed with `os.scandir`, because `Path.is_dir`
and `Path.glob` report an unreadable folder as an empty one, which once read as "no service".

**One broken descriptor makes only its own service unknown.** Each file is opened without following
a link and without waiting by `osprivacy.read_regular` (`O_NOFOLLOW`, `O_NONBLOCK` on POSIX; on
Windows a link is refused by `lstat` first), must be a regular file, and is read up to 1 MB: a FIFO named `*.json` once blocked the
guard inside the cleanup lock, and a link to `/dev/zero` was read without end. A file that fails
any of that, is not JSON (deep nesting included: its `RecursionError` once escaped the run), is not
an object, or declares a path breaking the contract's `localPath` (not a string, not absolute or
under `~`, longer than 1,024 characters, or holding a NUL character) is named in `degraded`, and
its service id is taken from the file name `<id>.<instance>.json`. For that run, the folders named
for that id are kept with the reason "descriptor could not be read": the folder name is the id, or
starts with the id and `.`, `-` or `_`, or is a reverse-DNS name whose last part is the id, case
ignored. An id shorter than 3 characters keeps nothing (`co` would match most of a cache root);
it is named only. Both classes run for everything else, so one broken file cannot stop the guard on
every run until a person removes it. A file whose name matches no service is named and keeps
nothing.

**A declaration that is too wide, or foreign, is refused.** A declared state path that is or holds a
cache root, a temporary root, the Trash, a checkout the build-output class plans from, `~/Library`,
`~/Library/Application Support`, the home folder or a filesystem root would keep other programs'
data forever. It is ignored, and `degraded` names the service and the path. A folder inside
Application Support (`~/Library/Application Support/<service>`) is honoured. A path in another
operating system's grammar (a drive letter on macOS or Linux) names nothing here and is named in
`degraded` the same way.

The report carries `kept` (class, path, reason; at most 200), and the Machine page shows it as
"Kept by the guard". `tests/test_disk_guard.py::LiveStateUnderCacheRoots` pins each rule.

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
without `lsof`, planning remains available but path actions skip. Windows holder coverage is unknown and path deletion is refused; successful
rename does not prove absence of open handles. `FreshTargetProbe` pins the bounded and unknown outcomes.

The shared full listing accepts only exit-zero, diagnostic-free, decodable
`lsof`/`ps` output. `lsof` warnings are no longer suppressed. A nonzero partial
listing cannot declare an omitted worktree free, and `running()` returns unknown
for an incomplete cache-command process survey. The unbounded synchronous Linux `/proc` fallback is retired. Its legacy
entry point returns unknown without enumeration or file reads; missing `lsof`
cannot trigger a second unbounded inventory. No external diagnostic text is retained.
`HoldersCompleteness` covers these negative and complete positive observations.

Both clean and archived dirty worktrees now use the same target-scoped
`holders.fresh_busy` after Git/archive validation and immediately before removal.
The shared 20-second inventory is only a preliminary refusal cache. The final
probe has at most five seconds or the smaller remaining action/archive budget;
held, unknown or exhausted observations preserve the worktree. The disk tier
already used this strict final probe; the change closes the git tier's cached
observation gap. `WorktreeFinalHolders` exercises both tiers with held, unknown,
free and expired-budget outcomes. Worktree age remains a planning-time condition;
this change does not add an action-time idle-age measurement. Windows now preserves path targets because holder coverage is unknown;
rename/delete sharing cannot supply the missing evidence.

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

## Windows holder coverage correction

`FILE_SHARE_DELETE` permits an open Windows handle to coexist with rename/delete
access. Therefore the old rename-probe convention cannot establish non-use.
`holders.Holders.take()` returns unknown on Windows; `fresh_busy()` returns `None`.
An old `rename` mode also refuses path actions. Generic cache commands keep their
separate tool policy; this is not a claim that every Windows maintenance operation
is disabled. Native inventory support remains unimplemented.

Evidence: `WindowsHolderUnknown` and the legacy-mode case in engine
`tests/test_disk_guard.py`; the native shared-delete control runs only on Windows.
[Microsoft CreateFile sharing contract](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew).

## POSIX argv completeness correction

Rendered `ps` text is not a lossless argument protocol. A closed script can remain
active while a target-specific lsof query is empty; spaces, relative paths and
symlink aliases defeat the existing lexical path heuristic. Positive file or
command hints still refuse removal, but unmatched text is unknown. Production
`Holders.take()` retains its observed paths with `known=false`; `fresh_busy()`
returns true for a positive hold and otherwise unknown. Consequently current
production path-removal tiers refuse instead of inferring lifecycle inactivity.
The legacy manually constructed `Holders([...])` complete snapshot remains an
explicit test/consumer input, not the completeness state returned by production.

`running(nonempty_names)` returns true for a recognized tool and otherwise unknown.
An empty requested set is vacuously false. Cache commands with a nonempty `busy_if`
refuse unknown; positive daemon requirements and commands without holder preconditions
retain their existing separate policy. No native argv collector, owner lease or
extra process/environment persistence is introduced here. Native argv could improve
positive evidence but would not recover a process's launch cwd or prove universal
non-use after arguments are modified.

Acceptance: `tests/test_disk_guard.py::LossyArgvEvidence` and `LossyArgvAdmission`
cover four missing path forms, positive holds, incomplete snapshots, stale-free
path/clean-worktree/archived-dirty-worktree admission and cache child suppression.
The source-bound research and original failing assertions are described in
[the run packet](../runs/2026-10-13-posix-holder-argv/README.md).

## Avoid process inventories that cannot admit cleanup

The three preliminary callers (guard class admission, cleanup planning and cached
cleanup refusal) request `Holders.take(collect_hints=False)`. This performs no
lsof/ps/proc collection and returns unknown with `observation=not-attempted`.
Guard phase metadata carries a null measurement time rather than inventing a
process observation. Unknown still preserves paths; it does not enable cleanup.
Default take() retains positive hints and incomplete coverage. Final target probes
and cache-command preconditions preserve unknown refusal. Explicit captures use
the bounded contract below; avoided calls are not measured host RAM reclaimed.
Five `CheapHolderAdmission` cases in `tests/test_disk_guard.py` exercise the API,
all three callers and unchanged positive-hint behavior.

## Bounded explicit holder observations

`holders._capture` reads POSIX child pipes incrementally, with a combined stdout
and stderr cap of 2 MiB and reads of at most 64 KiB. Retained stdout stays within
the cap; stderr contributes only a flag/count and is discarded. Parsers accept at
most 100000 fields, 10000 retained path hints and 4096 bytes per token/path. These
limits bound payload and collections, not total Python or child RSS. A limit,
decode/read error, diagnostic or incomplete exit produces unknown, never free.

Each operation shares one monotonic deadline across lsof and ps. Cleanup time is
reserved inside that budget; a final target probe uses at most five seconds or
the caller's smaller remainder. Expired or invalid budgets spawn nothing. Only
the newly created POSIX process group is signalled on incomplete capture or
unexpected surviving descendants; the direct child is reaped and pipes closed.
Escaped descendants, uninterruptible process creation and scheduler delays are
outside this normal-execution bound. No discovered holder or service is stopped.
Windows remains unknown without a capture. No raw command text or diagnostics
are persisted. The existing lossy-argv and lifecycle limits still apply.

`tests/test_holder_capture.py` exercises actual owned flood, deadline, pipe-holding
descendant, outside-child survival, descriptor and parser boundaries.
`tests/test_disk_guard.py::LossyArgvAdmission` retains action/cache refusal tests.
See [source and validation packet](../runs/2026-10-13-bounded-holder-capture/README.md).
