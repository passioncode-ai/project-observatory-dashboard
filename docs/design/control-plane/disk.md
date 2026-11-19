# The Disk page (M13, OBS-562, CP-16)

## What

Disk and cleanup as their own page, `disk.html` ("Disk"), in the System group after Machine. Its question: how much
space is free, what the guard and auto cleanup removed and keep, and how they are set. The disk guard's last pass, the
largest files, the folders the guard keeps and the cleanup tables moved here from the Machine page; the Machine page
keeps one line (auto cleanup on or off, when the guard last ran) with a link, and the running simulators (they hold
memory, not disk space). Every setting the configuration door (M14, [configuration.md](configuration.md)) declares for
the guard and the cleanup is shown and can be changed from the page.

## Data

- `machine_view.summary()`, as the Machine page reads it: `disk.volume` (free space now), `guard` (the last pass: free
  before and after, pressure, planned classes, `kept`, `largeFiles`), `cleanup` (`autoEnabled`, `counts`, `journal`,
  `guardRuns`, newest first as `guardRunsOrder` says, `guardLast`, the last pass on its own, and `guardTotals`, the
  guard's run summaries of 7 days, freed by the disk's own count) and `degraded`
  (the page names the gaps of `machine.json`, `disk-guard.json` and `cleanup-plan.json`).
- `machine.diskSettings`: `configuration_door.listing()` cut by `disk_page.settings_of` to the keys in
  `configuration_door.DISK_SCOPE` (`features.auto_cleanup`, `guard.*`, `cleanup.auto.*`), each row with `value`,
  `default`, `set` (a file sets it) and `file`, plus the listing's `degraded`. Written by `build_dashboard.build()`.
- Free space has no history in the payload: the trend is the last pass (before and after) and the passes of 7 days.

## Wireframe

```
Disk
How much space is free, what the guard and auto cleanup removed and keep, and how they are set.
[Free disk 31.4 GB | Last guard pass 2026-11-10 | Auto cleanup on | Freed in 7 days 11.3 GB]
Auto cleanup is on: the guard and the auto tier run from the maintenance job.
Disk guard ran ...: free 20.1 -> 31.4 GB, 42 removed . pressure low
[Disk guard: what / planned]  [Kept by the guard]  [Largest files]
[Guard passes in 7 days: when / pressure / removed / freed]
[What auto cleanup removes and keeps: what / tier / now / kept for]
[Cleaned in the last 7 days]
Settings
  This page was opened as a file, so it only shows the settings. ...   (tooltip names observatory_configure)
  setting (key) | value | default | source | [number] [Preview] or [Turn on]
[Change preview: label: before -> after . Apply . Cancel]  then  [Applied . Undo]
[Not measured: source . reason]
```

## Contract

- Pages are static HTML. The live route is the key server's, the pattern of the Keys page's actions (`ACTIONS` in
  `tools/keyserver.py`, `call()` in the page script): the server writes its token into the page it serves; the page
  posts `/api/configure` with `X-Observatory-Token`, the same Host, Origin and token checks, and the audit line written
  before the act (key names only). The status server (`serverd.py`) stays GET-only and has no person's actions.
- `POST /api/configure` with `{op: "plan", changes}`, `{op: "apply", digest}` or `{op: "undo", journal}`. Every key
  (of the changes, of the plan the digest names, of the applied change the journal id names) must be in the
  catalogue and in `DISK_SCOPE`, else 400. It calls `configuration_door.plan`, `apply` or `undo` with the actor
  `person:page`, which passes the person's gate as `person:cli` does (`decision: {by: person, via: page}` in the
  journal). The answer never carries a command line. A delegation token is refused for this action: an agent uses
  `observatory_configure`, so `parity.json` records the action as `covered` by that tool.
- The page builds every control disabled with a tooltip naming `observatory_configure`. The page script enables them
  only when `LIVE` (served over http with the token): a control asks for a plan, the page shows each change's
  before and after and whether it is the person's decision, Apply applies that digest, Undo undoes by the journal id.

## Failure behaviour

- Opened as a file, or by a server without the token: the controls stay disabled and the explanation says so.
- A refusal (unknown key, a key outside the scope, an invalid value, a stale or spent plan, an undo after a later
  write) is a 400 with the door's sentence; the page shows "not changed" with it. A fault after the act may have begun
  is shown as unknown, never as failed.
- A settings file the door cannot read: its rows show "unreadable" (never the default) and the file is named under
  Not measured. A guard that never ran says so without a command; a survey not run is named under Not measured.
- No command line is shown on a row of this page.

## Tests

