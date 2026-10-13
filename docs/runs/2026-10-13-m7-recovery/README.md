<sub>ssheleg skills — task-pipeline · evidence-docs</sub>

# M-7 lesson evidence recovery

## Objective and inherited work

An observed lesson must not retain unsupported promotion after its evidence disappears.
The same project cannot become a second witness by changing a stored identity, and a
failed replacement must preserve the previous usable record. This branch preserves the
original implementation and six inherited tracked edits in a separate commit:
[provenance](INHERITED.md), commit `da44a8f9196aa4a4551ad47af61bca281b171248`.
The original checkout was not modified. No original PR or remote branch was updated.

## Implemented

- `store/lessons.py:has_support` checks resolving commits in two distinct known root
  identities on observed writes, including identical repeats. A lost observation becomes
  archived history plus a new proposal. Proposed unknown-origin lessons remain proposals.
- `store/ledger.py:append` refuses class, project and kind mutation on existing lessons.
  `corroborate` independently checks lesson support; supplied check text cannot substitute
  for evidence. Operator-owned records retain the ownership guard.
- `store/ledger.py:atomic_revisions` takes the writer lock before deduplication and commits
  archive, successor, outbox and lexical index together. Every append still runs ownership,
  lifecycle, compare-and-swap and tombstone guards. Existing caller transactions are refused.
- `store/lessons.py:revalidate` compares the scanned revision under that transaction;
  a stale scan cannot replay old wording over a newer revision. Scheduled corroboration
  rechecks observed lessons and records replacements in `lessons.invalidated` and counts.
- Source inventory, [memory design](../../design/AGENT-MEMORY.md#lessons) and
  [scenarios](../../ux/scenarios.md) were updated.

Paths above are relative to `observatory/engine/` unless linked otherwise.

## Checks

[Portable receipt](checks.json): five synthetic suites, all exit 0, Python 3.14.7.
122 unittest cases plus 95 explicit assertions: lesson 29, workflow_memory 65,
memory_access 28; corroboration 56 assertions, ledger 39 assertions.

```sh
python observatory/engine/tests/run_portable.py --suite lesson --suite corroboration --suite ledger --suite workflow_memory --suite memory_access --jobs 1 --timeout 120 --report-dir REPORT_DIR
python tools/update_inventory.py --check
python tools/check_public_release.py
```

Inventory and diff-whitespace checks passed. The bounded public-release pattern check
passed with no private denylist or history scan; it is not a complete privacy attestation.

Tests were written before fixes. Initial lesson suite had four failures, including the
inherited wrong archive revision expectation; the real Git corroboration regression had
two failing assertions. A separate test reproduced generic promotion with invented project
evidence before its ledger guard was added. The final suites include positive real Git
promotion and wording changes, disappearing evidence, immutable identities, rollback of
all three storage surfaces, stale-revision refusal, expired Git budget and operator support.

## Limits and exact next task

Independent review found a further conversion bypass at `939ddf0`: a corroborated
ordinary note could be revised into an observed lesson with unresolvable evidence.
The shared append seam now permits note-to-lesson migration only for proposals
with a valid lesson/1 body. The migration tool leaves promoted and operator-owned
notes unchanged, with an explicit review reason. Existing proposed bridge migration
remains supported. The new regression reproduced the bypass before the correction;
25 lesson tests pass after it, alongside the unchanged 56 corroboration and 39
ledger assertions. Exact corrected-head independent acceptance remains pending.

### Migration and shared insert follow-up

Independent review found a two-connection race at base `c430516`: a typed writer could
create the same identity after migration's initial lookup. The regression now runs the
contender in an independent context with a real second SQLite connection: it is blocked
while migration holds `BEGIN IMMEDIATE`, then retries to the same lesson id unchanged.
The candidate is reread under the lock and a changed revision is skipped. Dry runs remain
read-only. The shared insert also enforces active identity uniqueness atomically for
ordinary ledger new/conversion writes, preserving caller-owned transactions.

A second regression reproduced erasure between the generic insert's preliminary read and
its write. The INSERT itself now refuses a record-wide tombstone as well as a duplicate
active lesson identity, so an erased record cannot receive a hidden revision in that gap.
These checks use synthetic databases only. Proof lives in `TheBridgeNotesMigrate`; the
original foreign worktree remains unchanged. Final focused receipt: `checks.json`.

Git subprocess checks share a five-second budget per write-time validation; wording
revisions may validate twice (helper and shared ledger guard). Filesystem access and SQLite
lock acquisition are outside that budget. A temporarily unavailable clone conservatively
loses automatic observation; history is preserved. The scheduled pass retains its existing
per-command Git timeout. Neither this change nor synthetic checks authenticate the local
stdio caller to a project identity. Fabric export remains unimplemented by design.

Next: independent review of the final commit, including atomicity and identity boundaries;
then the parent may integrate under the repository policy. Native Windows, live ledger
mutation, live provider acceptance and installed deployment were NOT_RUN. The focused
suites do not constitute whole-system completion. The private-denylist and full-history
privacy checks remain an integration responsibility.

---

**Made with [ssheleg skills](https://github.com/ssheleg/sshlg-skills)**

- [`task-pipeline`](https://github.com/ssheleg/task-pipeline) — M7 tests-first repair
- [`evidence-docs`](https://github.com/ssheleg/task-pipeline) — source-linked recovery packet

<sub>A star on [the bundle](https://github.com/ssheleg/sshlg-skills) helps.</sub>
