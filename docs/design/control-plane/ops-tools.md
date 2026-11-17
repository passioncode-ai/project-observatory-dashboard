# The operations verbs for an agent (M10 part 2, next wave: OBS-559, CP-18, decision D8)

## What

The CLI's maintenance, backup, update and disk verbs had no MCP tool: `parity.json` listed them `missing`. One tool,
`observatory_ops`, gives an agent the same acts with the same effect, each the CLI's own function run in a job:

| action | CLI verb | decision | runs |
|---|---|---|---|
| `workspace-backup` | `workspace-backup --pause-writers --json` | | in the runner |
| `machine-backup` | `machine-backup now` | | in the runner |
| `backups-migrate` | `backups migrate` | `backups.migrate`, target `legacy-copies` | in the runner |
| `backups-decrypt` | `backups decrypt FILE OUTPUT` | `backups.decrypt`, target OUTPUT from the home | in the runner |
| `cleanup-plan` | `cleanup [--level L]` | | in the runner |
| `cleanup` | `cleanup --apply [--include manual] [--level L]` | `disk.cleanup`, target `auto` or `manual` | in the runner |
| `disk-guard-plan` | `disk-guard [--level L] [--large-files]` | | in the runner |
| `disk-guard` | `disk-guard --apply [--level L] [--large-files]` | `disk.guard`, target L or `current` | in the runner |
| `update` | `update --apply --unattended [--version X.Y.Z] [--reinstall]` | `engine.update`, target X.Y.Z or `latest` | own process group |
| `maintain-run`, `-ensure`, `-uninstall`, `-app` | `maintain run`, `ensure`, `uninstall`, `app` | | in the runner |
| `auto-update-on`, `-off` | `auto-update on`, `off` | | in the runner |

Left `missing`, for a later wave: `upgrade --apply` (needs the writers stopped, which `update --apply` does inside its
transaction), `restore` and `machine-restore --apply` (they overwrite a workspace or a home; a separate design), the
`machine-move` verbs and `retention-apply` (the ledger's retention, not the disk).

## Decision: one tool, every act a job

- One tool for the family, as the key page's acts are one tool: the fifteen actions share one shape (a CLI verb, a
  handful of optional arguments, an optional decision) and one answer (a job handle). Three tools (backup, disk,
  maintenance) would repeat the same arguments, refusals and job handling three times.
- Every act answers a job (fabric-interop/0.1 `fabric.job.get`, `fabric.job.cancel`), the quick switches too: the CLI
  prints its answer on stdout, and capturing stdout inside the stdio server would share the process with the JSON-RPC
  channel. The detached runner (`tools/run_job.py`) is its own process, so it captures the CLI's output safely, and a
  backup or an update outlives the host session that asked for it.
- `jobs.start(..., exclusive=True)` per action: a second call with the same arguments joins the running job
  (`joined: true`); with other arguments it is refused `busy`.

## Same effect

`ops_acts.command` builds the argument list a person types after `project-observatory full`; `ops_acts.run` runs it
through `workspace.main` (or `workspace.machine_command` for `cleanup` and `disk-guard`, with the JSON summary line
the maintenance job reads, `json_summary=True`, which is not a flag a person types). An update runs the engine's CLI
(`python -P -m observatory --home HOME full update ...`) in a process group of its own, the route the maintenance pass
uses (`maintenance.Commands.apply`): a cancel of the job stops following it and never stops pip or the release's
upgrade half-way. The runner checks the job's stored arguments again and rebuilds the argument list; a record that no
longer checks fails the job. A test reads `ops_acts.py` for any backup, cleanup, guard or update function called
directly, and keeps it so.

## The gate

A stdio server has no caller identity to check, so an act that deletes or is hard to undo takes the operator's words
in contract DEC-0121's shape, checked by `key_acts.decided` (`node/actions.py`), as the key page's acts are:
`backups-migrate` removes the older legacy copies, `backups-decrypt` writes a backup's plaintext, `cleanup` and
`disk-guard` remove files (the Trash included), `update` replaces the running engine. The plans, the backups, the
schedule and the switch are reversible or create only, and take no decision; one sent with them is refused, not
dropped.

`backups-decrypt` writes only a NEW path inside the workspace: no `..`, the folder it goes in exists inside the home
once symlinks are resolved, the path itself does not exist. The plaintext never reaches an answer: the job's output is
the CLI's `{status, kind, output}`.

The acts that touch launchd, systemd or the credential store (`maintain-run`, `maintain-ensure`, `auto-update-on`,
`update`) run with `OBSERVATORY_SYSTEM_SETUP=1`, as the scheduler and the plugin hook run them; a server started with
`=0` refuses them (`system-setup-off`), before and inside the job.

Not offered: `workspace-backup --output` and `--plaintext` (a plaintext copy of the vault's secrets outside the managed
root), `--writers-stopped` (a person's claim the agent cannot check; `--pause-writers` stops and restarts them), and
`update --no-rollback`.

## Contract

```
observatory_ops {action, file?, output?, include?, level?, largeFiles?, version?, reinstall?, decision?}
  -> {action, job: {id, status: working}, follow: "fabric.job.get", joined?, recordedAs?: {by, via, caller}}
fabric.job.get {id} -> {job: {..., result: envelope}}
  envelope.output = {action, command, exit, result: the CLI's JSON (scrubbed) or null, said: its last stderr lines}
  envelope.outcome = succeeded (exit 0) | partial (another exit, with the CLI's JSON)
  a job whose command exited non-zero with no JSON fails with the command's last line, redacted
refusal -> {error: {code, message, action?, target?}, degraded: []}
  codes: invalid-argument, operator-decision-required, decision-invalid, system-setup-off, busy, failed
```

The journal `store/logs/ops-acts.jsonl` (0600) gets one row per accepted act before its job's process starts:
`{at, action, params, caller: agent:mcp, caller_verified: false, job, decision?, target?}`, every string scrubbed.

## Failure behaviour and secrets

- No secret crosses: an argument shaped like a credential is refused by field name and never echoed; a decision
  carrying one is refused; every string of a job's answer and of the journal goes through `credential_shape.redact`.
- A command that fails is said by its exit and its last lines (redacted), never by a silent success.
- A cancel of an act in the runner is a SIGTERM, as Ctrl-C at a terminal: the act's own `finally` runs (the backup
  starts the writers again). A cancel of an update stops following it; the update's own journal and rollback stand.

## Tests

`observatory/engine/tests/test_ops_acts.py` (suite `ops_acts`): every action's argument list; the gate (missing,
wrong action, wrong target, old, a credential in the words); the decrypt path (outside, `..`, a symlink out, an
existing path, a missing folder); stray and malformed arguments; a pasted fake key refused and absent from every
answer and the journal; `system-setup-off`; `busy` and a failed start; the runner with an injected CLI (its JSON, a
partial and a failed exit, the setup flag set and restored, the disk verbs through `machine_command` with
`json_summary`, the update in a process of its own in a new group, a tampered record refused); the source checks.
`test_mcp_wire.py` calls the tool over stdio (an update without words, a decrypt outside the workspace).
`tests/test_agent_parity.py` holds the parity list.
