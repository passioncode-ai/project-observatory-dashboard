# Run 2026-10-26: release 0.29.2

Objective: release what merged since 0.29.1 (`full probes` explaining its fixture workspace, session servers that
move past an open listen stream, updates that keep `OBSERVATORY_SYSTEM_SETUP=0`, a check-in that no longer reports a
server policy as applied, and the standing review policy), then tag, verify and install it.

Base: `8c4a6d75` (origin/main, #357 merged) on branch `release/0.29.2`. This record is committed with the version
bump; resolve the final object id with `git log -1 --format=%H` on that branch.

## What the release carries

Everything merged since `v0.29.1` (`git log v0.29.1..8c4a6d75`, five pull requests):

- #355: `full probes` without `--fixture-home` says the probes need an explicitly synthetic fixture workspace, names
  `fabric/FABRIC-CONFORMANCE.md` and still exits 2; `full probes --fixture-home PATH` and `full probes-check
  --fixture-home PATH` forward an absolute path. It corrects the 0.29.0 and 0.29.1 notes: nothing is needed after an
  upgrade for `full check` (OBS-224).
- #356: documentation only (OBS-225 partly done: the signing cache fix in passioncode-ai/.github `v1.3.2`).
- #359: a session MCP server no longer counts an open `subscriptions/listen` stream as a request still running; it
  ends each stream gracefully just before it moves, after a stamped `notifications/tools/list_changed` when the tools
  changed, and retries a refused move the moment the last request answers (OBS-228). `full update --apply` keeps an
  explicit `OBSERVATORY_SYSTEM_SETUP=0` and reports `"maintenance": "not-scheduled-here"` (OBS-229).
- #360: a policy a check-in response carries is recorded per source and reported `failed` at revision 0 on every
  later check-in until OE-06 verifies and applies signed policies (OBS-231).
- #357: the standing review policy (OBS-227, `docs/design/REVIEW-POLICY.md`): `full review policy show|enable|disable|run`,
  the tick's optional step `review-policy`, `full review undo-policy`, `full review batch prepare` and `batch apply FILE`,
  the finding `review.queue_waiting`, `review` in `full doctor` and the Health row "review queue".

## Decisions

- **0.29.2, a patch**: the new commands of #357 are opt-in and inert until a person enables the policy; the other
  pull requests fix behaviour within the declared contracts. No contract pin moved.
- **The plugin stays at 0.28.0**: `git diff --stat v0.29.1..8c4a6d75 -- observatory/engine/skill .claude-plugin`
  prints nothing.
- **CHANGELOG**: `## Unreleased` becomes `## 0.29.2 (2026-10-26)` with a summary; each entry gains its pull request
  number. The summary names the one operator step: `project-observatory full review policy enable` once at a terminal
  (`observatory/engine/observatory.py` hands `full review ...` to `tools/review.py`, whose parser takes
  `policy enable`). It also says that the OBS-228 and OBS-229 fixes act from the update after this one, because the
  move and the update are made by the release already running.
- **No `RELEASE.json` marker**: enabling the policy is optional and nothing needs a person before installation.
- **Rows closed**: OBS-224, OBS-227, OBS-228, OBS-229 and OBS-231 read `done in 0.29.2 (#n)`. OBS-243 (#357, review
  round 3 at `51e22f5c`) and OBS-244 (#359, review round 2 at `5bbc0b38`), reserved through the OBS register, record
  the final reviews' non-blocking findings with their lines on `8c4a6d75`. Next free id: `OBS-245`.

## Checks run

On this branch, with `~/.local/share/project-observatory-venv/bin/python`:

- `python -m unittest tests.test_version_consistency tests.test_desktop_app tests.test_plugin_manifests
  tests.test_changelog_releases tests.test_backlog_board tests.test_docs_drift tests.test_engine_doc_copies
  tests.test_release_boundaries tests.test_licensing`: 165 tests OK.
- `python -m unittest discover -s tests` from the repository root: 313 tests OK (1 skipped).
- `tools/check_backlog.py`: 0 problems.
- `tools/update_inventory.py` regenerated `SOURCE-INVENTORY.json`; `--check` exits 0.
- `tools/sync_engine_docs.py --check`: stale none, exit 0.
- `python -m compileall -q observatory`: exit 0.
- `observatory/engine/tools/check_docs.py`: documentation current, exit 0.
- The engine's own suite (`observatory/engine/tests`) was not run locally; the pull request's checks run it on macOS,
  Linux and Windows.

## Next

Merge this pull request, tag `v0.29.2` on the merge commit, verify the release (sums, attestation, GPG), install it
with `project-observatory full update --apply`, run `project-observatory full review policy enable` once at a terminal,
and record the receipts here in a follow-up pull request.
