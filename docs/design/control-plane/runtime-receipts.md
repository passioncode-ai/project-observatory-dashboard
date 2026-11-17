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
