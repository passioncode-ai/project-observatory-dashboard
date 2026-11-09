# Run 2026-11-09: release 0.35.8

Objective: release the engine wave of PR #479 (a readable agents page in the wiki, DigitalOcean onboarding signals,
organizations matched through provider accounts, the organization certificate rotated before its notice, the tick's
install reloaded without racing launchd), with the organization onboarding scenarios of PR #478 on `main`; then tag,
verify and install it.

## State now

- Open: the install on the second Mac, handed to its host agent.
- Waits: the second Mac's host agent; no watcher of this record runs, the next release session reads its answer.
- Next task: release 0.35.9 ([record](../2026-11-09-release-0.35.9/README.md)); PR #481 merged on 2026-11-09 as
  `2d1d6a41` and ships there. No answer about the second Mac's install was found by that release session
  (`observatory_machine` `section=move` read no other machine's facts younger than 48 hours).

## What it releases

Merged to `main` since `v0.35.7`: PR #478 (`5668f63f`, documentation: the organization onboarding scenarios in
`docs/ux/organization-onboarding.md` and the board rows OBS-411 to OBS-420, OBS-425 and OBS-426) and PR #479
(`8d8d9774`, the engine changes below).

- OBS-317, first half (`tools/project_into_vault.py` `agents_markdown`): the wiki projection writes
  `inventory/agents.md` beside `inventory/agents.json`, one row per service key with its product, version, where it
  answers, contract pin, who holds it and its MCP declarations, ordered by service key, with the inventory notes'
  frontmatter and a header saying it is generated; no path, token or plist. The wiki's `[[inventory/agents]]` link no
  longer reads as missing on every tick. The finding for a wiki agent page naming a key with no descriptor stays open.
- OBS-421 (`workspace.py` `_digitalocean`, `tools/digitalocean.py` `unstashed_contexts`, `scan_digitalocean.py`):
  `full doctor` reports `digitalocean` (`integration`, `teams_stashed`) and the attention code `digitalocean-off` with
  `project-observatory full configure integrations digitalocean true` when team tokens are stashed while the
  integration is off; `digitalocean.py teams` names each `doctl` context with no stashed token offline, and `teams
  --probe` and the collector ask DigitalOcean which team it is (10 s per context) and name the stash command. A
  context name shaped like a credential is never printed; no value is printed or written.
- OBS-412 part 1 (`organizations.py`, `collectors/emit_registry.py`): `config/organizations.json` `match` takes
  `digitalocean_teams`, `cloudflare_accounts` and `heroku_teams`; a project matches only through a resource the
  registry already ties to it, and an account two organizations claim assigns nothing. The DigitalOcean and
  Cloudflare account rows and `heroku-apps.json` carry the organization answer only when organizations are
  configured; every resource row names its account row (`account_ref`). With organizations configured,
  `registry/projects.json` is written once per emit, after the provider documents.
- OBS-423 (`node/org.py` `rotation_due`): an organization certificate is rotated at two thirds of its validity or an
  hour before the `expiring` notice, whichever comes first, never in the first half, judged on the server's clock;
  `org status` and the tick read the same rule.
- OBS-424 (`tools/install_launchd.py` `install`): the tick is reloaded through `stop_job` and `start_job`, and a stop
  that did not finish is named.
- The `observatory-log` plugin stays 0.35.6: nothing under `observatory/engine/skill/` or `.claude-plugin/` changed
  since `v0.35.7` (`git diff --stat v0.35.7..origin/main` on those paths prints nothing).

## Decisions

- **0.35.8, a patch release**: additive registry fields and doctor rows, two fixes; no setting, schema or migration
  change. An older engine ignores the new `match` keys.
- **OBS-317 and OBS-412 stay open**, each with what 0.35.8 released written in its row; OBS-421, OBS-423 and OBS-424
  close as `done in 0.35.8`.
- **Follow-ups found by the reviews of #474, #476, #478 and #479 become board rows** (OBS-427 to OBS-434), none of
  them blocking this release.

## Checks run

- PR #479: an independent review and its fixes (`927b9486`: `projects.json` written once per emit, board rows
  restored, `teams` offline unless `--probe`, escaped cells on the agents page, a malformed `match` rule named, the
  install naming an unfinished stop); its remaining LOW finding is OBS-434.
- PR #479, local engine suites one at a time (`run_portable.py --jobs 1`, macOS, Python 3.14) on `6635d4e3`, all
  PASS: scan_agents, projection_mirror, digitalocean_door, workspace, organizations, node_org, lifecycle,
  infra_pages, i18n, pipeline, emit_purity, handed_commands, osprivacy, maintenance (14 suites); root gates and tests
  `check_backlog`, `check_docs`, `check_paths`, `sync_engine_docs --check`, `update_inventory --check`,
  `test_backlog_board`, `test_docs_drift`, `test_engine_doc_copies`, `test_changelog_releases`,
  `test_release_boundaries`.
- PR #479, hosted CI at `6635d4e3`: Linux 3.11 and 3.14, macOS 3.14 and Windows green (runs
  [42541024082](https://github.com/passioncode-ai/project-observatory-dashboard/actions/runs/42541024082) and
  [42541030622](https://github.com/passioncode-ai/project-observatory-dashboard/actions/runs/42541030622)).
- This release PR: `tools/sync_engine_docs.py` and `--check`, `tools/update_inventory.py` and `--check`,
  `observatory/engine/tools/check_docs.py`, `observatory/engine/tools/check_paths.py`, `tools/check_backlog.py`, and
  the root suites `test_version_consistency`, `test_desktop_app`, `test_plugin_manifests`, `test_changelog_releases`,
  `test_backlog_board`, `test_docs_drift`, `test_engine_doc_copies`, `test_release_boundaries`, `test_licensing`,
  `test_analytics_catalogue` and `test_update_inventory`. No engine code changes here, so no portable suite was rerun.

## Receipts

- Release PR #480 merged as `af0a513f` on 2026-11-09T02:37:47Z. Its `observatory-release-check` run on `af0a513f`
  ([42551995792](https://github.com/passioncode-ai/project-observatory-dashboard/actions/runs/42551995792)) succeeded.
- Tag `v0.35.8` (tag object `c0d389f5`) on `af0a513f`. Release run
  [42555968513](https://github.com/passioncode-ai/project-observatory-dashboard/actions/runs/42555968513): every job
  (guard, manifest, wheel, windows x64 and arm64, linux x64 and arm64, macos, feed, publish) succeeded on the first
  attempt. The release published 19 files on 2026-11-09T04:10:49Z.
- Verified by the release manager: `SHA256SUMS` (17 entries) OK, `gpg` `VALIDSIG 63B30DC324BD697487AA31944FAFB8AEC803B6A7`,
  `gh attestation verify` of the wheel exited 0.
- Installed on the release manager's machine with `full update --version 0.35.8 --apply` (exit 0). The server came
  back by itself on 0.35.8: `serverd.out` shows 0.35.8 and the well-known answered 200.
- The workspace's organization configuration then took `digitalocean_teams` rules (OBS-412 part 1) and validated with
  no problems: three of its four DigitalOcean teams are assigned to an organization by rule, one is left for a decision.
- Dependabot PRs #392 (`1ffc266e`) and #389 (`4b751775`) merged at 04:03Z, after the tag: they are not in 0.35.8.
- The second Mac's install is open (State now).
