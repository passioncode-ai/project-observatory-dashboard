# Run 2026-11-09: release 0.35.9

Objective: release the first part of organization onboarding (PR #481: the read-only accounts and keys views and the
remaining account rules; PR #483: the local selection record and its apply) and credential requests with delivery
and provenance through the OpenRouter and Cloudflare doors (PR #484); then tag, verify and install it.

## State now

- Open: nothing in this release; its receipts are below.
- Waits: none.
- Next task: release 0.35.10 ([record](../2026-11-10-release-0.35.10/README.md)); PRs #486, #489, #490 and #488
  merged after this record was written and ship there.

## What it releases

Merged to `main` since `v0.35.8`, in order:

- Dependabot PR #392 (`1ffc266e`, `actions/setup-python` 7.0.0 to 7.1.0) and PR #389 (`4b751775`, `rustls` 0.23.45
  to 0.23.46 in the desktop app), merged at 04:03Z after the `v0.35.8` tag.
- PR #482 (`00cf5b31`, documentation): the 0.35.8 receipts and the board rows OBS-439 to OBS-441.
- PR #481 (`2d1d6a41`), the first organization onboarding wave:
  - OBS-411 and OBS-415, first wave: `full org accounts [--json]` and `observatory_org_accounts` (schema
    `org-accounts/1`) list every provider account this space reaches under exactly one organization or under "needs
    a decision", with the door's command for an account not connected; `full org keys --org NAME [--json]` and
    `observatory_org_keys` (schema `org-keys/1`) classify each vault slot of the organization's projects as
    organization, personal or ambiguous, with the issuer from the door's record, and list the doors' admin and
    provisioning keys as never moved. Names only: no value file is opened.
  - OBS-412 part 2: `organizations.json` `match` takes `github_owners` and `openrouter_accounts`; the session-start
    line, the analytics findings and `observatory_project` read a project's organization through one function; the
    findings `organization.unresolved` and `organization.config_problem`.
  - Board rows OBS-435 to OBS-438 from its reviews.
- PR #484 (`2516aabd`), OBS-191 parts 2 and 3 and OBS-360: `tools/credential_requests.py` files a request as the
  contract's `credential-request/1` (no value), records the administrator's decision as `credential-decision/1`,
  issues through the OpenRouter or Cloudflare door straight into the requested slot with its provenance in the slot's
  metadata, revokes with proof, and journals every step in `credential-requests.jsonl`; `vault.py list`,
  `use_secret.py names` and `observatory_credentials` (`issuedOnRequest`) show the provenance. Board rows OBS-454 to
  OBS-456, OBS-460 and OBS-463 to OBS-469.
- PR #485 (`492d53f3`, documentation): the board rows OBS-475 to OBS-477.
- PR #483 (`56b17ed2`), OBS-417, the second organization onboarding wave: `full org select --org NAME plan, show,
  apply, undo, confirm-replacement KEY` and `observatory_org_selection` keep the local record `org-selection/1`
  (owner-only in `store/org-selection/`, never sent, each revision naming its predecessor's digest), apply only the
  previewed digest, and start a journal in which projects wait for Fabric's adaptation plan and keys for the request
  queue and the rotation; a project or key the organization's rules propose after a selection is named on the
  findings board (`organization.selection_new`). Board rows OBS-442 to OBS-453, OBS-457 to OBS-459, OBS-461, OBS-462,
  OBS-470 to OBS-474, OBS-478 and OBS-479 from its reviews.
- The `observatory-log` plugin moves from 0.35.6 to 0.35.9 with its three skills: PR #484 added to the
  handling-secrets skill how an agent files a credential request and runs the person's decision
  (`git diff --stat v0.35.8..origin/main -- observatory/engine/skill .claude-plugin` names that one file).

## Decisions

- **0.35.9, a patch release**, as 0.35.8 was for additive work: new commands, MCP tools, findings and files, no
  setting, schema or migration change (`docs/COMPATIBILITY.md`, the three sections now headed 0.35.9). An older
  engine ignores the new `match` keys, the request queue and the selection record.
- **OBS-191, OBS-360, OBS-411, OBS-412, OBS-415 and OBS-417 stay open**, each naming in its Item what 0.35.9
  released; none of these rows read `done — merged (#n), unreleased`, so no row closes with this release.
- **Two stale notes corrected on the board**: OBS-226 read `done — merged (#363), unreleased` and OBS-194 named its
  step 1 merged and unreleased, but both commits (`6045ab7a`, `14b395b8`) are in `v0.30.0` (`git tag --contains`);
  they now read `done in 0.30.0 (#363)` and "released in 0.30.0 (#364)".
- **Two new rows with the ids reserved for them** (agent-sync register `OBS`, reserved on 2026-11-09 by this run):
  OBS-480, a port forward that `full machine-move leave` installs for a service with a local origin (the request of
  the Fabric Dashboards agent on the project board, thread `189591b7`, for FD-196), and OBS-481, short private
  identifiers matched at word boundaries by the public privacy checker. Both `open`.

## Checks run

- PR #483, hosted CI at its head before the merge: Linux 3.11 and 3.14, macOS 3.14 and Windows green (runs
  [42671349678](https://github.com/passioncode-ai/project-observatory-dashboard/actions/runs/42671349678) and
  [42671356830](https://github.com/passioncode-ai/project-observatory-dashboard/actions/runs/42671356830)).
- This release PR (dev venv, Python 3.14): `tools/sync_engine_docs.py` and `--check`, `tools/update_inventory.py`
  and `--check`, `observatory/engine/tools/check_docs.py`, `observatory/engine/tools/check_paths.py`,
  `tools/check_backlog.py`, and the root suites `test_version_consistency`, `test_desktop_app`,
  `test_plugin_manifests`, `test_changelog_releases`, `test_backlog_board`, `test_docs_drift`,
  `test_engine_doc_copies`, `test_release_boundaries`, `test_licensing`, `test_analytics_catalogue` and
  `test_update_inventory`. No engine code changes here, so no portable suite was rerun.

## Receipts

- Release PR #487 merged as `c8907e20` on 2026-11-09T20:54:58Z. Its `observatory-release-check` run on `c8907e20`
  ([42697484506](https://github.com/passioncode-ai/project-observatory-dashboard/actions/runs/42697484506)) succeeded.
- Tag `v0.35.9` (tag object `6825c61c`) on `c8907e20`. Release run
  [42705759236](https://github.com/passioncode-ai/project-observatory-dashboard/actions/runs/42705759236) needed
  four attempts:
  - The guard first failed on the Windows suite of `check.yml` timing out; the rerun was green.
  - Windows x64 signing then failed with "Invoke-ArtifactSigning is not recognized": the floating tag `v1` of the
    shared action `passioncode-ai/.github` still pointed at v1.3.1 after v1.4.0 was released, and v1.4.0 is the
    release that stops restoring the Azure signing action's dependency cache across architectures, so the x64 job
    restored a cache entry made on arm64. On 2026-11-10 `v1` was moved to v1.4.0 (commit `b5b97e1`), as that
    repository's `AGENTS.md` requires after a release, and the arm64-made cache entry was deleted.
  - The fourth attempt succeeded and published 19 files on 2026-11-10T01:41:32Z.
- Verified by the release manager: `SHA256SUMS` all OK, `gpg` `VALIDSIG 63B30DC324BD697487AA31944FAFB8AEC803B6A7`,
  `gh attestation verify` exited 0 for the wheel, the Windows x64 installer and the macOS app.
- Installed unattended by the engine's maintenance job, which picked up 0.35.9 by itself.
- Merged after this record was written, all in 0.35.10: PR #486 (`dabe3825`, OBS-475, space daemons 1.2.0), PR #489
  (`290fddda`, the `dispatch_packet` documentation), PR #490 (`897065d3`, `zip` 9.5.0 in the desktop updater) and
  PR #488 (`e1f2fb74`, OBS-476 and OBS-477).
