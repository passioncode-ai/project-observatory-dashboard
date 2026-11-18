# The MCP supply-chain watch (M6, OBS-555, CP-09)

## What

A watch over what an MCP server is and whether it stays what the person approved. Three parts: the server's command
line is judged by rule; each tool's full definition is pinned by digest when a server is probed, and a later change is
a finding the person accepts; third-party remote scanners, which send tool definitions out, stay off until the person
turns them on. It extends H5 to H11 (`collectors/scan_harness.py`, `docs/design/HARNESS.md`), which already judge skills,
plugins and hooks, to the servers of `collectors/scan_mcp.py`.

## Data

- `mcp_supply.judge(command, args)`: rule ids only, called in `scan_mcp.classify` beside the redaction. A declaration row
  gains `risky: [rule]`; the registry row (`registry/mcp-servers.json`) carries it. The command line, an argument, an
  environment value and a header value are never stored.
  - `unpinned-runner`: `npx`, `bunx`, `uvx`, `pipx run`, `pnpm dlx`, `yarn dlx`, `npm exec`, `bun x` without an exact
    version (`pkg@1.2.3`, `pkg==1.2.3`), or with `@latest` or a range.
  - `fetch-execute`: `curl`/`wget` as the program, a download piped to a shell or interpreter, a command substitution of a
    download, an interpreter given a URL.
  - `outside-install-places`: an absolute path in a temporary or download folder or under no installer's root, or a
    relative path. A bare name is looked up on the PATH and passes.
- `store/state/mcp-pins.json` (`mcp-pins/1`): `servers["<agent>:<name>"] = {pinned_at, checked_at, accepted_at?, tools:
  {tool: "sha256:..."}, pending: {tool: {change, old, new, seen_at}}}`. The digest is SHA-256 of the canonical JSON of
  name, description, input schema (and output schema when present), so a changed parameter description counts. Only digests
  are stored.
- Setting `features.mcp_remote_scanners` (default off, gate `on`: the person decides, it sends content out), declared in
  the configuration door (`configuration_door.FEATURE_TEXT`). `mcp_supply.remote_scanners()` reports the state and the
  reason; this module sends nothing itself, and an unreadable setting is off.

## Flow

1. Tick: `scan-mcp --declarations-only` reads configs and judges commands. No server is started.
2. `machine.mcp.refresh` or `full scan-mcp` (explicit): after the Claude probe, `mcp_supply.probe_tools` lists the tools of
   each enabled stdio server (initialize, `tools/list`, then stop it; its own process group, 20 s, 300 s in all). The
   declared environment is not passed, because values are never read, so a server that needs its own credentials is named
   in `degraded` and not pinned. A server judged `fetch-execute` is not started. Remote servers are not listed.
3. `observe`: the first sight pins; a later probe compares with the ACCEPTED pins and replaces `pending`, so a change put
   back clears. `accept` makes pending the pins (one tool, one server, or all).
4. `tools/build_findings.py`: `mcp.risky_command` (subject `mcp:<agent>/<server>/<rule>`, `fetch-execute` critical, the
   others warning) and `mcp.tool_changed` (subject `mcp:tool:<agent>/<server>/<tool>`, warning; added, removed or
   changed), both in the Security group. They name the server, the rule or the tool, never a command line or a definition.

## Wireframe (MCP page, above the table of servers)

```
MCP
Agent servers, connections and reachability.
Supply chain
  Commands are judged from the declarations ... No command line, argument or key is kept.
[Tools pinned 24 | Risky commands 2 | Changed tools 1 | Remote scanners Off]
[Risky commands: server | agent | rule | why]
[Changed tools: [Accept all]  server (agent) | tool | what changed | seen | [Accept]]
[Third-party remote scanners: Off: nothing is sent ... (the setting named, no command)]
[Not measured: source . reason]
```

## Contract

- MCP: `observatory_mcp_supply` (read: `risky`, `changes`, `pins`, `pinned_servers`, `scanners`, `rules`, `degraded`) and
  `observatory_mcp_accept(server?, tool?)` (write, idempotent: `{accepted, servers, degraded}`; invalid names are a typed
  `invalid-argument`). The page button is the key server's `mcp-accept` action, the same function
  (`mcp_supply.accept_pending`); parity entry `mcp-accept` is `covered`.
- Buttons are built disabled, enabled by the page script only when the control server serves the page; opened as a file
  they say why. No shell command on a row.

## Failure behaviour

An unreadable pin file is read as empty (everything pins afresh, nothing is accepted by it). A server that cannot be asked
is a `degraded` row `mcp-tools:<agent>:<name>`, its old pins kept. No scan yet is `degraded` and no verdict. An unreadable
scanner setting is off and says so. One server's failure never stops the refresh.

## Not done

No vendor client for a remote scanner: the switch and its reason exist, and nothing consumes it yet. Remote (HTTP) servers
are not listed or pinned (they need headers or OAuth, which are never read). Plugin-shipped servers have no command line
here, so they are not judged.

## Tests

`tests/test_mcp_supply.py` (rules with fixtures for each, exact and unpinned versions, the digest, first sight, changed,
added, removed, put back, accept one and all, only digests stored, a probe that cannot ask, `fetch-execute` not started,
the tick starting nothing, the switch, the page in both languages, the key server action, the MCP tools, no planted secret in
any file) and `tests/test_mcp_supply_board.py` (the findings through the real builder).

## No command line in the not-scanned state (OBS-579)

The MCP page was the last page with command text in a row (CP-01): its not-scanned state printed three
`project-observatory full ...` copy buttons with the workspace path, and the probe line a fourth. Both now name the
agent's tools instead: "An agent points it at your home and switches it on with `observatory_configure`
(`sources.mcp_config_root`, `integrations.mcp`), then scans with `machine.mcp.refresh`", and the probe line "an agent
asks with `machine.mcp.refresh`". No button: the key server has no scan action, and the configuration door's page
action is limited to the Disk page's keys; `parity.json` already holds `scan-mcp` as covered by
`machine.mcp.refresh`. Tests: `observatory/engine/tests/test_dashboard_render.py` (the MCP page's `#out` carries no
`project-observatory full`, in the not-scanned state and with servers not probed).
