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

On macOS, [Homebrew Python](https://formulae.brew.sh/formula/python@3.14)
provides a supported installation path:

```sh
brew install python@3.14
"$(brew --prefix python@3.14)/bin/python3.14" -m venv .venv
. .venv/bin/activate
python -c "import sqlite3; c=sqlite3.connect(':memory:'); c.enable_load_extension(True); c.enable_load_extension(False)"
```

Install the full package and its pinned dependencies in that environment using
the release installation instructions, then run the full doctor command.

## Start without credentials

From a checkout of the public release:

```sh
python3 -m venv .venv
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

`init` is idempotent. It seeds missing initial state only for an empty new home;
it does not replace an existing installation. Configuration belongs in
`config/settings.json`; curated overrides and policy files are adjacent.
`project-observatory full-path` prints the immutable engine directory, useful
for connecting scripts, hooks and MCP.

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

List current settings with `doctor`. Enable a selected integration with
`project-observatory full configure integrations NAME true`. The collector's
installed help and source describe its exact input format. Start with the
smallest account access that can read the requested resources; inventory access
and token provisioning are different permissions.

| Integration name | Purpose | User-owned setup |
|---|---|---|
| `github` | Repository inventory | Authenticate GitHub CLI locally; it enumerates resources visible to that account |
| `bitbucket` | Repository inventory | A local Bitbucket credential in the configured secret store |
| `cloudflare` | Zones and DNS inventory | User-owned account credentials; token administration is separately scoped |
| `heroku` | Hosting inventory | Local authentication for the intended Heroku account |
| `google` | Analytics property and search inventory | The user's service account files and resource grants |
| `domains` | Domain observations | Explicit domain export and network access |
| `mcp` | Configured server inventory | Explicit `sources.mcp_config_root` |
| `sessions` | Local agent activity | Explicit `sources.sessions` |
| `wiki` | Knowledge-base inventory | Explicit `sources.wiki` |
| `openrouter` | Key and usage inventory | A locally supplied provider credential |
| `remote_env` | Compare deployed environment metadata | Explicit opt-in to provider environment reads |

Metric plugins have their own manifest requirements and opt-ins. Run
`project-observatory full plugins-check` to validate them and read
[plugins/README.md](../plugins/README.md) before adding trusted executable plugins.
An unavailable input must remain visible as unavailable; it does not prove that
there are no findings.

## Enter credentials locally

For project slots, the full engine's `tools/vault.py put PROJECT ENV NAME`
accepts the value only on stdin. Prefer a human-controlled hidden prompt or a
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

## Connect an agent

The complete MCP server uses stdio. Configure a Python executable from the
installed virtual environment, the engine's `mcp/server.py` as its argument,
and `OBSERVATORY_HOME` for the selected private workspace. Detect MCP support
in the chosen agent rather than assuming it from the product name. The optional
`observatory-log` plugin adds Claude Code hooks; other hosts can use the CLI
and MCP without those hooks.

For a directory-sourced Claude Code installation, resolve the engine root with
`full-path`, add its `skill` directory as a marketplace, then install
`observatory-log@observatory-log`. Set `OBSERVATORY_ROOT` and the matching
`OBSERVATORY_HOME` in the host environment. Restart sessions after updating a
skill: a running session may still hold older instructions. No MCP server is
silently inserted into agent configuration by package installation.

## Enable background or paid actions deliberately

Settings under `features` control scheduler, agent interpretation, embeddings,
notifications, retention, fixture cleanup, wiki projection, memory remediation
and private registry history. Their default is disabled. Configure model
selection and a budget before enabling reasoning or embeddings. On macOS,
`tools/install_launchd.py` and `tools/serverd.py` install workspace-specific
jobs only after explicit scheduler opt-in; Linux can run the CLI under a
supervisor chosen by the user. Do not create duplicate writers for one home.

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
removing anything.

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

Stop every writer, including old executables and background jobs. A lock in a
new version cannot constrain an old process that does not know that lock.

```sh
project-observatory full upgrade
project-observatory full workspace-backup --writers-stopped
project-observatory full upgrade --apply --writers-stopped
project-observatory full doctor
```

The first command previews changes. Apply creates a private snapshot and
validates the staged upgrade. The original `full backup` command retains its
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
platform default — on macOS `~/Documents/Project Observatory/Backups`, which
iCloud Desktop & Documents syncs off the machine; elsewhere `<home>/backups`.
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
