# Anonymous usage and error events: the catalogue

Every event the engine sends to its anonymous usage analytics (OBS-324), with its status, where
it is sent from (`file:line` of the emit) and every property it may carry. The code's own list is
`EVENTS` in [`observatory/engine/usage_analytics.py`](../observatory/engine/usage_analytics.py);
`tests/test_analytics_catalogue.py` fails when the two disagree or when an emit moves without its
line here.

The rules this file follows:

- An event's name is a time series: it is never renamed and never changes meaning. A command or
  tool that moves keeps its event; where it happens is a property (`surface`), never part of a name.
- A new meaning is a new event. A removed event stays here with status `retired`, its date and why.
- Properties change additively: a new key or a new value, written here in the same change; an
  existing value never changes meaning or unit.
- No identifier, path or name is ever a value: every value is a word from the lists below, a name
  the engine itself defines (a command, an MCP tool, an exception class) or a number.

What the sink adds to every event (`wire_event`, `observatory/engine/usage_analytics.py`):

| Property | Values |
|---|---|
| `iid` | a random UUID made for analytics only, kept in the workspace (`store/analytics/install-id`), deleted when the switch is turned off; never derived from the machine, an account or a person |
| `environment` | `production` (an installed release) or `sandbox` (a checkout, an editable install, a pre-release) |
| `platform` | `macos` on a Mac, `server` on Linux and Windows (growth's list has no desktop value for them; `systemProps.osName` names the OS) |

Beside the properties, every event on the wire (`wire_event` and `_system_props`,
`observatory/engine/usage_analytics.py`) carries:

| Field | Value |
|---|---|
| `timestamp` | when the event was recorded, UTC, milliseconds |
| `sessionId` | one per process (`session_id`): unix seconds x 10^8 plus 8 random digits, the numeric form Aptabase's own SDKs use; it groups the events of one command, MCP server or tick and names nothing |
| `eventName` | the event's name from the table below |
| `systemProps.isDebug` | true exactly when `environment` is `sandbox` |
| `systemProps.osName` | `macOS`, `Windows` or `Linux` |
| `systemProps.osVersion` | the macOS version, the Windows release (`10`, `11`), or on Linux the kernel's major.minor only (`6.5`; `os_version`) |
| `systemProps.appVersion` | the engine version that recorded the event |
| `systemProps.sdkVersion` | `project-observatory@<engine version>` |
| `systemProps.engineName`, `engineVersion` | `python` and the Python version |

No locale, device model, host name or user name is sent.

## Events

| Event | Status | Added | Sent from (surface, file:line) | Properties |
|---|---|---|---|---|
| `command_run` | live | 2026-11-04 | cli: `observatory/engine/observatory.py:1766` | `surface` (`cli`), `command`, `outcome`, `exit_code`, `duration_ms` |
| `mcp_tool_called` | live | 2026-11-04 | mcp: `observatory/engine/mcp/capability_tools.py:366` | `surface` (`mcp`), `tool`, `outcome`, `duration_ms` |
| `tick_finished` | live | 2026-11-04 | tick: `observatory/engine/tools/tick.py:543` | `surface` (`tick`), `outcome`, `steps_failed`, `duration_ms` |
| `error_raised` | live | 2026-11-04 | cli: `observatory/engine/observatory.py:1770`; mcp: `observatory/engine/mcp/capability_tools.py:369`; tick: `observatory/engine/tools/tick.py:546` | `surface` (`cli`, `mcp`, `tick`), `kind` |
| `analytics_notice_shown` | live | 2026-11-04 | cli and dashboard: `observatory/engine/usage_analytics.py:711` (reached from `observatory/engine/observatory.py:1771` at a person's terminal, and from `observatory/engine/tools/serverd.py:710` when the local server serves Home) | `surface` (`cli`, `dashboard`) |

## Properties

| Event | Property | Values |
|---|---|---|
| `command_run` | `surface` | `cli` |
| `command_run` | `command` | the first word of `project-observatory full ...` when the dispatcher knows it (`doctor`, `local`, `update`, `analytics`, `learning-review` ...), `help` for none or `--help`, `other` for anything else; never an argument |
| `command_run` | `outcome` | `ok` (exit 0), `refused` (exit 2), `error` (any other exit) |
| `command_run` | `exit_code` | the exit code, a number |
| `command_run` | `duration_ms` | milliseconds, a number |
| `mcp_tool_called` | `surface` | `mcp` |
| `mcp_tool_called` | `tool` | a tool name this MCP server serves (`observatory_status`, `estate.survey` ...), `other` for a name it does not serve; never an argument or an answer |
| `mcp_tool_called` | `outcome` | `ok`, `error` (an `isError` answer or a crash) |
| `mcp_tool_called` | `duration_ms` | milliseconds, a number |
| `tick_finished` | `surface` | `tick` |
| `tick_finished` | `outcome` | `ok`, `failed` (a step failed, or the tick stopped or crashed), `skipped` (another run held the registry) |
| `tick_finished` | `steps_failed` | how many steps failed, a number |
| `tick_finished` | `duration_ms` | milliseconds, a number |
| `error_raised` | `surface` | `cli`, `mcp`, `tick` |
| `error_raised` | `kind` | the exception's class name (`OSError`, `ConfigurationError` ...); never its message |
| `analytics_notice_shown` | `surface` | `cli` (printed once on a person's terminal), `dashboard` (the first time the local server served Home while the notice was on it; a page opened as a file is not counted, and the tick's build never sends it) |

## Retired

None.
