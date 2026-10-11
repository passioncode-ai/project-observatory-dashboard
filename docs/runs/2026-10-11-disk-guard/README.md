# 2026-10-11 — the disk guard (OBS-63)

Handoff for the next agent. No private names, hosts or paths here; the machine is "one
maintainer's machine".

## Objective

The operator's disk kept running out. Make the self-cleaning automatic, regular and
repeatable, never breaking a live session: forgotten worktrees and branches, caches and
global caches, build output that piles up, temporary files, the Trash, large files, and
an account of what memory and swap cost the disk.

## What was measured before anything changed

On one maintainer's machine (494 GB volume), 2026-10-11:

| Measurement | Value | How |
|---|---|---|
| Free space | 2.7 GB at the start, 0.7 GB at the lowest | `df -k /` |
| Last time the cleanup's auto tier ran | 2026-10-10 02:26, a day and a half earlier | `store/raw/cleanup-plan.json` `planned_at` |
| Why | `machine` and `cleanup` logged "not started — the last 300 s before the tick's ceiling are kept…" on every tick | `store/logs/tick.log` |
| Machine survey age | 35 hours | `store/raw/machine.json` `measured_at` |
| Swap | 22.5 GB used of 23.5 GB, on the same APFS container | `sysctl vm.swapusage`, `/System/Volumes/VM` |
| Abandoned browser code-sign clones | 19 clones, "13 GB" by `du`; removing them freed 0 MB | `df` before and after |
| Temporary entries older than a day | 21,563 in the per-user temporary directory | the guard's first plan |

Two conclusions shaped the design. The cleanup sat in the middle of a tick that a loaded
machine never finishes, so it starved exactly when the disk filled. And `du` is not a
measure of what a removal gives back on APFS: clones share blocks.

## What shipped (branch `agent/disk-hygiene`)

- `observatory/engine/tools/disk_guard.py` — the guard. Pressure from free space
  (`normal`, `low` < 25 GB, `critical` < 10 GB) moves every threshold. Classes, in yield
  order: the cache tools' own cleanup, git-ignored build output of idle checkouts,
  temporary entries, the Trash, browser code-sign clones; then the cleanup's git auto tier.
  One `lsof` listing per run, re-checked at each removal; without it, no path is removed.
  Freed space is the `statvfs` delta per action (`freed_mb`). Planning has a time budget.
  Largest files under the home directory, daily, reported only. A memory report from the
  machine survey: who holds the memory, running simulators with the command that stops each.
- `maintenance.step_guard` — the hourly maintenance job runs the guard first in every pass,
  before the update and the backup, when `features.auto_cleanup` is on; a crash in it never
  stops the pass. The guard's summary comes back as one JSON line on stdout.
- `full disk-guard [--apply] [--level L] [--large-files]`; `full --help` names it.
- Findings: `machine.guard_idle` (auto cleanup on, no report for three hours);
  `machine.disk_low` now names the guard's last run, the swap holders, the simulators and
  the largest files; `cleanup.done` counts freed space by the disk's own number.
- The Machine page: the guard's last run, what it planned, the largest files, running
  simulators. `observatory_machine` gains a `guard` section.
- `defaults/cleanup.json` → `guard`; docs: `docs/ONBOARDING.md` (The disk guard),
  `docs/AGENT-ONBOARDING.md`, scenario OSS-22; backlog OBS-63, OBS-64, OBS-66, OBS-15 superseded (the alias layer requested the same day is OBS-65, filed by its own pull request).

Operator decisions taken in this run (2026-10-11): supersede draft #109; build output by
pressure 14 / 3 / 1 days; Trash after 30 days (3 at `critical`); memory and processes are
reported, never stopped.

## Requirements and their evidence

| REQ | Requirement | Evidence |
|---|---|---|
| R1 | Runs by itself, hourly, independent of the tick | `maintenance.step_guard`; `tests/test_maintenance.py` Guard `test_it_runs_before_the_update_and_a_crash_never_stops_the_pass`, `test_on_runs_the_auto_tier_and_records_the_disk` |
| R2 | Repeatable by hand | `full disk-guard`; Surfaces `test_the_command_line_takes_the_guard_and_refuses_an_unknown_level` |
| R3 | Never breaks a live session | Holding (both cases), Acting `test_a_path_that_became_held_after_the_plan_is_skipped`, Run `test_no_listing_of_open_files_means_no_path_is_removed`, Temp `test_old_unheld_entries_go_fresh_held_and_kept_ones_stay` (held file, live socket), Builds `test_idle_checkouts_lose_ignored_output_and_busy_or_recent_ones_keep_it`, `test_git_at_work_holds_the_checkout`, Acting `test_a_cache_command_waits_for_its_tool_and_never_starts_a_daemon` |
| R4 | Forgotten worktrees and branches | the cleanup's auto tier, run by the guard; existing `tests/test_machine.py` Estate |
| R5 | Builds do not pile up | Builds (idle by checkout/commit/uncommitted file, never the index; tracked directories kept) |
| R6 | Project and global caches | `plan_commands`; Acting `test_commands_are_chosen_by_pressure_and_by_what_is_installed`, `test_a_tool_that_says_its_cache_is_in_use_is_skipped_not_failed` |
| R7 | Temporary files and system leftovers | Temp (age, nested roots, level-gated roots, symlinks); Clones |
| R8 | The Trash | Trash `test_items_go_by_when_they_were_trashed`, `test_an_unreadable_trash_is_named_not_guessed` |
| R9 | Large files found | LargeFiles (privacy-guarded places never walked; fresh result reused) |
| R10 | Processes and memory analysed | `memory_report`; Surfaces `test_the_disk_low_finding_names_the_guard_the_swap_and_the_largest_files` |
| R11 | Honest numbers | Acting `test_a_removal_records_what_the_disk_said`; Surfaces `test_freed_space_is_the_disks_count_when_a_row_has_one` |
| R12 | Visible to both readers | Surfaces page tests (both languages, no escaped markup), finding `machine.guard_idle`; MCP `guard` section; docs above |
| R13 | Bounded | Temp `test_a_spent_budget_stops_planning_and_says_so`; Order `test_commands_first_then_largest_paths_and_the_cap_is_per_class` |

