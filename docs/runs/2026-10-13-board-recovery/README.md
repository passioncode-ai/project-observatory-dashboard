<sub>ssheleg skills — skill-search · evidence-docs · task-pipeline · agent-sync</sub>

# Board recovery — 2026-10-13

## State now

The board's eight lint failures are corrected. All 79 existing OBS IDs are retained;
eight reserved rows bring the board to 87, with the next-free marker at OBS-88.
The live-board test now runs as an ordinary required test. The new rows remain open:
this change does not certify their implementation or a running installation.

Next task: independently review this board change, then land it before branches that refer
to OBS-84 (cleanup preservation), OBS-85 (runner receipts), OBS-86 (guard apply-time
safety) and OBS-87 (survey coverage). Rebase #209 and change its conflicting OBS-76
reference to its reserved OBS-80. Preserve OBS-81/82/83 for the already planned alias
interface, proposal withdrawal and agents registry.

## Objective, scope and decisions

Restore a usable, evidence-backed execution board without renumbering existing tasks,
closing unfinished operations or copying private operational details into public source.
Scope is the board, its live test, the stale coordination sentence in AGENTS.md and this
handoff. Product-package rebaselining and implementation of the eight new rows are separate.

The evidence sources are this repository's base
[`ad93c46369976e7ad9c10d11d9da00afb6663d95`](https://github.com/passioncode-ai/project-observatory-dashboard/commit/ad93c46369976e7ad9c10d11d9da00afb6663d95),
[PR #252](https://github.com/passioncode-ai/project-observatory-dashboard/pull/252),
[PR #253](https://github.com/passioncode-ai/project-observatory-dashboard/pull/253),
[the release entries](../../../CHANGELOG.md) and [the execution board](../../backlog.md).
The requirements are: retain every old ID; reserve new IDs through the remote CAS counter;
make board lint and its live test pass; preserve unfinished work as open; record the exact
checks and next task. [evidence.json](evidence.json) records each outcome.

## What went wrong

PR #252 (`74a4c7402f0de8afc6593641072fbea6c1e97a98`) installed the OBS register before
its required next-free marker and deliberately made the live-board test an expected failure.
The change's commit message explicitly deferred the marker to another unit of work. Thus
`reserve OBS` could not operate even though the coordination documentation advertised it.
PR #253 did not overwrite the board: `git diff ad93c463^ ad93c463 -- docs/backlog.md` is empty.
The prevention is the ordinary `test_the_live_board_is_clean` assertion, which now fails a
candidate missing the marker or carrying any other live-board violation.

The other corrections escape OBS-55's table pipe, leave OBS-56 and OBS-61 open while their
remainders exist, keep OBS-75 open pending a consumer-switch and revocation receipt, date
OBS-65 to its published release, and record OBS-63's shipped baseline separately from
new safety and coverage work. They make no provider calls and revoke nothing.

## Reservation and preservation

The base board contained OBS-01 through OBS-79. At bootstrap, the remote had no OBS counter,
and 231 available local/remote-tracking branch boards had neither a next-free marker nor a
row numbered 80 or higher. The board lease was acquired before adding the derived floor
OBS-80. This is a snapshot of locally available branch refs, not a claim that every remote
branch was inspected. The remote CAS counter then allocated OBS-80 through OBS-87.

The first four reservation request labels initially described the recovery additions.
Before any row was published, the coordination journal corrected those purposes to retain
the already planned meanings: 80 for #209, 81 aliases, 82 withdrawal, 83 agents registry.
The numbers were not reused, reset or released; the new recovery work received four new
reservations, 84 through 87. The counter commit and full old/new ID lists are in the evidence.

## Validation and limitations

- Baseline board lint: exit 1, eight findings. Candidate: exit 0, zero findings.
- `python3 -m unittest discover -s tests -p test_backlog_board.py -q`: 18 tests passed.
  The suite includes missing-marker, duplicate-ID, malformed-row and private-name negative cases.
- Board lint with the maintainer's local private denylist: exit 0, zero findings;
  4,068 effective private entries after 36 intentional public identifiers were excluded.
- Full-tree privacy scan: **FAIL**, three identifier matches in two unchanged base files,
  `collectors/scan_harness.py` and `tests/test_fabric_service.py` under the engine. Their bytes
  equal the base commit. A separate scan of all six changed/new files passed with the same
  denylist and path allowlist. No identifier was printed or newly allowlisted; whole-tree
  release acceptance remains open.
- Source inventory check: pass, no differences.
- ID set comparison: 79 before, 87 after, zero removed; counter next value 88.
- `agent_sync.py check`: exit 0, nine setup checks; warnings that `python` is not on this
  shell's PATH and the register has no local as-built baseline. Tests used `python3`.
- Full engine tests, hosted CI, installed-runtime acceptance, cleanup, releases and deployment
  were not run for this documentation and board-test change.

No existing worktree, provider state or local user data was modified by these checks.
This branch is a candidate until its independent review and the repository's integration checks.

---

**Made with [ssheleg skills](https://github.com/ssheleg/sshlg-skills)**

- [`skill-search`](https://github.com/ssheleg/make-skill) — catalogue search
- [`evidence-docs`](https://github.com/ssheleg/task-pipeline) — source-bound receipts
- [`task-pipeline`](https://github.com/ssheleg/task-pipeline) — bounded board repair
- [`agent-sync`](https://github.com/ssheleg/agent-sync) — leases and CAS ID reservation

<sub>A star on [the bundle](https://github.com/ssheleg/sshlg-skills) helps.</sub>
