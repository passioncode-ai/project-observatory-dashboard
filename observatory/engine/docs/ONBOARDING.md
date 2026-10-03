# Install the complete Project Observatory

The public package contains the original engine with per-user paths and generic
configuration. Its database, registries, keys and generated pages live in a
private workspace outside the installed source. macOS and Linux are supported;
Python 3.11+, SQLite 3.37+, Git and Node.js are required for the complete local
checks. The Python package's `full` extra installs the MCP, schema, vector-store
and Google authentication libraries at the versions tested by this release.

## SQLite runtime prerequisite

The full engine requires Python 3.11+ with SQLite 3.37+ **and loadable SQLite
extensions**, plus the locked sqlite-vec dependency. Some macOS Python builds
omit `enable_load_extension`; installing sqlite-vec alone cannot add it.
Initialization, doctor, migration and upgrade check this before workspace writes.
The isolated regression is
`tests/test_workspace_upgrade.py::WorkspaceUpgrade::test_missing_sqlite_extension_support_refuses_before_writes`.

**Choose the interpreter explicitly.** The package declares
`requires-python = ">=3.11"`. The `python3` that ships with macOS is 3.9, and
pip run from it does not say so: it fails with a `ResolutionImpossible` about
the locked dependencies. On macOS,
[Homebrew Python](https://formulae.brew.sh/formula/python@3.14) provides a
supported installation path; on Linux, name any Python 3.11+ your distribution
provides. Both check lines fail with a message rather than later, inside pip:

```sh
brew install python@3.14                                 # macOS
PYTHON="$(brew --prefix python@3.14)/bin/python3.14"     # on Linux, e.g. PYTHON=python3.12
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Python 3.11 or newer is required; this is " + sys.version.split()[0])'
"$PYTHON" -c "import sqlite3; c=sqlite3.connect(':memory:'); c.enable_load_extension(True); c.enable_load_extension(False)"
```

The second line raising `AttributeError` means this build cannot load
sqlite-vec; choose another interpreter.

## Start without credentials

From a checkout of the public release, with `PYTHON` chosen and checked as
above:

```sh
"$PYTHON" -m venv .venv
. .venv/bin/activate
python -m pip install -c requirements-full.lock '.[full]'
export OBSERVATORY_HOME="$HOME/.local/share/project-observatory-full"
project-observatory full version
project-observatory full init
project-observatory full doctor
project-observatory full configure sources projects "$HOME/projects"
project-observatory full local
```

Choose an existing project directory you own instead of the example
`$HOME/projects`. The directory is an explicit observation boundary; the scanner
reads its project metadata and Git history. Do not point it at a directory that
you are not authorized to inspect. `local` invokes no external provider or
arbitrary plugin. Inspect the generated `docs/dashboard/index.html` beneath
`OBSERVATORY_HOME`. These pages contain private project information. They do not
belong on the public marketing website.

A workspace path must not pass through a symbolic link (on macOS `/tmp` is one):
`init` refuses such a path and prints the resolved `OBSERVATORY_HOME` to use instead.

`init` is idempotent. It seeds missing initial state only for an empty new home;
it does not replace an existing installation. Configuration belongs in
`config/settings.json`; curated overrides and policy files are adjacent.
`project-observatory full-path` prints the immutable engine directory, useful
for connecting scripts, hooks and MCP.

**Which environment an app serves** comes from the provider (a Heroku pipeline stage) or from you:
`config/environments.json` holds `{"deployments": {"heroku:<app>": "production"}}`. The names
`production`, `staging`, `review`, `development`, `test` and `local` are recognised (`prod` and
`stage` are read as their full names). An app with neither is listed as `unassigned` in
`registry/environments.json`. Its name is never read as a hint.

`full doctor` also reports whether the scheduled tick is alive (`tick.verdict`): `disabled` while
`features.scheduler` is off (the default), `never` before the first tick, `running` during one and
`ok` after a recent one. A tick that was killed mid-run, for example by a restart, is `interrupted`.
When no tick has finished for six hours the verdict is `stale`, and when one has held the lock for
longer than that it is `running-long`.
The dashboard server's `/health` and every MCP answer's `degraded` list say the same. Its findings
cannot: they are built by the tick itself, so a dead tick leaves the last report looking current.

The old 0.1 commands remain available without `full`. They use their previous
workspace format and previous default home. Do not combine the two formats.
`OBSERVATORY_FULL_HOME`, when set, selects the full launcher home ahead of
`OBSERVATORY_HOME`; an explicit global `--home` takes precedence over both.
Always pass the same home to the CLI, server and scheduler.

## Give this prompt to your agent

Copy [AGENT-ONBOARDING.md](AGENT-ONBOARDING.md), or run
`project-observatory full onboard`. The agent starts locally, explains available
integrations, and guides credential entry on your own machine. It does not need
the author's accounts or data.

## Choose integrations individually

List current settings with `doctor`: it names every integration and feature `configure`
accepts with `true` or `false`, and every source with whether it is configured. Enable a selected integration with
`project-observatory full configure integrations NAME true`. An unknown integration,
feature or source name is refused (exit 2) with the list of known names. The collector's
installed help and source describe its exact input format. Start with the
smallest account access that can read the requested resources; inventory access
and token provisioning are different permissions.

| Integration name | Purpose | User-owned setup |
|---|---|---|
| `github` | Repository inventory | Authenticate GitHub CLI locally; it enumerates resources visible to that account |
| `git_remotes` | Whether each checkout is current with its remote: one `git ls-remote` per checkout | None added: it uses each checkout's own remote and SSH keys (and your `url.<base>.insteadOf` rewrites), asks no credential helper, allows only the https, http, ssh, git and file transports, and reports a remote that wants a password, or names another transport, as unreachable with git's own reason |
| `bitbucket` | Repository inventory | A local Bitbucket credential in the configured secret store |
| `cloudflare` | Zones and DNS inventory | User-owned account credentials; token administration is separately scoped |
| `heroku` | Hosting inventory | Local authentication for the intended Heroku account |
| `google` | Analytics property and search inventory | The user's service account files and resource grants |
| `domains` | Domain observations | Explicit domain export and network access |
| `mcp` | The MCP servers the agent configs on this machine declare (Claude Code, Cursor, OpenCode, Codex, Gemini CLI, Kiro), served as `machine.mcp.inventory` | Explicit `sources.mcp_config_root` |
| `sessions` | Local agent activity | Explicit `sources.sessions` and `sources.companion_db` |
| `wiki` | Knowledge-base inventory | Explicit `sources.wiki` |
| `openrouter` | Key and usage inventory | A locally supplied provider credential |
| `remote_env` | Compare deployed environment metadata | Explicit opt-in to provider environment reads |
| `ga4`, `search_console`, `cloudflare_analytics` | The traffic metric plugins | The `google` service-account files, or Cloudflare credentials, in `secret_store` |

Metric plugins have their own manifest requirements and opt-ins. Run
`project-observatory full plugins-check` to validate them and read
[plugins/README.md](../plugins/README.md) before adding trusted executable plugins.
`project-observatory full plugins --only ID --force` runs one plugin now, past its age
gate, and `project-observatory full google --force` refreshes the analytics inside their
twelve-hour cache. Step options are declared per step; every step and command explains
itself with `--help` and runs nothing when asked
([CLI compatibility](../docs/CLI-COMPATIBILITY.md#step-arguments-and-help)).
An unavailable input must remain visible as unavailable; it does not prove that
there are no findings.

## Sources

Nothing outside the workspace is read until you name it. Each source is a path set with
`project-observatory full configure sources NAME PATH`; `full doctor` lists every enabled
integration or feature whose source is unset, missing (a path deleted after it was configured)
or of the wrong kind under `coverage_warnings`, because a collector without its source reports
nothing rather than failing. Each row carries two exact commands: `fix`
(`project-observatory full configure sources NAME PATH`) and `disable`
(`project-observatory full configure integrations NAME false`, or `features` for a feature).
The `projects` source is the exception: the filesystem scan always runs, so a missing
`projects` source is a row with `fix` and no `disable`.

| Source | What it points at | Read by |
|---|---|---|
| `projects` | the directory that holds your project checkouts | filesystem scan, events, leak scan |
| `sessions` | agent session transcripts, e.g. `~/.claude/projects` for Claude Code | `sessions` integration, leak scan |
| `mcp_config_root` | the home directory whose agent configs declare MCP servers | `mcp` integration |
| `wiki` | a Markdown knowledge base, e.g. an Obsidian vault | `wiki` integration, `wiki_projection` |
| `secret_store` | a private directory of provider credentials and project slots (`projects/`) | vault, OpenRouter, Google, Cloudflare analytics |
| `companion_home`, `companion_db` | a memory companion's home and database file (claude-mem) | `sessions` integration (`companion_db`), `companion_remediation` |
| `gateway_root` | an optional directory whose `bin/` holds a credential backup script | `vault.py backup` |
| `domain_export` | a registrar's domain CSV export | `domains` integration |
| `cloudflare_snapshot` | a JSON snapshot of your Cloudflare zones, kept as evidence | `validate` (snapshot parity; reported as degraded when unset) |
| `secrets` | overrides where the workspace keeps its own credential files | provider tools |

The `mcp` integration reads the declarations from the agent configs under
`mcp_config_root` — `~/.claude.json` (user and project scopes), `~/.cursor/mcp.json`,
`~/.config/opencode/opencode.json`, `~/.codex/config.toml`, `~/.gemini/settings.json`
and `~/.kiro/settings/mcp.json` — for names, transports and whether a key is present,
never a value, and asks `claude mcp list` whether Claude reaches them. A config that is
absent or does not parse is recorded as such, and the `machine.mcp.inventory` MCP tool
names it in `degraded`. That command health-checks every
server before it prints, so it gets up to 180 seconds; on a machine with many
servers raise the limit with `OBSERVATORY_MCP_PROBE_TIMEOUT` (seconds, 1 to 3600),
set in the shell that runs `tools/install_launchd.py install` so the scheduled tick carries it.
A probe that still runs out of time is degraded, not failed: servers it reported
keep their verdict and the rest are `not-probed`.

## Enter credentials locally

The engine's tools below are files inside the installed engine, not CLI
subcommands. Run them with the environment's `python` and the same
`OBSERVATORY_HOME` as the CLI; `project-observatory full-path` prints the
engine directory:

```sh
python "$(project-observatory full-path)/tools/vault.py" put PROJECT ENV NAME   # the value on stdin
```

`ENV` is one of `local`, `stage` or `prod`, and `NAME` is UPPER_SNAKE_CASE.
The command accepts the value only on stdin. The same tool manages the slot afterwards —
`list`, `rotate` (the new value on stdin; the old one is archived at mode 600), `leak` and
`settle` (record an exposure and its settlement), `moved` / `movements` (a change made at
the provider), and `remove PROJECT ENV NAME` (the value, its metadata and its retired
archives; refused while a leak on the slot is open unless `--force`; `--retired` removes
only the archives). Every change is a line in the movements journal and none prints a value.
A command uses a slot by name, never by value:

```sh
python "$(project-observatory full-path)/tools/use_secret.py" names PROJECT
python "$(project-observatory full-path)/tools/use_secret.py" run --env local PROJECT NAME -- COMMAND …
```

Flags come before the project name (`run --env ENV PROJECT NAME -- …`); a flag after it is
refused with the right order, and a program that does not exist exits 127. The command's
output is filtered for the exact value. `project-observatory full local` refreshes the
inventory of `.env` names this reads (`env`, local, names and keyed fingerprints only), and
the Keys page shows a new slot after the next `full local`. Prefer a human-controlled hidden prompt or a
pipe from an already authenticated provider tool. Do not put a secret in a chat,
command argument, test fixture or tracked file. Read the installed
`handling-secrets` skill before an agent works with these commands.

Slots are private files under the workspace by default. A separate
`sources.secret_store` is optional and belongs to the user. File permissions
limit local access; this is not a claim that the default files are encrypted at
rest. Protect private backups just as carefully. A configured external store
is not included in workspace backup snapshots.

Known-value scanning finds occurrences of values already known locally. It
cannot prove the absence of unknown, encoded or previously deleted values.
Rotation changes the local slot; provider revocation is a separate action.

A file the scan could not open is listed as unread and raises a warning. It is never counted as
clean. When a sighting is known and accepted, for example a synthetic value a fixture prints on
purpose, record the decision in `config/leak_suppressions.json`:
`{"suppressions": [{"secret": "<project>/<NAME>", "where": "<complete sighting location>", "version_id": "<copy from the sighting>", "reason": "…", "expires_on": "YYYY-MM-DD"}]}`.
The `secret`, complete `where` (including the SQLite column when present), and `version_id` must match the sighting in `store/raw/leak-scan.json` exactly. The version is an opaque workspace-keyed HMAC, never the credential value. A replacement value or a different location needs a new decision. Legacy rules without a version are reported as invalid and do not suppress anything. Without the workspace fingerprint salt, sightings remain visible and cannot be suppressed. Changing or expiring a rule rechecks existing files and SQLite content on the next scan.
A suppressed sighting is still shown along with its reason. A rule without a reason or an expiry date
is not applied, and neither is an expired one; both are reported.

With `companion_remediation` enabled, each tick replaces known values in the memory companion's
stores with `[REDACTED:<name>]`, after taking a backup of each store it changes. The first pass reads
every row; later ticks read only rows added since, per table, and record where they stopped in
the workspace store's `scrub-watermark.json`. That file identifies the value set by an HMAC under the workspace's
salt, so it holds nothing a value could be recovered from. A complete pass runs again when the set of
known values changes, a week after the last complete pass, when a table was emptied or recreated, or
on `python "$(project-observatory full-path)/tools/scrub_companion.py" --full`. A row edited in place between complete passes is caught by the
weekly pass, not sooner.

The leak scan reads the companion's database the same way: transcripts from their last offset, the
database from its last rowid per table (in `store/raw/leak-scan-state.json`), with the same reasons
for a complete pass. So a sighting is reported once, by the tick that first reads it, and stays on
record in the leak register until it is settled. `python "$(project-observatory full-path)/tools/scan_leaks.py" --full` reads everything again.

**The agent's OpenRouter key** (`python "$(project-observatory full-path)/tools/install_key.py" --for observatory`,
stdin only) is kept at
`store/.openrouter-key`, or under `OBSERVATORY_STATE` when you redirect the state. After such a
redirect, a key left at the old `store/` location is still read, with a note saying where it
belongs, and is never moved for you. If a key exists in both places, reading refuses and names both
files, rather than guessing which one is current. Remove the one you no longer use.

## Connect an agent

The complete MCP server uses stdio. Configure a Python executable from the
installed virtual environment, the engine's `mcp/server.py` as its argument,
and `OBSERVATORY_HOME` for the selected private workspace. Detect MCP support
in the chosen agent rather than assuming it from the product name.

For Claude Code, with the environment active and `OBSERVATORY_HOME` exported,
this line declares it for your user (the first scan raises
`mcp.own_unregistered` until some agent does, and that finding's action prints
the same line with this workspace's values filled in):

```sh
claude mcp add observatory --scope user -e OBSERVATORY_HOME="$OBSERVATORY_HOME" -- \
  "$(python -c 'import sys; print(sys.executable)')" "$(project-observatory full-path)/mcp/server.py"
claude mcp list | grep observatory     # Connected
```

Restart open Claude Code sessions afterwards; they read their MCP servers at
start.

The optional `observatory-log` plugin adds Claude Code hooks; other hosts can
use the CLI and MCP without those hooks.

For Claude Code, run `project-observatory full agent install`. It adds the
GitHub marketplace `passioncode-ai/project-observatory-dashboard`, installs
`observatory-log@observatory-log`, turns plugin auto-update on (opt out with
`--no-auto-update`) and writes `OBSERVATORY_ROOT` and `OBSERVATORY_HOME` into
Claude Code's user settings for the hooks. `full agent status` shows the
installed and shipped versions and anything the hooks would miss; `full agent
uninstall` reverses it. The hook environment it writes is `OBSERVATORY_ROOT`
(the engine), `OBSERVATORY_HOME` (the workspace) and `OBSERVATORY_PYTHON` (the
interpreter that runs the hooks). A directory-sourced marketplace from an earlier setup is
replaced. When another channel already installs and enables the plugin under
its own id (the PassionCode launcher installs `observatory-log@passioncode`),
`install` leaves that copy alone, adds no second id (two copies would fire every
hook twice) and writes only the hook environment; `status` names the managing
channel, and `uninstall` keeps the environment that copy still reads. Restart
sessions after installing or updating: a running session may still hold older
instructions. Installing the Python package inserts nothing
into any agent configuration; only this explicit command does.

`observatory` (the short name of `project-observatory`) with no arguments opens the
dashboard of the workspace in `OBSERVATORY_HOME`, or the default one; with no workspace yet it says
how to create one, on stderr, and exits 2, because nothing was opened. Open it explicitly with `project-observatory full open` (local files) or
`project-observatory full open --serve` (a read-only loopback server on
127.0.0.1:47311; it answers GET only and changes nothing).

The Keys and ENV pages' buttons — mint, cap or revoke a provider key, mark a leak,
reveal one inventoried value — act only when the pages are served by the
token-guarded credential server, `python "$(project-observatory full-path)/tools/keyserver.py"`
(127.0.0.1:7717 by default; `--port N`). Opened any other way, those pages hand over
the command instead. A vault value is never put or rotated from a page; that is
`tools/vault.py` on stdin.

The server `--serve` starts runs detached, so closing the terminal does not end
it. Stop it with the same port:

```sh
project-observatory full open --stop              # or: --stop --port PORT, if you served on another port
```

`--stop` ends only a server that serves this workspace and that `--serve`
started. With nothing answering on the port it exits 0 with `"stopped": false` and the
reason, because the state asked for already holds; a server it must not stop (another
workspace's, or one that is not this engine's) is refused with exit 2. A server installed as an always-on agent with `serverd.py --install`
is restarted by launchd, so `--stop` refuses it and names
`python "$(project-observatory full-path)/tools/serverd.py" --uninstall`, which
stops it and keeps it off.

## Enable background or paid actions deliberately

Settings under `features` control scheduler, agent interpretation, embeddings,
notifications, retention, fixture cleanup, wiki projection, memory remediation
and private registry history. Their default is disabled. A fresh workspace has no
model chain and every budget ceiling at 0, so the agent and the assistant refuse
(`model-unconfigured`, `budget-unset`) until both are set:

```sh
project-observatory full configure model chain VENDOR/MODEL[,VENDOR/MODEL...]   # tried in order
project-observatory full configure budget daily_ceiling AMOUNT                 # also monthly_ceiling, velocity_ceiling
```

Ceilings are non-negative numbers in the wallet's denomination, written to
`config/models.json`; all three must be above zero. To use the assistant (the Mac
app's assistant window, `full assistant`, or the MCP `observatory_assistant_ask`):

1. `project-observatory full configure features agent true`
2. the OpenRouter key, on stdin: `python "$(project-observatory full-path)/tools/install_key.py" --for observatory`
3. `project-observatory full configure model chain VENDOR/MODEL`
4. the three `full configure budget …` ceilings above

With the agent on, `full doctor` shows an `agent` section with `model_status` and the
`next` commands still missing; `project-observatory full assistant status` says the same
without spending. On macOS the
two launchd installers write workspace-specific jobs only after the scheduler
is explicitly enabled; Linux can run `project-observatory full tick` under a
supervisor chosen by the user. Do not create duplicate writers for one home.
On macOS, `tools/serverd.py --install` also writes a `fabric-service/0.1`
descriptor (`project-observatory.<instance>.json`, in
`~/Library/Application Support/ai.passioncode.fabric/services/` or
`FABRIC_SERVICES_DIR`) so a local host such as Fabric Dashboards can find the
server; `--uninstall` removes it. The server keeps one copy per workspace
(`service.lock`; a second copy exits 75) and creates `service.token` for its
events feed. See the [service design](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/FABRIC-SERVICE.md).

```sh
project-observatory full configure features scheduler true
ENGINE="$(project-observatory full-path)"
python "$ENGINE/tools/install_launchd.py" install      # the tick, every 30 minutes; status | uninstall | run-now
python "$ENGINE/tools/serverd.py" --install            # the always-on dashboard server; --status | --uninstall
```

A launchd job does not inherit your shell. The installer writes the directories
of the `PATH` it runs with (absolute, existing, writable by no other account;
group-writable only when you or root own it, as Homebrew's directories are)
followed by the system directories into the job, so tools such as `claude`,
`heroku` or `gh` resolve as they do in your terminal. After installing a tool
in a new directory, run both installers above again.

## The machine: what runs, where the disk goes, cleanup

With `features.machine_watch` on, each tick surveys the machine the estate runs
on and the Machine page shows it; `project-observatory full machine` prints the
same summary.

- **Processes by origin.** Every process is grouped by the nearest ancestor
  that explains it: an agent session (Claude Code, Codex), a launchd job, a
  simulator (by device name), an application bundle, the system, or
  `detached` — a process whose parent is launchd and which nothing else
  explains, often a server that outlived its session. A process whose working
  directory is inside a project is attributed to it. `full machine --explain
  PID` says why one process runs; with [witr](https://github.com/pranshuparmar/witr)
  installed (`brew install witr`) it adds the service, port and file detail.
  Neither a process's environment nor its full command line is ever kept.
- **Memory**: physical, compressed, swap; the largest origins.
- **Disk**: free space on the home volume and the places in
  `config/machine.json` — caches, simulators, VM disks, histories — each with
  how its space comes back. Each place is re-sized every `every_hours`, within
  `disk_budget_seconds` per tick (oldest first; the rest keep their last number),
  and `du -x` never counts a mounted image as the host disk. Swap files are a row too: on
  macOS they share the disk's container, so memory pressure is disk pressure.
- **Git hygiene**: every registered checkout's worktrees (clean, dirty,
  missing, in use, idle days) and branches (merged, patch-merged, pushed,
  unique).

With `features.auto_cleanup` also on, the tick removes what loses nothing:
branches merged into the default branch or identical to their upstream, clean
worktrees nobody has touched for a week, stale worktree records, and git-ignored
build output of projects idle for a month (thresholds and protected branch
names in `config/cleanup.json`). Each target is re-checked when acted on, and
each removal is a line in `store/logs/cleanup.jsonl` with what brings it back.
The rest needs a person:

```sh
project-observatory full cleanup                          # the plan; removes nothing
project-observatory full cleanup --apply                  # the auto tier, now
project-observatory full cleanup --apply --include manual # also unique branches and dirty worktrees, archived first
```

The manual tier writes a thin git bundle per branch and a patch plus a tarball
of untracked files per worktree under `<home>/archive/cleanup/<date>/` before
removing anything. A worktree's patch is re-applied, in check mode, to a scratch
index of its HEAD first; if git cannot produce it or it does not apply, the
worktree stays and the run reports it as failed with the reason.

## Organizations and resources

An estate that serves more than one owner keeps each owner's analytics, design
files and clouds in that owner's accounts. `config/organizations.json` says
which owner a project has and where that owner's accounts are; every project in
the registry then carries `organization`, `organization_source` and
`organization_why`, and `observatory_project` answers with the destination:

```json
{"organizations": {
   "company": {"label": "Company", "ga4_account": "accounts/100",
               "figma": {"team": "Company Design", "project": "Builder"},
               "match": {"repository_owners": ["company-org"], "products": ["product:suite"]}},
   "person":  {"label": "Person", "default": true, "ga4_account": "accounts/200",
               "ga4_legacy_accounts": {"accounts/300": ["properties/31"]},
               "figma": {"team": "Personal"}}},
 "projects": {"project:partner-app": {"organization": "person", "why": "…"}}}
```

The first answer wins: a declaration (`projects` here, or `organization` in
`project_overrides.json`), then a `match` rule on repository owner or product,
then `external` when every repository owner is outside the estate
(`ownership.json`), then the `default` organization. Two organizations matching
one project is reported as a `conflict` and assigns neither. An empty file turns
the feature off.

Agents read the destination at session start: the Claude Code plugin prints the
project's organization, its Google Analytics account and Figma team, and the
duty to report what they create. The `tracking-resources` skill makes that duty
explicit. A resource — an analytics property or tracker, a Firebase or Google
Cloud project, a server, database, DNS zone, cloud, payment or app-store
account, a Figma file — is reported with `observatory_propose` and
`{"resources": [{"kind", "identifier", "account", "url", "note"}]}`. Accepting
it appends to the project's list in `project_overrides.json`; it never replaces
what another proposal added.

The findings board checks Google Analytics against the split: a property whose
account is not its project's organization's `ga4_account`
(`analytics.property_wrong_account`), a new property in a legacy account
(`analytics.legacy_account_property`), and an organization's account no service
account can read (`analytics.organization_account_unreadable`).

## Upgrade, back up, restore

From 0.7.0 on, `project-observatory full update --apply` does everything in this section for an
installed release in one command — fetch, verify, stop the jobs, snapshot,
install, upgrade, verify, restart, and roll back on failure; see
[Staying in step](#staying-in-step). The steps below are the same work by hand, for a
source checkout or a scheduler this engine did not install.

Stop every writer, including old executables and background jobs. A lock in a
new version cannot constrain an old process that does not know that lock.

```sh
project-observatory full upgrade
project-observatory full workspace-backup --writers-stopped
project-observatory full upgrade --apply --writers-stopped
project-observatory full doctor
```

The first command previews changes. Apply creates a private snapshot and
validates the staged upgrade. `workspace-backup`, `upgrade` and `restore` print a
refusal as one `Observatory: …` line and exit 2 (for example, `--writers-stopped`
missing) before changing anything; a failure while working exits 1. The original `full backup` command retains its
older database-only meaning; `workspace-backup` snapshots the whole managed
workspace. Keep the snapshot and matching previous application release.

To restore, select a new empty home with global `--home` and pass the private
snapshot directory to `full restore`. Never point old code at newer data to
simulate a downgrade. [COMPATIBILITY.md](COMPATIBILITY.md) specifies supported
formats, failure handling, backup exclusions and the tested migration matrix.

### Encrypted backups off this disk

Every copy the engine keeps — the daily database copy, `workspace-backup`
snapshots and the snapshot `upgrade --apply` takes first — is written to one
**backups root**, encrypted, once a passphrase is configured:

```sh
project-observatory full backup-passphrase set     # stdin; a terminal prompts twice
project-observatory full backups status            # root, which rule chose it, what exists
project-observatory full backups migrate           # move the newest legacy copies there, encrypted
```

The root is chosen in this order: `OBSERVATORY_BACKUPS`, then
`project-observatory full configure storage backups /absolute/path`, then the
platform default — on macOS `~/Documents/Project Observatory/Backups` when
`~/Documents` exists, which iCloud Desktop & Documents can sync off the machine;
elsewhere, or with no `~/Documents`, `<home>/backups`. A root inside the
workspace keeps the encrypted copies in the same tree as
`secrets/backup-passphrase`, so one lost disk takes both: `backups status` and
`doctor` warn about it (`inside_workspace: true`) until the root is pointed
elsewhere.
Each workspace writes into its own subfolder (`<home-name>-<instance>`), so two
workspaces sharing one root never rotate each other's files. Three artifacts
per kind are kept (`observatory-db-*.obsdb`, `snapshot-*.obsnap`,
`before-upgrade-*.obsnap`); a new one is decrypted in full before anything
older is pruned.

The format is `OBSENC1`: AES-256-GCM in authenticated chunks, key derived from
the passphrase by scrypt (N=2^17, r=8, p=1), header bound as associated data,
last chunk flagged — a truncated, reordered, extended or re-headed file fails
to decrypt instead of decrypting to less. A snapshot includes `secrets/`,
which is why nothing reaches the root unencrypted: **without a passphrase,
copies stay inside the workspace, plaintext, on this disk only**, and `full
doctor` says so under `backups.warnings`.

The passphrase lives in `<home>/secrets/backup-passphrase` (mode 600).
`backup-passphrase show` prints it only to a terminal. Keep a copy outside the
machine: restoring elsewhere needs it, and nothing can recover it.

```sh
# On another machine: the passphrase from the environment or a prompt
OBSERVATORY_BACKUP_PASSPHRASE=… project-observatory --home NEW_EMPTY_HOME full restore snapshot-….obsnap
project-observatory full backups decrypt observatory-db-….obsdb ./copy.db
```

`workspace-backup --output DIR` still writes a plaintext snapshot directory
where you name it; that is an explicit choice and it is not encrypted.

For an original installation whose state lived beside source, set a new home
and run `full migrate-local ORIGINAL_DIRECTORY` to preview. Apply requires
`--apply --writers-stopped`. It leaves the original in place and does not start
services. Verify external source paths, enabled features, credentials, registry
counts and database health before choosing which installation becomes active.

## Second machine

Two operators, or one operator on two Macs, run the same engine version and the
same functional configuration; each machine keeps its own paths, credentials and
estate. On the new machine:

1. **Install the same release.** From the release page, save `project_observatory-X.Y.Z-py3-none-any.whl`
   and `SHA256SUMS`, check the digest, and install the wheel
   into the Python environment from [SQLite runtime prerequisite](#sqlite-runtime-prerequisite):

   ```sh
   shasum -a 256 -c SHA256SUMS --ignore-missing
   python -m pip install 'project_observatory-X.Y.Z-py3-none-any.whl[full]'
   ```

   A machine that already has 0.7.0 or later installed moves to the exact version
   with `project-observatory full update --version X.Y.Z --apply`. Releases before 0.7.0
   have no `update` command: move such a machine to 0.7.0 once by hand, as in
   [Upgrade, back up, restore](#upgrade-back-up-restore) (stop the writers, install the
   verified wheel, `full upgrade --apply --writers-stopped`, start the jobs again), and
   use `full update` from then on.
2. **Create the workspace:** `project-observatory full init`.
3. **Import the profile.** On the first machine, `project-observatory full profile
   export observatory-profile.json` writes the portable configuration: integrations,
   features, the dashboard language, `models.json` and the policy files that hold no
   paths, accounts or project names. Every included and excluded item is listed in the
   file with its reason; `sources`, `storage`, secrets, the registry, the store,
   credential annotations and account ids never are. Carry the file across, then:

   ```sh
   project-observatory full profile import observatory-profile.json          # preview: the diff
   project-observatory full profile import observatory-profile.json --apply
   ```

   Import refuses a profile made by a newer engine (run `full update` first), a format
   it does not know, and a file whose sha256 no longer matches its content. It keeps
   every local field the profile does not name and never touches `sources` or
   `features.scheduler`.
4. **Connect what the import report lists.** `connect_on_this_machine` names each enabled
   integration's missing source (`coverage_warnings`, the same check `full doctor` runs)
   and the credential or CLI login it needs here. Set sources with `full configure sources
   NAME PATH` and enter credentials as in [Enter credentials locally](#enter-credentials-locally).
5. **Connect the agent:** `project-observatory full agent install`.
6. **Scheduler, deliberately:** `full configure features scheduler true`, then
   `tools/install_launchd.py install` and `tools/serverd.py --install` from
   `project-observatory full-path`, as in
   [Enable background or paid actions deliberately](#enable-background-or-paid-actions-deliberately).
   Scheduling is a per-machine decision, which is why a profile does not carry it.

## Staying in step

After each release, on both machines:

```sh
project-observatory full update --check     # 0 up to date, 10 update available, 3 could not look
project-observatory full update             # preview: current, target, what --apply does
project-observatory full update --apply
```

`--apply` downloads the wheel and `SHA256SUMS` from the GitHub release and installs only
when the wheel matches both GitHub's published asset digest and its `SHA256SUMS` line.
It keeps a verified wheel of the running release for rollback (under
`backups/engine-releases/`, cached by every update, or downloaded from that release) and
refuses without one unless `--no-rollback` is given. It stops this workspace's launchd
tick and server if they are loaded (unless `--writers-stopped` says you stopped your own
scheduler; a foreground `open --serve` is yours to stop), snapshots the workspace, installs
the wheel with its `full` extra using the running interpreter's pip (or `uv pip`), runs the
new release's `upgrade --apply --writers-stopped` in a new process, verifies the installed
version, its pinned dependencies and `doctor`, and starts again exactly the jobs it
stopped. Any failure after the install reinstalls the rollback wheel; if the workspace had
already been upgraded, the pre-update snapshot is restored at the same path and the changed
copy is kept beside it as `<home>.failed-update-…`. Every step is a line in
`store/logs/update.jsonl`.

It refuses a downgrade, the same version without `--reinstall`, and a source checkout or
editable install (update those with Git). `--version X.Y.Z` pins the target; `--repository
OWNER/NAME` and `--api-url URL` (or `OBSERVATORY_RELEASE_REPOSITORY`,
`OBSERVATORY_RELEASE_API`) select a fork or a mirror. Only HTTPS is accepted, except to
this machine.

| Exit | `--check` | `--apply` |
|---|---|---|
| 0 | up to date, or this machine is ahead of the target | updated |
| 1 | — | failed after the install began; rolled back (`rolled_back`, `workspace_restored`) |
| 2 | refused: no such release, bad arguments | refused before any change: verification, downgrade, unsafe state |
| 3 | could not look: network, rate limit, a release without its assets (`degraded`) | could not look (network, rate limit), before any change |
| 4 | — | rollback incomplete; `human_steps` says what to run |
| 5 | — | updated, but a stopped job did not start; `services_not_restarted` has the command |
| 10 | a newer installable release exists | — |

Anonymous GitHub API requests are limited to 60 an hour per address; a `--check` from a
scheduler every few hours stays far below it.

## Choose the dashboard's language

The dashboard is English unless the workspace says otherwise. The setting lives in
`config/settings.json` as `"interface": {"locale": "ru"}` and is written by

```sh
project-observatory full configure interface locale ru   # en | ru
project-observatory full open --rebuild                  # pages carry the language they were built in
```

`full doctor` reports it under `interface`. Only `en` and `ru` are accepted; an unknown
key or value is refused. Releases before 0.4.0 ignore the section. Each reader can also
press **EN** or **RU** in the navigation rail: the choice is stored in that browser,
applied before the first paint and kept for pages opened as local files; choosing the
workspace's own language forgets it again. The interface and finding titles are
translated; a finding's details and suggested action stay in English. An agent asked to set the language runs the
`configure` command above and never edits other settings to do it.

## Reading the dashboard workspace

The navigation groups work, infrastructure, access and system pages. Projects and
ENV open with rows visible. Search and filters narrow the list; sorting applies
to the entire selection and has a separate control on narrow screens. Project
names open full detail; closing it returns to the list.

Overview previews eight open findings by severity and links to the full list.
Each finding keeps its title visible; “Evidence and action” opens the evidence
and next step. A direct finding link opens that evidence. Acknowledged history
remains available even if no findings are open.

“Command” means copy for a terminal, not execute. Keys and ENV state their current
mode above the list. Health describes the observer at measurement time, not a
continuous availability promise. Traffic distinguishes missing measurements from
zero and totals are sums across GA4 resources, not deduplicated human visitors.

Behavior contracts and checks: [workspace redesign](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/ux/DASHBOARD-REDESIGN.md),
`test_workspace_redesign`, `test_dashboard_shell`, `test_google_identity`.

## Socket inspection in the local conformance check

The synthetic `test_fabric_service.py` suite invokes the vendored conformance probe.
Its `network.loopback-only` rule needs `lsof` on `PATH` (on macOS it commonly lives in
`/usr/sbin`). Without it, the rule reports `NOT_RUN` with `lsof is not installed`;
that is missing socket-inspection evidence, never a loopback PASS. The suite checks
this exact missing-dependency outcome in `RunningServer.test_conformance_reports_a_missing_lsof_dependency`.
With `lsof` available it requires the loopback verdict to pass. HTTP host/origin guards
and binding behavior retain their own tests; add the system utility directory to `PATH`
when collecting complete local socket evidence.

## Mac app

0.12.0 ships a native macOS app (macOS 14+). It opens on this workspace's dashboard —
live when the workspace's own server answers, otherwise the saved pages under a banner
with **Start server**, and **Build the dashboard** when nothing is built yet — with the
assistant one window away (⇧⌘A). From a checkout:

```sh
macos/scripts/build-app.sh             # dist/macos/Project Observatory.app
macos/scripts/install-app.sh --open    # into /Applications (or ~/Applications), then opens it
```

On first launch it looks for the engine at `~/.local/bin/project-observatory`, then
`~/.local/share/project-observatory-venv/bin/project-observatory` (the README's install),
then `/opt/homebrew/bin` and `/usr/local/bin`, with the default workspace; otherwise choose
the engine's absolute path and an initialized workspace in **Settings**. The assistant
needs the setup in [Enable background or paid actions deliberately](#enable-background-or-paid-actions-deliberately).
It runs the installed engine's `full assistant` command; it needs no MCP server and registers none. Details:
[macOS app](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/macos/README.md).
