# Runtime receipts: the writers declare what may change during an update (OBS-610)

## What

An update stops the tick and the local server, snapshots the workspace, stages the new release, and then requires
every file but the databases to have held still; anything that moved refuses the update and rolls it back. Some
writers are never stopped: the session hooks of every coding session, the MCP servers those sessions run, the
five-minute update-check job, and a server whose launchd job something loads again meanwhile. Until now the files
they may change were hand-kept lists in `workspace_upgrade.py` (`SESSION_RECEIPTS`, `JOB_RECEIPTS`,
`ANALYTICS_RECEIPTS`, `RUNTIME_RECEIPTS`, the log rule), and each writer missing from them rolled an update back:
the check job (2026-11-10), the analytics queue (2026-11-11), the server's receipt (2026-11-17,
[update-server-receipt.md](update-server-receipt.md)).

Now the writers declare. `observatory/engine/runtime_receipts.py` holds one `Receipt` per file: its
workspace-relative path, its owner modules, which live writer writes it (`session-hook`, `session-mcp`, `check-job`,
`server`), one line of why, and optionally `belongs(workspace, file)`, how to tell this workspace's own writer wrote
it. Each writer takes its path from its declaration (`RECEIPT = rr.SERVERD.at(paths.SCRATCH)`), so the path it
writes and the path the check excuses are one value. The stillness checks read `runtime_receipts.live()` and
`runtime_receipts.foreign()` and nothing else.

## Data

```
runtime_receipts.DECLARED           Receipt(path, owners, writer, why, belongs=None)
  store/raw/record-turn.json          session-hook  tools/record_turn.py
  store/raw/sessions-seen.jsonl       session-hook  tools/session_start.py
  store/raw/companion-faults.jsonl    session-hook  companion_faults.py
  store/raw/why-asks.jsonl(.1,.lock)  session-hook  why_asks.py
  store/update-check.json, .lock      check-job     maintenance.py
  store/analytics/queue.jsonl, counters.json, .counters.lock, notice.json,
                  install-id, counters-problem.json   session-mcp  usage_analytics.py
  store/raw/serverd.json              server        tools/serverd.py   belongs: its `workspace` is this home
LOG_FOLDER, LOG_SUFFIXES            store/logs/*.log|.err|.out|.jsonl and their rotated generations
HOLDS_STILL                         files those modules name that hold still (reads, or a part the update stops)
```

A declaration also covers its writer's temporary file beside it, `.<name>.<anything>`: `atomic.write_json`,
`usage_analytics._private_write` and `maintenance.write_state` (now `.<name>.<random>.tmp`, so the daily doctor
still clears a crashed one) write there and rename.

## Contract

```
volatile(name, databases) = name is a database or its WAL companion, or runtime_receipts.live(name)
steady(inventory)         = every path not volatile, plus runtime_receipts.foreign(base)
snapshot / upgrade        : steady(before) == steady(after), else refuse naming the moved paths
```