`observatory/engine/tests/test_disk_page.py` (suite `disk_page`): a guard that ran, one that never ran, auto cleanup
off, a degraded source and an unreadable settings file, values with their defaults and sources, disabled controls with
the tool's name, a label for every key the door declares, the navigation and the build, the Machine page's line and
link, and the route (plan, apply, undo as the person; an unknown key, a key outside the scope and a plan made for
another key refused; refused without the token). `tests/test_i18n.py` holds the catalogue complete;
`tests/test_agent_parity.py` holds the action to `observatory_configure`. Scenario: OSS-117.

## Unused Docker volumes (OBS-584)

The manual tier's section "Unused Docker volumes", after the cleanup classes: per Lima or Colima VM each Docker
volume no container uses (`dangling`), its size and the owner its labels name (`com.docker.compose.project`, else
`com.docker.stack.namespace`), largest first, for the owner to delete. A VM whose engine did not answer is listed
under "Not listed" with the reason, never shown as having none; a guard that has not listed them yet says so.

- Data: `disk_guard.vm_unused_volumes(seconds, run)` (the process runner injected) reads each running VM's own
  engine through its socket (`engine_socket`, never the context or `DOCKER_HOST`) with two reads only,
  `VOLUME_READS`: `volume ls -f dangling=true` (names and labels) and `system df -v` (sizes, asked only when
  something is dangling). A run that acts, or `--refresh`, writes it as `vm_volumes` in `disk-guard.json`;
  `machine_view` carries it as `guard.vmVolumes`, and `observatory_machine` returns it under `guard.vmVolumes`
  (`null` when not listed).
- The guard NEVER removes a volume: `VOLUME_READS` and `ENGINE_PRUNES` carry no remove verb and no `--volumes`
  (asserted by `UnusedVolumes.test_the_guard_never_removes_a_volume`). No command line is printed on a row; a
  volume name that is not a plain Docker name is dropped.
- Tests: `test_disk_guard.py` (`UnusedVolumes`), `test_disk_page.py`
  (`test_unused_docker_volumes_are_listed_for_their_owner_and_never_removed`). Scenario: OSS-131.

## The user's temporary folder (OBS-585)

Two reports on 2026-11-16: the user's temporary folder filled the disk (about 100 thousand entries in two days, 8 GB;
a test suite's `mkdtemp` folders without cleanup made 86 folders every 10 minutes). The guard now treats that folder
as a class of its own, `user-temp`, and samples it on every pass for a growth finding.

- The folder: `disk_guard.user_temp_dir()`, which asks `getconf DARWIN_USER_TEMP_DIR` on macOS (a session may point
  `TMPDIR` elsewhere), else `$TMPDIR`, else `tempfile.gettempdir()`. While the class runs, the `temp` class leaves
  that root to it (`plan_temp(..., skip_roots=)`), so one entry is never planned twice.
- Removal (auto tier, `plan_user_temp`): top-level entries older than `guard.user_temp_hours` (default 12; shorter
  only when `temp_days` at the pressure is shorter; 0 switches the class off), oldest first, that no process holds
  (the complete `lsof` listing the class takes, and the fresh one `act` takes before each removal). Without a
  complete listing nothing goes and `degraded` names `user-temp`. Never a socket, a pipe, a link (not followed), an
  entry another account owns (at any depth), a name of `USER_TEMP_KEEP` (`com.apple.*`, `tsx-*`, `swift-*`,
  `TemporaryItems`) or `temp_keep`, another temporary root, a Git checkout, live state a service marks or declares,
  or one of the engine's own test sandboxes (`observatory-portable-*`) while `run_portable.py` runs or whether it
  runs cannot be read. Each removal is a journal row with `prefix` (the name class), `kb`, `age_hours` and
  `freed_mb` by the disk's own count; never file contents.
- Name classes (`prefix_class`): `tmp*`, `pytest-*`, `observatory-portable-*`, else the letters-and-separator words
  up to the first digit or made-up part (`claude-501` is `claude-*`), a name of plain words is its own class, and one
  with none is `other`.
- Growth (`user_temp_section`, in every pass's report as `user_temp`): entries and bytes per class, and those made
  since the previous pass's sample (the window is clamped to between one and six hours, shorter than the removal
  age; made means the birth time where the system keeps one, else the modification time). A class arriving faster
  than `guard.tmp_growth.entries_per_day` (1000) or `guard.tmp_growth.gb_per_day` (1) is listed under `growing`;
  for those, one `lsof -F pcn` listing names the executables holding files there (the command name only, never a
  command line or environment). The first pass has no window and measures nothing. A sample stops at 200,000
  entries or its time and says so in `degraded`.
- Finding `disk.tmp_growth` (`machine_findings.tmp_findings`): one per growing class, subject
  `machine:tmp:<class>`, warning, with the rate and the writers, so the owner of a leaking test gets a row.
