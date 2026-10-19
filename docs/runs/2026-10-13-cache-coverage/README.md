# OBS-87: cache lifecycle coverage receipt

<sub>ssheleg skills — task-pipeline · evidence-docs · copywriting · skill-search</sub>

## State now

Source `a68c81c` passed independent review after three reproduced corrections:
nonregular configuration source blocking, empty schema reported complete, and
malformed prior-row metadata crashing recovery. Eleven focused tests pass. The
original packet and earlier eight-case result remain below as development history.
Combined, hosted and installed acceptance are separate; OBS-87 remains partial.

## Packet and source map

`defaults/machine.json` declares aggregate application/tool caches, package stores,
DerivedData, SDK/toolchain, simulator and VM locations. `scan_machine.survey_disk`
recursively sizes configured locations; absent paths disappear from its list and old
sizes retain their source time. `size_kb` accepts a `du` total even after a nonzero exit,
so the old receipt cannot prove completeness. This adapter must retain that uncertainty.

`defaults/cleanup.json` declares broad cache roots, exclusions and application-managed
commands. `disk_guard.plan_cache_dirs` uses filesystem age/holders and protected-tree
checks; its claim of regenerability is not typed ownership or restoration evidence.
`plan_commands` lists tools and initializes estimated bytes to zero. Inventory must
not treat that value as a measurement or a promise of reclaimable bytes.

Implement `full machine --cache-coverage`: read those configuration declarations and
existing machine evidence, stat only non-protected declared root paths, and locate
executables without invoking them. No recursive walk, process scan, cache command,
service call, purge, deletion or planner mutation. Keep protected app containers and
OS-managed cache areas explicitly uncovered without accessing them.

Rows name category, declaration source, independent presence/tool/size timestamps,
coverage omissions and refusal reasons. Ownership, regeneration proof, current use,
restore cost and reclaimable bytes remain unknown. A configured `regenerable` string
is only a declaration. Existing rounded `gb` values are carried as `du-reported` with
unknown completeness and their original timestamps, never converted into exact bytes.
Bound row count, total elapsed work and persisted fair cursor. A partial empty receipt
must remain incomplete, and source failure must not erase prior observations.

## Synthetic acceptance

- Present, absent, denied and symlinked roots are distinct; zero size remains zero.
- A protected root is never stat'ed, even if a configuration names it as cache.
- No `du`, recursive enumeration, process argv or application command is called.
- Existing size keeps its old timestamp and unknown completeness/reclamation.
- Missing/malformed source rows and budget omissions are explicit, never zero.
- Command availability is executable presence only; bytes and daemon/use stay unknown.
- A bounded pass resumes beyond an expensive early row; older rows stay stale.

Next: implement and independently review the bounded adapter. The packet does not
close ownership, live runtime admission, safe regeneration, cache deletion, scheduled
inventory or UI/MCP integration. OBS-87 remains partial.

## Implemented and validated

`cache_coverage.collect` implements the bounded metadata adapter and the machine CLI
routes `--cache-coverage` to it. The output separates locations, command references,
uncovered system areas and source-read evidence. Command arguments are never copied;
credential-shaped executable declarations are rejected. Command rows always carry
unknown reclaimable bytes, regardless of legacy planner `bytes: 0`. Owner and
regeneration remain unverified. A single-writer lock preserves the fair cursor.

The first five tests failed before implementation. Eight focused synthetic cases now
pass: presence/absent/denied/symlink distinctions, protected-root no-stat, old measured
zero, non-execution, command argument suppression, bounded cursor continuation, failed
size source retention, unavailable policy no-probe, malformed nested configuration and
CLI uncovered-area semantics. Compatibility: machine 59 and checkout inventory 12 cases
passed, jobs 1. No recursive or live cleanup run was used for validation.

The adapter deliberately does not verify ownership or regeneration, inspect contents,
measure current holders, size the disk, control daemons or change cleanup planning.
`partial: true` and `coverage_complete: false` remain explicit. Installation, scheduling,
UI/MCP delivery and independently verified safe lifecycle adapters are separate work.

Next: independent exact-commit review, then decide the next typed ownership adapter from
actual resource-owner evidence. Source configuration alone cannot close that gate.

Source inventory and public pattern privacy checks passed (exit 0). History/private
name checks and installed acceptance were not run as part of this candidate.

**Made with [ssheleg skills](https://github.com/ssheleg/sshlg-skills)**

- [`task-pipeline`](https://github.com/ssheleg/task-pipeline): read-only cache inventory.
- [`evidence-docs`](https://github.com/ssheleg/task-pipeline): source map and limits.
- [`copywriting`](https://github.com/ssheleg/super-ux): CLI coverage wording.
- [`skill-search`](https://github.com/ssheleg/make-skill): bounded route selection.

## Independent-review corrections

Source files are refused when they are symbolic links, have a symlink ancestor, or
are not regular files. Nonblocking no-follow opens and descriptor identity checks
prevent a replaced FIFO from blocking at open. Reads remain capped at 1 MiB; kernel
filesystem stalls are outside the application deadline guarantee. A missing
configuration alone permits defaults; unreadable or invalid existing configuration
is degraded and never silently replaced by defaults.

Missing required declaration collections now differ from explicit empty lists.
Malformed old location metadata is skipped with a reason while valid peers remain
carried and stale when their declaration source fails. The three tests first failed
against the initial candidate: FIFO timeout, false completeness for empty objects,
and lost prior peers through a symlinked policy. Eleven focused synthetic cases pass
after correction (`test_cache_coverage.py`), jobs 1. No live inventory was run.
