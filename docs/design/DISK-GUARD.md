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
| One `lsof` listing at the start, reused for minutes of removals | **Fresh holders per class**, plus a re-check of the target right before removal: still held? still old (an early-stopping walk)? |
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

## Not done here, and why

- A person's `full update --apply` started while a pass runs is refused by the snapshot's
  stillness check; the update names the running pass. Making it wait is the update's design
  (OBS-40 family), not the guard's.
- Case-insensitive APFS paths written in the wrong case in a person's own roots do not match
  `lsof` names; the shipped roots are cased correctly.
