# Cheap worktree registration independent of hygiene classification

## State now

Collector slice implemented; disk-guard caller and build-sweep fairness are a
separate stacked slice. Base: `2d25b9f7a51ea4e4155cb905e6ef45da11899b25`.
No operational registry, worktree or service was changed by this work.

## Scope and contract

Run from the engine directory:

```sh
python3 collectors/scan_git_hygiene.py OUT --registrations-only --budget-seconds 20
```

The caller supplies a separate `OUT`, conventionally the workspace's
`store/raw/worktree-registration.json`. The command performs `git worktree list
--porcelain` through `safe_git`, without process enumeration, default-branch
resolution, working-tree status or branch classification. Its budget also honors
a shorter `OBSERVATORY_STEP_DEADLINE`. Each registration Git call receives at most
five seconds and never more than the remaining pass budget.

Output contract (`schema_version: 1`, `kind: worktree-registration`):

- `measured_at`: pass start; not the freshness of every row.
- `repositories[]`: `repository`, anchor `path`, `attempted_at` when attempted,
  `registered_at` from the last successful listing, `stale`, and `worktrees[]`.
- A worktree: `path`, `head`, `branch`, `locked`, `prunable`, `bare`. Main and
  linked worktrees are included. Registration is metadata, never a clean/dirty
  classification or deletion permission.
- `partial`, `degraded`, `attempted`, `cursor.last_attempted`.

Only rows with `stale: false` were listed successfully in this pass. A failed or
unreached row retains the old worktree list and its original `registered_at`,
marked stale; a never-listed row has no registration time and an empty list.
Source/read/list failures remain explicit in `degraded`. One unreadable registry
does not prevent reading the other. Exit zero means the bounded pass produced its
receipt; callers must inspect `partial` and row freshness.

Seeds are deduplicated by real anchor path. Both repository local paths and Git
folders named by the project source contribute. A linked anchor's `.git` file is
accepted through Git; an unresolved default branch does not block registration.
Different anchors for the same Git repository can produce overlapping worktree
lists, so callers deduplicate worktree paths before planning.

The next pass starts cyclically after `cursor.last_attempted`, including failed
anchors. The cursor and all current rows are atomically checkpointed **before**
the next listing; interruption inside Git cannot make that same slow anchor the
first one forever. This cursor schedules registration roots; it does not resume a
build-directory traversal. No cleanup actions are cached or replayed.

## Validation

`observatory/engine/tests/test_machine.py::WorktreeRegistration` initially failed
all five first regressions because the old collector still invoked process
classification. After implementation, eight registration tests and ten existing
`Estate` cases passed (18 total): linked-only source, unresolved default, source
failure, per-root failure, preserved old timestamp, short-pass fairness, interrupted
Git checkpoint, and outer deadline. All fixtures have isolated HOME and workspace.

## Next task and limits

Integrate the caller in `tools/disk_guard.py`: reserve a bounded registration pass
before build planning, consume its fresh rows independently of expensive hygiene,
and retain hygiene's own old timestamp/stale labels. Add a separate fair build-root
cursor and propagate remaining time to any full hygiene refresh child. Those caller
changes are not implemented by this commit. The complete gate, hosted CI, installed
upgrade and live acceptance remain separate checks. Disk mutations remain governed
by the existing holder, ownership, privacy, content and act-time rechecks.

## Independent-review correction: full survey degradation

The full survey originally discarded the new seed reader's errors and marked only
deadline carries partial. Review reproduced four false-complete outcomes: a failed
registry with a surviving source, both registries missing, unresolved default, and
an unreadable worktree list. A malformed prior registration (`repositories: 7`)
also raised instead of producing a receipt. All five negative cases were observed
failing before the correction.

Full hygiene now propagates source errors, marks any degraded result partial,
and retains old unavailable-root classifications explicitly stale with their old
time. Missing source evidence does not establish that a previously seen checkout
vanished. An unavailable worktree/current-branch/branch listing cannot publish a
fresh empty classification. Invalid prior registration rows are named and omitted;
successful new rows remain independently fresh. The focused registration and
existing Estate selection passed 23 tests after this correction.