The guarantee is kept: a path no declaration covers (config, registry, vault, secrets, the store's other files,
`store/analytics/` files not declared, the tick's receipts) must hold still and its change refuses with its name;
a declared file whose `belongs` says no (a server receipt naming another home, or no JSON object) holds still too.
The compatibility views `SESSION_RECEIPTS`, `JOB_RECEIPTS`, `ANALYTICS_RECEIPTS`, `RUNTIME_RECEIPTS` and
`foreign_runtime_receipts` in `workspace_upgrade` are derived from the registry.

Readers of a declared file (the dashboard, findings, `local_ports`, `peer_check`, `dashboard_open`, the assistant,
`service_connections`, the gate's `FOREIGN_WRITES_IGNORED`, `check_docs`) take the path from the registry too.

## Found while declaring

- `store/analytics/install-id`, made by a fresh workspace's first event: a session's MCP server recording its first
  call while an update staged rolled the update back (reproduced by
  `test_2026_11_11_a_sessions_analytics_while_an_update_stages`, red before this change).
- `store/analytics/counters-problem.json` and the analytics writer's temporary files, and the check job's
  temporary file (`.maintenance-<random>` in `store/`), were in no list.

## Failure behaviour

Unchanged: a refusal names up to five moved paths (never contents), the update rolls back from its own snapshot.
A new live writer that writes a file it did not declare fails `test_a_live_writer_writes_nothing_it_did_not_declare`
(static) and `test_everything_a_live_writer_changes_is_declared` (the real writers run against a workspace) before
it ships. An MCP tool that deliberately changes workspace state (settling a ledger line, the configuration door) is
not a receipt: its change still refuses an update that stages beside it, which is the safe side.

## Tests

`observatory/engine/tests/test_runtime_receipts.py` (registered in `run_portable.py`):

- `Declarations`: every declaration is a `store/` file with an owner and a reason; each owner takes its path from
  `rr.<NAME>`; no engine module spells a declared name or path itself (AST, docstrings excepted; `skill/` hooks run
  without the engine and are left out); a live writer's module joins no runtime folder with an undeclared name
  (AST, `HOLDS_STILL` for reads), and that scan fires on a synthetic undeclared writer; what is live; the upgrade's
  views equal the registry.
- `LiveWriters`, with the real writers (`maintenance.check_pass`, `usage_analytics`, `serverd.heartbeat`,
  `companion_faults`, `why_asks`, `session_start`): everything each changes is declared; the three rollbacks
  reproduced (`test_2026_11_10_...`, `test_2026_11_11_...`, `test_2026_11_17_...`) and the hooks while staged now
  pass; an undeclared change (settings, `store/raw/tick.json`, an undeclared analytics file, `store/maintenance.json`,
  a secret) still refuses both the snapshot and the upgrade and is named; a foreign server receipt still refuses.
- `tests/test_workspace_upgrade.py` keeps its OBS-591 and earlier cases, now over the registry's views.

## The server loaded again during an update (OBS-593)

### What loaded it

The 0.40.0 update of 2026-11-17 (UTC) stopped the server at 05:09:33 and rolled back at 05:25 because
`store/raw/serverd.json` moved while the upgrade was staged ([update-server-receipt.md](update-server-receipt.md)).
The server that wrote it was started by launchd at 05:18:06. What loaded the job, from the machine's records:

- An agent session working in another project ran the workstation check every agent runs before a change
  (the workstation launcher's `doctor`, 0.2.78). Its services row for this server read `fail`: nothing listened on the
  server's port, and the row's fix said the launchd job restarts it, with a `kickstart` repair attached
  (`lib/doctor.js`, the services section). The session then read `full doctor` and `launchctl print` (the label not
  loaded) and, at 05:18:04Z, ran `launchctl bootstrap gui/<uid> <the server's plist>`; it answered `rc=0` and the
  session noted the server "up again". launchd spawned the server two seconds later: the `uptime_s` 319 the moved
  receipt carried at 05:23:25. The session's transcript holds each step with its time.
- No engine code did it. Every path in the engine that loads or restarts the server's job was read: the update's
  own `start_writers` and `recheck_writers` (after staging), the daily snapshot's `finally` (under the update
  lock), `restart_left_stopped` (at a pass's start, from its own record), `recover_lost_server` (decided once on
  2026-11-09), `_step_launchd_jobs` (reloads only a job that is loaded), `local_ports._reinstall` and
  `serverd --install` (a port move, a person), the Mac app's Start server (`assistant.serve`: `kickstart -k`, which
  fails on an unloaded label) and `full open --serve` (a detached process, not the job). The check job returns at
  once while an update holds the lock. The machine's lifecycle broker enrols no Observatory target, and its
  journal has no entry in the window. launchd's own log keeps a `bootout` caller but not a `bootstrap` one, and its
  files did not reach back to 05:18; the transcript is the record.
- The same day's second attempt (0.41.0, 20:07Z to 21:04Z) was watched live: nothing loaded the server during its
  38 minutes of staging (a process watch on `launchctl bootstrap|kickstart|load|enable` and launchd's log for the
  label). It rolled back for another reason: the staged workspace upgrade ran past its 1800 s limit
  (`TimeoutExpired`, exit 125).

### The change: the server honours the update lock

Whoever loads the job, the process launchd starts is this engine's server. `serverd.serve` now begins with
`stand_down_while_updating`: while an update holds this workspace's update lock (`engine_update.update_lock_held`,
the lock beside the home that `full update --apply` and the daily snapshot hold from the stop to the start), the
server waits, polling every 2 s, before its instance lock, token, manifest, socket or receipt: it writes nothing
the stillness check reads. It says so once on stderr (`store/logs/serverd.err`, a log). When the lock is free it
starts as before.

```
launchd starts serverd ─▶ an update holds the lock? ─ no ─▶ lock, token, bind, beat (as before)
                                   │ yes
                                   ▼
                    wait 2 s, write nothing (SIGTERM ends it) ─▶ look again
```

- The update's own start comes while it still holds the lock (`start_writers`, then `maintain ensure`): that server
  waits the seconds until the update ends, then starts on the installed release. A job someone loaded meanwhile is
  restarted by the update (`restarted`), and the new process waits the same way.
- Only a server whose receipt goes into this workspace waits: one with a scratch redirected out of the home (a
  test, a probe) writes nothing the update checks. A probe of the lock that cannot answer (`OSError`) never keeps
  the server down. SIGTERM is unblocked before the wait, so launchd and `full open --stop` still end a waiting server.
- The receipt's exemption (OBS-591, the `SERVERD` declaration) stays: it covers a server that was already past its
  start when an update began, and a `belongs` refusal still holds a foreign receipt still.

### What this repository cannot change

The reload came from a check that cannot tell a server stopped on purpose from a server that died: nothing answers
on the port either way. The contract change it needs is outside this repository, and is recorded here, not filed:

- fabric-service/0.1 (the descriptor): a way for a service to say it is held stopped on purpose for a while, for
  example `lifecycle.held: {by: "update", since}` written by the update next to the descriptor for the stop window,
  or the descriptor naming the update lock a host may probe without taking it for long.
- The workstation launcher's doctor: a closed port of a service held that way is `info` ("stopped for an update"),
  with no repair attached, instead of `fail` with a `kickstart` repair.
- Fabric Dashboards' Start and Restart: refused while the service is held, as they are for a service placed
  elsewhere.

### Tests

`observatory/engine/tests/test_runtime_receipts.py`:

- `LiveWriters.test_2026_11_17_a_server_loaded_while_the_update_holds_its_lock_waits`: the real update lock is held
  around a real staged upgrade; a server started during staging (the gate, then the real heartbeat) waits, the
  receipt does not exist when the upgrade answers `upgraded`, and the server writes it once the lock is released.
  Red before this change (the server wrote at once).
- `TheServerStandsDownForAnUpdate`: waits while held and says so once; starts at once when free; a probe that raises
  does not keep it down; a scratch outside the home never waits; `serve` runs the gate before its single-instance lock.
