# Agent parity (M10 part 1, OBS-559, CP-18, decision D8)

## What

Agent parity is a test, not a promise. Every action a person can trigger on the dashboard and every
state-changing CLI verb either has an MCP tool, or is declared a person's gate with its reason, or is
recorded as missing with the board row that owes its tool. The list is
[`parity.json`](parity.json); the test is `tests/test_agent_parity.py`.

## Data

`parity.json` (`observatory-parity/1`):

- `actions`: one entry per action, `{action, kind, where, tool, gate, status, row?, note?}`.
  - `kind` is `dashboard` (a key of `keyserver.ACTIONS`, the reveal included) or `cli` (the verb as typed:
    `workflow handoff`, `cleanup --apply`).
  - `where` is the engine file and the function or verb that does the act (`tools/keyserver.py: act_mint`),
    without line numbers.
  - `status` is `covered` (`tool` names one served MCP tool, or a list of them), `gate` (`gate.reason` says
    why only a person can do it; no tool) or `missing` (`row` is the board row owing the tool; no tool, no gate).
  - `note` is optional context for part 2 (for example that the keyserver already serves an action over
    HTTP to a delegation token, or that the CLI requires a terminal today).
- `cli_excluded`: groups of top-level commands that are not acts, each with its reason.
- `cli_excluded_prefixes`: the same for whole families (`test*` suites, `scan-*` collectors).

## Contract

The test reads three surfaces from source with `ast`, never by import (importing the engine resolves a
workspace and the MCP SDK; this test runs in the plain `python -m unittest discover -s tests` job on
Linux, macOS and Windows):

- dashboard actions: the keys of `ACTIONS` in `observatory/engine/tools/keyserver.py`;
- CLI commands: `WORKSPACE_COMMANDS`, `OWN_PARSER_COMMANDS`, the keys of `STEPS` and `GROUPS` in
  `observatory/engine/observatory.py` (the dispatcher's whole surface, which
  `test_cli_compatibility.py` holds `main` to); a verb's further words must appear in that command's
  block of `PUBLIC_HELP`, or as a quoted literal in the file `where` names;
- MCP tools: `GOLDEN_TOOLS` in `observatory/engine/tests/test_mcp_wire.py`, the list the wire test holds
  equal to `tools/list`; the test also checks every `@server.tool` function of `mcp/server.py` is in it.

It fails when a keyserver action has no entry, a CLI command is neither named by an entry nor excluded, an
entry names a command or verb the CLI no longer has, a tool is not served, a `covered` entry has no tool, a
`gate` has no reason, a `missing` entry has no row, an exclusion has no reason or names a removed command,
or this note's counts disagree with the list. Each failure mode has a test that feeds a modified copy and
first proves the unmodified list passes.

## Counts (2026-11-14)

| status | count |
|---|---|
| covered | 7 |
| gate | 8 |
| missing | 94 |

109 actions: 9 dashboard (8 missing, the reveal a gate) and 100 CLI verbs (7 covered, 7 gates, 86 missing).
79 top-level commands are excluded by name and the `test*` and `scan-*` families by prefix.

Covered today: `workflow handoff` (`observatory_handoff_create`, `observatory_handoff_accept`), `workflow
close` (`observatory_checkpoint_write` with `close`), `org select` (`observatory_org_selection`),
`scan-mcp` (`machine.mcp.refresh`), `grant --check` (`grant.check`), `assistant ask`
(`observatory_assistant_ask`) and `assistant cancel` (`fabric.job.cancel`).

Gates: the reveal of a value to a person, `backup-passphrase set` and `assistant secret` (typed secrets),
`backup-passphrase show` (a value shown to a person), `assistant ceremony` and `share confirm`/`differ`
(a code two screens or two people compare), and `open` (the dashboard in a person's browser; the same data
is read through the MCP reads).

## Out of scope

- Read-only verbs (reports, listings, `status` and `show` subverbs, `doctor`, `version`, `machine`,
  `copies`, `digest`): they change nothing, and their data is read through `observatory_overview`,
  `observatory_status`, `observatory_machine` and the other reads. A command named by an action entry is
  classified as a whole; its read-only subverbs need no entry.
- Pipeline steps (collectors, merge, emit, the dashboard build, the tick): they rebuild derived workspace
  state; the tick and the maintenance job run them.
- Maintainer gates and development setup (`check`, `setup`, `probes`, the historical tests).
- `memory-http` and `relay serve`: server processes, not acts.
- `tools/vault.py`, `tools/use_secret.py` and the provider doors (`tools/openrouter.py issue` and the
  rest) are separate programs, not verbs of `observatory.py`; the keyserver refuses `put` and `rotate` and
  sends the caller to `vault.py`. Part 2 decides whether their writes join this list.

## Adding a new action

1. Add the action to the code (a key of `keyserver.ACTIONS`, a verb of `observatory.py`).
2. Add its entry to `parity.json`: with the MCP tool that does the same act (`covered`, the tool listed in
   `GOLDEN_TOOLS`), or with `gate.reason` when only a person can do it (a typed secret, a value shown to a
   person, a code compared by people), or as `missing` with the board row that owes the tool.
3. A read-only command goes into a `cli_excluded` group whose reason fits, or a new group with its reason.
4. Update the counts above; the test reads them.

Part 2 (M10, waves 2 to 4) turns each `missing` entry into a tool or, by a recorded decision, a gate.