- Page and agent: the Disk page's "Temporary folder" section (size, entries, the five busiest classes with their
  rate and the writer of a growing one, what the guard removed last); `observatory_machine` returns the same under
  `guard.userTemp` (`null` when not sampled). No command line on a row.

```
Temporary folder
100,512 entries, 8.2 GB, sampled 2026-11-16 10:00 UTC
The guard removes its entries older than 12 hours that no process holds open; ...
Names     Entries   Size    New a day   Growth
tmp*       98,000   7.9 GB     12,384   growing fast . python3.12
claude-*       12   0.1 GB          0   -
Last removed 2026-11-16 09:45 UTC: 820 entries, 0.4 GB by the disk's own count
```

- Failure: the folder not found, not readable or cut short is named in `degraded`; unknown holders or service state
  remove nothing; a failed sample never stops the report.
- Not in `full cleanup`: that command is the cleanup's git tier only (branches, worktrees); path classes, this one
  included, are the guard's, planned and run with `full disk-guard` and the maintenance job.
- Tests: `test_disk_guard.py` (`UserTemp`, `UserTempPlace`, `UserTempGrowth`), `test_machine_findings.py`
  (`TemporaryFolderGrowth`), `test_disk_page.py`
  (`test_the_temporary_folder_shows_its_size_fastest_growing_classes_and_last_removal`). Scenarios: OSS-133, OSS-134.

## Virtual machine disks and swap growth (OBS-620)

On 2026-11-17 a container log inside a Colima VM grew to 10.7 GB and filled the host disk twice, while swap held about
15 GB of the same APFS container. Neither showed as growth: the free-space series keeps one number a day and the
machine survey sizes its places every few weeks. The guard now measures both on every pass.

