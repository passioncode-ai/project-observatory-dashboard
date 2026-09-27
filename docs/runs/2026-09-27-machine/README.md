# 2026-09-27 — the machine: processes, memory, disk, git hygiene, cleanup (0.6.0–0.6.2)

Handoff for the next agent. No private names or ids here.

## What shipped

| Release | PR | Wheel SHA-256 (release asset, re-downloaded and compared) |
|---|---|---|
| 0.6.0 — machine survey, git hygiene, cleanup, Machine page, `observatory_machine`, eight findings | #70 | `840ec3bf6412b1550f24442ed8e3ce28084e9be580a37c9afffdaf0a20d77e44` |
| 0.6.1 — disk sizing within a time budget, oldest first; `du -x` | #71 | `4b7ed6955142b7daf1e392c681afefcb2a2f45b53bf122ee1911cf5fbf47074c` |
| 0.6.2 — swap files are a disk row; `disk_low` names swap | #72 | `db00e78c5b953bd2d45841d775ea5a26a89e8379edd2858ff14b5f5c5bb64b10` |

Each: clean `--no-local` clone, fresh venv from the built wheel; `full check` PASS (62/62 suites,
362 → 366 cases), root unittest 61 OK, package check passed, public-release history 0 findings,
hosted matrix 6/6 (macOS/Ubuntu × 3.11/3.14). From 0.6.1 on, `compileall` on Python 3.11 runs
locally before push. Planted defects, each failing a test: branch re-check off, protection off,
witr `Env` kept, busy directory ignored, no disk budget, `du` without `-x`.

## Verified on the operator's installation

- `features.machine_watch` and `features.auto_cleanup` are on. The first live tick's cleanup
  removed one patch-merged branch idle 7 days and one `dist` of a project idle 131 days; nothing
  skipped, nothing failed; each is a line in `store/logs/cleanup.jsonl` with its sha.
- On 0.6.2 a tick took 9 minutes end to end, the `machine` step within its 90 s disk budget.
- The Machine page renders in Russian at 1440 and 390 px without horizontal overflow; empty
  states render; the page reads and never acts.
- Findings on that machine the same evening: `machine.disk_low` critical (4.2 % free, naming
  the VM disk, swap and simulators), `machine.memory_pressure` (12.3 GB swapped),
  `machine.heavy_origin` for three running simulators and a VM, `git.idle_unique_branches`.

## What the run got wrong, and the check that now catches it

1. **3.11 syntax** (backslash inside an f-string expression) passed every local gate on 3.14 and
   failed the hosted 3.11 compile. Caught by CI; now `compileall` on 3.11 runs before push.
2. **The first live survey held a tick for 23 minutes**: synchronous `du` over running
   simulators' mounted images, 15-minute per-place timeouts, a timed-out `du` run twice. Fixed in
   0.6.1 (budget, `-x`, one attempt); a test fails if the budget is ignored.
3. **A self-matching wait**: `until ! pgrep -f '<script path>'` found its own shell, whose
   command line contains the pattern, and waited forever. Wait on the log (`tick done` count)
   instead.
4. **A synthetic `/Users/…` path** in a test tripped the public-release privacy gate; replaced
   before push. The gate did its job.

## Open work

1. **Next task — the operator's machine is at 4 % free.** The survey names the causes: the
   OrbStack VM disk (~28 GB), swap (~14 GB, driven by four running simulators and agent
   sessions) and simulator devices (~11 GB). This is the operator's call (stop simulators, prune
   Docker images/volumes they no longer need); the observatory reports and never stops a process.
2. `test_schema_compatibility.test_many_concurrent_first_opens` failed once under heavy memory
   pressure (5 s queue timeout) and passed 5/5 on re-run; not touched by this work. Worth a
   longer timeout or a load-independent assertion.
3. The 42 idle unique branches (manual tier) wait for the operator: `full cleanup --apply
   --include manual` bundles them before deleting.
4. The `analytics.stale` remedy text still names a command the step runner refuses (open since
   the 0.5.0 receipt).
