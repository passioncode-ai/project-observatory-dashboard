# From the original installation to the full public engine

Release 0.2.0 includes the original engine. The smaller 0.1 implementation remains
under its existing CLI commands; run `project-observatory full …` for the complete
engine. A new public repository was used to avoid publishing private operational
Git history. It is a distribution boundary, not a second reduced product.

This page records the extraction as release 0.2.0 delivered it. Later releases
changed some of what it describes; the [changelog](../CHANGELOG.md) and the
[onboarding guide](ONBOARDING.md) describe the current engine.

## Delivered extraction

| Area | Public implementation | Verification |
|---|---|---|
| Separate source and user state | `observatory/engine/paths.py`, `configuration.py`, `workspace.py` | `test_workspace.py`, `test_workspace_boundaries.py` |
| Original registry and SQLite migration | `workspace.py`, `store/compatibility.py`, `store/migrate.py` | `test_schema_compatibility.py`, every original migration prefix |
| Backup, staged upgrade and restore | `workspace_upgrade.py` | `test_workspace_upgrade.py`, injected failure/locking/restore cases |
| Original CLI with public command boundary | `observatory.py`, `docs/CLI-COMPATIBILITY.md` | `test_cli_compatibility.py`, old public launcher tests |
| Inventory, events, findings, metrics and pages | `collectors/`, `store/`, `dashboard/` | offline local pipeline, events/metrics/pages/render suites |
| Optional provider and analytics sources | collectors and `plugins/` | provider-boundary/health, analytics, private-source suites; live calls not part of release checks |
| Versioned trusted plugin interface | `collectors/run_plugins.py`, plugin manifests | `test_plugins.py`; manifest API and dependency boundaries |
| MCP and proposal authority | `mcp/server.py`, `fabric/schemas/` | wire contract/input/MCP/provenance suites; external host admission unverified |
| Credentials and explicit remediation | `tools/vault.py`, `use_secret.py`, `keyserver.py`, `scrub_companion.py` | vault/keyserver/provider boundary cases; live rotation not executed |
| Workspace-specific scheduler | `tools/tick.py` (wrapper `tools/tick.sh`), `install_launchd.py`, `serverd.py` | scheduler isolation/lease/opt-in tests; the tick and server start only when chosen (in 0.2.0 no service started by itself; since 0.17.0 `full init`, `install_launchd.py` and `full update` schedule the maintenance job, `maintenance.py`) |
| Agent instructions | `skill/plugins/observatory-log/` | both skill audits and strict plugin validation |
| Reviewed public distribution | source inventory, public privacy gate, wheel gate | clean source export and installed-wheel test; see [release receipt](RELEASE.md) |

Paths in the implementation column are relative to `observatory/engine/` unless
stated otherwise. Runtime tests live beneath its `tests/` directory. The exact
selected suite is [run_portable.py](../observatory/engine/tests/run_portable.py).

## Move an existing installation

Use the [onboarding guide](ONBOARDING.md#upgrade-back-up-restore) and
[compatibility policy](COMPATIBILITY.md). Select a new empty workspace, preview
`full migrate-local ORIGINAL_DIRECTORY`, stop every writer, then apply with
`--apply --writers-stopped`. Verify `doctor`, registry counts, database health,
external source references and selected integrations before activating services.
The original stays in place. Never run two schedulers against one workspace.

The default home for the full engine differs from the compatibility CLI. A 0.1
workspace is not an original full-engine source installation and must not be
silently converted into one. Keep both homes separate if using both commands.

## Boundaries deliberately retained

- Each user provides account access. Source publication does not enroll accounts,
  enable paid model calls or prove every third-party permission combination.
- Windows service/file-lock support is not claimed. Supported hosts are macOS
  and Linux with the documented Python/SQLite requirements.
- Authenticated local key reveal remains an explicit administrative operation.
  The local key service is not a multi-user internet service.
- Optional model interpretation, retention, notifications, wiki projection and
  remediation remain disabled until chosen. Known-value scans cannot find every
  unknown, encoded or historical secret.
- External credential stores and source directories are not included in managed
  workspace backups. The restore guide makes those exclusions explicit.
- Private registries, credentials, audits, original history and live screenshots
  were not distributed. The public case study contains only its reviewed aggregate.

## Future work, separately scoped

Release 0.2.0 closed extraction and compatibility. Next UX work is captured
in [UI-PLAN.md](ux/UI-PLAN.md): coverage-first navigation, clearer empty/error states,
accessible large tables and visible action consequences. These are design packets,
not claims that all interface improvements have shipped. Live integration acceptance
must be run by each operator on their own accounts with explicit authorization.