- Data: `disk_guard.vm_disks` lists each Lima or Colima VM disk file (`_disks/<vm>/datadisk`, `<vm>/disk`,
  `<vm>/diffdisk` under `~/.colima/_lima` and `~/.lima`) with its allocated size (blocks, not the length a sparse file
  claims) and names it by `vm_name`: tool, VM and file, `key` `colima/<vm>/datadisk`. `growth_report` records each
  key and the swap in use (`swap`, from the memory snapshot's `vm.swapusage`) into `tools/disk_growth_series.py`
  (`store/disk-growth-series.json`: 48 hours of samples a key, at most 240 a key and 64 keys, numbers and keys only,
  never a path) and returns each VM row with `growth` (`{gb, window_hours, gb_per_day}` from the oldest sample of the
  last day; None under an hour of history) and `growing` (faster than `guard.vm_growth.gb_per_day`, 2).
- Swap: `swap_section` puts the swap in use beside the free space with `same_container` (`swap_same_container`: on
  macOS the swap volume and the home volume reporting one container size; None where that cannot be read and on other
  systems), its growth, `growing` (faster than `guard.swap_growth.gb_per_day`, 2, and only on the same disk) and its
  `owner` (`swap_owner`): the coding-agent sessions running now from the coding-sessions reading
  (`sessions_view.facets`) and the memory their processes hold (the machine survey's `agent:` groups). A source not
  read is None, never zero.
- Report and view: `disk-guard.json` carries `vms` and `swap`; `machine_view` returns them as `guard.vmDisks` and
  `guard.swap`, and adds each VM disk file to `disk.locations` as a place of its own (kind `vm`, label "Lima or Colima
  virtual machine disk", `growth_gb_per_day`), so the Machine page's disk table and `observatory_machine`
  (`section="disk"`) list it with the survey's places, largest first. The overview returns `guard.vmDisks` (ten rows)
  and `guard.swap`.
- Findings: `disk.vm_growth` (subject `machine:vm:<vm>`) and `disk.swap_growth` (subject `machine:swap`, naming the
  sessions and their memory), both warnings (`machine_findings.vm_growth_findings`, `swap_growth_findings`).
- Observatory never changes a VM: no VM tool is asked by these readings, and the finding's action is the VM owner's.

```
Virtual machine disks
Each Lima or Colima virtual machine keeps its disk in a file on this disk; ...
Virtual machine   File       Space taken   Last day          Growth
colima colima     datadisk   23 GB         10.7 GB in 24 h   growing fast
default lima      diffdisk   4 GB          measured from the next day
Swap holds 15 GB . on the same disk . last day 10 GB in 24 h
Swap is growing fast.
Held by 38 agent sessions, their processes holding 32 GB of memory . claude 35
```

- Failure: a history that cannot be written is `degraded` source `growth-series` and the rows carry no growth; any
  other failure is source `growth`; the report is always written. A guard that has not measured says "Not measured
  yet", a machine with no VM says so.
- Tests: `test_disk_guard.py` (`VmDiskGrowth`, `SwapOnTheSameDisk`), `test_machine_findings.py`
  (`VirtualMachineGrowth`), `test_disk_page.py` (`test_vm_disks_and_swap_show_their_growth_and_who_holds_the_memory`).
  Scenario: OSS-139.

## The manual tier proposes what was cleaned by hand (OBS-621)

On 2026-11-17 the operator had three kinds of things cleaned by hand from a full disk: repositories nobody had worked
in for 90 days whose every commit was on a remote, dependency and build folders unchanged for two weeks outside the
projects running services use, and checkouts merged into their default branch. The cleanup plan (`tools/cleanup.py`)
now proposes the same in its manual tier: never on a tick, never from the guard (`--git-only` plans none of them),
removed only when a person applies `--apply --include manual`, and each item checked again then.

- Where it looks (`manual_roots`): every checkout the hygiene survey measured and every repository directly in the
  data folder; every clean linked worktree the survey found and every checkout directly in a `manual.checkout_roots`
  folder (default `{data}/_worktrees`). A checkout is judged as a checkout, never as an idle repository.
- `repo-idle`: no commit on any ref, no reflog entry and no agent session (a folder under `manual.session_roots`,
  default `~/.claude/projects`, named by the path) in `manual.idle_repository_days` (90); clean, no stash, no commit
  that no remote branch holds, no linked worktree, HEAD contained in a remote branch (as of the last fetch); its
  project no running service's. Applying it re-checks all of that, fetches the remote that holds HEAD and checks
  again, refuses ignored files that are not build output, and writes the restore record (folder, remotes with any
  credential stripped from the URL, commit, branch) to `archive/cleanup/<day>/restore-list.jsonl`, flushed to the
  disk, BEFORE a fresh complete holder listing and the removal. The journal row carries the record and the list's
  path (`UNDO_IN_JOURNAL`).
- `deps-stale`: a folder named in `artifact_dirs` or `manual.dependency_dirs` (`.venv`, `venv`, `.tox`, `.build`,
  `.dart_tool`) that `git ls-files` names nothing inside, with no entry changed in `manual.dependency_unchanged_days`
  (14) (`disk_guard.inspect`, which also refuses sockets, protected entries and another account's), in a project no
  process works inside and no launchd or systemd job names. Applying it checks the folder's identity, `ls-files`, the
  age and the jobs again, and the whole project must be free in a fresh complete listing.
- `checkout-merged`: clean, HEAD an ancestor of origin's default branch, its index unchanged for
  `manual.merged_checkout_idle_hours` (24), no process inside; a clone (not a linked worktree) also no stash and no
  commit off the remotes. A linked worktree goes with `git worktree remove` (its branch stays); a clone gets a restore
  record first, like a repository.
- Each item names its `rule` and its size (`kb`, allocated blocks; `size_complete` false when the walk stopped), and
  the plan takes at most `manual.plan_budget_seconds` (120, and never past the tick step's deadline) and
  `manual.max_proposals` (500), the largest first; what it could not read is in the plan's `degraded` (source
  `manual-proposals`), which `machine_view` passes on. The tick (`cleanup.py --auto`, every half hour) carries the
  previous plan's proposals while `manual_planned_at` is younger than `manual.plan_every_hours` (6), so the walk runs
  a few times a day; a person's plan or apply always walks anew. A dependency folder of more than 500,000 entries is
  not proposed. The restore list is written through `journal_writer` (owner-only, fsync).
- Disk page: the classes table lists the three as `manual` ("proposed only; checked again and removed by hand"), and
  "Proposed for removal by hand" lists the items, largest first, with their rule and size. No command line on a row.

```
Proposed for removal by hand
Nothing here is removed by itself. When a person applies the manual tier, each item is checked again, ...
What                     Item                          Rule                                              Size
deps-stale               /data/alpha-web/node_modules  not tracked by Git, unchanged for 14 days, ...    2 GB
repo-idle                /data/old-bot                 no commit, reflog or agent session in 90 days ... 0.33 GB
checkout-merged          /data/_worktrees/fix-1        clean, merged into origin/main, index idle 24 ... 0.08 GB
```

- Failure: an unreadable repository, session folder or data folder proposes nothing for it and is named; a fetch that
  fails, a restore list that cannot be written, a HEAD that moved, new work, a tracked file or a holder keeps the item
  and says why in its journal row.
- Tests: `test_machine.py` (`ManualProposals`), `test_disk_page.py`
  (`test_manual_proposals_name_their_rule_and_size_and_are_never_removed_by_themselves`), `test_disk_guard.py`
  (`test_git_child_envelope_is_typed_and_exit_status_wins`). Scenario: OSS-140.