Every guard named above was watched failing against a planted defect: busy check off,
no-lsof treated as free, sockets ignored, the index read as activity, `check-ignore`
skipped, symlinks followed, guarded places walked, no re-check at removal, a tool's
`busy_if` ignored, the level word passed as an escaped argument.

## Live runs on the maintainer's machine

| Run | Pressure | Result | Free space |
|---|---|---|---|
| 1 | critical | 500 removed, all temporary entries — the cap was spent on the smallest items | 1.5 → 1.4 GB (others were writing) |
| 2 (yield order, per-class cap) | critical | 2,064 removed, 1 skipped on re-check, 1 failed (`uv` cache in use; now a skip) | **1.3 → 6.9 GB** |
| 3 (an hour later, during a full local test run and other sessions' installs) | critical | 2,007 removed, 3 skipped on re-check, 0 failed | 2.1 → 0.8 GB: what it freed was outpaced by what others wrote at the same time; the per-row `freed_mb` is the disk's delta over each action and includes their writes |

Run 2 by class, by the disk's count: temporary entries ≈ 3.5 GB, `pnpm store prune`
0.46 GB, Homebrew 0.73 GB; build output found only `__pycache__` — every large
`node_modules` sat in a checkout worked in that day, and stayed.

## What the run got wrong, and the check that now catches it

1. **A test reached the machine's own browser clones.** `plan_clones` found its directory
   through `tempfile.gettempdir()`, which the test's `HOME` patch does not move, and the
   test's empty `lsof` listing marked them free: 19 real clones were removed. A live `lsof`
   taken minutes earlier showed none of them open and the guard's own plan had listed the
   same 19, so nothing running lost its clone — but that was luck, not design. Now
   `clone_root()` is one function every test patches, and `tempfile.tempdir` points into the
   test's directory (`tests/test_disk_guard.py` Base).
2. **The action cap starved the classes that matter**: 500 tiny temporary entries, 46 MB,
   and no cache command or build output reached. Commands first, paths largest first, a
   cap per class (Order).
3. **The page printed markup**: a translated word passed as a format argument is escaped.
   Found by opening the page in WebKit, not by a test; the test now asserts it.
4. **`uv` refuses while a long-running `uv tool run` holds its cache** and exits 2: that
   was a "failed" every hour. Now a skip when the tool says its cache is in use.
5. **The report file was read by path** from the maintenance job, which `check_paths`
   refused (the scratch directory can be redirected); the guard now answers in one JSON
   line on stdout.
6. While checking the page, one dashboard build was pointed at the live workspace with
   the wrong variable name (`OBSERVATORY_DASHBOARD` is the single page's), so that one
   generated page was rebuilt from this branch; the next tick rebuilds it.

## Checks actually run

- `tests/test_disk_guard.py`: all cases pass (3.14); `tests/test_maintenance.py` Guard pass.
- `tests/run_portable.py --suite i18n --suite machine --suite maintenance --suite disk_guard`.
- The full portable set locally: twice stopped by the runner with `OSError` when the disk ran out (221 of 232 suites had run the first time, with failures in `api_listings`, `git_hardening` and `osprivacy` that were this branch's — git outside `safe_git`, a uid outside `osprivacy`, a report read by path — and are fixed; `lifecycle` and `conformance_receipt` failed under the full disk and pass on re-run). The complete set ran in CI instead: `test` on ubuntu 3.11, ubuntu 3.14 and macOS 3.14 and the Windows job, all passing on the pull request.
- `python3.11 -m compileall` on every changed file; `tools/check_paths.py` 0 violations;
  `tools/update_inventory.py --check` passed; `tools/check_public_release.py` passed.
- The Machine page built from the live workspace into a scratch directory and opened in
  WebKit at 1440 px (Russian) and 390 px (English): no horizontal overflow.

## Open work

- OBS-66: the page's `du` sizes overstate clone-heavy places.
- The guard does not yet run more often than hourly when pressure is `critical`; a
  cheaper free-space check between passes is the obvious next step if hourly is not enough.
- Swap is the largest single consumer when many agent sessions and simulators run; the
  guard reports it and never stops a process (operator decision). Reducing it is the
  operator's call.
- OBS-64 (demo findings in one language) arrived from another session during this run and is on the board; the alias layer it also asked for is OBS-65, in its own pull request.

## Next task

Merge the pull request after the three required checks, release it with the next
version, update the maintainer's installation, and confirm in `store/maintenance.json`
that the hourly pass records `guard` with `result: ran`.
