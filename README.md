# Project Observatory

**Your projects. Back in view.** A local dashboard for the projects your agents work on. See what changed, what needs attention and where known API keys left a copy. In English or Russian. Project Observatory is an open-source tool from [PassionCode.ai](https://passioncode.ai/) ([product page](https://passioncode.ai/observatory/)), beside [Fabric Switchboard](https://passioncode.ai/switchboard/). It is Fabric-compatible — its server speaks `fabric-service/0.1` and its MCP `fabric-interop/0.1` — and works without Fabric.

![The Project Observatory overview page: findings from critical to info, project and activity counters, and a card per section, in the PassionCode dark theme](site/assets/dashboard-overview-en.png)

<sub>The real dashboard, rendered by `tools/demo_estate.py` over a fictional company's projects — no workspace, registry or key was read. Also in [Russian](docs/images/dashboard-overview-ru.png); the [projects page](docs/images/dashboard-projects-en.png).</sub>

The distribution is the complete engine: project and repository inventory, findings, history, metrics, a local dashboard, MCP, credential tools and optional provider integrations. Every user supplies their own project paths, accounts and keys. Private operational data and Git history are excluded from the source distribution. Releases and their notes are on the [releases page](https://github.com/passioncode-ai/project-observatory-dashboard/releases); what changed in each is in the [changelog](CHANGELOG.md).

## Quick start for a new teammate

The complete engine supports macOS and Linux, Python 3.11+ and SQLite 3.37+ with loadable-extension support. The `python3` that ships with macOS is 3.9, and pip run from it fails with a misleading `ResolutionImpossible` rather than naming the interpreter, so choose an extension-enabled interpreter such as Homebrew Python explicitly; the [onboarding guide](docs/ONBOARDING.md#sqlite-runtime-prerequisite) checks this. Every machine runs a tagged release, the same wheel byte for byte ([staying in step](docs/ONBOARDING.md#staying-in-step)).

### Install

The latest release's wheel, checked against its `SHA256SUMS`, into a virtual environment of its own:

```sh
V=$(curl -s https://api.github.com/repos/passioncode-ai/project-observatory-dashboard/releases/latest \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["tag_name"][1:])')
mkdir -p ~/observatory-release && cd ~/observatory-release
base=https://github.com/passioncode-ai/project-observatory-dashboard/releases/download/v$V
curl -sSLO "$base/project_observatory-$V-py3-none-any.whl" && curl -sSLO "$base/SHA256SUMS"
shasum -a 256 -c SHA256SUMS --ignore-missing                 # → OK
brew install python@3.14                                     # macOS; on Linux use your distribution's 3.11+
PYTHON="$(brew --prefix python@3.14)/bin/python3.14"         # on Linux, e.g. PYTHON=python3.12
"$PYTHON" -c 'import sqlite3; c = sqlite3.connect(":memory:"); c.enable_load_extension(True)'   # AttributeError: this build cannot load sqlite-vec
"$PYTHON" -m venv ~/.local/share/project-observatory-venv
export PATH="$HOME/.local/share/project-observatory-venv/bin:$PATH"
python -m pip install --no-deps "project_observatory-$V-py3-none-any.whl"       # the engine, and the lock it carries
python -m pip install -c "$(project-observatory full-path)/requirements-full.lock" \
    "project_observatory-$V-py3-none-any.whl[full]"                             # its [full] extra at the tested versions
```

The wheel carries `requirements-full.lock`, the dependency set this release was tested with; the second
`pip` line installs the `full` extra against it. Without `-c`, pip resolves the newest releases of the
transitive dependencies instead, which this release was not tested with.

Later releases arrive with `project-observatory full update --apply`, verified and reversible.

### Configure

```sh
export OBSERVATORY_HOME="$HOME/.local/share/project-observatory-full"   # your private workspace
project-observatory full init
project-observatory full configure sources projects "$HOME/projects"   # a directory you own
project-observatory full local
project-observatory full doctor
```

The first run needs no key; the workspace is wherever `OBSERVATORY_HOME` points. Integrations are switched on one by one (`full configure integrations NAME true` — `github`, `cloudflare`, `openrouter`, …), and a credential they need goes into your own vault as a named slot, the value on stdin only ([enter credentials locally](docs/ONBOARDING.md#enter-credentials-locally)). A teammate taking the organisation's settings imports its profile instead (org-index `ONBOARDING.md` §5). An agent can guide the setup: give it the [onboarding prompt](docs/AGENT-ONBOARDING.md), or run `project-observatory full onboard`.

### MCP

The engine's MCP server speaks stdio. Register it for your user with the same `OBSERVATORY_HOME`, then make one call:

```sh
claude mcp add observatory --scope user -e OBSERVATORY_HOME="$OBSERVATORY_HOME" -- \
  "$(python -c 'import sys; print(sys.executable)')" "$(project-observatory full-path)/mcp/server.py"
claude mcp list | grep observatory     # observatory: … - ✔ Connected
claude -p "Call observatory_status once and reply OK with the number of projects and its degraded list" \
  --allowedTools mcp__observatory__observatory_status
# → OK 1 degraded=[]   (one project under the configured source; an empty degraded list is full coverage)
```

Restart open Claude Code sessions; they read their MCP servers at start. Every read answer carries a `degraded` list naming what could not be read or was ignored — an argument the tool does not take, a `since` that is no ISO-8601 date, a cursor that names no position; a write and a job handle follow their published schemas, which have none. A malformed argument or an unknown tool answers `isError` with `{"error": "invalid-input"}` or `{"error": "unknown-tool"}` and a `detail` naming the field and the rule it broke, never the value sent. Any other refusal (an unknown project, a refused owner) is a typed answer with `error`, not `isError`, and quotes the input only when it is a well-formed identifier that does not look like a credential; otherwise it gives the length. To let `machine.mcp.inventory` list the MCP servers your agents declare (Claude Code, Cursor, OpenCode, Codex, Gemini CLI, Kiro — names and transports only, never a URL, header or key), point the scan at your home and take one:

```sh
project-observatory full configure sources mcp_config_root "$HOME"
project-observatory full configure integrations mcp true
project-observatory full scan-mcp
```

Other agents take the same command line; [connect an agent](docs/ONBOARDING.md#connect-an-agent) has the details.

### Develop

Run from source and run the gate — the commands CI runs are listed in order in [AGENTS.md](AGENTS.md#build-and-test):

```sh
git clone https://github.com/passioncode-ai/project-observatory-dashboard.git
cd project-observatory-dashboard
"$PYTHON" -m venv .venv && . .venv/bin/activate        # $PYTHON chosen as under Install
python -m pip install -c requirements-full.lock '.[full]'
python -m unittest discover -s tests -v
project-observatory full check
python tools/check_public_release.py --history
```

## Use it

### Open the dashboard

```sh
observatory                              # the short name; with no arguments it opens the dashboard
project-observatory full open            # builds the pages if needed, opens them as local files
project-observatory full open --serve    # serves them read-only on 127.0.0.1:47311 (GET only; changes nothing)
project-observatory full open --stop     # stops that server; it runs detached, so closing the terminal does not
```

The Keys and ENV pages' buttons (mint, cap, revoke, mark a leak, reveal one inventoried value) act only when the page is served by the token-guarded credential server, `python "$(project-observatory full-path)/tools/keyserver.py"` (127.0.0.1:7717 by default, `--port N`). Opened any other way, those pages hand over the command instead. Putting or rotating a vault value is never done from a page: it goes through `tools/vault.py` on stdin ([enter credentials locally](docs/ONBOARDING.md#enter-credentials-locally)).

### Choose the dashboard's language

The dashboard speaks English by default and Russian by choice. Set the language for your workspace, then rebuild the pages:

```sh
project-observatory full configure interface locale ru   # or: en
project-observatory full open --rebuild
```

Each reader can also switch with **EN / RU** in the navigation rail; that choice stays in the browser and works for pages opened as local files. Interface strings and finding titles are translated; a finding's details and suggested action stay in English. To add or change a string, see [Contributing](CONTRIBUTING.md#interface-strings).

`--no-browser` prints the address instead of opening it; `--rebuild` rebuilds the pages first; `--port` picks another loopback port. The pages live in `$OBSERVATORY_HOME/docs/dashboard/`, and `project-observatory full local` refreshes what they show. The server binds `127.0.0.1` only.

### Connect Claude Code (plugin updates automatically)

```sh
project-observatory full agent install   # marketplace + plugin, auto-update ON, hooks pointed at this workspace
project-observatory full agent status    # installed vs shipped version, auto-update, hook environment
project-observatory full agent uninstall # removes the plugin and only the settings install added
```

`install` adds the `passioncode-ai/project-observatory-dashboard` marketplace to Claude Code, installs `observatory-log@observatory-log`, turns plugin auto-update on and sets `OBSERVATORY_ROOT`, `OBSERVATORY_HOME` and `OBSERVATORY_PYTHON` in Claude Code's user settings so the hooks find your engine, workspace and interpreter. It backs up `~/.claude/settings.json` once and keeps every other setting. Pass `--no-auto-update` to keep updates manual (`claude plugin update observatory-log@observatory-log`). An earlier directory-sourced install is replaced by the GitHub one. When another channel already installs the plugin under its own id (the PassionCode launcher's `observatory-log@passioncode`), `install` writes only the hook environment and never adds a second copy, and `status` names that channel. `install` reports the plugin installed only once Claude Code's own `installed_plugins.json` lists it. Without the helper: `/plugin marketplace add passioncode-ai/project-observatory-dashboard`, then `/plugin install observatory-log@observatory-log`; that route leaves the three hook variables unset, and `full agent install` without `claude` on PATH prints their values to add under `env` in your Claude Code settings. Restart Claude Code sessions after any change; plugins load at session start.

## What the complete engine does

| Question | Capability | Implementation |
|---|---|---|
| What projects and repositories exist? | Filesystem discovery, explicit ownership rules, repository and provider inventory | [collectors](observatory/engine/collectors), [configuration](observatory/engine/configuration.py) |
| What changed? | Git events, project timelines, append-only reasoning and review proposals | [store](observatory/engine/store), [survey](observatory/engine/survey.py) |
| What needs attention? | Findings with evidence, acknowledgements and coverage/degradation signals | [finding rules](observatory/engine/tools/build_findings.py) |
| Is a project growing or costing more? | Versioned metric plugins for source/dependency size, branches, releases, hosting and traffic | [plugin contract](observatory/engine/plugins/README.md) |
| Where are credentials used or copied? | Named slots, environment metadata, known-value scans, rotation and movement records | [vault](observatory/engine/tools/vault.py), [scanner](observatory/engine/tools/scan_leaks.py) |
| Can another agent inspect the same facts? | MCP tools and resources, input/output schemas, proposal authority checks | [MCP server](observatory/engine/mcp/server.py), [wire contract](observatory/engine/fabric/FABRIC-CONFORMANCE.md) |
| Can it observe continuously? | Explicitly enabled workspace-specific scheduling and optional model interpretation | [scheduler](observatory/engine/tools/install_launchd.py), [agent](observatory/engine/agent/observe.py) |
| Can an agent's workflow continue in another session, account or model? | A checkpoint after every step, one executor at a time by lease token, and an immutable handoff pack the engine assembles — latest checkpoint, constraints first, a fresh git read, related records — so the session that leaves need not answer | [agent memory design](docs/design/AGENT-MEMORY.md), [workflow store](observatory/engine/store/workflow.py) |
| Can other agents call it through Fabric? | Every Fabric capability is an MCP tool of its own name with its published schemas (`fabric-interop/0.1`): W3C trace context in `_meta.traceparent`, a job handle for long work (`machine.mcp.refresh`, `fabric.job.get`, `fabric.job.cancel`), and `machine.mcp.inventory` for every MCP server your agents declare | [capability tools](observatory/engine/mcp/capability_tools.py), [design](docs/design/FABRIC-INTEROP.md) |
| Can a local host watch it? | The always-on server speaks `fabric-service/0.1`: one copy per workspace, a well-known identity and health document, a token-guarded events feed, a descriptor written by its installer | [server](observatory/engine/tools/serverd.py), [design](docs/design/FABRIC-SERVICE.md) |

Optional integrations include GitHub, Bitbucket, Cloudflare, Heroku, Google analytics/search, domain observations, agent sessions and a local knowledge base. Connecting one does not connect all of them. Model calls, remote environment reads, notifications and remediation are opt-in. See [onboarding](docs/ONBOARDING.md) for settings and credential entry.

The local dashboard is private. The public `site/` is a separate static artifact and cannot read your workspace: it serves the [field notes](https://observatory.sshlg.me/field-notes/) and redirects its former home page to the [product page](https://passioncode.ai/observatory/). Deploying the website must never upload generated dashboard pages, registries, keys or transcripts.

## Code is shared. State is yours.

| Installed code | Private workspace | External sources |
|---|---|---|
| Engine, schemas, generic defaults, tests and companion skills | Configuration, registries, SQLite history, keys, journals and generated pages | Only paths and accounts explicitly configured by the user |

Values enter credential tools locally through stdin. Agents work with names, and the command runner filters exact known values from captured output. This is accidental-output protection, not a sandbox against a hostile child process. The authenticated local credential UI can reveal a selected value on request; that is an explicit operation, not part of ordinary reports.

Known-value scanning cannot find unknown or transformed values. A copied value in an agent transcript or local memory store does not prove a vendor breach. [Security boundaries](SECURITY.md) describe what is and is not protected.

## Updates preserve supported contracts

An installed release is updated with `project-observatory full update --apply`: the wheel is verified against GitHub's digest and `SHA256SUMS`, the workspace is upgraded, and a failure rolls back ([staying in step](docs/ONBOARDING.md#staying-in-step)). A source checkout is updated with Git and reinstalled (`python -m pip install -U -c requirements-full.lock '.[full]'`), followed by `project-observatory full upgrade`. Both install the dependencies from the release's `requirements-full.lock`, which the wheel carries. `full profile export` / `import` carries the functional configuration to a [second machine](docs/ONBOARDING.md#second-machine). The Claude Code plugin updates itself when auto-update is on; see `full agent status`.

Application versions, workspace/config formats, database migrations, plugin API and tool schemas have separate compatibility rules. Newer unsupported state is refused. Updates preserve optional settings, back up SQLite including committed WAL data, and support restore into a separate home.

```sh
project-observatory full upgrade
# Stop all writers before either command below.
project-observatory full workspace-backup --writers-stopped
project-observatory full upgrade --apply --writers-stopped
```

Read [compatibility and recovery](docs/COMPATIBILITY.md) before upgrading. The original `full backup` retains its database-only meaning. `workspace-backup` covers the managed workspace; externally referenced stores require separate backups. With a passphrase set (`full backup-passphrase set`), every backup is encrypted into one backups root — on macOS `~/Documents/Project Observatory/Backups` by default (when `~/Documents` exists; otherwise `<workspace>/backups`), so iCloud can carry it off the machine; see [encrypted backups](docs/ONBOARDING.md#encrypted-backups-off-this-disk).

**Existing 0.1 commands remain available.** `project-observatory init`, `scan`, `serve`, `secret`, `leaks` and the other original commands keep their previous namespace and state format. They are documented in the [0.1 compatibility guide](docs/PORTABLE-0.1.md). The full engine has a separate default home; it never silently reinterprets the portable workspace, and the 0.1 commands refuse a complete-engine workspace (exit 2) rather than write into it — give them their own folder with `--home`. [CLI contract](observatory/engine/docs/CLI-COMPATIBILITY.md).

## Verify and contribute

The gate is under [Develop](#develop). Checks use synthetic projects and credentials. Real provider acceptance, external host admission and real credential rotation are separate checks and are reported as untested by the offline suite. The [source inventory](observatory/engine/SOURCE-INVENTORY.json) records the reviewed export; it is not a guarantee that a pattern scanner can recognize every private fact.

[Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Migration map](docs/MIGRATION.md) · [Release handoff](docs/HANDOFF.md)

### Repository name and existing installations

The repository moved to the PassionCode.ai organization as `passioncode-ai/project-observatory-dashboard` in 0.4.0 (it was `ssheleg/project-observatory-dashboard`, and before that `ssheleg/project-observatory-open-source`).
The Python package and command remain `project-observatory`; no workspace migration
or key rotation is required just for the repository rename. Existing clones can
update their remote with `git remote set-url origin https://github.com/passioncode-ai/project-observatory-dashboard.git`,
and `project-observatory full agent install` moves the plugin marketplace to the new address.
Published v0.2.0 Fabric schema identifiers retain their original URLs and content
hashes. Do not rewrite them in an existing installation. GitHub redirects the old
repository path; verify pinned URL resolution before removing any compatibility URL.

## License

Open source under the [GNU AGPL-3.0](LICENSE). A [commercial license](COMMERCIAL-LICENSE.md) is
available for use that does not meet the AGPL's terms — contact@passioncode.ai.
Versions up to and including v0.9.1 were released under PolyForm Noncommercial or Internal Use (v0.8.2–v0.9.1) and the MIT License (v0.8.1 and earlier); those releases keep their licence.
Contributions are accepted under the [CLA](CLA.md).

Part of [PassionCode.ai](https://passioncode.ai/) — the design system is [PassionCode 1.1.0](https://passioncode.ai/design-system/), dark theme. Observatory is also the observation component of the [ssheleg harness](https://skills.sshlg.me/harness/): skills guide the work; Observatory records and checks the state around it.

## Mac app

0.12.0 ships a native macOS app (macOS 14+) that opens on this dashboard: live when the workspace's own server answers, otherwise the saved pages with a **Start server** button, and **Build the dashboard** when there are none yet. An advisory assistant is one window away (⇧⌘A). Each release from 0.13.0 attaches the app signed with a Developer ID and notarized by Apple, `ProjectObservatory-<version>-macos.zip`, listed in `SHA256SUMS`: check it, unzip it and move it to `/Applications`. Or build and install it from a checkout:

```sh
macos/scripts/build-app.sh                 # dist/macos/Project Observatory.app, signed ad hoc for local use
macos/scripts/install-app.sh --open        # into /Applications (or ~/Applications), then opens it
```

On first launch it looks for the engine at `~/.local/bin/project-observatory`, then in the virtual environment from [Install](#install) (`~/.local/share/project-observatory-venv/bin/project-observatory`), then in `/opt/homebrew/bin` and `/usr/local/bin`, and uses the default workspace; anything else is chosen in **Settings** (the engine's absolute path and an initialized workspace). Details, the assistant's setup and its limits: [docs/macos/README.md](docs/macos/README.md).
