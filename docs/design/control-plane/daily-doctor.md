# Daily doctor (M9, OBS-558, CP-15, decision D7)

## What

Once a day the maintenance job runs a fixed catalogue of checks over the engine's own state. A check whose fix is
safe (reversible, journalled, inside the engine's own files) runs it and journals its undo; every other check only
reports, as a finding of its own or by naming the existing rule that already raises the same problem. A model writes
a short summary only when the day's result differs from the previous run's, inside the engine's model budget.

| Part | Where |
|---|---|
| Catalogue, fixes, journal, undo, view | `observatory/engine/tools/daily_doctor.py` |
| Findings | `observatory/engine/tools/doctor_findings.py`, called by `build_findings.py` |
| Daily step and step streaks | `observatory/engine/maintenance.py` (`step_doctor`, `record_streaks`) |
| CLI | `project-observatory full doctor-daily [--json] [--undo ID \| --undo-last N]` |
| MCP | `observatory_doctor` (`action`: `read`, `run`, `undo`) |
| Tests | `observatory/engine/tests/test_doctor.py` (suite `doctor`) |

## Catalogue

| Check | Reads | When it fires | Outcome |
|---|---|---|---|
| `temp_orphans` | `store/`, `store/raw/`, `store/state/` (not recursive) | a `*.tmp` or `.maintenance-*` file older than 1 hour whose writer (a pid in `<name>.<pid>.tmp`) is gone | safe fix: quarantine |
| `receipt_unreadable` | `store/raw/*.json` | a receipt older than 10 minutes that does not parse | safe fix: quarantine |
| `backup_stale` | `store/maintenance.json` `snapshot` | the newest backup is 48 hours old or none is recorded | `doctor.backup_stale` |
| `step_failing` | `store/maintenance.json` `step_streaks` | a step answered `error` or `failed` on its last 4 passes | `doctor.step_failing`, one per step |
| `vault_meta` | the vault's `*.meta.json` only | a slot's metadata does not parse to an object | `doctor.vault_meta_unreadable` |
| `registry_built` | `registry/projects.json`, `store/maintenance.json` `registry` | the registry is missing or holds no project (OBS-624) | `doctor.registry_never_built`, with the pass's last registry answer |
| `scan_fresh` | `store/observatory.db` `scans` (read-only) | no scan for 3 hours | covered by `scan.stale` |
| `mcp_doors` | `store/raw/lifecycle.json` | a session server runs replaced code | covered by `lifecycle.stale_servers` |
| `plugin_behind` | `store/raw/harness.json` | an installed plugin is behind its marketplace | covered by `harness.plugin_behind` |

A safe fix that could not move a file is `doctor.fix_failed`. A covered check names the rule (`covered_by`) and
adds no second row.

## Data

- `store/state/doctor.json` (`doctor/1`): `at`, `checks` (`id`, `title`, `state`, `count`, `items` up to 20, and
  `fix`, `finding` or `covered_by`), `fixes` (`journal_id`, `check`, `files`), `findings`, `degraded`, `summary`
  (`state`: `written`, `unchanged`, `off` or `failed`, with `text` and `reason`), `signature` (the per-check states
  the next run compares).
- `store/logs/doctor.jsonl`: one line per event, `applied` (`id`, `check`, `kind`, `files`), `undone` (`files`,
  `left`) or `expired`. A journal id is `doc_` and 16 hex digits.
- `store/state/doctor-quarantine/<id>/<path>`: what a fix moved, kept 30 days, then removed and journalled `expired`.
- `store/maintenance.json` gains `step_streaks` (`passes`, `since`, `detail`) and `doctor` (`at`, `result`, counts).

```
Health of the engine, 2026-11-16                          summary (only when it changed)
  temp_orphans        fixed     2 files moved aside       undo: doc_3f0c...
  receipt_unreadable  ok
  backup_stale        finding   60 hours old              doctor.backup_stale
  step_failing        finding   org (6 passes)            doctor.step_failing
  vault_meta          ok
  scan_fresh          ok
  mcp_doors           finding   1 server                  see lifecycle.stale_servers
  plugin_behind       unknown   harness scan not read     (degraded)
```

## Contract

- `observatory_doctor()` answers `{at, checks, fixes, findings, summary, undoable, degraded}`; `undoable` lists the
  fixes still applied with their journal ids.
- `observatory_doctor(action="undo", journalId=ID)` answers `{undone, refused, degraded}` or a typed
  `error.code`: `invalid-journal-id`, `unknown-journal-id`, `already-undone`, `expired`, `nothing-to-undo`,
  `busy`, `invalid-argument`.
- `observatory_doctor(action="run")` runs the catalogue now and answers the view; it may spend on the summary,
  inside the model budget.
- The CLI's `doctor-daily` is the same function; parity entries `doctor-daily`, `doctor-daily --undo` and
  `doctor-daily --undo-last` name `observatory_doctor`.

## Failure behaviour

- A source that cannot be read leaves its check `unknown` and names it in `degraded`, never `ok`.
- The summary is off without the agent feature, a model key or room in the budget, and `degraded` says which; a call
  that fails is `failed`, never a failed run. An unchanged day makes no call. The prompt carries check ids, titles,
  states and counts only, never a file or slot name.
- A run and an undo hold one lock (`store/state/doctor.lock`); a second one answers `busy`.
- An undo never overwrites a file a collector wrote since: it keeps the quarantined copy and names it `replaced`.
- In the maintenance pass the doctor runs in its own process with a 300-second ceiling; a failed run is recorded in
  the pass's report and tried again at the next pass.
- Never: a snapshot, a server restart, a vault rewrite or a deletion run by a check; a vault value opened.

## Tests

`test_doctor.py`: a safe fix runs, journals and undoes (CP-15); an undo refuses a replaced file; `--undo-last`
takes the newest first; a dry run moves nothing; the quarantine is pruned after its keep; a stale backup, a failing
step, unreadable metadata and a registry never built only report and change nothing; covered checks add no row; absent sources are
`unknown` and named; the summary runs only on a change and is off without a key; the pass runs the doctor once a day
and retries a failed run. The MCP wire test reads the empty view and an unknown undo; the parity test pins the CLI
verbs to `observatory_doctor`.

## Not done here

A page section (M11's agent panel reads this view) and a second fix kind. A future fix must be reversible, journalled
and inside the engine's own files; anything else stays a finding.
