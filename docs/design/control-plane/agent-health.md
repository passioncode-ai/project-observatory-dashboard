# Agent health (OBS-577, `agent-health/1`)

## What

One reading of what is wrong with this engine now, in the shape every product of the estate answers
(fabric-agent-contract DEC-0179, `docs/specification/agent-health.md` HL-1 to HL-7, rule `FAC-SEM-181`). A watcher on a
machine reads it on its own interval and keeps episodes over it; the engine only states the reading.

## Data

`machine_health.report()` builds `{schema, product: "project-observatory", version, at, state, checks}` from the
findings the engine already builds (`registry/findings.json`, acknowledged rows skipped). `state` is the worst of the
checks in the order `ok < unknown < warn < fail`, `unknown` for none (`worst_state`, HL-2).

| Check | Area | Counts | State |
|---|---|---|---|
| `disk_low` | environment | `machine.disk_low`, `host.disk_low` | `fail` at critical, else `warn`; `unknown` when `host.disk_unknown` |
| `memory_pressure` | environment | `machine.memory_pressure` | `warn` |
| `vm_disk_errors` | environment | `vm.disk_errors` | `fail` |
| `plugins_behind` | update | `harness.plugin_behind` | `warn`; `unknown` when the harness scan is degraded |
| `keys_leaked_unrotated` | security | `secret.leaked_unrotated` | `fail` if any is critical, else `warn`; `unknown` when the leak scan or register could not be read |
| `keys_expiring` | security | `credential.expiring`, `credential.expired` | `fail` if any is critical, else `warn` |
| `dependencies_exploited` | security | `dep.vulnerable` with the known-exploited mark | `warn`; `unknown` when the watch is stale |
| `keys_in_repositories` | security | `secret.in_repository` | `fail` if any is critical, else `warn`; `unknown` on a blind or unmeasured sweep |
| `mcp_risky` | security | `mcp.key_in_url`, `mcp.risky_command` | `warn`; `unknown` when the harness scan is degraded |
| `integration_keys_invalid` | connection | `integration.key_invalid` | `warn`; `unknown` when the daily integration health report (`store/raw/integration-health.json`) is absent or unreadable |
| `ssh_keys` | security | every `ssh.*` finding | `warn` |

Each check's `fix` is `{"tool": ...}` naming the Observatory MCP tool that shows it (`observatory_machine`,
`observatory_credentials`, `observatory_dependencies`, `observatory_findings`, `machine_mcp_inventory`,
`observatory_integrations`, `observatory_ssh_keys`). A check with nothing found and nothing blind is `ok` with count 0.

Not reported, because the engine measures nothing for it yet: an engine version behind the published one (the
update check is not recorded). It is omitted, never `ok` (HL-5). Adding it is a new check id; ids never change (HL-6).

## Contract

Served as the MCP tool `observatory_health` (read, no arguments) and the CLI `project-observatory full health --json`,
the same document. The contract's schema is closed, so the answer carries no `degraded` field: what could not be read
is a check with state `unknown`. No path, slot name, project name or finding text reaches the report (FAC-SEM-181,
DEC-0157); only counts, check ids, areas and tool names.

The contract's schema, `common.schema.json`, vectors and fixtures are vendored byte for byte under
`observatory/engine/fabric/agent-health/` at commit `db0d8f15` (see its README); `machine_health.py` ports
`worstState`, `readHealth`, `episodeKey` and the report rules.

## Wireframe

No page. A watcher or an agent reads the document:

    {"schema": "agent-health/1", "product": "project-observatory", "version": "0.39.1", "at": 1794830000,
     "state": "warn", "checks": [{"id": "disk_low", "area": "environment", "state": "warn", "count": 1,
       "fix": {"tool": "observatory_machine"}}, ...]}

## Failure behaviour

No `findings.json`, unreadable JSON or a findings value that is no list: every check is `unknown` and so is the
report. A scan that says it could not measure turns only its own checks `unknown`.

## Tests

`observatory/engine/tests/test_machine_health.py`: vendored digests; every vector (`worst`, `read`, `episodes`) and
fixture through the port; a report built from findings validates against the vendored schema and the rules; a planted
path or slot name never appears; unread sources give `unknown`. `tests/test_mcp_wire.py` pins the tool name and
`tools/shipped.lock.json` the add-only surface.
