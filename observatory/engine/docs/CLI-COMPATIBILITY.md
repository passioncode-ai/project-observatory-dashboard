# CLI and state compatibility

The full engine keeps the original Observatory implementation. The published
portable interface and full engine have distinct command namespaces and state
formats; sharing a version number does not make their data interchangeable.

## Public acceptance check

`public-profile.json` selects the public source profile. Its format is version 1;
an unknown or malformed profile fails closed. An unmarked original source tree
retains its historical `check` group. This is a distribution property, never a
per-user setting. [Implementation: `public_profile`](../observatory.py).

In the public profile:

```sh
python observatory.py check
python observatory.py check --suite schema_compatibility
python observatory.py check-portable --suite cli_compatibility
```

Both check commands execute [`tests/run_portable.py`](../tests/run_portable.py).
The runner has an explicit suite list and creates isolated synthetic workspaces.
Its receipt distinguishes tested code from live provider acceptance, external
MCP host admission and real credential rotation, which this check does not run.
A missing suite, execution failure or failed assertion is not silently discarded.
The public `check` needs no initialized operational workspace and does not tighten
its permissions or acquire its registry lease. See
`test_public_check_forwards_options_exit_and_never_opens_workspace` and
`test_missing_runner_is_an_error_not_a_skip` in
[`tests/test_cli_compatibility.py`](../tests/test_cli_compatibility.py).

Historical individual test names remain callable when their source payload is
included. Help omits commands with absent payloads; invoking one returns exit 2
before opening an operational workspace. The public acceptance set does not claim
to reproduce the private historical suite or its production evidence.

These source-only commands are unavailable in the public profile, with an
explicit error: `setup`, `deps`, `docs-current`, `paths-current`, `trap-map`,
`validate-plugin`, and the historical `all` composite. Their original payloads
assume a development checkout, private documentation or companion-plugin build.
Install the packaged full-engine extra using the installation instructions;
use `local` for a local observation cycle or an explicitly enabled `tick` for the
configured scheduled workflow. This is not a silent filtering of `all`.
[`PUBLIC_SOURCE_ONLY`](../observatory.py) is the authoritative list.

## Local and scheduled work

`local` runs these steps in order:

1. `scan-fs`, `merge`, `emit`, `validate`.
2. `scan-events`, `findings`, `dashboard`, `smoke-pages`.

Its children receive `OBSERVATORY_OFFLINE=1`, which disables configured
integrations for that invocation; settings are not rewritten. In particular,
merge cannot call GitHub to resolve repository names even if GitHub is normally
enabled. This is an application-level offline policy, not an operating-system
network sandbox for untrusted checkout code or Git helpers. There are no metric
plugins, remote collectors, model calls or secret scans in this group.
The page smoke check requires Node; absent Node is reported as unmeasured.
See [`GROUPS` and `run`](../observatory.py),
[`configuration.enabled`](../configuration.py), and
`test_local_step_passes_offline_flag_without_changing_parent_environment`.

`plugins` is explicit because plugin commands may contact configured services.
`tick` retains the complete observation workflow and independently gates remote
collectors and consequential features. It includes Cloudflare, MCP inventory,
remote environment comparison and Google collection before merge; collectors
retain their cache policies. Git remote probes require
`integrations.git_remotes=true`, including direct collector invocation.
The scheduler itself requires `features.scheduler=true`.
[`tools/tick.sh`](../tools/tick.sh),
[`INTEGRATION_STEPS` and `FEATURE_STEPS`](../tools/tick_lease.py), and
[`collectors/scan_remotes.py`](../collectors/scan_remotes.py) define this contract.

`tools/gate.sh` selects the same public check, preserves its exit status and
forwards runner options. Its logs belong to the selected private workspace,
not the code checkout. Synthetic coverage:
`test_gate_logs_and_exit_code_are_workspace_scoped` in
[`tests/test_workspace_scheduler.py`](../tests/test_workspace_scheduler.py).

## Backup names and upgrades

The original full-engine `backup` command still means a SQLite store backup.
It is not repurposed. `workspace-backup` creates a full private workspace snapshot;
`upgrade` previews by default; `restore SNAPSHOT` restores into a new empty
workspace. Mutating backup/upgrade operations require the documented
`--writers-stopped` declaration because older and external writers do not obey
new locks. No command activates a scheduler while restoring.
Since 0.4.1 `restore` also accepts an encrypted `.obsnap` file, and the default
`workspace-backup` output is exported encrypted to the backups root when a
passphrase is configured; an explicit `--output DIR` keeps its plaintext meaning.
`backups` and `backup-passphrase` are new workspace commands; nothing existing
was renamed. [`backup_vault.py`](../backup_vault.py) implements the root and format.
[`WORKSPACE_COMMANDS`](../observatory.py), [`workspace.main`](../workspace.py),
and [`workspace_upgrade.py`](../workspace_upgrade.py) implement the separation.

Future workspace/configuration/registry formats and unknown database migrations
are refused rather than guessed. Existing migration IDs are preserved, pending
migrations apply atomically, and SQLite backups include committed WAL data.
Old migration histories gain checksum records on first adoption; that adoption
cannot prove what historical code originally ran. Downgrade recovery uses a
compatible release and a snapshot restored into a new workspace; there is no
automatic destructive down-migration.
Tests: [`test_schema_compatibility.py`](../tests/test_schema_compatibility.py),
[`test_workspace_upgrade.py`](../tests/test_workspace_upgrade.py), and
[`test_workspace_boundaries.py`](../tests/test_workspace_boundaries.py).

## Portable 0.1 interface

The portable `project-observatory` interface owns `init`, `doctor`, `demo`,
`scan`, `status`, `dashboard`, `export`, `serve`, `project`, `secret` and `leaks`.
Full-engine work uses the explicit `full` namespace in the distribution, or
`python observatory.py` when running the engine's source directly. Identical
words such as `scan`, `project`, `leaks` and `dashboard` do not imply equivalent
arguments or behavior across these interfaces.

Portable schema 1 data includes `config.json`, `fingerprint-salt`,
`history.sqlite3`, `latest.json`, `leaks.json`, `dashboard.html` and flat
`secrets/NAME` slots. Full-engine state uses `workspace.json`, `config/`,
`registry/`, `store/observatory.db` and project/environment-scoped secrets.
Do not initialize one mode over the other's home. Keep the portable salt byte
for byte: changing it breaks stable fingerprint comparisons. Do not infer a
project/environment for a portable flat secret slot. Portable history remains
in its native schema, with no fabricated full-engine project identifiers.
The package CLI and initializer tests verify the distribution-level boundary;
the engine migration path accepts the original full-engine layout explicitly.

Two local servers require different ports when used concurrently. Workspace
labels prevent scheduler-job collisions; they do not allocate TCP ports.
Individual engine step commands have fixed arguments; additional arbitrary
arguments are rejected instead of silently ignored. Use the underlying tool's
CLI for its detailed options. Workspace commands and the portable check runner
forward their documented arguments.

## Regression receipt for this change

Executed in the isolated full-engine source, using synthetic fixtures only:

- `.venv/bin/python -W error::ResourceWarning tests/test_cli_compatibility.py`: 14 tests passed.
- `.venv/bin/python -W error::ResourceWarning tests/test_workspace_scheduler.py`: 16 tests passed.
- `/bin/bash -n tools/tick.sh` and `/bin/bash -n tools/gate.sh`: exit 0.

These receipts cover the new routing and scheduler boundaries. They are not a
claim that the complete packaged acceptance runner or live providers passed.
