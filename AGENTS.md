# Working in project-observatory-dashboard

## Read first

1. The organization's
   [roadmap](https://github.com/passioncode-ai/fabric-workspace/blob/main/knowledge/roadmap.md) —
   every major feature and release across PassionCode.ai as `RM-*` tracks with owner, phase and
   state. It is the entry point: a task here that serves a track names it, and the track's status
   is edited only in the roadmap.
2. The PassionCode.ai knowledge base — `fabric-workspace/knowledge/` in your clone (org-index
   `scripts/clone_all.sh` makes it) or https://wiki.passioncode.ai/knowledge — at least its
   [README](https://github.com/passioncode-ai/fabric-workspace/blob/main/knowledge/README.md),
   vision, principles and how-to-work.
3. This file, then the organization's
   [CONTRIBUTING.md](https://github.com/passioncode-ai/.github/blob/main/CONTRIBUTING.md) and this
   repository's [CONTRIBUTING.md](CONTRIBUTING.md); everything there applies to agents too.

`CLAUDE.md` holds one line, `@AGENTS.md`, so Claude Code reads this same file: it imports
`AGENTS.md` only through that line, and one file cannot drift from a copy of itself.

## What this repository is

`passioncode-ai/project-observatory-dashboard` is **the single home of Project Observatory
code**: the public engine, its Python package `project-observatory`, the companion Claude Code
plugin `observatory-log` and the public website. Engine changes land here first and ship from
here.

The private predecessor repository holds one operator's tooling and data only. It is not developed as an engine: do not port changes to it, do not copy its history,
documents or data here, and do not cite its records.

The organisation's map of repositories is
[passioncode-ai/org-index](https://github.com/passioncode-ai/org-index) (private; readable by
organisation members).

| Path | What it holds |
|---|---|
| `observatory/*.py` | the launcher and the old 0.1 portable CLI, kept working |
| `observatory/engine/` | the complete engine: collectors, findings, store, dashboard, MCP server, tools |
| `observatory/engine/tests/` | engine suites, run by `observatory/engine/tests/run_portable.py` in sandboxes |
| `observatory/engine/skill/` | the `observatory-log` plugin; `.claude-plugin/` at the root publishes it |
| `tests/` | root tests: launcher, packaging, release gates, version and plugin consistency |
| `macos/` | the native Mac app (SwiftUI): sources, tests, `scripts/build-app.sh` and `scripts/install-app.sh`; [docs/macos/](docs/macos/README.md) |
| `tools/` | release tooling: privacy gate, package checker, source inventory, demo estate |
| `site/`, `docs/site/` | the retired website (a redirect to the product page plus the field-notes article) and its checks; it never reads a workspace |
| `docs/` | onboarding, compatibility, security boundary, UX scenarios, run receipts |

## Commands

| What | Command |
|---|---|
| Install | `python -m pip install -c requirements-full.lock '.[full]'` (in a Python 3.11+ virtual environment) |
| Test (the gate) | `python -m unittest discover -s tests -v && project-observatory full check`, then the rest of the list below |
| Build | `python -m pip wheel --no-deps . --wheel-dir dist && python tools/check_package.py dist/*.whl` |
| MCP (register + proving call) | `claude mcp add observatory --scope user -e OBSERVATORY_HOME="$OBSERVATORY_HOME" -- "$(python -c 'import sys; print(sys.executable)')" "$(project-observatory full-path)/mcp/server.py"`, then `observatory_status` answers with an empty `degraded` list ([README → MCP](README.md#mcp)) |

## Build and test

Python 3.11 or newer with loadable SQLite extensions; choosing the interpreter is in
[docs/ONBOARDING.md](docs/ONBOARDING.md#sqlite-runtime-prerequisite). Node.js 22 and Git 2.17
or newer for the complete checks. These are the commands CI runs
(`.github/workflows/check.yml`), in its order:

```sh
"$PYTHON" -m venv .venv && . .venv/bin/activate
python -m pip install -c requirements-full.lock '.[full]'
python -m compileall -q observatory
python -m unittest discover -s tests -v                # root tests
swift test --package-path macos                        # macOS rows only: the Mac app's tests
macos/scripts/build-app.sh                             # macOS rows only: build and sign the app bundle (ad hoc)
project-observatory full check                         # every engine suite, each in a fresh sandbox
python tools/update_inventory.py --check               # engine files match SOURCE-INVENTORY.json
python -m pip wheel --no-deps . --wheel-dir dist
python tools/check_package.py dist/*.whl
claude plugin validate --strict .                      # and observatory/engine/skill, and its plugins/observatory-log
python tools/check_public_release.py --history --history-ref HEAD
python docs/site/check.py --self-test
python tools/build_article.py --check
node tools/check_site_interactions.cjs
# then: the installed CLI's demo, a sterile full-engine workspace, and an install
# from the wheel without the lock file (see the workflow for their exact lines)
git diff --exit-code                                   # the checks left no tracked change
```

- The `claude plugin validate --strict` step runs on the ubuntu-latest / Python 3.14 row only,
  with the Claude Code version pinned in the workflow (`npm install -g @anthropic-ai/claude-code@VERSION`).
- One engine suite: `project-observatory full check --suite NAME` (repeatable), `NAME` being
  `test_NAME.py` under `observatory/engine/tests/`.
- A new engine suite is listed in `SUITES` of `observatory/engine/tests/run_portable.py`:
  `LEGACY` for scripts that print `PASS`/`FAIL` and get a synthetic workspace from the runner,
  `BOUNDARY` for self-contained unittest files. A suite not listed there does not run.
- The runner's receipt marks a suite that exited 0 having run nothing (every check printed
  `SKIP`, or every unittest case skipped, as without `node`) as `SKIP`, names it in `not_run`,
  and never reports a selection in which nothing ran as `PASS`.
- After changing any engine file run `python tools/update_inventory.py` and commit the
  inventory with the change.
- Behaviour changes are test-first: the failing test, then the change, then green.
- CI runs on Python 3.11 as well as 3.14; run `compileall` on 3.11 before pushing when you can,
  since 3.14 accepts syntax 3.11 rejects.

## Privacy rules

This repository is public, and so is its Git history, commit messages included.

- Nothing added may name a private project, person, host, organisation other than
  `passioncode-ai`, machine path (a home directory such as `/Users/<name>/`) or credential
  value, and nothing may cite a decision record kept outside this repository.
- Fixtures are synthetic: `alpha-web`, `beta-api`, `example-org`, `example.com`,
  `*.example.invalid`. Tests use temporary `HOME` and `OBSERVATORY_HOME`, never an operational
  workspace, never live providers and never inherited credentials.
- `python tools/check_public_release.py --history` must report zero findings. A maintainer who
  holds the private predecessor adds `--private-denylist FILE`; that file is never committed.
  Names published on purpose are listed with a reason in `tools/public-identifiers.json`.
- Never commit a workspace, database, registry, dashboard page built from real data, provider
  response or local machine configuration.
- Credential values travel on stdin only. Read the `handling-secrets` skill
  (`observatory/engine/skill/plugins/observatory-log/skills/handling-secrets/SKILL.md`) before
  touching the vault or a provider door.
- Comments and docstrings are the design record: they explain why, under the same rules
  ([CONTRIBUTING.md](CONTRIBUTING.md#comments-and-design-notes)).

## Every behaviour is visible to both readers

Operator rule, 2026-10-04. Every command and behaviour must be fully transparent to both
readers: the agent that drives the engine (Claude Code over MCP and the CLI) and the person.
A change that alters what the engine does, refuses or sends updates every surface those
readers use, in the same pull request:

- **Agents:** the MCP server `instructions` (at most 1,800 characters, pinned by
  `observatory/engine/tests/test_mcp_wire.py`), each tool's description, the reason codes in `degraded`, and
  `docs/AGENT-ONBOARDING.md`, which `full onboard` prints. Edit `docs/ONBOARDING.md` and
  `docs/AGENT-ONBOARDING.md` only: the copies under `observatory/engine/docs/` are generated
  by `python tools/sync_engine_docs.py`.
- **People:** `full --help`, `full doctor`, `docs/ONBOARDING.md`, the dashboard where it shows
  the state, and the scenario in `observatory/engine/docs/ux/portable-scenarios.md`.
- **Both:** a refusal names its reason and the command that changes it. "Nothing happened"
  is never silent.

A test pins each claim that can drift, the way
`tests/test_embedding_enforcement.py::WhatAgentsAndPeopleAreTold` does. A decision that only
the operator can make does not block the work: ship the safe default, say it in those
surfaces, and record the open decision in `docs/backlog.md`.

## How a change reaches `main`

1. Branch from `main`; commit in small logical commits.
2. Open a pull request against `main`. Three checks are required: the `test` job of
   `observatory-release-check` on each matrix row (ubuntu-latest with Python 3.11,
   ubuntu-latest with 3.14, macos-latest with 3.14).
3. The code is open source under `AGPL-3.0-only OR LicenseRef-PassionCode-Commercial`
   ([LICENSE](LICENSE), [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md); the knowledge base's
   [licensing](https://github.com/passioncode-ai/fabric-workspace/blob/main/knowledge/licensing.md) page), and
   contributions are accepted under [CLA.md](CLA.md): opening a pull request is the agreement,
   and the template carries no checkbox for it. Releases up to and
   including v0.9.1 keep the licence they shipped with (PolyForm Noncommercial or Internal Use
   from v0.8.2, MIT up to v0.8.1); describe the current version as AGPL or commercial.
4. `main` keeps a linear history and refuses force-pushes, so a pull request merges by squash
   or rebase, never by a merge commit. `.github/CODEOWNERS` requests the review.

Do not bump the version, edit `CHANGELOG.md` or tag in a feature pull request unless it is the
release itself.

Shared registers are edited under a lease. [docs/AGENT_SYNC.md](docs/AGENT_SYNC.md) (generated
from `.claude/agent-sync.json` by `agent_sync.py setup`; never edited by hand) lists the guarded
files and the gate. Run `agent_sync.py acquire <file>` before editing one and
`agent_sync.py release <file>` after, on every path including failure. The lease is a ref under
`refs/agent-sync/leases/` on `origin`, so another contributor's agent sees it
(`git ls-remote origin 'refs/agent-sync/leases/*'`); the record plane is local (`fs`), and
`.agent-sync/` is git-ignored. No register here carries a "Next free ID" line, so nothing is
reserved yet; a register that gains one is declared under `idRegisters` and taken with
`agent_sync.py reserve <REG>`.

## Lifecycle

The organisation's [lifecycle contract](https://github.com/passioncode-ai/fabric-workspace/blob/main/knowledge/lifecycle.md)
(LC-01…LC-16) holds here. What this product leaves running, and who stops it (LC-09):

| Process | Started by | Cadence | With no window | Stopped by | Idle budget |
|---|---|---|---|---|---|
| tick: `tools/tick_lease.py run -- bash tools/tick.sh`, launchd `org.project-observatory.<sha16 of the workspace path>.tick` | `python "$(project-observatory full-path)/tools/install_launchd.py" install` | `StartInterval 1800`, `RunAtLoad false`, Background, Nice 5 | runs every 30 min after the last one ended | `install_launchd.py uninstall`; `full update` stops and restarts it | none between ticks; each step under a watchdog (`DEFAULT_STEP_SECONDS` 900 s), the whole tick under a 1500 s ceiling below the interval, `ExitTimeOut` 30 s above the supervisor's 10 s + 5 s grace |
| server: `tools/serverd.py --run`, launchd `org.project-observatory.<sha16>.server`, HTTP on `127.0.0.1:47311` (`OBSERVATORY_SERVER_PORT`) | `serverd.py --install` | `RunAtLoad` + `KeepAlive`, `ExitTimeOut` 40 s above its 30 s drain | always up; beat 20 s while a client asked within 5 min, else 120 s; receipt written on change, at least every 300 s | `serverd.py --uninstall` (stays off); `full update` restarts it | ~0.2 CPU-s/min measured before the change, ~40 MB RSS; no subprocess on a timer |
| maintenance: `tools/maintain.py run`, launchd `org.project-observatory.<sha16>.maintain` (macOS) or systemd user timer `project-observatory-<sha16>-maintain.timer` (Linux) | `full maintain ensure`, `install_launchd.py install`, `full init` in a terminal, `full update --apply` (the new release), the `observatory-log` SessionStart hook | `StartInterval 3600`, `RunAtLoad true` with `--first-check-delay 90`, Standard (not Background: its throttling timed the pass out on a loaded machine), Nice 10; systemd `OnStartupSec=90s`, `OnUnitActiveSec=60min`, `Persistent=true` | hourly; a network check every 6 h (the first 90 s after the job starts, one retry within the hour after a failed check), an update activated only when the local server and memory-http have had no client for 5 min, a full snapshot once a day (tick and server stopped for the copy, then started again), the app swapped only while it is not running | `full maintain uninstall` (stays off until `maintain ensure`); `full auto-update off` (the file `<home>/auto-update`) stops only the updates | one short process an hour when nothing is due; never one of `managed_jobs()`, so `full update` does not stop it (docs/runs/2026-10-05-auto-update, D3) |
| per-session MCP server `mcp/server.py` | each agent session that declares it (`claude mcp add observatory …`) | one per session | lives as long as its session | the session (stdin EOF); answers `stale-server` once an update replaced its code | ~18 MB idle; no timer |
| job runner `tools/run_job.py <id>` (`machine.mcp.refresh`, `agent.ask`) | the MCP server or the app, on a request | per request | until the job ends | the job ends; `fabric.job.cancel` stops its process group | — |
| on-demand MCP probe (`claude mcp list` and the servers it starts) | `full scan-mcp` or `machine.mcp.refresh` only — never the tick | per request | until the CLI answers or 180 s | its own process group is killed on exit or timeout | — |
| keyserver `tools/keyserver.py` on `127.0.0.1:7717` | a person, by hand | — | while the terminal runs it | Ctrl-C | — |
| memory over HTTP: `mcp/http_service.py`, Streamable HTTP on `127.0.0.1:47313` (`--port`) | `full memory-http`, by a person or the caller's own supervisor; no job is ever installed for it | — | while the process runs | Ctrl-C or its supervisor; nothing to clean up, it keeps no state | stateless; per-binding rate `--rate` (default 120/min); bodies over 4 MiB refused 413 by the MCP SDK's session manager (`DEFAULT_MAX_REQUEST_BODY_SIZE`) |
| plugin `observatory-log` hooks | Claude Code: `SessionStart` (15 s), `Stop` (20 s) | per session start and per agent turn | nothing resident | the hook's own timeout | — |
| `Project Observatory.app` | the person (not a login item); its own update helper reopens it after *Restart to update* (in front) or an idle restart (`--background`: no window, no focus) | reads `store/maintenance.json` at launch and every 6 h; an idle check every 60 s | stays in the Dock after its window closes; no polling at idle beyond those two | Quit; `install-app.sh` quits it before replacing it. A staged update is activated only at the person's *Restart to update*, the person's quit, or 30 min with no window on screen, no input and no work in flight (LC-16): a detached `/bin/sh` helper waits for the app to exit, runs `full maintain app`, reopens it (≤ 60 s wait, ≤ 15 min run) | ~80 MB, 0% CPU |

- **No background job touches a protected place** (LC-06): disk sizing skips Documents, Downloads,
  Desktop, media folders, iCloud Drive and other apps' containers unless a person runs
  `full machine --disk` (`collectors/scan_machine.py#protected_place`).
- **Logs** (LC-12): one policy in `observatory/engine/log_policy.py` — 5 generations of 5 MB, mode
  0600, launchd-held files copied and truncated — applied by the tick's `logs` step and by the
  server to its own `serverd.err`/`.out`; every line the server writes to `serverd.err` starts with its UTC time (`serverd.StampedLines`). The logs live in the workspace's `store/logs/`, not
  `~/Library/Logs/<Product>/`: one account can hold several workspaces, and each keeps its own.
  The app is one per account, so its own update events (`update_restart`, `update_install`,
  UTC; codes plus the release `version` the event concerns) go to `~/Library/Logs/Project Observatory/app.log`, 0600, under 1 MB with one
  previous generation (`macos/Sources/ObservatoryCore/AppUpdate.swift`, `AppLog`).
  The one exception is the shared update log LC-16 asks for (`update_events.py`):
  `~/Library/Logs/Project Observatory/updates.jsonl` (macOS) or
  `$XDG_STATE_HOME/project-observatory/logs/updates.jsonl`, codes only, each line carrying the
  workspace's instance id; `OBSERVATORY_PRODUCT_LOG_DIR` moves it, and the test runner sets it.
- **Plists** (LC-05): a minimal `PATH` (the directories holding the engine's tools, then the
  system ones) and the interpreter by its virtual-environment or Homebrew `opt` path; the
  installers refuse a plist naming a Cellar path (`install_launchd.lint_plist`).
- **Automatic updates (LC-16)**, row by row:

  | LC-16 part | Here | Deviation, and why |
  |---|---|---|
  | Switch | the file `<home>/auto-update`; absent = on, only `off` is off; `full auto-update on\|off` writes it; label "Install updates automatically" / «Устанавливать обновления автоматически» on Health, in `doctor` and `auto-update status` (`switch.label`) | the folder is the workspace home, one per workspace, not one per account. `updates.auto: false` from 0.17/0.18 still reads as off while the file is absent, and the next `auto-update` command moves it into the file and says so |
  | Cadence | `CHECK_EVERY` 6 h; `--first-check-delay 90` from launchd's `RunAtLoad`, `OnStartupSec=90s` on systemd; one retry within the hour after a failed check | the job is a scheduled pass, not a resident process: it runs hourly and checks when due. The plugin hook schedules it at most every 6 h; where no scheduler exists the hook's pass is the check |
  | Verification | wheel: GitHub digest + `SHA256SUMS` line + `SHA256SUMS.asc` by the pinned release key + wheel metadata version; app: the same, plus `codesign`, the pinned team `KJ35UYYL22` (`app_update.TEAM_ID`), Gatekeeper; never older | none for the environment: `OBSERVATORY_RELEASE_REPOSITORY`/`OBSERVATORY_RELEASE_API` are honoured only in a development build (`engine_update.development_build`: a checkout or an editable install) and ignored, with a stderr line, by an installed wheel. `--repository`/`--api-url` on the command line remain a person's explicit choice of a fork or a mirror; the automatic job never passes them, and the signature is checked against the pinned key whatever the feed |
  | Install vs activation | the job runs `full update --apply --unattended` only when no tick or workspace operation holds its lock (`_tick_busy`; `--unattended` checks again just before stopping the jobs, a 2026-10-06 update having stopped a tick mid-run) and `maintenance.live_clients` finds no client of the local server (its receipt's `clients.recent`, set by any request but `/health`, the service document and the events feed) and no allowed memory-http call in 5 min; otherwise `deferred`, retried next pass. A person's `full update --apply` applies at once | install and activation are one transaction here: `pip` replaces the package in place, so the tick and server must be stopped before the files change, not only before they restart. The whole update therefore waits for the safe point; nothing is staged ahead of it. A Claude Code session's stdio MCP server is never stopped: it keeps the code it loaded, answers `stale-server` from its next call (`code_freshness.py`) and picks up the release when the session reconnects it |
  | Held releases | `observatory/engine/RELEASE.json` in the verified wheel, `{"version", "needs_person"}`: `--unattended` verifies and exits 6 (`held`), nothing stopped or installed; status, `doctor` and Health show the step; not downloaded again for the same version; a person's `--apply` shows it and installs | the marker travels inside the wheel rather than as a separate release asset, so the existing digest and signature chain covers it with no new asset to verify. Engines before 0.19.0 do not read it: a held release must also refuse its own `upgrade --apply` until the step is done |
  | Log events | `update_events.py`; `update_check`, `update_download`, `update_install`, `update_restart`, `auto_update` with LC-16's codes; subject `engine` or `app` | the line adds the workspace's instance id (sha16 of its path, as in the launchd labels) so two workspaces on one account can be told apart; no path, version or message |
  | Tests | `tests/test_engine_update.py` (tampered, unsigned, another key, older, held, log codes), `tests/test_maintenance.py` Switch, UpdateLog, Pass (cadence, first-check delay, deferral, held, switch off), App (another team, the pinned team, staged while running); `tests/test_release_boundaries.py` (a marker for another version is refused at build) | the release gate's "feed names only files of its own release" is not tested in this repository: the release is assembled and summed by the organization's shared publish workflow |

- **The lifecycle watch** (`collectors/scan_lifecycle.py`, findings `lifecycle.*`) reports, per
  owning product, orphaned processes, session servers on replaced code, jobs past their interval
  and logs past their cap or readable by others.
- **Deferred, with the reason:**
  - *Stable signed identity for the jobs (LC-05).* macOS privacy consents follow the code
    identity of the interpreter that runs, and Homebrew's python is ad hoc signed: a
    `brew upgrade` still voids every consent the tick was given, even through the `opt` link.
    The fix is a launcher executable inside the app bundle, signed by the release workflow
    with the app, that spawns (not execs) the virtual environment's python as its child and is
    the plists' `ProgramArguments[0]`, so the launcher stays the responsible process. Open: it
    changes the app bundle the release workflow builds and signs, and the installers'
    dependency on an installed app.
  - The app itself is Developer ID signed, notarised and stapled by
    `.github/workflows/release.yml` (F12 closed there); a local `build-app.sh` build stays ad
    hoc unless `OBSERVATORY_SIGN_IDENTITY` is set, and is never attached to a release.

**Build output (LC-15).** Release artefacts are `dist/project_observatory-<version>-*.whl`,
`dist/project_observatory-<version>.tar.gz` and `dist/macos/Project Observatory.app`. At most
the current and the previous release stay: `macos/scripts/build-app.sh` runs
`tools/prune_builds.py` after every build (older wheels and sdists removed, other Observatory
bundles beside the new one unregistered from LaunchServices and removed), and the wheel build is
`python -m pip wheel --no-deps . --wheel-dir dist && python tools/prune_builds.py`. Caches have
caps: `macos/.build` (SwiftPM) 2 GB and `build/` (setuptools) 200 MB; an agent that built runs
`python tools/prune_builds.py --clean-caches` before ending its run when a cap is passed.

## Decisions and receipts

A unit of work leaves its record in `docs/runs/<date>-<slug>/README.md`: what shipped (PRs,
commits, wheel digests), the checks actually run with their counts, what went wrong and the
check that now catches it, open work and the exact next task. Machine-readable evidence goes
beside it. [docs/HANDOFF.md](docs/HANDOFF.md) points at the current ones and says which next
tasks are the operator's and which a contributor can take.

## Releasing

1. Bump the version in `pyproject.toml`, `observatory/__init__.py`,
   `observatory/engine/configuration.py` and both `COMPATIBILITY.md` files
   (`tests/test_version_consistency.py` fails until they agree), and add the `## X.Y.Z — date`
   section to `CHANGELOG.md`. A plugin change bumps `observatory-log` separately, in its three
   manifests and every `SKILL.md` (`tests/test_plugin_manifests.py`).
2. Merge that pull request through the required checks, then push the annotated tag `vX.Y.Z` on
   the merge commit.
3. The tag starts `.github/workflows/release.yml`:
   - It builds the wheel and the Mac app.
   - It signs the app with the organization's CI Developer ID, notarizes and staples it.
   - It attests every file (Sigstore), writes `SHA256SUMS` and `SHA256SUMS.asc` (the
     organization's GPG key), and publishes the release.

   The signing jobs and the publish job wait for an approval in the `release` environment
   from any member of `release-approvers`, the person who pushed the tag included (operator
   decision, 2026-10-03). Approval is a person's act: an agent never approves a release run,
   even when the account it uses could; it starts the run and says whose approval is pending
   ([organization release signing](https://github.com/passioncode-ai/.github/blob/main/release-signing/README.md)).
   Then:
   - Re-download the assets and run `shasum -a 256 -c SHA256SUMS` and
     `gh attestation verify <file> -R passioncode-ai/project-observatory-dashboard --signer-repo passioncode-ai/.github`
     (the shared `release-publish` workflow signs the attestation; without `--signer-repo` the
     check fails with "verifying with issuer sigstore.dev"), and `gpg --verify SHA256SUMS.asc
     SHA256SUMS` after importing the organization's release key.
   - A release that needs a person before it may be installed (a data or schema migration
     to run or confirm by hand) carries `observatory/engine/RELEASE.json`:
     `{"version": "X.Y.Z", "needs_person": "<the step, or its runbook URL>"}`. The automatic
     update verifies such a release and holds it; a person's `full update --apply` shows the
     step and installs. `tools/check_package.py` refuses a marker naming another version, so
     delete it in the next release. Engines before 0.19.0 do not read it: make the release's
     own `upgrade --apply` refuse until the step is done as well.
   - To rehearse first, push `vX.Y.Z-rc.N` and run
     `gh workflow run release.yml --ref vX.Y.Z-rc.N -f publish=false`.
   - A locally signed build (`build-app.sh` + `notarize.sh`) is for debugging and is never attached.
4. Every machine then runs `project-observatory full update --apply` (without `--apply` it only previews).
5. Record the release in `docs/runs/<date>-<slug>/`.

## Shared backlog

[docs/backlog-sources.json](docs/backlog-sources.json) declares this repository's canonical
local task sources and their vision goals. The [common backlog contract](https://github.com/passioncode-ai/fabric-workspace/blob/main/knowledge/backlog.md)
owns aggregation; [the workspace backlog](https://wiki.passioncode.ai/backlog) is a derived view.
Edit a task only in its canonical source under an agent-sync lease, retain stable IDs and
closure receipts, and declare any new source in the manifest. Do not edit generated task
status in the workspace or copy another repository's task into a second editable row.
Land the source change, then run `node scripts/workspace.mjs sync` from a Fabric checkout
(or use the scheduled sync); check the published source commit before calling it current.

## After work

In the same run: update this repository's docs with the change; if a cross-repository fact changed
(a product, a version, a plan row, a principle), update the page in `fabric-workspace/knowledge/`
that owns it; land both; publish (`node scripts/workspace.mjs sync` from a Fabric checkout) or
leave it to the scheduled sync. Leave a handoff with the exact next task — here, the run record
in `docs/runs/<date>-<slug>/README.md` and the pointer in [docs/HANDOFF.md](docs/HANDOFF.md).
