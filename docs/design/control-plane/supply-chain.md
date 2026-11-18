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
- The setting `features.mcp_remote_scanners` was removed by OBS-606 (see "Remote scanners: removed" below).
  `mcp_supply.remote_scanners()` answers `{enabled: false, wired: false, reason}`; a setting left on from before is a
  `degraded` entry with code `not-wired`.
- OBS-606: every declaration row (and registry row) carries `supply` (see "Remote and plugin servers" below), and a
  plugin's servers carry `plugin` (`name@marketplace`) and `plugin_version`.

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
[Tools pinned 24 | Risky commands 2 | Changed tools 1 | Unpinned servers 3]
[Risky commands: server | agent | rule | why]
[Changed tools: [Accept all]  server (agent) | tool | what changed | seen | [Accept]]
[Remote and plugin servers: server | agent | kind | where | transport | key | pinned]   (OBS-606)
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

Remote (HTTP) servers' tools are not listed or pinned (they need headers or OAuth, which are never read); their endpoint is
(OBS-606). A plugin's stdio servers are judged and pinned to the plugin's version, but not started for a tool listing
(their command names the plugin's folder through `${CLAUDE_PLUGIN_ROOT}`).

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

## Remote scanners: removed (OBS-606, CP-09)

Decision: **remove the switch; no client is planned.** The refactor audit found `features.mcp_remote_scanners`
(`mcp_supply.py:408` before this change) with no client: a person could turn it on and nothing happened, while the page
said scanners "may be sent tool definitions". The two candidates in [research.md](research.md), "MCP servers, skills and
plugins", decide it: Snyk Agent Scan sends tool definitions and skill text to its vendor and needs that vendor's token,
which this estate does not hold and would be a new party receiving content; Cisco mcp-scanner's offline `static` mode
runs locally and so needs no opt-in at all, and its vendor and model analyzers need keys again. Neither is planned, and
the built-in rules (`judge`, the tool pins, and the remote and plugin judgements below) run offline. Decision D4 of
CONTROL-PLANE.md holds trivially: nothing is sent, because no sender exists. A future client comes with its own switch
in the configuration door (gate `on`), the spend gate of the meter (`meter.py`, `observatory.grant_check`) when it
charges, and its own design note.

What changed: `mcp_remote_scanners` left `configuration.KNOWN_FEATURES` and the door's `FEATURE_TEXT`; the MCP page's
"Remote scanners" tile and section are gone (the tile is now "Unpinned servers"); `observatory_mcp_supply` keeps its
`scanners` key (no field is taken from an agent's answer) as `{enabled: false, wired: false, reason}`. A settings file
that still holds the key loads as before (unknown features are not refused) and the answer names it (`not-wired`).

## Remote and plugin servers (OBS-606, CP-09)

**Remote servers** (`mcp_supply.judge_remote(url, cfg, transport)`, called in `scan_mcp.classify` beside the
redaction): `{kind: remote, host, transport, auth, credential_in_url, pinned: false, pin: {endpoint}, codes}`.
`auth` is `url` (a query or a user part in the URL), `header` (a key-named header, or Codex's `bearer_token_env_var`),
`oauth` (declared) or `none-declared` (the client may still sign in when the server asks). `credential_in_url` is a
boolean: the URL's query and user part never leave `classify`. The endpoint pin is SHA-256 of scheme, host, port, path
and transport, so rotating a key in the query does not move it. A remote server's code is its provider's, so it is never
version-pinned: code `remote-endpoint` (info); plain `http` to a host other than this machine adds `plain-http`
(warning).

**Plugin-shipped servers** (`scan_mcp.plugin_declarations(home)`): for each plugin enabled in `~/.claude/settings.json`
and installed (`~/.claude/plugins/installed_plugins.json`), the servers of its `.mcp.json` and of its
`.claude-plugin/plugin.json` `mcpServers` (an object, or a file inside the plugin named by a string), classified like a
declaration with `scope: plugin:<name@marketplace>`, `plugin` and `plugin_version` (release-shaped, else absent). Read
on every scan, the tick included: only files are read, nothing is started. A server the Claude probe hears under the
same name is the same row (its liveness attaches), not a second one. A plugin file that cannot be read is
`degraded` `claude:plugins:<plugin>`, with the file's name and the error's kind.

**Commands** (`mcp_supply.judge_command(command, args, plugin_version)`): pinned to an exact runner version
(`{version}`), an image digest (`{digest}`, `docker`/`podman`/`nerdctl run image@sha256:...`), the plugin's version
when the command runs from `${CLAUDE_PLUGIN_ROOT}` (`{plugin_version}`), or its installation (`{by: install}`). Codes:
`image-tag` (warning: a tag can be moved), `plugin-unversioned` (warning). An unpinned package runner stays the
`unpinned-runner` rule (said once, as `mcp.risky_command`), and a plugin's runner is judged by it too.

**Findings**: `mcp.unpinned_server`, subject `mcp:<agent>/<server>/<code>`, severity from `mcp_supply.UNPINNED`, in
the Security group and the Assistant's supply-chain area. **Answer**: `observatory_mcp_supply` gains `judged` and
`unpinned`; the registry row (`estate_surfaces.mcp_rows`) carries `supply`, `plugin`, `plugin_version`.

Tests: `tests/test_mcp_supply.py` (`NoRemoteScanner`, `RemoteServers`, `CommandPins`, `PluginServers`: the judgements,
auth kinds, loopback, versions and digests, a plugin's four servers through both files, a disabled plugin ignored, an
unreadable plugin file named, one row per probed plugin server, the registry row and the summary, and no planted
secret in any answer) and `tests/test_mcp_supply_board.py` (the finding per code and severity through the real builder,
no digest printed).
