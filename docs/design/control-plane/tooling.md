# Installed tools page (M3, OBS-552, CP-05)

## What

A dashboard page, `tools.html` ("Installed tools"), in the System group after Machine. It answers: which coding agents,
skills, plugins and hooks are installed here, and which are out of date. The Machine page links to it; Home gets a card
(agents found, plugins behind). The page only reads: it has no button, so it adds nothing to the parity list (CP-18).
An agent reads the same inventory with `observatory_machine(section="skills")`, which already existed.

## Data

Nothing new is collected. `observatory/engine/tooling_view.py` builds one answer from what the harness scan already
writes to `store/raw/harness.json` (`collectors/scan_harness.py`):

| Part | Read from | HARNESS.md |
|---|---|---|
| coding agents and versions | `coding_agents` (catalogue id, version when a release's) | H16 |
| skills (name, scope, version, agent), plugins (scope, project, version, enabled, latest, behind) | `machine_view.skills_section()` over `inventory`, so the page and the MCP tool agree on what was read | H17 |
| source tier of a skill | the security scan's `skills` (`source`, `name`, `tier`); a project's own skill is tier 1 (the scan reads it so); a Codex skill has no recorded tier and shows "not recorded" | H6 |
| hooks (event, matcher, source, program, command digest) | `hooks` (Claude Code's settings and enabled plugins only) | H11 |
| findings beside items | the open findings of the build (`PAYLOAD["findings"]["items"]`, type `harness.*`), so an acknowledged one stays silent here too | H4 to H11, H17 |

Placing a finding: by its `subject` (`harness:plugin-behind:<agent>:<plugin>:<scope>:<project[.key]>:...`,
`harness:skill:<skill>:<rule>:<digest>` with `codex/` and `<project>/` prefixes, `harness:changed:<plain|plugin:KEY>/<folder>`,
`harness:hook:<source>:<event>:<rule>:<sha>`); `harness.listing_overflow`, `mcp_deferral_off` and
`project_listing_overflow` belong to Claude Code, `instruction_long` to the agent whose file it names. A finding that
names no item still on the page (a plaintext credential in a tool's config, a scan gap, a skill since removed) is listed
under "Findings about the setup as a whole", never dropped.

`build_dashboard.py` puts the answer in `PAYLOAD["tooling"]`; `shell.slice_for` sets it to null on every page's data,
since the page is rendered on the server and no script reads it (a machine holds a thousand skills).

## Wireframe

```
Installed tools
Which coding agents, skills, plugins and hooks are installed here, and which are out of date.
Harness scanned 2026-11-15 10:00:00 UTC
[Coding agents 3] [Plugins 4, 2 behind] [Skills 120] [Hooks 2] [Findings 9]
+ Not fully read --------------------------------------------+
| codex:config.toml: the config could not be read            |
+------------------------------------------------------------+
+ Findings about the setup as a whole ----------------------+
| (warning) opencode.json keeps 2 credentials in plaintext  ->|
+------------------------------------------------------------+
+ Claude Code ------------------------------------------------+
| version 2.1.0                                              |
| (warning) 3 skill descriptions do not fit the listing    -> |
| PLUGINS                                                    |
| Plugin | Installed for | Version | Newest | State | Findings|
| tool@x | user          | 1.0.0   | 1.1.0  | on, behind | (info) plugin ... -> |
| SKILLS                                                     |
| > User skills 12      (opens by itself if one has a finding)|
| > Project skills 30                                        |
| > Plugin skills 78                                         |
| HOOKS                                                      |
| Event | For tools | From | Program | Findings              |
+------------------------------------------------------------+
+ Codex CLI ... (Hooks are read for Claude Code only.)       |
+ Aider: version 0.80.0; its skills, plugins and hooks are not read yet |
Agents read the same inventory with the MCP tool observatory_machine, section skills.
```

## Failure behaviour

- No scan, an unreadable scan, a stale one (over 24 hours), an inventory older than H17, a part of the inventory that
  could not be read, the coding-agent catalogue unreadable, findings not built: each is a line under "Not fully read",
  first on the page, with a plain reason. A collector's reason that ends in a command is cut before the command; the
  page shows a person no command (IS rules), and the MCP tool is named instead.
- With no agent read and nothing degraded: "No coding agent found here." An agent the scan found but whose records it
  does not read (any but Claude Code and Codex) says so instead of "none".
- A build error in the view: `PAYLOAD["tooling"]` carries it in `degraded`, and the page names it.

## Tests

`observatory/engine/tests/test_tools_page.py` (suite `tools_page`): agents and versions, order (agents with records
first), plugins behind and with findings first, skills per scope with tier, hooks, each finding beside its item, a
finding whose item is gone, degraded (inventory part, catalogue, findings not built), no scan and an unreadable scan
over a temporary workspace, the page in both languages, collapsed scopes with counts, no command and no em dash on the
page, the nav placement, the Machine page link, the built page and its data slice, the Home card.
`tests/test_i18n.py` reads the page's message ids (`tools_page_ids`).

## Open

- A Codex skill's source tier is not recorded by the scan (only its rule hits carry one); recording it in the
  inventory rows is a collector change for a later row.
- Hooks are read for Claude Code only; other agents' hooks wait on the scan.
