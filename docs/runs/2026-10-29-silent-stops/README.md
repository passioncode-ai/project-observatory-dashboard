# Run 2026-10-29: three silent stops made to say why

Objective: on one installed 0.29.2 (the operator's machine; no operator data is in this repository)
the local server stopped answering and nothing said why. Two independent causes were found and
repaired on that machine by hand; this change makes each of them name itself, and fixes the server
plist the fabric-service/0.1 probe has failed on every install. Branch `godslive/observatory-down`
from `3dbb73a`.

## What was measured

- A package manager removed the Python the engine's virtual environment was made from
  (`pyvenv.cfg` `home` pointed at a keg that no longer existed). launchd kept the server, the
  maintenance pass and the update check loaded, and every start exited 78 (EX_CONFIG) with an empty
  `.err`: no Python process ever began. `full doctor` read the jobs as `installed: true, loaded: true`.
  `tools/tick.sh`, started through `/bin/bash`, did not fail: it took the first `python3` on `PATH`,
  which has none of the `[full]` dependencies, and the daily backup inside each tick failed with
  `No module named 'cryptography'`.
- Ten days before that, from the day 0.25.0 was installed, every tick stopped at `emit`: 0.25.0 folds
  a remoteless worktree into its main checkout's project, so `projects.json` would have gone from 94
  projects to 52, and the wholesale-change guard refused it each time (−45 %, limit ±25 %). The
  emit's own line, with the counts and `OBSERVATORY_ALLOW_BULK=1`, reached `bounded()`'s output and
  went nowhere: `tick.log` said only "emit failed or REFUSED a wholesale change", and Health said
  "10 of the last 10 ticks did not finish or did not refresh the registry". The registry stayed as
  it was through about 450 ticks.
- `check_service.py` (fabric-service/0.1) failed `lifecycle.supervisor-marker` on the server plist:
  `serverd.build_plist` builds its own dictionary and never set `FABRIC_SERVICE_SUPERVISOR=launchd`,
  which only `fabric_service.launchd_plist` sets, so a copy KeepAlive starts while the lock is held
  exits 75 at once and is respawned every ThrottleInterval.

## What shipped

- `tools/tick.sh`: an `OBSERVATORY_PYTHON` that is set and not executable is a stop: one
  UTC-stamped line naming it and the `home` its `pyvenv.cfg` names, exit 78, and no other `python3`.
- `tools/tick.py` `Tick.bail`: a stopped step's ` REFUSED: ` line is logged and becomes the report's
  `registry_note`.
- `tick_health.refused`: while the newest runs are `partial` at the emit's guard, `registry_refused`
  in `full doctor` `tick`, and a `registry-refused` row in `degraded` (`/health`, the service card,
  every MCP answer), within the card's 300 characters: the counts and
  `OBSERVATORY_ALLOW_BULK=1 project-observatory full local`.
- `install_launchd.job_state`, `job_problem`, `job_report`: one `launchctl print` gives `loaded`,
  `pid` and `last_exit`; `full doctor` gains `jobs` (tick and server), the maintenance and check jobs
  report `last_exit` and a `job-exit` warning (Health, in English and Russian). A loaded job that is
  not running with a last exit above 0 is a problem; 78 is named as a missing interpreter; a signal
  (`kickstart -k` leaves −9) is not.
- `tools/serverd.py` `build_plist`: `FABRIC_SERVICE_SUPERVISOR=launchd`.
- Docs: `docs/ONBOARDING.md` (the wholesale-change guard, the tick's health, the server
  descriptor), `docs/AGENT-ONBOARDING.md`, the scenario "A frozen registry and a job that cannot
  start say why" in `observatory/engine/docs/ux/portable-scenarios.md`; the engine copies by
  `tools/sync_engine_docs.py`; `SOURCE-INVENTORY.json`.

## Decisions

- **The guard is not relaxed.** It exists for collector failures that look like bulk changes (a
  missing `git` once turned 156 projects into 206). Letting a swing through on the first emit after
  an engine update would also let through a collector failure that coincides with one. The guard now
  says what it refused and the command, and the person decides.
- **An installed server keeps its old plist until `serverd.py --install` runs once.** An update
  restarts the server job and rewrites only the maintenance jobs' plists (`ensure_maintenance`); making
  it rewrite the server and tick plists changes what every update does to a running machine, and is
  left as its own task.

## Checks run

On macOS with Python 3.14.10, from a clean virtual environment (`pip install -c requirements-full.lock '.[full]'`):

- The new and changed cases first failed against the unchanged code: 7 of them (`TickWrapper`,
  `TailBail`, the two `registry-refused` cases, `job_state` and `job-exit`, the server plist's
  environment), then passed after the change.
- `project-observatory full check`: 298 suites, 297 PASS; the one FAIL was `i18n`, the glossary
  reserving «задача» for workflows, found by the blind unit review as well; the strings say «задание»
  now and `--suite i18n` passes. After the review's fixes `tick_health`, `tick_py`, `maintenance`,
  `fabric_service`, `workspace_scheduler` and `i18n` were run again: all PASS.
- `python -m unittest discover -s tests`: 318 run, OK (1 skipped).
- `python -m compileall -q observatory`, `python tools/update_inventory.py --check` (no differences),
  `docs/site/check.py --self-test`, `tools/build_article.py --check`,
  `node tools/check_site_interactions.cjs`: pass.
- `python tools/check_public_release.py --history --history-ref HEAD`: 0 findings.
- Not run here: Python 3.11 `compileall` (no 3.11 on the machine), the Linux rows,
  `claude plugin validate` (no plugin file changed); CI runs them.

A blind unit-tier review read the diff and found the i18n failure and four risks, all fixed with a
case each where they are behaviour: the pass job's 78 hidden by the merged `loaded` when the check job
is not loaded; `registry-refused` named with the scheduler off and after the person had already run
`full local` (the streak now ends at a registry written after the newest refused run); the wording of
exit 78 for the tick, whose program is `/bin/bash`; a `sys.path` entry added on every `doctor`.

## Open work

- An update rewrites the server's and the tick's plists, so a plist fix reaches installed machines.
- The automatic update waits for five minutes with no client of the local server
  (`maintenance.live_clients`); a host that polls the server more often than that (Fabric Dashboards)
  keeps it deferred indefinitely, on the measured machine from the first check of 0.30.0 until a person
  ran `full update --apply`. Which requests should count as a client is an operator decision.
- Backlog rows for this change and the two items above: the ids could not be reserved from an
  account without write access to `origin` (`agent_sync.py reserve OBS` needs to push
  `refs/agent-sync/ids/OBS`), so the rows are proposed in the pull request for a maintainer to add.
