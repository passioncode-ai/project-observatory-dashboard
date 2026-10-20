# Run 2026-10-20: the Stop hook counts this turn's changes, not the tree's (OBS-169)

Objective (operator request, 2026-10-20): stop the `observatory-log` Stop hook from writing a
record and asking for a `why` on almost every turn.

Base: `2f07e557` (origin/main) on branch `fix/stop-hook-noise`. This record is committed with the
implementation; resolve the final object id with `git log -1 --format=%H` on that branch.

## What was measured

- An audit on one machine (2026-10-20): the hook asked on every recorded turn in 21 sessions (103
  of 103 in one); one session of one repository was asked 35 times, each time "4 files changed,
  +0/-0", each with a new `mem:…@1` id. Stop latency: median 838 ms, p90 2.4 s, max 20 s (the
  hook's own kill).
- Reproduced in a fixture before the fix (`OnlyThisTurnsChanges`, red): a clean checkout with four
  standing untracked files recorded `files: 4, +0/-0, ask: true` on a turn that changed nothing.
- Cause 1, `tools/record_turn.py` `diff_facts` (at `2f07e557`, line 253): `git status --porcelain`
  was taken as the turn's work, so files that existed before the turn counted on every turn; the
  module docstring (line 8) promises one record per session.
- Cause 2, the new id per turn: before 0.25.0 the session lookup ordered by revision and kept
  continuing a promoted record (fixed in 0.25.0). Left over: `tools/corroborate.py` promotes a
  session's record while the session is still revising it, so its next changed turn starts
  another record and asks again. Ledger evidence: one session held 29 records in 34 rows on
  2026-10-19 (most minted before 0.25.0 was installed); after it, each of the two promotions of
  that session's record was followed by a new record at the next changed turn.
- Cause 3, delivery: the plugin installed by the PassionCode set is pinned at `v0.18.0`, whose
  `ask-why.py` asks whenever `recorded` is true and ignores the recorder's `ask`.
- `git status` in a checkout with eleven submodules: 0.45-1.9 s; with
  `--ignore-submodules=dirty`, 0.01-0.3 s.

## Decisions

- **The session's baseline is taken at SessionStart** (`tools/session_start.py` `baseline`,
  `tools/turn_state.py`): each `git status` entry's status, size and modification time, and
  HEAD. The record's files are those that differ from it.
- **A turn counts only when something differs from the previous turn's snapshot**, or HEAD
  moved. A turn that changed nothing writes nothing ("nothing changed this turn", not a fault),
  before the store is opened.
- **Only tracked content asks** (the operator's rule): an edit, a staged or intent-to-add file,
  a deletion, a commit. A new untracked file is recorded but not asked about until it is added.
- **No baseline (a session started before this engine): untracked files at the first Stop are
  taken as already there**; tracked ones count. An untracked file cannot be told apart from one
  the first turn created, and the failure is toward silence.
- **State outside the workspace store**, in `$XDG_STATE_HOME/project-observatory/turns/`
  (0700 directory, 0600 files, pruned after seven days), keyed by digests: no path, session id or
  checkout name inside. The snapshot gate and backups never see it.
- **A live session's record settles before it is corroborated**: `SESSION_SETTLE_SECONDS` six
  hours, `settling` in `store/raw/corroboration.json`. One record per working stretch of a
  session instead of one per tick.
- **Bounded runtime**: the recorder's git calls share an 8-second budget (5 s per call); a turn
  that runs out is unrecorded, silently ("git timed out", not a fault), and its changes are
  counted at the next turn because no snapshot was saved. Status and diff skip dirty submodule
  trees. The hook parses its payload once instead of four interpreter starts (passioncode PC-05,
  audit F12).

## Checks run (local, this branch)

- `tests/test_ask_why.py OnlyThisTurnsChanges`: red before the fix (8 failures), 7 tests green
  after.
- `tests/run_portable.py` suites `ask_why` (141 tests), `ask_why_reach`, `companion`,
  `companion_hook`, `companion_faults`, `workflow_memory`, `skill`, `session_start`,
  `corroboration`, `git_hardening`: all pass. Two `WithinTheBudget` timing cases failed once at a
  load average of 684 and pass when run alone (2.1 s for both).
- Root tests `python -m unittest discover -s tests`: 237 run; the one failure was the source
  inventory, refreshed with `tools/update_inventory.py` (`--check` passes).
- `python3.11 -m compileall` on the changed engine files; `claude plugin validate --strict` on
  the root, `observatory/engine/skill` and the plugin: passed.

## Outcome

Merged as `b8181ead` (#315), released in 0.26.0 and installed with the PassionCode set 0.2.16 (Observatory Log
`v0.26.0`): see the [release record](../2026-10-20-release-0.26.0/README.md) for the receipts. Nothing of this
unit is left open; OBS-170 (a macOS timing bound in another suite) was found on the way.
