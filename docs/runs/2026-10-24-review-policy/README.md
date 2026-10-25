# Run 2026-10-24: a standing review policy (OBS-227)

Objective: the operator asked that the whole queue of proposals be reviewed constantly and never
forgotten, preferably without a coding agent running, and that what needs a person accumulate so one
launch resolves it. Pull request [#357](https://github.com/passioncode-ai/project-observatory-dashboard/pull/357),
branch `feat/review-policy` from `04514443`.

## What was measured before

On one installed 0.29.1 (the operator's numbers; no operator data is in this repository): 112 pending
registry proposals, 93 of them `resources`-only (append-only, never replacing what is recorded), the
rest touching `description`, `organization`, `aliases` and `local_folders`, about 9 duplicate
resources; 901 proposed ledger conclusions across 180 projects, 43 of them superseded same-day
readings that `review.py reject-group` folds.

## What shipped

- `observatory/engine/review_policy.py`: one evaluator for both queues; the closed, versioned rule
  set; the policy file `config/review-policy.json` (`review-policy/1`), enabled only by a person at a
  terminal; the journal `store/review-policy/decisions.jsonl`; undo; the batch file
  (`review-batch/1`); the summary that doctor, Health and the finding read.
- `tools/review.py`: `policy show|enable|disable|run`, `batch prepare|apply`, `undo-policy`; the
  accept path refactored into `accept_proposal` and sharing the review lock; `full review` is an
  own-parser command and lists with no arguments, as before.
- The tick's optional tail step `review-policy` after `corroborate` (`tools/tick.py`,
  `tick_budget.TAIL_ORDER`, `tick_lease.TAIL_STEPS`, `STEP_SECONDS` 120, `tick_progress.STAGES`).
- `tools/review_findings.py`: `review.queue_waiting`; `review` in `full doctor`; the Health row
  "review queue"; labels and the Russian catalog.
- Docs: `docs/design/REVIEW-POLICY.md`, `docs/ONBOARDING.md` (section "The review queue and its
  standing policy"), `docs/AGENT-ONBOARDING.md`, `full --help`, scenario OSS-77, the
  `observatory_propose` description, CHANGELOG `## Unreleased`, backlog OBS-227 and OBS-230.

## Decisions

- **The ledger fold writes nothing to the ledger.** `proposed` goes only to `observed` or
  `rejected`; `rejected` is terminal; only the operator may change another writer's record. A policy
  rejection would therefore mint authority or impersonate the author, and no undo could reverse it.
  The fold is a journal decision; the batch offers the folded readings as rejections for the one launch.
- **Changing the rules un-enables them**: the enabled hash covers every rule id, version and on/off,
  and an unknown rule id makes the whole file invalid, so an edited file cannot grant more than the
  engine ships.
- **A resource recorded with other fields waits for a person**: appending never replaces, so an
  update is not the resources rule's to make.
- **The policy never overwrites a person's row provenance** in `project_overrides.json`; it writes
  `why`, `proposed_by` and `evidence` only on a row it creates.
- **The design document lives in `docs/design/`** with the other design records, not under
  `observatory/engine/docs/design/` (which does not exist); the engine's ONBOARDING links it by URL.

## Checks run

On this branch, with a virtual environment from `requirements-full.lock` (Python 3.14.7):

- `project-observatory full check` (283 suites, `--jobs 6`): 278 PASS and 5 FAIL on the first run;
  the five fixed (`docs_current`, `osprivacy`, `tick_progress`, `write_surface`) or not reproduced
  (`tick_repo`, PASS twice on re-run) and re-run green with `review_policy` (31 cases).
- `python -m unittest discover -s tests`: 313 tests OK after the inventory refresh.
- `tools/update_inventory.py --check`, `tools/sync_engine_docs.py`, `observatory/engine/tools/check_docs.py`:
  clean. `tools/check_backlog.py` with the maintainer's local denylist: 0 problems.
  `tools/check_public_release.py` with it: the working tree's only finding was a local, git-ignored
  `.claude/settings.local.json`; the pre-push hook's history check passed.
- `python3.11 -m compileall` on the new and changed modules.

## Review fixes, 2026-10-25

An adversarial review of the pull request at `cb2004db` found four defects, fixed on the same branch
with a failing test first (`observatory/engine/tests/test_review_policy.py`):

- `batch apply` could decide items that were not pending (a hash recomputed by hand passed). It now
  prepares the batch again, applies only items that fresh preparation lists and that are still
  `proposed`, updates statuses `WHERE status = 'proposed'` and reports a change of nothing as refused;
  a file listing one item twice is refused whole (`BatchOnlyDecidesPending`, `Batch`).
- The person approved edits they never saw: only totals and eight items were shown, and
  `recommended` was in the editable file. Every departure from the engine's fresh recommendation and
  every item it will not apply is listed before the question (`BatchShowsWhatChanged`).
- Resource fields were copied unchecked: `added_by: operator` from an agent was accepted, and no
  field was checked for a credential shape. Such proposals now wait for a person
  (`ResourceFieldsAreChecked`).
- A crash between the file write and the journal left changes no undo could find. A run now journals
  a pending intent first and closes it last; the next review writer reverts an open one (`Crash`).

The review's low findings that remain are OBS-242.

A confirming review at `d6b26a91` found that `reject-proposal` took no lock and ran no recovery, so a
resource a crashed run had written stayed in force after the operator rejected its proposal, with no
decision naming it. Fixed with tests (`CrashThenAPerson`, `RevertOrder`, `ReasonsEchoNoCredential`):
both single commands recover under the lock first; recovery removes the run's writes for a proposal a
person rejected since and keeps them for one accepted since, journals each outcome and closes an
intent `reverted` only when every decision was; it reverts in the reverse of the order of application;
an unreadable curation file keeps the intent open and degrades the tick instead of raising; and no
reason quotes a credential-shaped key.

## Open

- OBS-230: whether Health's "Nothing here needs you" changes for structural conclusions; per-rule
  decision history on Health.
- The pull request's CI on the three required rows.

## Next

Merge #357 once its required checks are green, then the next release carries it; the operator
enables the policy with `project-observatory full review policy show` and `policy enable` at a
terminal after updating.
