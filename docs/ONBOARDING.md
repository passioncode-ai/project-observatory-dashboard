# Install the complete Project Observatory

The public package contains the original engine with per-user paths and generic
configuration. Its database, registries, keys and generated pages live in a
private workspace outside the installed source. macOS and Linux are supported;
Python 3.11+, SQLite 3.37+, Git and Node.js are required for the complete local
checks. The Python package's `full` extra installs the MCP, schema, vector-store
and Google authentication libraries; installed with `-c requirements-full.lock`,
every dependency, direct and transitive, lands at the version tested by this
release. A checkout has the lock at its root; a wheel carries it at
`"$(project-observatory full-path)/requirements-full.lock"` (the
[README](../README.md#install) installs the wheel `--no-deps` first, then the
extra against that file). Without `-c`, pip resolves newer transitive releases
this release was not tested with.

## Read verification results

`project-observatory full check` keeps a process's `exit_code` separate from the
checks it observed. A selected suite that exits zero without reporting executed
tests is `ERROR`, not PASS. The JSON gives its `test_protocol`,
`minimum_executed`, `executed_checks`, `skipped_checks` and reason code. Repair a
missing test entry point or reporting contract and rerun the affected suite.
An explicit full skip remains `SKIP`; reported failures remain `FAIL` even when
the script exits zero. A trusted script can forge well-formed output, so this
contract does not authenticate tests or establish complete coverage.
The repository-root test module `test_runner_evidence.py` exercises these boundaries.

Each suite runs in its own sandbox under the temporary folder (a copy of the engine's source,
about 14 MB, and the suite's own files). The sandbox is removed as soon as its suite ends;
only its `run.log` stays until the run ends (`--keep` keeps the sandboxes for inspection), and
its row's `sandbox_bytes` says how much the sandbox held, so a run names the suites that write most.
Before copying anything the run checks the temporary folder's free space: below one source copy
per running suite (`--jobs`) plus the template and a 2 GB floor it refuses, exit 7, with
`reason_code: insufficient_disk`, the bytes it needs and has, and the way out: free space,
lower `--jobs`, or pass `--min-free-gb 0` to start anyway (`--min-free-gb N` sets the floor).
Before this a full run kept all 273 copies to the end, about 3.5 GB (OBS-167).

## SQLite runtime prerequisite

The full engine requires Python 3.11+ with SQLite 3.37+ **and loadable SQLite
extensions**, plus the locked sqlite-vec dependency. Some macOS Python builds
omit `enable_load_extension`; installing sqlite-vec alone cannot add it.
Initialization, doctor, migration and upgrade check this before workspace writes.
They name the two faults apart: an interpreter that cannot load extensions
(choose another Python, below), and a package installed without its `[full]`
extra, where the refusal lists the missing modules and prints the one `pip`
command, with the engine's own `requirements-full.lock`, that installs them into
the same interpreter.
The isolated regression is
`observatory/engine/tests/test_workspace_upgrade.py::WorkspaceUpgrade::test_missing_sqlite_extension_support_refuses_before_writes`.

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

**A wholesale change to the registry is refused, not written.** Each `full local` (and each
scheduled tick) compares the new `projects.json` and `repositories.json` with the previous ones,
because a collector failure (a missing `git`, an unreadable folder) shows up as projects
appearing or vanishing in bulk. The emit step stops with `emit REFUSED: … would go from N to M`
and writes nothing when a count moves by more than 25% **and** by more than five records
added, or more than one record lost; a registry that would fall to zero is refused at any size.
Adding a project or two to a small workspace, or removing one, passes. When the change is
genuine (you pointed `projects` at a new folder of many checkouts, or deleted several on
purpose), run it once with `OBSERVATORY_ALLOW_BULK=1 project-observatory full local`; the
next run compares against the new registry. The first emit into an empty workspace is never
refused.

A workspace path must not pass through a symbolic link (on macOS `/tmp` is one):
`init` refuses such a path and prints the resolved `OBSERVATORY_HOME` to use instead.

`init` is idempotent. It seeds missing initial state only for an empty new home;
it does not replace an existing installation. Configuration belongs in
`config/settings.json`; curated overrides and policy files are adjacent.
`project-observatory full-path` prints the immutable engine directory, useful
for connecting scripts, hooks and MCP.

**What else a project is called** — a store id, a hosting app, another agent's own slug — is
curated in `config/identifiers.json` as `{"aliases": {"<system>:<value>": {"resolves_to":
"project:…" or "product:…", "owner": …, "source": …, "since": "YYYY-MM-DD"}}}`; agents can propose
rows with `observatory_propose` (`patch: {"aliases": [...]}`). One name has one target: two targets
for one name, or a name that contradicts a measured repository, deployment or domain, is listed as
a conflict in `registry/identifiers.json` and withheld. `project.resolve` answers any of these
names with the one canonical id ([design](design/IDENTITY.md)).

**Which repository is a copy or a move of which** is yours to record, because git cannot see it:
a copy rewritten for another owner shares no commit with its source, so no fork flag or merge
base names the relation, and the copy falls behind without a word. Record it in
`config/derivations.json`:

```json
{"schema": "derivations/1", "derivations": [
  {"copy": "repository:example-org/alpha-agent", "source": "repository:personal/alpha", "kind": "copy",
   "copy_point": "1a2b3c4", "sync_base": {"source_commit": "5d6e7f8", "date": "2026-11-02"},
   "builder": {"path_hint": "~/builders/alpha/build.sh", "rules_count": 12}},
  {"copy": "repository:example-org/beta-bot", "source": "repository:personal/beta-bot", "kind": "move"}]}
```

Both ids are spelled as the registry spells them (`observatory_status` lists them). `kind` is
`copy` (kept in step with its source) or `move` (the source is frozen and nothing is carried);
`copy_point` is the source commit the copy was taken from (needed for a copy), `sync_base` the
source commit it was last carried forward to. A copy may also say so itself, in a tracked
`.copy/upstream.json` with `kind` (`rewritten` or `export`, both a copy here, or `move`),
`copy_point`, `sync_base` and `deny` (paths the copy never takes, such as run data), and never the
source's name. The next tick's emit writes
`registry/derivations.json`: each relation with which side every value
came from (your file wins where both give one), and how many commits the source's branch has after
`sync_base`, counted in the source's local clone as of its last fetch. Observatory never fetches
and never writes to either repository. An entry it cannot stand behind (an unknown repository, a
repository derived from itself, a cycle, two sources for one copy, a move with `sync_base`) is
refused by name in `degraded` and in `full copies list`, never repaired. The findings
`copy.behind_source`, `copy.relation_unrecorded`, `copy.record_differs`, `copy.moved_source_open`
and `copy.name_unprefixed` (the copy lacks the prefix its entry's `name_prefix` names) say what each
relation owes; `observatory_project` shows it both ways
(`derived_from`, `copies`). To carry a copy forward,
`project-observatory full copies plan repository:example-org/alpha-agent` prints the steps (the
copy's builder at both commits, the diff without the deny paths, `git apply --3way` on a branch
`sync/<sha12>`, the copy's own audits) and runs none of them; after the pull request lands, write
the new `sync_base` into the file.

**Which Fabric project each project is** is Fabric's to say: Fabric owns the canonical project id
(fabric-agent-contract DEC-0089). Each tick, and `project-observatory full fabric-attribution`,
asks Fabric's hub for every folder the registry holds and records the answer as a `fabric:<uuid>`
alias (owner `fabric`) in `store/raw/fabric-attribution.json`, with when and from which Fabric
process it came. Fabric writes nothing into the workspace. The first pass asks Fabric once for
access to `project.resolve`; on your own machine Fabric allows it at once and journals it. The
credential it hands over stays in an owner-only file under the workspace state and goes only to
the hub `hub.json` names, only while that process is alive and owns the hub's listening port as
you, and never through a proxy. A folder Fabric later detaches
withdraws the alias, a different answer replaces it, and both stay in the file's `history`. A
project whose folders sit in two Fabric projects is a `fabric.attribution_conflict` finding until
you settle it in Fabric. When Fabric is not running nothing recorded changes, and `degraded` says
why (`hub-absent`, `hub-dead`, `hub-origin-refused`, `hub-unverified`, `access-pending`, `access-refused`,
`tool-absent`, `hub-error`, `budget`). `project.resolve` answers `fabric:<uuid>` and an absolute
folder too: a folder by the contract's own rules over the registry's folders, with the `_*`
folders under the projects root and `config/folder_exclusions.json` as declared containers.

**Which environment an app serves** comes from the provider (a Heroku pipeline stage) or from you:
`config/environments.json` holds `{"deployments": {"heroku:<app>": "production"}}`. The names
`production`, `staging`, `review`, `development`, `test` and `local` are recognised (`prod` and
`stage` are read as their full names). An app with neither is listed as `unassigned` in
`registry/environments.json`. Its name is never read as a hint.

**One verdict** (OBS-349). `full doctor` ends with `attention` and `clean`. Each `attention` row names its
`area` (`backups`, `machine_backup`, `maintenance`, `sources` or `tick`), its `code`, a `severity`, the sentence and
the `step` that fixes it (`null` when the fix is by hand); a code is listed once, each missing source is its own
`source-missing` row with the `configure sources` command, and a tick that is `interrupted`, `stale` or
`running-long` (below) is a `tick-<verdict>` row with the tick's own reason. `clean` is true when no row is a `warning`: a release
announced (`update-available`), automatic updates or the schedule turned off on purpose (`auto-off`,
`schedule-off`) and an update waiting for a quiet moment (`deferred`) are `info`, so a deliberate setting does not
keep doctor from reading clean. `clean` does not cover the other sections yet: an expiring or removed
organization certificate (`organizations`), a meter without an issuer (`meter`), refused session servers
(`session_servers`), `analytics`, the review policy (`review`) and the embedding and vector state keep their own
fields, so read those too before taking `clean` as the whole answer. Doctor is a report, not a gate: it exits 0
either way. To read the verdict alone:

```sh
project-observatory full doctor | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["clean"], [a["code"] for a in d["attention"]])'
```

`full doctor` also reports whether the scheduled tick is alive (`tick.verdict`): `disabled` while
`features.scheduler` is off (the default), `never` before the first tick, `running` during one and
`ok` after a recent one. A tick that did not finish is `interrupted`, and the reason says how it
ended — stopped at its ceiling (from the supervisor's record) or killed by a restart, a sleep or a
signal. When no tick has finished for six hours the verdict is `stale`, and when one has held the
lock for longer than that it is `running-long`. `tick.recent` counts how the last 10 runs ended
(`store/raw/tick-runs.jsonl` keeps 50); when 3 or more did not finish, `tick.history_warning` says
so even if the last one did. A run that reached its end without refreshing the registry (it stopped
at a step, or its merge or emit ran out of time and the findings and the dashboard were built over
the registry as it stood) is recorded as `partial` and counted with them.
The dashboard server's `/health`, the service card and every MCP answer's `degraded` list say the
same (`tick` and `tick-history` rows). Its findings cannot: they are built by the tick itself, so a
dead tick leaves the last report looking current.

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
| `git_remotes` | Whether each checkout is current with its remote: one `git ls-remote` per checkout, run by `project-observatory full remotes` (`full local` calls no remote and skips it) | None added: it uses each checkout's own remote and SSH keys (and your `url.<base>.insteadOf` rewrites), asks no credential helper, allows only the https, http, ssh, git and file transports, and reports a remote that wants a password, or names another transport, as unreachable with git's own reason |
| `bitbucket` | Repository inventory | A local Bitbucket credential in the configured secret store |
| `cloudflare` | Zones and DNS inventory, and each account's Workers, Pages, R2, D1 and zones for the Cloudflare page | User-owned account credentials; token administration is separately scoped; the page reads through one `inventory-read` token per account |
| `digitalocean` | Droplets, apps, databases, volumes and each team's month-to-date usage for the DigitalOcean page | A team token stashed with `tools/digitalocean.py`, or its narrow `observatory-read` token minted in the control panel and installed |
| `heroku` | Hosting inventory | Local authentication for the intended Heroku account |
| `google` | Analytics property and search inventory | The user's service account files and resource grants |
| `domains` | Domain observations | Explicit domain export and network access |
| `mcp` | The MCP servers the agent configs on this machine declare (Claude Code, Cursor, OpenCode, Codex, Gemini CLI, Kiro), served as `machine.mcp.inventory` | Explicit `sources.mcp_config_root` |
| `harness` | What every agent session loads before the first word: skills and their share of the skill listing, instruction files, the settings that load every MCP tool upfront, and deterministic security rules over skills ([design](design/HARNESS.md)) | Explicit `sources.harness_root` |
| `sessions` | Local agent activity | Explicit `sources.sessions`; `sources.companion_db` is optional, set only where a memory companion (claude-mem) is installed |
| `wiki` | Knowledge-base inventory | Explicit `sources.wiki` |
| `openrouter` | Key and usage inventory | A locally supplied provider credential |
| `remote_env` | Compare deployed environment metadata | Explicit opt-in to provider environment reads |
| `ga4`, `search_console`, `cloudflare_analytics` | The traffic metric plugins | The `google` service-account files, or Cloudflare credentials, in `secret_store` |

Metric plugins have their own manifest requirements and opt-ins. Run
`project-observatory full plugins-check` to validate them and read
[plugins/README.md](../observatory/engine/plugins/README.md) before adding trusted executable plugins.
`project-observatory full plugins --only ID --force` runs one plugin now, past its age
gate, and `project-observatory full google --force` refreshes the analytics inside their
twelve-hour cache. Step options are declared per step; every step and command explains
itself with `--help` and runs nothing when asked
([CLI compatibility](../observatory/engine/docs/CLI-COMPATIBILITY.md#step-arguments-and-help)).
An unavailable input must remain visible as unavailable; it does not prove that
there are no findings.

## Sources

Apart from an exact SessionStart declaration described below, nothing outside the workspace is read until you name it. Each source is a path set with
`project-observatory full configure sources NAME PATH`; `full doctor` lists every enabled
integration or feature whose source is unset, missing (a path deleted after it was configured)
or of the wrong kind under `coverage_warnings`, because a collector without its source reports
nothing rather than failing. Each row carries two exact commands: `fix`
(`project-observatory full configure sources NAME PATH`) and `disable`
(`project-observatory full configure integrations NAME false`, or `features` for a feature).
The `projects` source is the exception: the filesystem scan always runs, so a missing
`projects` source is a row with `fix` and no `disable`.

A path under your home folder is stored home-relative: `configure sources projects ~/src` (or the
same path written out in full) is saved as `~/src`, and the answer says `"stored_as":
"home-relative"`. A path outside the home (`/Volumes/Work/src`) is stored as given
(`"stored_as": "absolute"`). So a workspace restored under another home, another user name or
another platform's home layout, still finds its sources. `full doctor` names each configured
source's `form`; a value an earlier release stored absolute is not changed by itself, and
configuring it again stores it home-relative.

| Source | What it points at | Read by |
|---|---|---|
| `projects` | the directory that holds your project checkouts | filesystem scan, events, leak scan |
| `sessions` | agent session transcripts, e.g. `~/.claude/projects` for Claude Code | `sessions` integration, leak scan |
| `mcp_config_root` | the home directory whose agent configs declare MCP servers | `mcp` integration |
| `harness_root` | the home directory whose `.claude` and `.codex` folders hold the skills, plugins and instruction files every session loads | `harness` integration |
| `wiki` | a Markdown knowledge base, e.g. an Obsidian vault | `wiki` integration, `wiki_projection` |
| `secret_store` | a private directory of provider credentials and project slots (`projects/`) | vault, OpenRouter, Google, Cloudflare analytics |
| `companion_home`, `companion_db` | a memory companion's home and database file (claude-mem); optional, only where one is installed | `sessions` integration (`companion_db`, optional: an absent file is fine, a path that is not a file is a warning), leak scan (`companion_db`, when the file exists), `companion_remediation` (both required) |
| `gateway_root` | an optional directory holding `backup-secrets.sh`, at its root or in its `bin/` (the root is tried first): a bash script run with no arguments, whose output and exit code are `vault.py backup`'s | `vault.py backup` |
| `domain_export` | a registrar's domain CSV export | `domains` integration |
| `cloudflare_snapshot` | a JSON snapshot of your Cloudflare zones, kept as evidence | `validate` (snapshot parity; reported as degraded when unset) |
| `secrets` | overrides where the workspace keeps its own credential files | provider tools |

### Exact Git checkouts declared by SessionStart

When a coding session opens an eligible Git repository or linked worktree outside
the `projects` root, SessionStart resolves any existing registry identity first
and writes only that checkout's absolute path to `config/tracked.json`. It does
not widen `sources.projects` to the parent and never writes
`registry/projects.json`. The next `full local` or scheduled tick reads the
declaration and runs the ordinary `scan_folder` → `local.json` → merge → emit
pipeline. Same-remote clones and worktrees keep the existing primary-checkout
and `extra_checkouts` dedupe.

Declarations are locked, atomic and idempotent. Folder exclusions still win;
non-Git folders, a home or other broad parent, and an ambiguous existing
identity are refused with a reason. `full untrack local-folder PATH` removes a
declaration and leaves a removal marker that later SessionStart calls do not
undo. An explicit `full track local-folder PATH` opts back in; `full tracked`
lists active and removed declarations and degraded rows, and `full doctor`
summarizes them under `tracked`. Missing or malformed declarations appear in
`local.json` `degraded` without hiding the ordinary root scan. This observation
grants no credential or memory binding and changes no organization ownership.

The `mcp` integration reads the declarations from the agent configs under
`mcp_config_root` — `~/.claude.json` (user and project scopes), `~/.cursor/mcp.json`,
`~/.config/opencode/opencode.json`, `~/.codex/config.toml`, `~/.gemini/settings.json`
and `~/.kiro/settings/mcp.json` — for names, transports and whether a key is present,
never a value. A config that is absent or does not parse is recorded as such, and the
`machine.mcp.inventory` MCP tool names it in `degraded`. Whether Claude reaches each
server is asked of `claude mcp list` **only on request** — `project-observatory full
scan-mcp` or the `machine.mcp.refresh` tool — never by the scheduled tick: that command
starts every stdio server Claude knows and can refresh your Claude login, which no
background job may do. The tick reads the configs alone and carries the last
requested verdict forward with its time (`liveness_at`). The probe runs in its own
process group, killed as a whole when it answers or times out, so no server it started
outlives it. It health-checks every server before it prints, so it gets up to 180
seconds; on a machine with many servers raise the limit with
`OBSERVATORY_MCP_PROBE_TIMEOUT` (seconds, 1 to 3600). A probe that still runs out of
time is degraded, not failed: servers it reported keep their verdict and the rest are
`not-probed`.

## Enter credentials locally

The engine's tools below are files inside the installed engine, not CLI
subcommands. Run them with the environment's `python` and the same
`OBSERVATORY_HOME` as the CLI; `project-observatory full-path` prints the
engine directory:

```sh
python "$(project-observatory full-path)/tools/vault.py" put PROJECT ENV NAME   # the value on stdin
```

`PROJECT` is the project's folder name (`alpha-web`, the directory under your `projects` source);
its registry id (`project:local-alpha-web`, or `local-alpha-web`) is accepted too and normalised to
that folder, and the command prints which folder it used, so one project keeps one vault directory.
A name two projects claim (a folder of one, the id of another) is refused with both named.
`ENV` is one of `local`, `stage` or `prod`, and `NAME` is UPPER_SNAKE_CASE.
The command accepts the value only on stdin. The same tool manages the slot afterwards —
`list`, `rotate` (the new value on stdin; the old one is archived at mode 600), `leak` and
`settle` (record an exposure and its settlement), `moved` / `movements` (a change made at
the provider), and `remove PROJECT ENV NAME` (the value, its metadata and its retired
archives; refused while a leak on the slot is open unless `--force`; `--retired` removes
only the archives). Every change is a line in the movements journal and none prints a value.
A `PROJECT` or `NAME` shaped like a credential (a provider key prefix, a UUID, 32 or more
hexadecimal characters, a long random-looking run) is refused by every command, and so is a
`--how` or evidence note that carries one; a `--where` that carries one is recorded with that
part replaced by `[redacted]`, so the sighting is kept and the value is not. Cite a commit by
its short id.
A command uses a slot by name, never by value:

```sh
python "$(project-observatory full-path)/tools/use_secret.py" names PROJECT
python "$(project-observatory full-path)/tools/use_secret.py" run --env local PROJECT NAME -- COMMAND …
```

Flags come before the project name (`run --env ENV PROJECT NAME -- …`); a flag after it is
refused with the right order, and a program that does not exist exits 127 (`pipe` too). With
`--env`, only that environment's vault slot and env files named for it (`.env.production` for
`prod`, `.env.staging` for `stage`) are read; an unlabeled `.env` answers only when no `--env` is
given, and a refusal says which environments do hold the name. The command's
output is filtered for the exact value. `project-observatory full local` refreshes the
inventory of `.env` names this reads (`env`, local, names and keyed fingerprints only), and
the Keys page shows a new slot after the next `full local`. Prefer a human-controlled hidden prompt or a
pipe from an already authenticated provider tool. Do not put a secret in a chat,
command argument, test fixture or tracked file. Read the installed
`handling-secrets` skill before an agent works with these commands.

**An HTTP MCP server's bearer, from the vault, through Claude Code's `headersHelper`.** Claude
Code runs a server's `headersHelper` command on each new connection and puts the JSON object it
prints into the request headers, not into the conversation. Its documentation
(<https://code.claude.com/docs/en/mcp>, "Use dynamic headers for custom authentication") says:
"Claude Code runs the helper fresh on each connection, at session start and on reconnect",
"Claude Code runs the command in a shell and gives up on it after 10 seconds", and it sets
`CLAUDE_CODE_MCP_SERVER_NAME` ("the name of the MCP server") and `CLAUDE_CODE_MCP_SERVER_URL`
("the URL of the MCP server") for the helper. `use_secret.py header` relies on that last
variable. Store the value, bind the slot to the server once, then name the door in the server's
entry:

```sh
python "$(project-observatory full-path)/tools/vault.py" put PROJECT prod NAME   # the value on stdin
python "$(project-observatory full-path)/tools/vault.py" bind PROJECT prod NAME --header-for https://mcp.example.com
```

```json
{
  "mcpServers": {
    "example-server": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headersHelper": "/path/to/venv/bin/python \"$(project-observatory full-path)/tools/use_secret.py\" header --env prod PROJECT NAME"
    }
  }
}
```

Give the interpreter by its absolute path, the one `project-observatory` is installed in
(`python -c 'import sys; print(sys.executable)'` from that environment), since Claude Code
chooses the helper's working directory and `PATH`. The door prints exactly
`{"Authorization": "Bearer <value>"}` and a newline; `--name X-Api-Key` changes the header and
`--scheme ""` prints the bare value. It reads the vault only, never an env file or the
environment, and refuses, printing nothing on stdout, when stdout is a terminal, when
`CLAUDE_CODE_MCP_SERVER_URL` is unset and no `--server-url` was given, when the server URL it
has names a scheme, host or port other than the binding, when the slot is not bound, when `--name` or `--scheme` is not an RFC 7230 token, or when the
value carries a control character. `--header-for` takes an https URL or a bare host (https on
the default port), or plain http to a loopback literal with its port —
`http://127.0.0.1:<port>` or `http://[::1]:<port>` — since loopback crosses no network;
`localhost` is refused (a name `/etc/hosts` can remap), as is http to any other host. The path
is not part of the binding, and userinfo, a query or a fragment are refused. A client that does
not export the server's URL to its helper — Codex's `http_headers_helper` — passes it with
`--server-url URL`, used only when `CLAUDE_CODE_MCP_SERVER_URL` is unset; when both are set and
name different servers the call is refused (`url-conflict`). `vault.py bind … --clear` removes
the binding; `vault.py list` shows it; both verbs are journalled in the movements journal. Each
`header` call adds a row to the `secret-use.jsonl` audit (served or refused, the server's name
and host, the parent process, and the caller — `claude-headersHelper` when the URL came from
the variable, `codex-helper` when it came from `--server-url`), never the value. The binding
narrows an accident: a bare `header … | cat` in an agent's shell is refused for want of a server
URL. It is not a sandbox: anything that names the bound URL — in the variable or with
`--server-url` — and reads stdout receives the value.

**Codex is untested.** The `--server-url` form follows Codex's documented `http_headers_helper`
("a local command that prints a JSON object"), but no Codex configuration entry has been
verified against this door, and Codex documents `http_headers_helper` as supported only for
local MCP servers. Until an entry is verified, Codex's documented fallback is an environment
variable read by Codex itself — `bearer_token_env_var = "<ENV>"` for an `Authorization: Bearer`
header, `env_http_headers = { "<header>" = "<ENV>" }` for any other — with Codex started under
`use_secret.py serve`. That is weaker than the helper: the value then sits in the agent's own
environment for the whole session, where any command the agent runs can read it, and none of the
door's refusals (a terminal, an unbound slot, a server other than the binding, `url-conflict`)
applies.

Slots are private files under the workspace by default. A separate
`sources.secret_store` is optional and belongs to the user. File permissions
limit local access; this is not a claim that the default files are encrypted at
rest. Protect private backups just as carefully. A configured external store
is not included in workspace backup snapshots.

**A Heroku credential for one purpose, and the teams behind it.** `tools/heroku.py` is
the Cloudflare door's twin on the Heroku Platform API. `heroku auth:token | python
"$(project-observatory full-path)/tools/heroku.py" stash` (or `stash --from-cli`) keeps the
CLI session's token as the admin, read by the door alone. `issue --preset read|deploy|identity
--vault PROJECT/ENV/NAME [--team TEAM] [--app APP]` mints a direct OAuth authorization with that
scope only (`config-read` and `config-write` reach config vars and need `--app`), proves it with
its own value, and delivers it on stdin; `rotate` regenerates it in place, `revoke` deletes it
first at Heroku, `list` and `ping` name what exists. Heroku narrows an authorization by scope
only, never to a team or an app: `--team` and `--app` are its stated purpose and what the proof
reads. `teams`, `members --team` and `apps --team` read the teams with the slots issued for each;
`member add|role|remove --team TEAM --email ADDRESS [--role admin|member|collaborator]` prints
its plan and changes nothing until `--apply`, then reads the team again to verify. It refuses
`owner`, the last admin and the admin's own membership. On a team whose `team-invite-acceptance`
feature is on, as the Heroku CLI does, `member add` sends an invitation, `members` shows pending
invitations, and `member remove` of a pending invitee cancels the invitation. The Cloudflare door
reads the same way: `cloudflare.py accounts` lists every stashed account with its tokens and the
vault slot each managed one names, `members --account` reads its members, and `member
add|role|remove --account` is a plan until `--apply`. It counts Super Administrators from both
roles and permission policies, refuses to leave the account without one on the whole account, and
refuses any write while it cannot read who holds that role.

**Cloudflare presets.** `tools/cloudflare.py issue --preset P` mints one narrow token from the
stashed admin token: `analytics` for the observatory's own traffic reader (filed where the plugin
reads it), and into a vault slot (`--vault PROJECT/ENV/NAME`) `zone-analytics-read` (one zone's
counters), `dns-edit`, `email-routing`, `email-sending-domain`, `redirect-rules`, `workers-routes`,
`cache-purge` (one zone each, `--zone`), and `d1-edit`, `email-send`, `workers-edit`,
`workers-observability-read`, `access-app`, `fabric-inbox-server`, `fabric-inbox-account` (one account each,
`--account`). `r2-bucket` is one bucket (`--account --bucket NAME`) and delivers an S3 pair to three
slots under `--vault PROJECT/ENV/PREFIX`: `PREFIX_ACCESS_KEY_ID`, `PREFIX_SECRET_ACCESS_KEY`,
`PREFIX_ENDPOINT`. Each is verified with its own value before delivery; the handling-secrets skill says
what each grants.

**The Cloudflare page** (OBS-271). `tools/cloudflare.py issue --preset inventory-read --account
<slug>` mints one read-only token per account (Workers Scripts, Pages, R2 Storage and D1 Read on the
account; Zone, DNS and SSL and Certificates Read on its zones) and files it in
`secrets/cloudflare-inventory.d/<account>`, never in the analytics folder and never in a vault slot,
once its new value lists the account's Workers. `rotate`, `revoke`, `ping` and `list` name it
`inventory/<account>`. With `integrations.cloudflare` on, `full scan-cloudflare-account` (daily in the
tick) records each account's Workers, Pages projects with their source repository, R2 buckets, D1
databases and zones with their plan and price, DNS record count and certificate expiry in
`store/raw/cloudflare_account.json`; `emit` turns it into `registry/cloudflare-account.json`.

**A Sentry token for one purpose, and the broad one retired** (OBS-292). A site's build needs only to create
releases and upload source maps, and error triage only to read projects, issues and events; a broad Sentry token
(org:admin, member:admin, and so on) in a vault slot can do far more. `tools/sentry.py issue --preset releases
--from PROJECT/ENV/NAME --vault PROJECT/ENV/NAME [--org SLUG] [--project SLUG]` reads the minting credential from
its slot (vault only), mints a Sentry organization token with the one scope `org:ci` (Sentry lets it list, create and
update releases, upload source maps, artifact bundles and debug files, read the organization's integrations and read
and write bulk code mappings; it cannot delete a release or a debug file, read events or members, or change a
setting), proves it with its own value
(exactly that scope, the releases read on the organization's region host, members and issues refused), writes it
to the `--vault` slot on stdin and records it in `secrets/sentry-issued.json` (kind, scope, organization, project,
token id, never the value). Sentry mints an organization token by API only for an OAuth application's token holding
`org:tokens` and one of `org:read`, `org:write`, `org:admin`; a personal token (`sntryu_`) can mint nothing and cannot revoke itself, so
with one in `--from` the door refuses before any call and names the Organization Tokens page where a person makes
the token in one click (Sentry offers only `org:ci` there). `--preset read` (an internal integration token with
exactly `org:read`, `project:read`, `event:read`) is always made by a person, because Sentry creates an
integration's token only from a browser session. Either way the person puts the token in the slot (through
`observatory_secret_request` or `vault.py put` from a protected file), and `sentry.py install --preset
releases|read --vault PROJECT/ENV/NAME` verifies and records it. `sentry.py revoke --from PROJECT/ENV/NAME [--org
SLUG] [--base-url URL]` first verifies every narrow token of that organization again. The slot must hold a Sentry
token (a known prefix), and it is sent only to its own Sentry: `--base-url`, or the base recorded the last time that
slot answered there. While Sentry accepts it, the door records that proof, names the page where a person deletes it
and exits 3. It counts as revoked, and its slot is removed, only when Sentry refuses it (HTTP 401) after such a proof:
a 401 from a Sentry that never accepted the value proves nothing, and the slot is kept. The proof belongs to the
value, not the slot: it is recorded as the token's kind and a keyed digest (HMAC-SHA256 under a random key kept
owner-only beside the ledger, never a plain hash), so a slot whose value was replaced since is not sent to the
recorded Sentry, and a 401 to it needs a new proof. A base named with `--base-url` for the first time is said so,
and so is a `--base-url` that replaces a recorded Sentry whose proof no longer holds for the value; a value still
proved on one Sentry is never sent to another. `sentry.py status` follows the same rule: it asks about a broad token
only when the slot still holds the proved value, and otherwise reports it as unproven or changed without sending it.
Narrow tokens follow the same rule: `issue` and `install` record each one's keyed digest, `status` and both kinds of
`revoke` send a narrow slot's value only while it is still that value, and `revoke PROJECT/ENV/NAME` sends the
minting credential only while its slot holds the value proved on that Sentry (otherwise it names the `revoke --from
... --base-url` step that proves it again). `revoke PROJECT/ENV/NAME` deletes one token this door minted (or, for one a
person made, waits for that person), proves it dead and removes the slot; `list` and `status` say what exists and
whether it still works. Without `--base-url`, `issue` and `install` use `https://sentry.io` and say so on
stderr; `--base-url` takes a self-hosted Sentry over https; the region host (`us.sentry.io`,
`de.sentry.io`) is read from the organization, or given with `--region us|de` or `--region-host`. A region read from a
token or an answer must be the base itself or one of its documented region hosts, by exact name; any other host is
used only when you name it with `--region-host`. Redirects are never followed, so a token never travels to a host
nobody named. Sentry organization tokens have no expiry: that is recorded as the reason, not as a missing
record.

**Every minted credential expires, and the expiry is recorded** (OBS-191). `cloudflare.py issue`,
`openrouter.py issue` and `heroku.py issue` mint with an expiry 90 days out unless told otherwise:
`--token-expire-days N` for Cloudflare (its `--expire-days` stays the R2 bucket's object lifecycle),
`--expire-days N` for OpenRouter and Heroku, 1 to 365; `--no-token-expiry` or `--no-expiry` mints one
that does not expire. A Cloudflare roll and an OpenRouter rotation renew the lifetime the credential
was issued with; Heroku regenerates in place only when the authorization's expiry already matches what
is asked, and otherwise replaces it. The expiry is written where the credential's metadata already
lives: the vault slot's `meta.json` (`expires_at`, or null with `expiry_reason`), the OpenRouter and
Heroku ledgers, a plugin token's record. `vault.py list`, `use_secret.py names`,
`observatory_credentials` (each vault row's `expiresAt`, `expiry` and `expiryReason`) and each door's
`list` show it; a value put by hand reads "expiry not recorded", which is not the same as "never".
What is recorded is the provider's answer, never the request: an answer without an expiry field records
null with "the provider's answer did not confirm an expiry". The findings board raises
`credential.expiring` (warning) for a recorded expiry within 14 days and `credential.expired` (critical)
once it has passed, each with the door's rotate command. Heroku's `rotate` regenerates in place and cannot
change a lifetime; `issue` again replaces an authorization whose lifetime differs from what is asked.
DigitalOcean's API cannot mint a token: `digitalocean.py issue` prints the control-panel recipe with
an expiration to choose, and `install --expires-on YYYY-MM-DD` (and `rotate --expires-on`) records the
date the panel shows, or null with the reason when none is given.

**Narrow credentials on request** (OBS-191 parts 2 and 3). An agent that needs a credential files a
request instead of asking a person to mint one, and the administrator of this space decides it:

```sh
T="$(project-observatory full-path)/tools"
python "$T/credential_requests.py" presets                       # what each door offers on request
python "$T/credential_requests.py" request --agent urn:fabric:agent:acme:alpha-web \
    --provider openrouter --preset model-key --limit 5 --reset monthly \
    --slot alpha-web/prod/OPENROUTER_API_KEY --reason "summaries for the release notes"
python "$T/credential_requests.py" list                          # open requests, names only
python "$T/credential_requests.py" issue ID [--account LABEL]    # the door mints into the slot
python "$T/credential_requests.py" refuse ID --reason not-allowed
python "$T/credential_requests.py" revoke ID                     # at the provider, proved, then the slot
```

A request names the agent (`urn:fabric:agent:<publisher>:<id>`), the context (`personal`, the default,
or an organization's `org:ID`), the provider and one of the presets its door offers, what it reaches
(`--scope zone=example.com`), a ceiling where the provider has one (an OpenRouter key needs `--limit`,
reset daily, weekly or monthly), how long it should live (`--expires-in-days`, 2 to 30 in an
organization's context, default 30, and 2 to 365 in the personal one, default 90; or `--expires-at`),
the vault slot the consumer reads, and one line of reason. No value travels with it. It is the
contract's `credential-request/1` (fabric-agent-contract DEC-0111); `show ID --json` prints it and the
decision as `credential-decision/1`, the documents an organization's exchange carries, and a request in
the personal context has no request document because it never leaves this space. Refused before
anything is written: a reason or scope that carries a path or anything shaped like a credential, more
than 30 days in an organization's context, a provider or preset no door offers on request (Cloudflare's
`dns-edit` and `zone-analytics-read` name one token per zone, so issuing one for a slot would roll the
token another slot holds), a request in an organization's context from a space that is not enrolled in
that organization, and a second open request for the same slot and context.

`issue` is the administrator's `issued`: the issuing door (`openrouter.py`, `cloudflare.py`; Heroku,
DigitalOcean and Sentry are not offered on request yet) mints a narrowed credential in the account
`--account` names, ending no later than the request asked, in whole days counted from the issue (so it
ends up to a day before the end asked for: a 30-day request issued soon after it was filed lives 29
days), and delivers it straight into the slot in
its own process, so the value is never an argument, never printed and never written elsewhere. It is
refused, before any provider is called, when the slot already holds a value (a name already present is
never overwritten: refuse the request, or file one for another NAME), and, for an organization's
context, unless this space is enrolled in that organization and not removed from it. A provider answer
that does not confirm an end no later than asked is revoked again, the value deleted, and the request
stays open with the failed attempt recorded. The slot's `meta.json` then carries `provenance`: the
request, the issuing door and account, the agent, context and slot it was issued for, the provider,
preset, scope, ceiling, the end, and how to revoke it. `vault.py list` and `use_secret.py names` say
"issued on request ID for CONTEXT by DOOR/ACCOUNT", and `observatory_credentials` gives each such row
`issuedOnRequest`. A put or a rotation of the slot drops the provenance, since it describes one value.
`refuse` takes one of the contract's reasons (`not-allowed`, `preset-unknown`, `expiry-too-long`,
`scope-too-wide`, `provider-unavailable`, `unanswered`); `pending` records that it was seen and stays
open; the consumer may `withdraw` an open request. `revoke` revokes at the provider through the same
door the credential that request issued, by its identity (the OpenRouter key's hash, the Cloudflare
token's id, kept in the queue), so a key the name holds after a rotation is never touched; it proves
it by reading it back and deletes the slot's value only while it is still the one that request issued
(a revocation names its request, DEC-0113). A Cloudflare re-issue rolls a token in place, so when the
slot holds another value the token is left alone and the revocation reads "not proved". A refused
revocation is recorded and the request stays `issued`; an unproved one may be run again, or closed
with `revoke ID --settle "how it was checked"` once it was checked at the provider, which also closes
a credential a failed issue could not revoke. An issue the process did not finish (the door started,
no decision followed) is named on the next `issue` of that request instead of reading as a taken
slot, and while it left a value in the slot the request cannot be refused or withdrawn either. An agent acting on the operator's word adds `--relayed-instruction "the words"
--source WHERE`. Every request, decision, issue attempt and revocation is one line of
`credential-requests.jsonl` beside the vault's movement journal, names only, and the board raises
`credential.request_waiting` for a request nobody decided and `credential.revocation_refused` for one
whose revocation the provider refused or did not show done (`revoke` then exits 1), and for a
credential a failed issue could not revoke; `credential.request_queue_unreadable` says when lines of the
queue could not be read, so a damaged queue never reads as an empty one. A credential the door minted
and delivered before a later step failed is revoked again by its identity and its value deleted; a
value put by someone else while the door ran is never overwritten, and a removal deletes only a value
whose provenance names the request or that carries none (a value put by hand in the moment between the
door's delivery and the queue's own check is the gap OBS-466 closes). An unproved revocation
keeps the value, since its credential may still work. Sending a request to an organization in a check-in and
receiving its sealed envelope are not built yet: the queue serves the space that holds both the
request and the issuing door.

**A DigitalOcean team's tokens, one place per team.** `tools/digitalocean.py stash` keeps a team's
own token, read on stdin (or `stash --from-doctl CONTEXT|all`, which reads doctl's config and prints
nothing), owner-only in `secrets/digitalocean-admin/`; it is never handed out. `teams [--probe]`
lists each team with your role and the vault slots holding its tokens, `members --team T` gives the
role and member count (the public API lists no members), and `team create NAME --org T [--invite
EMAIL:ROLE]` is a plan until `--apply`. DigitalOcean's public API cannot mint a token: `issue
--preset P --team T --vault PROJECT/ENV/NAME` refuses and prints the control-panel recipe (the
preset's custom scopes; `presets` lists them all), and `pbpaste | digitalocean.py install --preset P
--team T --vault PROJECT/ENV/NAME` takes the minted token on stdin, refuses one of another team or
one that cannot do its preset's reads, delivers it and records team, preset and dates in
`secrets/digitalocean-issued/` (never the value). The presets are `observatory-read` (what
`full scan-digitalocean` reads; kept in `secrets/digitalocean-read.d/` for the collector),
`read-only`, `app-deploy`, `droplet-deploy`, `dns-edit` (DigitalOcean scopes are per team, so it
reaches every domain of the team), `registry`, `spaces-keys` and `billing-read`. `rotate LABEL`
(new value on stdin) and `revoke LABEL` count the old value as revoked only when `/v2/account` then
answers 401 to it. With `integrations.digitalocean` on, `full scan-digitalocean` (daily in the tick)
records per team the balance, Droplets with their monthly price, apps, databases without
credentials, volumes and domains in `store/raw/digitalocean.json`, naming each 401, 403 and 429.
`emit` turns it into `registry/digitalocean-account.json` (OBS-235), and the DigitalOcean page lists
every Droplet, app, database and volume with its project, team, state, stated monthly cost and
findings, beside Heroku under Infrastructure. A cost the listing does not state (an app, a database,
a volume) reads "not stated", never a zero. The Cloudflare page has the same six columns. A page
never connected names the switch and the door's commands; a scan that read nothing names why.
Two signals keep a team from going unread in silence (OBS-421): `full doctor` reports how many team
tokens are stashed (`digitalocean.teams_stashed`, counted from the records, never a value) and, while
`integrations.digitalocean` is off, an `attention` row `digitalocean-off` whose step is
`project-observatory full configure integrations digitalocean true`; and `teams` names each `doctl`
context whose token is not a stashed one, offline (team unknown), while `teams --probe` and the collector
ask DigitalOcean which team the context's token answers for (10 s each) and name a team with no stash
("member of team X via doctl context Y: no token stashed; `digitalocean.py stash --from-doctl Y`", in the
scan's `degraded` as `digitalocean:doctl:Y`). No value is printed, and a context name shaped like a
credential is not printed either.

**A DigitalOcean managed database's password, without printing it.**
`tools/digitalocean.py` uses the operator's `doctl` context. `db list` and
`db consumers CLUSTER` show metadata; the latter lists only apps whose spec binds the
cluster, so it is not a complete consumer inventory. `db credential CLUSTER USER --db NAME
--to vault:PROJECT/ENV/NAME` delivers a URL on stdin. `db rotate … --app ID --spec-env ID:KEY`
resets once and names local `use_secret.py serve` consumers that still need a restart.
A folder populated with `vault.py inject` keeps the old value until injected again.
Do not ask a provider MCP tool for credentials: its whole answer may be retained.

DigitalOcean rotation verifies every selected `--spec-env APP:KEY` literal before any reset.
Its name is not evidence: a plain value must equal the selected database user's current
provider password, or be a database URL with that password, user, engine and the provider's
connection host/port. Opaque encrypted values and unverified pool endpoints are refused;
migrate those consumers to App Platform database bindings before rotation. No command in
this flow prints the current password. Only the named consumers are moved, with downtime
between reset and ACTIVE; ACTIVE is not application health verification.

The door writes an owner-only receipt under `store/digitalocean-rotations/` before reset,
initially `reset: unknown`. A lost or malformed response, timeout or nonzero CLI result can
follow a committed reset: exit 5 means **unknown**, not “nothing changed”. Never repeat
`db rotate` to recover it. Use `db credential` to deliver the current value into the selected
vault slot, then reconcile each consumer without another reset. Exit 1 is a refusal before
reset; after an attempt, 2 means deployment unconfirmed, 3 vault delivery failed, 4 an app
update/redeploy or temporary spec cleanup failed, and 6 receipt update failed. The largest
code wins. Independent consumers continue after a confirmed reset despite another consumer's
failure. Provider/vault error bodies are withheld. A crash or failed final receipt write
leaves the earlier uncertain receipt; there is no automatic reconciliation, retry or claim
of a transaction across provider and vault. A failed temporary-file cleanup may leave its
owner-only spec file and is reported for local recovery.

Reset, app updates and redeploys each make one attempt; only read calls retry.
A lost/nonzero mutation response leaves that consumer `unknown`, even if the provider
already applied it. The receipt distinguishes this from `not_started` for a local
pre-call failure. Read current spec and deployment state before any retry; a blind
redeploy is not recovery for a literal. Independent peers still continue. Deployment
confirmation accepts a validated phase equal to `ACTIVE`, never a suffix or arbitrary
provider text; malformed metadata remains unconfirmed and its body is not printed.


Known-value scanning finds occurrences of values already known locally. It
cannot prove the absence of unknown, encoded or previously deleted values.
Rotation changes the local slot; provider revocation is a separate action.

Vault slot discovery reports unreadable ancestors and excludes symbolic links or special files as unmeasured. Session directory links are likewise unmeasured and are never followed.

The leak scan reads the transcripts of every agent CLI it knows, each from the tool's own
location (`paths.transcript_homes`):

| Agent | What is read |
|---|---|
| Claude Code | the configured `sessions` source; `history.jsonl` in `$CLAUDE_CONFIG_DIR` (default `~/.claude`) |
| Codex | in `$CODEX_HOME` (default `~/.codex`): `sessions/` and `archived_sessions/`, including compressed `rollout-*.jsonl.zst` files, and `history.jsonl`; in `$CODEX_SQLITE_HOME` (default `$CODEX_HOME`): `state_*`, `logs_*`, `thread_history_*`, `memories*`, `queue_*`, `goals_*` and `agent_message_board_*` `.sqlite` |
| Hermes | `$HERMES_HOME` (default `~/.hermes` followed by `$HERMES_DATA_DIR_SUFFIX`) and each `profiles/<name>/`: its `state.db` database and `sessions/*.json|*.jsonl` |
| opencode | `$XDG_DATA_HOME/opencode` (default `~/.local/share/opencode`): its `opencode*.db` databases (or `$OPENCODE_DB`) and the legacy `storage/**/*.json` tree |

These variables are read from the scan's own environment. On the tick that is the scheduler's
environment (the launchd job or systemd unit), not your shell profile: a `CODEX_HOME` exported
in `~/.zshrc` does not reach the tick, which then reads `~/.codex`. A `HERMES_HOME` outside
`~/.hermes` replaces it, as it does for Hermes itself: `~/.hermes` is then not read.

Every read-only or `file:`-URI open of a SQLite store goes through a percent-encoded URI. That
covers the leak scan's stores and Observatory's own store: `paths.sqlite_uri`, or `as_uri()` in
the few modules that build their own. So a folder name holding `?`, `#`, `%` or a space is read
as the path it is, read-only, and no empty file is created where the path used to be cut. (Opening
a WAL store read-only may still create its `-shm` and `-wal` beside it; see below.)

Observatory's own writer (`store/db.py`) opens its store by plain path, not a URI. A plain path
is not parsed, so it cannot be cut.

The URI spells the absolute path as given and never looks it up. `..` is kept, so
`C/link/../store.db` names the file the operating system and the writer reach through `link`.
Only a trailing slash (`state.db/` opens `state.db`) and `.` parts are dropped, as `pathlib`
drops them. A path under a symlink loop fails inside SQLite, as an error the caller already
handles, on every supported Python.

A home that is absent is not a degradation. A home that exists and cannot be read, a profile
that is a symbolic link or not a folder, a database that will not open, and a compressed
rollout this Python cannot decode (zstd needs Python 3.14) are named in `not_scanned` with
the reason. `coverage.transcript_homes` in `store/raw/leak-scan.json` lists each home as
`absent`, `scanned`, `partial` (the step's deadline left a file or a database under it unread)
or `unreadable`; `coverage.transcript_stores` lists each database with the rows it read.

A `.jsonl` is read from its last offset; any other file is read whole again when its size or
modification time changes.

A database is read from the highest rowid each table reached last time. The rows below that mark
are read again whole in three cases:

- the file was replaced;
- the row count or the sum of the rowids changed in a way the new rows do not explain, for
  example rows deleted, or a row inserted under the mark with a new rowid;
- the newest row was rewritten or its rowid reused.

A table with an update column is also read by that column. The finest-grained column wins, in
this order: `updated_at_ms`, `time_updated`, `updated_at`, `last_activity_at`,
`source_updated_at`. Rows above the last maximum are read again. A row at that maximum is read
again only when its digest is not the one recorded for it, so an edit within the same second
is seen and the newest row is not reported every tick. Up to 64 such rows keep a digest each.
Above that, one digest covers the whole set, and the set is read again only when that digest
changes, so a bulk update is reported once.

A table that cannot be read is a hole, never a clean table. Examples are a damaged page, a
missing module, and a table name that is not UTF-8. The store is named in `not_scanned` as read
only in part, with each table and the error's type, never SQLite's message, which can quote a
cell. Its other tables are still read and its marks do not move. Every SQLite store error is
reported by its type only, whether the store would not open, its schema is malformed, or a
table failed.

Some cases are handled without making the table a hole:

- **A column whose name is not UTF-8** cannot be put in a statement. It is named the same way,
  and the rest of its table is read.
- **VIRTUAL generated columns** are not read. They are computed from the columns that are, and
  evaluating one can fail. **STORED generated columns** are read, because their values are on
  disk.
- **A column named `rowid`** (or `_rowid_`, or `oid`) does not stand in for the real rowid: rows
  are paged on whichever of the three names the table's own columns leave free. A table that
  takes all three is read whole on every pass, so a value in one is reported every pass.
- **Text that is not UTF-8** is read as its bytes.
- **A store that fails in any other way** is named as having stopped while reading, with the
  error's type. The sightings counted before it stopped still stand, and the rest of the scan
  goes on.

Because the marks of a store read only in part never move, a table that can never be read keeps
every pass over that store a re-read from its old marks: complete, if it never had any.

**Seen only by the weekly complete pass, not sooner:**

- A row replaced under its own rowid below the newest row, in a table with no update column.
  This covers a delete and re-insert of the same id, and `INSERT OR REPLACE`.
- An edit that does not move the update column. Codex renames a thread with
  `UPDATE threads SET title = ?` and bumps nothing.
- An edit below a maximum that is already in the future.
- An update column holding values of mixed types; its maximum is compared within one type.
- A change of rowids under the mark whose count, rowid sum and sum of squares all collide. Two
  independent sums make this impractical, not impossible.
- Codex `thread_items`, which is upserted in place
  (`ON CONFLICT … DO UPDATE SET item_json …`) and whose `updated_at_ordinal` counts per thread,
  so for this scan it is effectively weekly. The rollout `.jsonl` files carry the same content
  and are read incrementally.

Reading a database is read-only, though not free of side effects. SQLite opens a WAL
database's `-shm` file, and may create `-wal` and `-shm` beside it. Rows are read 500 per
statement. The signature's aggregates, however (the count, the two rowid sums and the update
maximum), each scan the whole table in one statement. On a rollback-journal database a writer
can therefore wait for one such scan. Codex, opencode and Hermes run their stores in WAL mode by
default, and there a reader does not block a writer.

Not covered:

- Codex's `sqlite_home` setting in `config.toml`; only `CODEX_SQLITE_HOME` is read.
- Codex development databases under `~/.codex/sqlite/*.db`, which the installed binary does not
  name.
- The transcripts of other CLIs: Cursor, Gemini CLI, Kiro.
- Rows with a rowid of 0 or below: every read starts above rowid 0.
- Text stored in a column without text affinity (an `INTEGER` or `REAL` column holding text);
  only columns with TEXT or BLOB affinity are read.
- A `WITHOUT ROWID` table, or one whose own columns take all three of `rowid`, `_rowid_` and
  `oid`, is read whole on every pass, so a value in one is reported every pass.
- An FTS5 table whose name does not end in `_fts` is read as well as its `<name>_content`
  shadow table, so its text is counted twice. Only names matching `*_fts` are skipped as
  mirrors.

Leak discovery reports unreadable sessions, log directories, dashboard metadata and
tracked-tree listings as `source-unreadable`, preserving sightings from readable
siblings. Permission/stat/timeout errors never become an empty healthy source.
A package with no Git marker in its ancestry explicitly reports that it has no
tracked-tree source; a present marker whose Git listing fails is a coverage hole.
Missing optional paths remain absent. A source expected to be a file but resolving
to a directory, submodule entry or special file is explicitly unmeasured and is
never opened as a file. An interrupted or unreadable source is
unmeasured, not proof that it contains no credential.

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

The leak scan reads the companion's database the same way, when that file exists (an absent one leaves `coverage.companion_store` at `unread` in `store/raw/leak-scan.json`, with no warning): transcripts from their last offset, the
database from its last rowid per table (in `store/raw/leak-scan-state.json`), with the same reasons
for a complete pass. So a sighting is reported once, by the tick that first reads it, and stays on
record in the leak register until it is settled. `python "$(project-observatory full-path)/tools/scan_leaks.py" --full` reads everything again.

**The agent's OpenRouter key** (`python "$(project-observatory full-path)/tools/install_key.py" --for observatory`,
stdin only) is kept at
`store/.openrouter-key`, or under `OBSERVATORY_STATE` when you redirect the state. After such a
redirect, a key left at the old `store/` location is still read, with a note saying where it
belongs, and is never moved for you. If a key exists in both places, reading refuses and names both
files, rather than guessing which one is current. Remove the one you no longer use.

## SSH keys on this machine

`project-observatory full ssh-keys` lists the machine's SSH keys, makes a new one, copies the part you
paste and sends it to GitHub or a server. **The private key is never shown or moved**: Observatory
reads only a private file's clear header (the cipher name, and the public key the OpenSSH format
stores in clear), never its private section, and everything it shows, copies or sends is the public
line of `NAME.pub`, checked to be that key's own. The design is
[docs/design/SSH-KEYS.md](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/SSH-KEYS.md);
the scenarios are OSS-62–67.

```sh
project-observatory full ssh-keys list                    # every key: type, fingerprint, passphrase, where it is used and registered
project-observatory full ssh-keys create deploy-alpha     # ed25519; ssh-keygen asks for the passphrase; on macOS it goes to the Keychain and the agent
project-observatory full ssh-keys copy deploy-alpha       # the public line on the clipboard, and printed with its fingerprint
project-observatory full ssh-keys send deploy-alpha --to github                 # gh ssh-key add, once; --as signing|both
project-observatory full ssh-keys send deploy-alpha --to server:alpha --verify  # append to authorized_keys once, then prove the server accepts this key and no other
project-observatory full ssh-keys rekey id_ed25519        # a second machine: the plan for its own key where the other machine's is registered
project-observatory full ssh-keys findings                # no passphrase, weak, readable by others, unused, registered nowhere
```

- `create` never overwrites: a name already taken is refused before anything runs. `--no-passphrase`
  makes an unattended key (the findings list it); `--host ALIAS --hostname HOST [--user U] [--port N]`
  also appends a Host block to `~/.ssh/config`. On macOS the passphrase goes into the login Keychain
  and the key into the running agent; `--no-keychain` skips that. A passphrase is typed into ssh-keygen's own prompt, so
  `create` without `--no-passphrase` needs a terminal.
- `list` asks GitHub through `gh` (scopes `read:public_key` and `read:ssh_signing_key`); `send --to
  github` needs `admin:public_key` (`admin:ssh_signing_key` for signing). A source it cannot read is
  named with the command that fixes it, never shown as "not registered". `--no-github` skips it.
- `send --to server:ALIAS` uses the access that already works; `--interactive` lets you answer a
  password prompt. It only ever appends one line. `--verify` logs in with no config (`-F none`) and
  the new key alone, and passes only when ssh's log names the new key's fingerprint, so a Host block
  that still names the old key cannot make it pass. Clouds have no door yet (OBS-112): the refusal says
  so and the copied line goes in the provider's console.
- `rekey OLD` is for a second machine, whose restore wrote `~/.ssh/config` but not the other machine's
  private key. It plans first: this machine's own key (named OLD, so the carried `IdentityFile
  ~/.ssh/OLD` finds it; `--name NEW` for another name), and every place the other machine's key is
  registered: GitHub sign-in and signing, only where the account gh is signed in to (the plan names
  it) holds the other key's fingerprint (`--fingerprint SHA256:...` as `ssh-keys list` prints it
  there, or `OLD.pub` when it is here; without it GitHub is listed as not known, with `gh auth
  status` to check the account), and each Host of `~/.ssh/config` that names `~/.ssh/OLD`
  (`--server ALIAS` adds one; a Host logging in as `git`, such as GitHub Enterprise Server, gets
  that service's step instead). A config with `Match exec` is read as text: the plan never runs
  `ssh -G`, which would run that command.
  `--apply` creates the key, sends it to each place with `send`, verifying each server, and ends with
  what still accepts only the other machine's key and the next step for each. A server usually does
  not let this machine in until it has the key: answer a password with `--interactive`, or add the
  printed public line from the other machine, as the answer says. It removes nothing: the other
  machine keeps its key and its registrations. The other key's private file is never read.
- Every create and send writes a receipt to the key movements journal (`tools/vault.py movements`).
- `list` saves the inventory to `store/raw/ssh-keys.json`; the findings board reads it. Exit codes:
  0 done, 1 refused by the provider or the server, 2 refused before anything changed, 3 done without
  the clipboard, 4 done but the receipt was not written.

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

Agents keep their work memory here. `observatory_record` writes a note about one
project and `observatory_learn` a lesson inside one project; `observatory_lesson`
records a lesson for every project — a retro entry's class with the project's
pseudonymous key (`p-` and 12 hex digits of its root commit, never its name) and
the evidence commit. Every one is a proposal (confidence 0.5). A lesson becomes
`observed` without you when a second, distinct project records the same class and
both commits resolve in clones the registry knows on this machine:
`tools/corroborate.py`, which the tick runs, asks git for each commit in the clone
whose root commit gives the project's key. A key no registered clone carries is not
a witness; its lesson stays proposed and `corroboration.json` lists it as "commit not
resolvable here". If a promoted lesson later cites a different commit, or its evidence
stops resolving in two known projects, the observed record is archived and a new
proposed one replaces it (`outcome: replaced`), so it must be corroborated again.
`supported` is yours, through `review.py`.

### The review queue and its standing policy

Two queues wait for a decision: registry proposals (`observatory_propose`) and proposed ledger
conclusions. `project-observatory full review` lists them, `full digest` groups the conclusions,
and every decision you take there needs your terminal (there is no `--yes`). Since OBS-227 you may
delegate a closed set of safe decisions once, so the queue is reviewed every tick without an agent
running ([docs/design/REVIEW-POLICY.md](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/REVIEW-POLICY.md)):

```sh
project-observatory full review policy show       # the rules and what each would decide now; writes nothing
project-observatory full review policy enable     # at your terminal: enable exactly what show listed
project-observatory full review policy disable    # at your terminal
```

The rules: an exact duplicate of a pending proposal is superseded; a `resources`-only proposal from
an agent for a project in the registry is accepted, appending only the resources not yet recorded;
an `aliases`-only proposal is accepted when no name conflicts with a curated or a measured one; the
superseded readings of one project's day are folded out of your count (the ledger is not touched).
A `description`, an `organization`, `local_folders` and every conflict wait for you. The tick's
`review-policy` step applies an enabled policy, at most 500 decisions a run, each recorded as
`policy:<rule>@<hash>`, never as you, and journaled in `store/review-policy/decisions.jsonl`; with
the policy off it only counts what it would decide (`store/raw/review-policy.json`). Changing a rule
in `config/review-policy.json` makes the policy `changed`, and it decides nothing until you enable
it again. `full review undo-policy ID` or `--since TIME`, at your terminal, takes decisions back
and removes from the curation files exactly what they added.

What is left accumulates and is resolved in one launch:

```sh
project-observatory full review batch prepare     # store/review/batch.json: every item, a recommendation, a reason
project-observatory full review batch apply FILE  # at your terminal: what departs from the engine, one question, then all of it
```

An agent may read the batch file and edit each `decision` (`accept`, `reject`, `keep`) and
`reason`; applying it is yours. Before the question you see every item whose decision or reason
differs from what the engine recommends now, and every item that will not be applied because the
engine did not prepare it or it is no longer pending. An item that changed after the file was
prepared is skipped and named. A resource whose `added_by` names someone other than the agent that
proposed it, and any proposal with a credential-shaped field, wait for you instead of being decided
by the policy. The finding `review.queue_waiting` appears when 20 items wait for you or the oldest is 14
days old (`remind` in `config/review-policy.json`), and `full doctor` and the Health page's row
"review queue" show the policy's state, its last run and what waits.
Proposed lessons are kept 365 days (`retention.json` → `ledger.kinds.lesson`), and
an observed one is never erased by age. An agent finds lessons with
`observatory_search` and `kinds: ["lesson"]`, without a project. task-pipeline
writes them with `observatory_lesson` where the engine has it and as `lesson:`
notes through `observatory_record` otherwise. Lessons stay in
this workspace: they are agents' memory, not the project's. Lessons recorded earlier
as `lesson:` notes are converted once with
`python "$(project-observatory full-path)/tools/migrate_lessons.py" --apply`
(without `--apply` it lists what it would change and writes nothing).

The optional `observatory-log` plugin adds Claude Code hooks; other hosts can
use the CLI and MCP without those hooks. It declares three, and none of them can
block a turn or a prompt:

- **SessionStart** prints at most two lines about the project, only when one is
  actionable. It also keeps a snapshot of the checkout (each `git status` entry's
  status, size and modification time, and HEAD) as the session's baseline.
- **Stop** records the facts of a turn that changed a watched repository as a
  `proposed` ledger record, one per session, revised per turn. Only this session's
  changes count: the record's files are those that differ from the baseline, so a
  file that was already untracked or dirty when the session opened is never
  reported as its work, and a turn that changed nothing since the previous turn's
  snapshot writes nothing. A session with no baseline (it started before this
  engine was installed) takes the untracked files it first sees as already there.
  It writes the record first, then asks for the `why` of that record **once**, and
  only when the turn changed tracked content (an edit, a staged or intent-to-add
  file, a deletion, a commit): a new untracked file is recorded but not asked
  about until it is added. The snapshots live in
  `$XDG_STATE_HOME/project-observatory/turns/` (mode 0600, keyed by digests, no
  path inside, pruned after seven days). The recorder's git calls share an
  8-second budget and skip dirty submodule trees; a turn that runs out is left
  unrecorded, silently, and counted at the next turn. It does not ask
  when this session's own tools already wrote the rationale. That is checked
  after the record exists, within 2 seconds, from one pass over the last 4 MB of
  the session's transcript (the Stop payload names it). The pass collects the
  session's writing calls:
  - Edit, Write, MultiEdit and NotebookEdit, with the file each names;
  - Bash commands that write a file through `>`, `>>` (also `cat > file <<EOF`),
    `tee` or `sed -i`, with the files they write.

  The rationale counts in two cases:
  - **A decision register.** A file named `decisions.md` in any folder and any
    letter case, or a `.md`, `.markdown` or `.txt` file under an `adr/` directory.
    It must be outside vendored trees (`vendor/`, `node_modules/`, `third_party/`,
    `.venv/` and similar) and submodules, and not deleted, and the work must have
    added at least one non-blank line to it, ignoring whitespace. One of those
    calls must target that exact file, and its text must carry the addition: the
    entry's own heading (the shallowest heading the work added, so a Context /
    Decision / Consequences entry anchors on its title, not on a subheading), or,
    when the addition has no heading, more than half of the added lines. One
    common line is not enough. That heading must also appear in the register
    exactly once as a heading a Markdown reader would see (see below).
  - **The run ledger.** A `decision:` or `stage:` line in `.task-pipeline/run.md`,
    consulted only when one of those calls wrote the ledger, or ran task-pipeline's
    `stage_checkpoint.py`. The ledger's newest 64 KB and newest 200 such lines are
    read, including when git ignores the file, and the line's text must be in what
    those calls wrote.

  The pass skips calls whose result was an error, and subagent (`isSidechain`)
  lines. It does not scan a Bash command longer than 65,536 characters. The `git
  diff` it reads gets only the time left in the 2 seconds; a diff that needs longer
  (a register of megabytes) is asked about.

  The turn is asked about in every other case:
  - another session's edit in the same checkout;
  - a file the session only read or grepped;
  - a pull-request body that shares a heading;
  - a write by a tool the pass does not know (a copy, a script, `perl -i`);
  - `sed -i` behind a prefix (`sudo`, `xargs`, `env`), `gsed`, or a sed script
    containing `|` or `;` (the command is split there);
  - a relative path written after a `cd` in the same command;
  - a path whose letter case differs from the file's on a case-insensitive volume;
  - a subagent's edit;
  - a failed call;
  - a command longer than 65,536 characters;
  - no transcript;
  - a pass that runs out of its 2 seconds.

  When the rationale counts, a second revision of the same record links that file.
  The link is fixed in the evidence by four facts, not by the display anchor:
  - the repository's id;
  - the register's path in the repository;
  - the exact raw heading line, at full length;
  - the length and SHA-256 of the whole register file. No entry boundary is
    guessed.

  A heading longer than 120 characters is never linked, and the turn is asked
  about.

  Later revisions carry the link only while it holds **by bytes**, with no
  Markdown parsing:
  - this hook wrote it;
  - it names the current repository;
  - the stored line occurs exactly once as a whole line of the file;
  - the file is byte-identical to the linked one, or its first bytes are (same
    length and SHA-256) and what follows starts, at its first non-blank line, with
    a column-0 heading no deeper than the entry's own. That append is accepted
    only when the linked file ended in a newline outside any code fence and HTML
    comment.

  So appending a later entry keeps the link, and the record stays explained after
  the register is committed and pushed and after the write scrolls out of the
  transcript. Any other change to the file drops it, wherever it is: an edit to
  this entry or to any other, a paragraph or a deeper heading appended, a change
  of line endings, a `# comment` line inside a code block or HTML comment followed
  by an edit below it. So does a session that moves
  to another repository. A dropped link is attributed afresh only from the
  session's writes after that link was made (a mark of that point is carried in
  every later revision, so it holds for every later turn), and the turn is asked about when
  there are none. A wrong ask is accepted; a wrong silence is not.

  Every read of a register or the run ledger, fresh or carried, passes one gate.
  The path must be inside the checkout, not a symbolic link and not under one.
  The file is then opened once, without following a link and without blocking. The
  opened file must be a regular file with a single hard link, and at most its limit
  plus one byte is read (8 MB for a register).

  A fresh link still uses a Markdown heading scanner, only to choose the anchor.
  It is a linear pass over the file split on newlines only. A heading has at most
  three spaces of indent, one to six `#`, then a space or tab, and `## Use C#`
  keeps its `#`. Lines in fenced code are not headings: a fence closes only on
  the same character, at least as long, with no info string, and an unclosed
  fence runs to the end. Lines in a multi-line HTML comment are not headings
  either. The heading must be the shallowest one the work added and must appear
  exactly once. An added block whose heading syntax the scanner rejects is asked
  about, never matched line by line.
- **UserPromptSubmit** hands that request to the model with the session's next
  prompt, as context, once. It records the delivery and prints nothing when no
  request is pending. The context is capped at 4,000 characters. A request that
  does not fit is not marked delivered: it goes back to wait for the next prompt. The request waits in a private per-session file under
  `$XDG_STATE_HOME/project-observatory/ask-why/` (`~/.local/state/…` by default;
  mode 0600). The file is removed when delivered. An undelivered one older than
  seven days is removed the next time either hook writes or delivers a request. If
  the file cannot be written, the request falls back to a Stop notice. An engine
  older than this plugin, whose recorder does not decide whether to ask, also gets
  the Stop notice, never an injected prompt.

Every request is journalled in `store/raw/why-asks.jsonl` (it rotates to
`why-asks.jsonl.1` past 1 MB; both are read, so a record is not asked again when it
scrolls), and so is every delivery. A delivered request counts as answered when its
record later carries a `why`. A request that never reached the model, such as a
headless session with a single prompt, is pending and is never counted as
unanswered. The rate over seven days, answered ÷ delivered, is in
`store/raw/record-turn.json` (`whyAsks`: `asked`, `delivered`, `pending`,
`answered`, `rate`, `window`) and from
`python "$(project-observatory full-path)/why_asks.py"`. Below 10 % with at least
20 delivered requests, the board raises `observatory.why_unanswered`.
`why_asks.py reached <transcript.jsonl> <memory id>` says whether a request
reached the model in a given session transcript.

For Claude Code, run `project-observatory full agent install`. It adds the
GitHub marketplace `passioncode-ai/project-observatory-dashboard`, installs
`observatory-log@observatory-log`, turns plugin auto-update on (opt out with
`--no-auto-update`) and writes `OBSERVATORY_ROOT`, `OBSERVATORY_HOME` and
`OBSERVATORY_PYTHON` into Claude Code's user settings for the hooks. `full agent status` shows the
installed and shipped versions and anything the hooks would miss; `full agent
uninstall` reverses it. The hook environment it writes is `OBSERVATORY_ROOT`
(the engine), `OBSERVATORY_HOME` (the workspace) and `OBSERVATORY_PYTHON` (the
interpreter that runs the hooks: the engine's venv `bin/python3`). When a venv rebuilt on
another Python leaves `OBSERVATORY_ROOT` naming a folder that is gone, the hooks find the
engine under the interpreter's venv and `full maintain ensure` rewrites the value (OBS-393). `install` says "installed" only after Claude Code's
own `installed_plugins.json` lists the plugin, and reports the version it lists
(`installed_version`) beside the one this engine ships; a `claude` that exits 0 without
installing is refused and nothing is written to settings. `uninstall` removes the keys
it added and the `env`, `enabledPlugins` or `extraKnownMarketplaces` objects they leave
empty. Installing by hand inside Claude Code (`/plugin marketplace add
passioncode-ai/project-observatory-dashboard`, then `/plugin install
observatory-log@observatory-log`) leaves those three variables unset; without `claude`
on PATH, `install` prints their values and the settings file to put them in. A
directory-sourced marketplace from an earlier setup is
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
127.0.0.1:47311; it answers GET only and changes nothing, except `POST /mcp`, the
lifecycle tools of [node.lifecycle](#which-apps-an-agent-may-wake-stop-and-restart-nodelifecycle),
which serve only callers you allowed).

The Keys and ENV pages' buttons — mint, cap or revoke a provider key, mark a leak,
reveal one inventoried value — act only when the pages are served by the
token-guarded credential server, `python "$(project-observatory full-path)/tools/keyserver.py"`
(127.0.0.1:7717 by default; `--port N`). Opened any other way, those pages hand over
the command instead. A vault value is never put or rotated from a page; that is
`tools/vault.py` on stdin.

The server `--serve` starts runs detached, so closing the terminal does not end
it. Stop it with:

```sh
project-observatory full open --stop              # finds the port this workspace's server reported; --port PORT names one
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

With the agent on, `full doctor` shows an `agent` section with `model_status` (`no-model`,
`no-budget`, `no-key` or `ready`), `key_status`, `key_source` and the `next` commands still
missing, the key step included; `project-observatory full assistant status` says the same
without spending. An `OPENROUTER_API_KEY` in the environment of the process counts as the
assistant's key and is used ahead of the key file (`key_source` then says `environment`):
a personal key exported in your shell profile is spent by the assistant and the agent. On macOS the
tick and server installers write workspace-specific jobs only after the scheduler
is explicitly enabled; Linux can run `project-observatory full tick` under a
supervisor chosen by the user. The maintenance job (every 15 minutes: the disk guard, updates, the daily backup) is
separate and scheduled by default: see [Staying in step](#staying-in-step). Do not create duplicate writers for one home.
On macOS, `tools/serverd.py --install` also writes a `fabric-service/0.1`
descriptor (`project-observatory.<instance>.json`, in
`~/Library/Application Support/ai.passioncode.fabric/services/` or
`FABRIC_SERVICES_DIR`) so a local host such as Fabric Dashboards can find the
server; `--uninstall` removes it. The server keeps one copy per workspace
(`service.lock`; a second copy exits 75) and creates `service.token` for its
events feed. See the [service design](design/FABRIC-SERVICE.md).

Its well-known document also says what Observatory uses (`extensions/connections/0.1`),
so a host can draw the chain from one agent to the next: Fabric's hub (asked which Fabric
project each folder is), the release feed (the update check), one connector for each
provider integration you turned on (GitHub, Bitbucket, Cloudflare, Heroku, DigitalOcean,
Google, OpenRouter, domains, the MCP servers your agents' configs declare) and, while anonymous usage data is
collected, the analytics host. Each carries its state (`ok`, `degraded`, `down`, `unknown`)
and when it last ran, from the collector's own receipt in `store/raw`; an integration you
never turned on is not listed, and one that never ran reads `unknown`. A receipt older than three
times its collector's interval in the tick, and never less than six hours, reads `degraded`: its
collector has stopped running, and the tick's log says why. With `OBSERVATORY_OFFLINE=1` set for the
server no provider is listed, as no collector runs. Fabric's hub reads `unknown` when Fabric is not
installed and `degraded` when the hub refused anything else. No key, URL with a
key, path or name is in it. Not declared yet (OBS-338): git remotes (`ls-remote`), remote environments, GA4, Search Console, Cloudflare analytics, the assistant's model provider, the organization server, relay and mailbox, and release downloads (only the feed is declared).

```sh
project-observatory full configure features scheduler true
ENGINE="$(project-observatory full-path)"
python "$ENGINE/tools/install_launchd.py" install      # the tick, every 30 minutes; status | uninstall | run-now
python "$ENGINE/tools/serverd.py" --install            # the always-on dashboard server; --status | --uninstall
```

A launchd job does not inherit your shell. The installer writes a minimal `PATH`
into the job: only the directories of the `PATH` it runs with that hold one of
the tools the engine calls (`git`, `gh`, `heroku`, `wrangler`, `node`, `claude`,
`dig`, `curl`, …), followed by the system directories — every entry absolute,
existing and writable by no other account (group-writable only when you or root
own it, as Homebrew's directories are). Plugin and version-manager directories
your shell collected stay out. The interpreter is named by its virtual-environment
path or Homebrew's `opt` link, never a versioned `Cellar` path, and the installers
refuse a plist that names one. After installing a tool in a new directory, run
both installers above again.

The tick is bounded: every step runs in its own process group under a wall-clock
limit (15 minutes, 20 for the leak scan), and a collector also takes at most its share
of the ceiling (40 %; the leak scan 30 % and the `.env` inventory 20 %, both of which resume
where they stopped; `tick_budget.py`), so no one collector uses the time the others were due.
The whole tick stops at a 25-minute ceiling below its 30-minute interval, and
`store/raw/tick-run.json` records each run's start, end, outcome and reason
(`store/raw/tick-runs.jsonl` the last 50). A stopped step is a failed step in the log and on
the board, never a hung tick.

The time after the collectors is measured, not fixed. The collectors leave the tail what its
steps have needed: the 90th percentile of each one's last 20 runs, plus a quarter, never less than
the 5 minutes it used to keep (a fifth of a shorter ceiling) and never more than 40 % of the ceiling.
The `.env` inventory and the production configuration fetch stop at their step's deadline and
resume on the next tick; `store/raw/env.json` and `remote-env.json` are replaced only by a complete
pass, which says how many ticks it took (`pass`). Inside the tail every step leaves the steps that
must finish after it (the merge, the emit, the validator, the indexes, the ledger, the rollup, the
findings, the dashboard and the notice) their measured time, and never less than a minimum (10 s for
the validator, 30 s for the findings, 20 s for the dashboard, 5 s for the others), so a slow `events`
stops early rather than leave `findings` a few seconds, and a step that must finish always gets its
own time. When the merge, the emit or the validator runs out of time, the tick no longer stops there:
the findings and the dashboard are built over the registry as it stands (validated first when the
validator had the time; a validator that answered red still stops the tick), the index, the ledger
and the rollup run as usual, and the steps that read a fresh registry, its commit among them, wait
for the next tick; `tick.json` says `registry_refreshed: false` and the run is `partial`. A temporary
file a stopped writer left in `registry/` is removed before the registry is committed.

The store's integrity is checked first in every tick, but the full `integrity_check` runs once a
day. In between, while a full check has passed within the day, the light check reads the file's
header, its schema and one row of every table (the full-text index among them), a few pages rather
than the whole file; `store/raw/integrity.json` names which ran (`pragma`). After `damaged` or
`unopenable`, with no full pass on record, or once the last one is a day old, the full check runs on
every tick until one passes, and a check that did not finish keeps the verdict it found (`attempt`
names it), so a light `ok` never stands in for a full pass. An `ok` is reported as stale from the
last full pass as well as from its own time.

For stage diagnosis, `store/raw/tick.json` adds `progress`: a run ID and bounded
stage rows with start, end, wall duration, exit and outcome. It includes silent
tail commands such as merge and emit. During a run the top-level `finished_at`
and failed-step report still describe the previous completed report. After the
child stops, the supervisor copies only matching, validated stage metadata into
`tick-run.json` and its history as `stage_progress`. Read `coverage` first:
`unknown` means missing, stale or invalid evidence; `partial` includes a start
without a terminal receipt or a failed receipt write. Such a stage has unknown
outcome and no inferred exit or duration, even when the whole run timed out.
`not_started` requires the watchdog's explicit budget refusal. A row's start and
duration cover the watchdog call, including overhead; an absent row proves
nothing about whether a stage ran. These fields contain no child output or
command arguments. Receipt failures preserve the command's original exit.

The ceiling reserves time for fresh findings and a fresh dashboard, without
guaranteeing that those stages complete: the collectors stop 5 minutes before it (at most a fifth of the
ceiling), the tail's optional steps — plugins, rollup, the agent, the vector index,
retention and review — stop 2 minutes before it (at most a twelfth), and the core
from `findings` to the registry commit uses what is left. A step that did not run
leaves the previous run's output in place, and the collectors take turns: after the store check they
run least recently started first, so one that was not started goes first on the next tick; one not
started for 3 ticks in a row goes ahead of all others, and one whose feature is off runs last, so it
never pulls the step it reads from to the front
(`store/tick-rotation.json` keeps, per collector, when it last started, how it ended — ran, failed, stopped or
not started — how many ticks in a row it was not started and when it last ran; `tick.json` names the order, what was
not started, and each failed step's `outcome` beside its exit). A cycle in the collectors' same-tick reads is named
in the tick log and the planned order runs. Each step is told its own deadline in
`OBSERVATORY_STEP_DEADLINE` (epoch seconds); the folder scan uses it to write what
it measured and carry each folder it did not reach, marked `carried_from`.

### Memory embeddings leave the machine only by your consent

Agent memory is searched on this machine by word forms (Russian and English). A remote
embedding model (OpenAI, configured in `config/models.json`) adds similarity search, and it
sees the text it embeds. So since PB-137 N-003 nothing is sent until you consent, one
project at a time, at your own terminal:

```sh
project-observatory full embedding-policy show                                # what is in force; nothing by default
project-observatory full embedding-policy grant PROJECT --statement "TEXT"    # public + project-internal by default
project-observatory full embedding-policy revoke PROJECT                      # stops new export at once
```

`PROJECT` is a project id such as `project:alpha`. `TEXT` is your consent in your own words,
naming the provider, for example `texts of alpha are sent to OpenAI`.

The `embeddings` feature and an embedding key are needed too. A consent never covers
`confidential` text, workflow checkpoints and handoff packs, records without a project, or an
agent's search query: those stay on the machine whatever the policy says. `full doctor` shows
the policy under `embedding_policy` (`state`, the projects `inForce`, `summary`) and every vector
index under `vector_namespaces`: the model it belongs to and its state, where the pre-namespace
OpenAI index is `legacy` and a new local one stays `inactive` or `ready` until it is accepted. A policy file
that is broken, or older than the last one applied, is refused, and then nothing leaves. The
rules and their reasons: [design/EMBEDDING-POLICY.md](design/EMBEDDING-POLICY.md).

### Who may reach memory: the local agent, and the bindings you issue

The agent your own configuration starts over stdio is the **local agent**. It reads every
project and writes only proposals, as before. Any other caller reaches memory only through a
**binding** you issue at your terminal (PB-137 N-008). A binding names one principal, its
projects, its scopes, the highest class it may read and whether it may write proposals, and
it always expires. It reaches only the memory tools, and, with the scope `ops.digest.record`,
the one tool that records a product's daily operations digests (below). Everything else (the
estate, credentials, the machine) stays with the local agent.

```sh
project-observatory full access-binding show                        # who may reach memory; nobody by default
project-observatory full access-binding issue PRINCIPAL --project PROJECT --token-file PATH
project-observatory full access-binding revoke BINDING_ID           # refused from the next call
```

- `PRINCIPAL` is `agent:<name>` or `service:<name>`, and `PROJECT` is a project id. `PATH` is a
  new file the bearer is written to, owner-only.
- The bearer is never printed and the registry keeps only its SHA-256. Give the client the
  file, never its contents in a chat.
- `issue` defaults to reading (`memory.read`, `memory.search`), class `project-internal` and 30
  days. `--scope`, `--class`, `--effect propose`, `--workflow` and `--days` change that.
- `full doctor` shows the state under `access_bindings`, and the Health page shows the same.
- Every decision about a binding is logged in `store/logs/access.jsonl`, without the bearer.
- `project-observatory full forget MEMORY_ID --why "TEXT"` erases a record's text everywhere
  this workspace holds it and prints a receipt per backend; `--plan` shows what it would
  touch first. Backups keep the text until they rotate, and the receipt says so
  ([design/FORGET.md](design/FORGET.md)).
- Every memory search leaves a receipt in `store/logs/retrieval.jsonl`. The receipt holds the
  exact results and the scope, and the query only as an HMAC; it holds no text. The caller
  that received a receipt can read it back with `observatory_explain`. Memory answers are
  redacted on the way out too, and say so in `redacted`
  ([design/RETRIEVAL-AUDIT.md](design/RETRIEVAL-AUDIT.md)).
- `project-observatory full memory-http` serves memory over loopback HTTP (port 47313) to
  callers holding a binding. A client sends its bearer as `Authorization: Bearer …` from the
  file you gave it. Without a valid binding every request is refused before it is read
  ([design/HTTP-TRANSPORT.md](design/HTTP-TRANSPORT.md)).

Rules and reasons: [design/ACCESS-BINDING.md](design/ACCESS-BINDING.md).

### Products' daily operations digests: ops.digest.record

A product that keeps an operations log folds each closed UTC day into one line of counts,
`ops.digest.daily@1` (fabric-agent-contract DEC-0153): operations by outcome, the operations
whose failures rose, and nothing else. Fabric collects those lines on the machine and sends
each one to Observatory with the MCP tool `ops.digest.record` (shown as `ops_digest_record`
where a client turns dots into underscores). Its input is `digest` (the line as a JSON object),
`source` (the label the line was read under, `app` by default) and an optional `service`
(`{id, instance}`, the Fabric service it came from).

The project is never an argument; it comes from the connection:

- **Over stdio**, from an access binding you issue for that one project, with the scope and the
  effect the tool needs. `issue` prints the server line that goes with it:

  ```sh
  project-observatory full access-binding issue service:fabric --project PROJECT --scope ops.digest.record --effect propose --days 365 --token-file PATH
  # then the connection starts the server with: .../mcp/server.py --stdio --binding-token-file PATH
  ```

  A stdio server started without `--binding-token-file` (your own agent's, for one) answers
  `binding-required`: the local agent is not a product and names no project.
  `issue` refuses such a binding when it names more than one project.
- **On the memory-http door**, from a delegation token Fabric's hub minted for
  `project-observatory` with the scope `ops.digest.record` and the project the lines are of:
  a `project:<slug>` this registry holds, or the `fabric:<uuid>` Fabric's own attribution
  records for one project (a curated alias is not taken, and the review policy never accepts
  an alias for a `fabric:` name by itself: it waits for you). Without a token, the answer is
  `delegation-required`.

Observatory checks every line against the contract (its closed schema, the arithmetic of
`FAC-SEM-166`, and `FAC-SEM-167`: no operation named like a key or a home folder), takes only
closed days within the last 400, and checks that a named `service` is one the registry
attributes to that project. It keeps one record per project, service, source and day under
`store/ops-digests/` (owner-only): the same line again changes nothing (`repeated: true`), a
different one becomes the next revision, and the first one is the day's line. A record keeps
at most 16 revisions (`revisions-exhausted`), a project's day at most 8 source labels
(`sources-exhausted`) and a month file at most 64 MiB (`store-full`). Days older than 400 are
dropped. In those counts, the `ok` of a host's `retro-after-work` operation counts the
retro requests answered by a retro the host saw written, never the retros themselves.

The daily learning review (next section) is the reader of these lines.

### The daily learning review: what agents learned, what failed

Once a UTC day the tick writes one local report of what agents recorded about what they
learned and what failed, by arithmetic and with no model. It reads:

- **lessons** (`observatory_lesson`): each lesson minted, revised, replaced, promoted or
  invalidated that day, with its class, its pseudonymous project key, the project the key's
  clone belongs to when a registered clone carries it, and its record
  (`ledger:<memoryId>@<revision>`, readable with `observatory_recall`);
- **classes held by two or more projects** (`class-repeated`), with what the corroborator says
  of them: promoted, waiting for a second project, not resolvable here, or contradicted by git.
  Only the corroborator promotes a lesson, by its own rule. When no tick that refreshed the
  registry ran it within a day, the review runs it (bounded in time; it promotes there but never
  archives an observation), so a lesson two projects hold is promoted at least daily;
- **products' daily digests** (previous section): every rise the product itself recorded in an
  hour (`risen-in-hour`), an operation whose failures reached at least 10 and at least twice the
  day before (`rose-since-yesterday`), and one that rose on at least 3 of the last 7 days
  (`rising-repeatedly`); a line sent after its day's report was written is reported once, in
  the next report (`arrived-late`). Only the first revision of a day's line is read;
- per project, the lessons recorded and the `retro-after-work` counts as asked, answered by a
  retro the host saw written, refused and not seen. They are never read as retros written.

The day is a UTC day, as a digest's is. The report of a day is due at 06:00 UTC the next day,
so an overnight machine's catch-up arrives first; the hour is a setting:

```sh
project-observatory full configure learning_review settle_hour_utc 9
```

A complete report is never rewritten by the tick. A report a source could not be read for is
`partial`, says which source and why, and is written again when what it reads changes (or every
6 hours) until it is complete or until a day later. A damaged digest month that only the search
for late lines reaches is named without making the report partial, and a report file that no
longer reads is moved aside as `<day>.damaged-<n>.json` before the day is written again. After the machine was off, up to 7 missing days are written, oldest first;
older ones are named under "days not reviewed". To read and run it:

```sh
project-observatory full learning-review show              # the newest report; prints where its page is
project-observatory full learning-review show --json       # the report itself
project-observatory full learning-review list              # the last 14 days: state, revision, counts
project-observatory full learning-review run               # write what is due now
project-observatory full learning-review run --day 2026-11-05 --again   # a new revision, keeping the previous
```

The report is `store/learning-review/<YYYY-MM>/<YYYY-MM-DD>.json` with the same report as a
self-contained page beside it (`.html`, no script and nothing loaded from elsewhere),
owner-only, kept 400 days. Every item names its source. The report carries no path, account or
person, a lesson's text is scrubbed again of known values before it enters, and nothing is sent
anywhere: not to the registry, the wiki, the board or Fabric. Not yet read: the `insights/`
lines and the digest lines Observatory refused (the report's notes say so); the review is not
yet on Health, in `doctor`, in the findings or over MCP.

### Give an agent a key without pasting it into a chat: the secret prompt

An agent that needs a key the vault does not hold calls `observatory_secret_request` (project, env,
slot, why, and optionally the provider and `replace`) instead of asking you in its chat (OSS-84,
OBS-260). Observatory files the request with its local server and asks you in its own window; the
agent learns only the outcome (`stored`, `declined`, `no-answer`, `unconfirmed` or `no-client`),
never the value. The node names the agent from a **binding** you issue when you connect it, never
from text the agent wrote:

```sh
project-observatory full agent-binding issue claude-code     # at your terminal; prints the MCP line with --agent claude-code
project-observatory full agent-binding show                  # connected agents, never a token
project-observatory full agent-binding revoke claude-code    # refused from the agent's next request
```

- `issue` writes a random token to the owner-only file `config/agent-bindings/NAME.token` and keeps
  only its SHA-256 in `config/agent-bindings.json`. It prints the `claude mcp add` line with
  `--agent NAME`; an existing registration only needs `--agent NAME` added after `server.py`.
  Issuing again for a connected agent keeps its token.
- **The limit.** The token file is owner-only, so any agent running as your user can read another
  agent's token and file a request under its name. The window still shows the project, the slot and
  the reason, and nothing is stored unless you type it. Running agents as another user, or in a
  container, is the stronger defence.
- The local server keeps the queue in `<state>/secret-requests/` (owner-only): one open request per
  slot (the same agent asking again joins it; another agent is told the slot is busy), five new
  requests per agent per hour, ten minutes for you to answer. A slot that already holds a value is
  refused until the agent asks with `replace: true`; the window then asks whether to replace it and
  names its last change. Each filing, answer and refusal is one line in `events.jsonl` there, names
  only. `stored` is reported only when the slot shows a write after the request; otherwise the
  request reads `unconfirmed`.
- **Declining through an agent.** You can also tell an agent to decline a waiting request ("decline
  the request for the deploy key"). It carries your words to the local server's `secret.decline`
  (contract DEC-0147); the request is answered `declined`, its window closes, and the agent that
  asked reads that you declined it through an agent, not in the window. Without your words nothing
  is declined. An agent declining another agent's request learns only the outcome, not who asked or
  for which slot. Your words are kept in `<state>/secret-requests/decisions.jsonl` (owner-only, two
  days), never in `events.jsonl` or an answer. An agent that withdraws its own request never reads it as yours.
  The tool is on the local server's agent surface (`POST /mcp`, the agent's binding); the
  `observatory` MCP server an agent runs does not relay it yet (OBS-298).
- **Refusals that file nothing.** The local server is down: "Observatory is not running", and no
  window appears. No desktop session (a server, a remote shell): "no desktop to ask the person on",
  with the step for you: `python "$(project-observatory full-path)/tools/vault.py" put PROJECT ENV
  NAME < FILE` in your own shell, the value on its stdin. There is no terminal answer to an agent's
  request, and no scripted dialog (`osascript`, `zenity`, `kdialog`): any program of your user could
  draw an identical one.
- **The window, on a Mac.** Enroll Observatory's app with the node's lifecycle broker once:
  `project-observatory full lifecycle enroll observatory.app --kind app --path PATH --caller service:observatory --startup-effect TEXT`,
  with PATH the installed app (usually `/Applications/Project Observatory.app`, quoted in your shell).
  A request then asks the broker to start the app (or finds it running); the local server never
  launches it itself. The app reads waiting requests every five seconds through its own child
  process (`project-observatory full assistant secret`), brings its window forward and shows the
  agent from its binding, the project, the slot, the reason quoted as the agent's words, whether
  the slot already holds a value (Replace), and **your phrase**. Cancel or closing the window
  answers `declined`. Save hands the value to that child process on its stdin, and the child
  writes it into the slot the way `vault.py put` (or `rotate`, for Replace) writes it; the value
  is never in an argument, the environment, a log, a notification or anything the agent reads,
  and the local server sees only the slot's metadata afterwards. The child answers only when its
  own file lies inside the enrolled app's bundle and its immediate parent, while running,
  satisfies the enrolled app's code requirement (its team and bundle id); anything else is
  refused `client-unverified`. A program of your user that deliberately runs as the app's direct
  child (the engine named in the app's settings, for one) can still pass, which the limits below
  already allow: it can read and write the queue and the vault slots anyway.
- **Only through the engine inside the signed app, and none ships yet.** That parent check proves
  only that the request came through the app's own process tree, and whatever the app runs is in
  that tree: an engine named by the program path in its settings, PATH or a launcher could be
  changed by any program of your user, which could then read the queue, answer requests and see
  what you type. So the window never uses that program: it runs only an engine inside the app's
  own bundle, by a path the app computes, after checking the app's own signature (resources
  sealed, the team its sealed Info.plist names) right before each call, with a scrubbed environment. **No release
  bundles that engine yet, so today every request is answered `no-client`, reason
  `engine-untrusted`, with your `vault.py put` step, before anything is filed and before the app
  is opened.**
- **Your phrase.** At the first request the window asks you to set a short phrase; change it in
  the app's Settings. Every genuine prompt shows it, so a window without it is not Observatory's:
  cancel it. It is kept in the data protection keychain, in the app's own access group, which no
  other program of your user can read, and never in a vault slot or a tool result. The window is
  kept out of screen sharing and of capture that honours a window's sharing setting, and the
  phrase is left out of the accessibility tree (a neutral label stands in for it). The limit: a
  program you granted Screen Recording may still capture the window on recent macOS, so do not
  grant it to an agent's host. That keychain group needs the app signed with a provisioning
  profile, and **this release's app is not**: it keeps no phrase, shows no window and answers each
  request `no-client` with your `vault.py put` step (the release will fetch the profile through
  the App Store Connect API in a follow-up).
- **Not on Windows or Linux yet.** Windows has no lifecycle broker in this release (OBS-277), and
  the window is opened only through it, so a request there is answered `no-client`, reason
  `broker-unsupported`. On Linux the local server cannot verify that a window is Observatory's own
  signed app, so the answer stays `no-client`. When the desktop app's window ships, its phrase is
  kept with DPAPI on Windows and the Secret Service on Linux, which any program running as your
  user can read: there the phrase only stops an agent that does not look for it.
- **The limits, on the Mac too.** The request queue is an owner-only file, so any program running as
  your user can read and write it: read the requests' text, file a request under any agent's name,
  or mark a request answered. It cannot see a value typed into the genuine window. The signature
  check hashes the app once, and a Python engine reads its files as it imports them, so an engine
  will be bundled only once it cannot change after the check. A stored value sits in the vault
  slot, which any program of your user can read: the prompt keeps the value out of the agent's
  chat, transcript, logs and tool results, not out of reach of a program that goes looking.
  Running agents as another user, or in a container, is the stronger defence.

Design: [design/PERSON-SURFACES.md](design/PERSON-SURFACES.md), D4.

### Share a key with another person's Observatory

You can give another person's machine a key from your vault, see how their agents use it, and take
it back (contract DEC-0142 `fabric-peer-share/1`; OSS-79 to OSS-81; OBS-264, OBS-276). Neither of you
needs an organization. The value is sealed to a key their machine made for this share alone; a
**mailbox** you name carries the sealed copies, the codes' exchange and the usage reports, and sees
only ciphertext and names. Deploy your own into your Cloudflare account once with `full cloud
deploy` (below); `full share create` then uses it, or `--mailbox https://...` names another.

The sender, at their own terminal:

```sh
project-observatory full cloud deploy               # once: your mailbox in your Cloudflare account
project-observatory full share create alpha-web prod OPENROUTER_API_KEY --to Alex
#   prints the link once: https://MAILBOX/s/SHARE-ID#c=CODE&k=FINGERPRINT; send it to Alex yourself
project-observatory full share status SHARE-ID      # once Alex accepted: a code is ready
project-observatory full share confirm SHARE-ID     # type the six digits Alex reads to you: a match seals and sends
```

The recipient, at their own terminal:

```sh
project-observatory full share accept               # paste the link when asked; read what the sender will see, type yes
project-observatory full share accept SHARE-ID      # or accept a link you told an agent to open (below)
project-observatory full share status SHARE-ID      # in your terminal: the code to read to the sender, then the keys
python "$(project-observatory full-path)/tools/use_secret.py" run --share SHARE-ID OPENROUTER_API_KEY -- COMMAND
```

- **The code.** Both machines commit, then reveal, and show the same six digits only if nobody sat
  in the middle. The digits appear only in a terminal: never in `--json`, a pipe or a file a program
  reads. The recipient reads theirs to you by a call or a thread you already trust, and `confirm`
  asks you to type what they read (not what your own screen shows). A match seals; a mismatch seals
  nothing and spends the link. `confirm ID --without-comparing` asks you to type "share without
  comparing", seals anyway and the share reads "shared without comparing": whoever claimed first
  may hold the value. One attempt per link: a second claim, a link opened twice on one machine or an
  expired link (at most 7 days) burns the share, and a new attempt is a new share.
- **They differ.** `full share differ ID` on either side. Before the key is sealed, nothing is
  sealed and the link is spent. After it is sealed, the sender's `differ` takes the share back (as
  `revoke` does): once the mailbox holds the revocation it refuses every renewal, and the recipient's
  machine stops at its next renewal, within one lease. If the mailbox does not answer, `status`
  says so, and every later pass (by hand, or the maintenance job's within the hour) hands it the
  revocation again until it shows it. The recipient's `differ` deletes its own copies at once and
  warns the sender.
- **The link.** `accept` asks for the link rather than taking it as an argument, which keeps it out
  of your shell history; it is opened once on one machine, and a second machine needs its own share.
- **Asking an agent to open a link.** Tell an agent "open this share: LINK" and it hands the link to
  your local server's `share.open` with your words (contract DEC-0147). Nothing is claimed: `full
  share status` shows the share `handed-over`, and accepting stays yours, at your terminal, with
  `full share accept SHARE-ID`: it names the mailbox the link points to and whether the sender's key
  is pinned, then you read what the sender will see and type yes. `full share differ SHARE-ID`
  refuses a handed-over link instead: it is deleted and nothing is sent. At most 20 handed-over
  links wait at once. The sender's key is
  pinned from the link only when the link is in your words unchanged; a link the agent found
  elsewhere (a page, a mail) is opened without the pin and rests on the six-digit code. A link
  nobody accepts lapses 7 days after the hand-over. The claim code is in the agent's transcript
  until the claim spends it, so accept soon and compare the code: whoever claims first otherwise
  could. Your words are kept in the local server's owner-only audit record with the link's
  fragment replaced by the share id. Like `secret.decline`, the tool is on the local server's agent
  surface and the agent's own MCP server does not relay it yet (OBS-298).
- **The person's acts, and their limit.** `confirm`, `accept` and `provider-ended` need a terminal,
  and none takes its answer as an argument. That stops an agent that runs commands the ordinary
  way. It does not stop a program running as you that drives a terminal of its own (a
  pseudo-terminal), reads your screen or reads the share's files under `node/shares/`, where the
  code is kept so `confirm` can compare it: on this machine, a program running as you can do what
  you can. The comparison moves into Observatory's signed app window in a later release (OBS-280).
  `create` prints the link only to a terminal, or with `--show-link`.
- **Using it.** The recipient's agents take the key through `use_secret.py run --share ID NAME`
  (flags before the names; `--consumer NAME` names the agent in the report). The key stays sealed
  on disk under `node/shares/received/`, apart from every vault, and is opened in memory for that
  one command, under a lease the mailbox renews (one hour by default, `create --lease SECONDS`, at
  most a day). Known values are removed from the command's printed output, as with every
  `use_secret.py` run; a command can still print the key some other way, which is a use, not an
  export. An agent that holds a Fabric delegation token is refused a shared key (`no-grant`): who
  pays for a share's use is not decided yet (contract CT-187).
- **What the sender sees.** `full share status ID`: uses per day and the agents, as reported by
  their machine (sealed to you, unreadable by the mailbox); deliveries, as seen by your mailbox; and
  exports with the export button (none in this release: there is no export button yet). Each report
  restates its day and the 35 days before it (from the day the link was accepted), so a report lost
  on the way is made up by the next; your machine replaces each day's count with the latest and
  counts each export once, however many reports restate it. It reads only those days of each report
  (never one before your machine read the claim or after the report's own day, never further back
  than a report it already read allows, and only the keys you shared) and keeps at most
  400 exports of a key a day, so another machine cannot rewrite your past days or grow your
  records without bound; what it ignores is listed under `usage.outOfWindow` in `full share status
  ID --json`. Each
  envelope lives at most 90 days; the maintenance job (`full maintain`, every 15 minutes once
  installed) makes one pass over your shares an hour: your machine seals each key again in the week
  before its copy ends and when the vault value changes (a rotation reaches the recipient at their
  next renewal), reads the reports, and on the recipient's machine renews and reports at least once a
  day. A pass that cannot reach the mailbox is recorded as `shares.unsynced` and tried again. If
  your machine stays off past a copy's end, the recipient's key stops until it is sealed again.
  `full share list` and `full share sync` make one pass by hand.
- **Taking it back.** `full share revoke ID` hands the mailbox a signed revocation, and it refuses
  every renewal from then on; the recipient's machine deletes its copy at its next contact and
  returns a signed receipt. A key that reached another machine can have been copied there, so
  revoke a key minted for the share at its provider, or rotate your own (`vault.py rotate`), then
  run `full share provider-ended ID`: only then does the share read "taken back".
- **Trying it on one machine.** `project-observatory full share mailbox-trial` runs a mailbox on
  loopback and prints the `OBSERVATORY_CLOUD_RESOLVE` line two workspaces (two `OBSERVATORY_HOME`s)
  export to reach it. It never serves another machine.
- **Files.** `node/shares/sent/ID/` and `node/shares/received/ID/` (owner-only): the share's own key
  pair, the other side's public keys, the sealed copies, the comparison code and the use journal;
  never a value, and the claim code only until the claim is read. One step of a share runs at a time
  (`share.lock`), so the maintenance pass and your own command never write over each other. A
  workspace or `node/` folder reached through a symbolic link is refused: shares are kept only where
  no link can redirect them. Owner-only means any program
  running as you can read them, the recipient's share key and its sealed copies included: a program
  that holds both can open the key without the use path.

Design: [design/PERSON-SURFACES.md](design/PERSON-SURFACES.md), D2, D3, D8.

### Your share mailbox in your Cloudflare account: full cloud

The mailbox a share link names (`observatory-cloud/1`, contract DEC-0142 PS-10; OSS-83; OBS-263) is a
Cloudflare Worker with one Durable Object per share, deployed into your own account. It carries the
code exchange, the sealed copies, the sealed usage reports and the revocations; it opens nothing
and never holds a value or a private key. It forgets an unclaimed share a day after its claim
deadline, and a sealed one 30 days after the latest of its last sealed copy's end, its
revocation and its last renewal; a share taken back loses its sealed copies at once.

```sh
project-observatory full cloud deploy --dry-run     # which account, Worker, token slot and code; no call is made
project-observatory full cloud deploy               # deploy, or update to this engine's code
project-observatory full cloud status               # current, outdated, unreachable or not deployed
project-observatory full cloud url                  # the address `full share create` uses
```

- **The token.** `deploy` asks the Cloudflare door for a narrow token (its `workers-edit` preset:
  Workers on one account), delivered into the vault slot
  `observatory-cloud/prod/CLOUDFLARE_WORKERS_TOKEN` and read from there in memory for the API calls;
  `--new-token` rolls it. It never reaches an argument, a log or the command's output. The door
  mints it from the admin token you stashed once; with none stashed, `deploy` stops before any call
  and names your step: create a token that can create tokens at
  <https://dash.cloudflare.com/profile/api-tokens>, save it to a file only you can read, and run
  `python "$(project-observatory full-path)/tools/cloudflare.py" stash < FILE`. With several
  accounts stashed, name one with `--account`.
- **The owner key.** Only this machine files shares on your mailbox: `deploy` binds the public half
  of an owner key made once under `node/shares/owner/`, and a share's creation is signed with it.
  Another machine of yours deploys its own mailbox, or passes `--mailbox`.
- **The address.** `https://observatory-cloud.<your workers.dev subdomain>.workers.dev`, recorded in
  `config/cloud.json`. An account with no workers.dev subdomain yet gets the step with its address
  (the account's Workers and Pages page) and nothing is recorded. `deploy` records the address only
  after it answers with this engine's code and this machine's owner key.
- **What it sees.** Ciphertext and names: share ids, the keys' public halves, the slot and provider
  names in sealed headers, the times of claims, renewals and reports, and both machines' IP
  addresses (Cloudflare's request logs keep those too). Anyone who learns a share id, which travels
  in the link's path, can learn whether a claim arrived yet, and can burn the link with a claim of
  their own; never take it, because the six-digit code is compared, and never read the share's frames,
  status or reports, which answer only the sender's and the recipient's share keys. A share's
  requests are limited per side (120 a minute), counted only once a signature holds, and the
  sender's revocation is never refused for it. A refused request, a repeated late claim and a read
  store nothing; every other accepted request keeps its signature's nonce until the signature is too
  old to be played again (at most about ten minutes) and the next request purges it, so a stranger's
  flood cannot grow what the mailbox keeps for a share. A renewal is refused once every sealed copy has ended,
  until your machine seals the key again.
- **Cost.** The free plan's Workers and SQLite-backed Durable Objects cover a person's shares; the
  account's own limits apply.
- **Opening a link in a browser.** The link's page tells the recipient to run
  `project-observatory full share accept` and paste the link. It runs no script, so it reads
  nothing of the link's code.

### Your own relay for your machines: full relay

Your machines reach one another through a relay (`fabric-relay/0.1`, contract DEC-0047 and
DEC-0066). Until the hosted relay runs, run your own on the machine that is always on (OBS-287, the
relay project's interim RLY-04). It is a router with mailboxes and holds no authority: every message
is signed by its sender's device key and checked by its recipient, and what must stay secret is
sealed to the recipient before it is sent.

```sh
project-observatory full relay serve                            # in the foreground, on http://127.0.0.1:47317
project-observatory full relay enable --origin https://NAME     # inside the node's own server, from its next start
project-observatory full relay status                           # enabled, answering, and how many machines it admits
project-observatory full relay disable
```

- **Whom it admits.** The machines of the estate record this machine's node verified and applied
  (`full org records apply`, kept under `node/records/`), minus every machine and key that record
  or an applied delta entry revokes. A machine proves its device key in its `auth`; any other caller
  gets one answer, `unauthenticated`, and the connection closes (4401). With no record applied, an
  expired record or a record file changed on disk, the relay admits nobody, and `status` and the
  server's health say why. Every open session is checked again before each frame and every two
  seconds: a machine revoked since it connected is closed (4403), and so is every machine once the
  record's window ends.
- **The origin.** The address every machine signs into its `auth`. On this machine alone it is
  `http://127.0.0.1:PORT` (the default port is 47317). For your other machines put a TLS terminator
  in front (your private network's HTTPS, a tunnel or a reverse proxy) and pass its `https://`
  origin; the relay itself serves plain HTTP on the address you bind with `--host`, by default
  127.0.0.1. An `http://` origin is refused for anything but a loopback address and its own port.
  Have the terminator forward each caller's address in `X-Forwarded-For` and pass
  `--trusted-proxy`: the relay then counts a connection from this machine against the address the
  terminator appended (the rightmost one; a port is stripped). This is a requirement on the
  terminator: it MUST set the header itself to the real client address, overwriting it or
  appending to it, and never pass a header the client sent through as the last entry. In nginx:
  `proxy_set_header X-Forwarded-For $remote_addr;` (with `proxy_http_version 1.1;`,
  `proxy_set_header Upgrade $http_upgrade;` and `proxy_set_header Connection "upgrade";` for the
  WebSocket). In Caddy: `reverse_proxy 127.0.0.1:47317 { header_up X-Forwarded-For {remote_host} }`.
  Four residuals stay. Without `--trusted-proxy` the header is ignored and every caller behind the
  terminator is one address, so one caller flooding through it can keep everyone behind it out
  until the flood stops. With `--trusted-proxy`, every program on this machine that can reach the
  relay's port can claim any address it likes, so keep the relay bound to 127.0.0.1 and reachable
  from other machines only through the terminator. A flood from 32 or more address groups (one
  IPv6 /48 holds 65536 of them) fills every pending slot with one connection each, and while it
  lasts a newcomer is refused: the relay's pending budget is a capacity, not a filter. And machines
  that share one group (one home IPv6 /64, one NAT address) can each be cut while they wait for
  `auth` when an attacker from a smaller group arrives; their node connects again (OBS-293).
- **Inside the node's server.** `enable` writes `config/relay.json`; the node's local server starts
  the relay beside itself when it starts (a login, `full update`, or
  `python "$(project-observatory full-path)/tools/serverd.py" --install` to restart it now), with no
  service of its own. A relay that cannot start, or that admits nobody, is a `relay` row in the
  server's `degraded`, and the server keeps running.
- **Messages.** A message waits in its recipient's mailbox until the recipient acknowledges it or the
  ttl its sender set ends (the node signs the message's `exp` at the same time). Each mailbox holds
  at most 1000 messages and 64 MiB; a machine sends at most 120 messages, 30 presence queries and 12
  delta queries a minute, counted over all its connections. Over a limit the relay answers
  `mailbox-full` or `rate-limited` (with the seconds to wait), and closes a connection that keeps
  sending (1008). A message to a machine the
  record does not admit is `unknown-recipient`. A repeat of a message within its ttl is answered
  with its first number and is not delivered twice. The relay remembers at most 20000 messages per
  sender for that; it forgets first those already acknowledged or expired, and while all 20000 are
  still queued it refuses that machine's new messages (`rate-limited`, retry in 60 seconds) rather
  than forget one.
- **Connections.** A caller has 10 seconds from connecting to send its request and 10 seconds from
  the upgrade to send its `auth`, however it spreads the bytes; then it is closed (4401 for a missed
  `auth`). Callers not admitted yet share their own budget (32 at once, at most 8 from one address,
  this machine's own included; an IPv6 address counts with its whole /64). When it is full, a
  caller from an address holding at least two fewer connections than the fullest address takes a
  place of that address; otherwise a caller that has sent its request keeps its place for 2
  seconds to answer, and a connection that has sent nothing gives way first. So a caller from a
  fresh address gets in while any address holds two or more; several callers sharing one address
  (every caller behind a terminator without `--trusted-proxy`) compete among themselves only. That budget is kept apart from the admitted machines' (128 connections,
  at most 4 per machine, a fifth replacing its oldest), and both stay below the process's limit of
  open files. A machine that stops reading its messages slows only its
  own connection, which is closed when its queue fills. If the relay cannot accept connections for
  more than a second (out of open files, say), it keeps retrying and the server's health says so.
- **What it keeps.** Queued messages, in `node/relay/spool.sqlite`, each sealed with AES-256-GCM
  under the relay's own key `node/relay/spool.key`; the folder and both files are readable by your
  account only. A copied spool (a backup or a snapshot) carries no message in clear. The key sits
  beside it, so the seal does not hide a message from a program running as you: the protection
  that counts is the sender's own sealing. `serve --memory` keeps nothing on disk, and queued
  messages end with the process. Logs are one JSON line per event with kids, codes and counts, never
  a body, a nonce, a signature or the estate's id.
- **Not carried yet.** Blobs (`/v1/blobs/` answers 501), a hub's `delta` and `heartbeat` frames
  (answered `unknown-type`; `welcome` names the delta head this node applied and no heartbeat), and
  a deployment into your own Cloudflare account beside the share mailbox. A machine catches up on
  the delta with `delta-query`, answered from the entries this node applied.

### Which apps an agent may wake, stop and restart: node.lifecycle

An agent should never launch, quit or kill your desktop apps or background jobs itself. The
local server (`serverd.py`, the same port as the dashboard) serves six MCP tools at
`POST http://127.0.0.1:47311/mcp` that do it for the agent, on targets you enrolled and only for
callers you allowed (OBS-79; [design/LIFECYCLE.md](design/LIFECYCLE.md)):
`lifecycle.list`, `lifecycle.status`, `lifecycle.ensure_running`, `lifecycle.stop`,
`lifecycle.restart` and `lifecycle.operation.get`. Nothing is served until you enroll a target:
until then every caller is refused and no state is created.

```sh
project-observatory full lifecycle status                  # targets, callers, the last events; starts nothing
project-observatory full lifecycle build-helper            # macOS: compile the helper (needs swiftc); again when status says it is outdated
project-observatory full lifecycle enroll alpha.app --kind app --path /Applications/Alpha.app --caller agent:alpha --startup-effect TEXT
project-observatory full lifecycle allow agent:alpha --binding BINDING_ID --target alpha.app --tool lifecycle.list --tool lifecycle.status --tool lifecycle.ensure_running --tool lifecycle.operation.get
project-observatory full lifecycle policy alpha.app --idle-minutes 30    # or --desired always_on, --background-launch on
project-observatory full lifecycle hold alpha.app          # off on purpose: agents get held_off; --release undoes it
project-observatory full lifecycle revoke agent:alpha      # refused from its next call
```

- Every verb that changes something needs your terminal; there is no `--yes`. `status` and
  `build-helper` grant nothing.
- After an update that changes the helper's source, apps answer `setup_required` until you
  run `build-helper` again: `status` says the helper "was built from an older native_app.swift",
  and no helper built from another source is run.
- `enroll` takes the path you name and never looks for one. On macOS it verifies the app's
  signature and records its signing team and bundle id, so an update from the same team keeps
  matching and anything else is refused; a LaunchAgent is recorded by its label and the hashes
  of its definition and the files it runs. `--startup-effect TEXT` is your own sentence for
  what starting it does (for example "opens its main window"), and it is required.
- The caller proves who it is with an access binding (above), and the grant names that ONE
  binding: `allow PRINCIPAL --binding BINDING_ID` (the id `full access-binding show` lists),
  never a bearer. A binding's memory scopes and ceilings do not reach the apps, so another
  binding of the same principal, issued to read memory, is refused here. A caller acts on a
  target only when the target lists it (`enroll --caller`) and its grant lists the tool and the
  target (`allow`). The service token in the descriptor opens the events feed, never these
  tools.
- The person comes first: an agent may stop or restart only an app the node started that you
  have not taken over and are not using right now, otherwise it is told `in_use_by_person`. A
  forced stop and holding an always_on target off are yours, at the terminal.
- A damaged lifecycle store does not take the server down: `/mcp` answers 503 with the fault,
  the well-known document names it in `degraded`, and the dashboard, `/health` and the events
  feed keep answering.
- What happened is one sentence per operation in `store/node-lifecycle/events.jsonl` (the last
  ten in `status`), and the detail in `store/node-lifecycle/logs/lifecycle.jsonl`, both
  owner-only. Enrollments and grants are in `config/node-lifecycle.json`, owner-only.
- On Linux and Windows there is no adapter yet: every tool answers `unsupported_platform` and
  nothing is started or stopped.
- While the local server runs, it supervises what you enrolled: an app the node started that
  no caller renewed for its `--idle-minutes`, that you have not taken over and are not in
  front of, is stopped; an `--desired always_on` target that exited is started again, until
  three failed starts in 30 minutes make it stop retrying and say so once in the events. It
  looks every 10 minutes, every minute while an app it started runs, and at once when an
  enrolled app launches or quits. An app you bring forward is yours from then on and is never
  idled out. A failed supervision pass is one event in `full lifecycle status` (the log has
  every one); it does not reach the events feed yet (OBS-250).
- Moving from another lifecycle broker on this machine (step L5): `full lifecycle import --from
  FOLDER --dry-run` reads that broker's state folder (its `registry.json`, `callers.json` and
  store) without writing anything there and says what would change; without `--dry-run`, at your
  terminal, it writes what the engine accepts into `config/node-lifecycle.json` in one step and
  records one event. Whatever the engine refuses is one line with a code, and the exit code is 1;
  running it again changes nothing. Its callers become `agent:<name>` (`--principal NAME=PRINCIPAL`
  names another); its owner and host credential are not callers here. No token is copied: a grant
  comes over only when an access binding you issued already carries that caller's token, and
  otherwise the report prints the `access-binding issue` and `lifecycle allow` to run. An app
  still enrolled under the older identity is verified now; an updated one keeps its enrollment
  only under a signing team you name with `--trusted-team`, otherwise enroll it again.
- An always_on target comes over as on_demand, because until the other broker is off both would
  restart it when it crashes (`--keep-always-on` keeps it, with that warning).
- After the import: `full lifecycle status`, point each agent at `POST /mcp` with its binding,
  check one call, and only then switch the other broker off with its own uninstall. Then turn
  each target the report names back with `full lifecycle policy TARGET_ID --desired always_on`.
  The engine never stops the other broker, and treats the processes it started as yours.

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
  and `du -x` never counts a mounted image as the host disk. `du` counts an APFS clone in full, so
  on macOS a place of 512 MB or more is walked a second time, inside the same budget, for the
  private size of its files: where clones hold at least a tenth of it (and 100 MB), the row says
  what removing it frees at least. That walk is repeated only every `private_every_hours` (72;
  `--disk` always walks); one that runs out of time or budget keeps the last figures, dated "as of",
  and is named under "Not measured", and a git-hygiene pass cut short carries its unreached
  checkouts with their old date, from which the cleanup takes no automatic action. Swap files are a row too: on
  macOS they share the disk's container, so memory pressure is disk pressure.
- **Git hygiene**: every registered checkout's worktrees (clean, dirty,
  missing, in use, idle days) and branches (merged, patch-merged, pushed,
  unique).
- **Agents**: `registry/agents.json`, one row per Fabric service installed here
  (each `<id>.<instance>.json` descriptor in the services directory): where it answers,
  the contract its manifest pins, who supervises it and whether launchd has it loaded or
  running, and which MCP declarations reach it by its origin. The wiki projection mirrors it
  to `inventory/agents.json` and writes a readable page beside it, `inventory/agents.md` (OBS-317):
  one row per service key with its product, version, where it answers, contract pin, who holds it
  and its MCP declarations, generated and never to be edited. Nothing of a descriptor's `auth` is copied, and no row is guessed
  from a name. A descriptor, manifest or launchd answer that could not be read is listed under
  `degraded`; without an MCP scan (`project-observatory full scan-mcp`) a row's
  `mcp_declarations` is `null`, not empty. URLs fail closed: an origin or repository that is
  not a strict `scheme://host[:port][/path]` is written as null, and in any other text a scheme URL with
  user-info, a query or a fragment is replaced whole by `[url]`, never partly cleaned (a schemeless
  `token=…` is not recognised; a Windows drive path such as `C:\work\x` becomes `[url]`). If the scan is
  missing or broken, the last rows are kept, marked `stale`, with the reason in `degraded`. With `features.machine_watch` off the document is marked
  `stale`. Its top-level `updated_on` is the day the emit last wrote it; `scanned_on`, `first_seen_on` and
  `last_seen_running_on` come from the scan, never from that day. A scan dated in the future is stale too.

With `features.auto_cleanup` also on, the tick and the disk guard (below) remove what loses nothing:
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

The manual tier writes a thin git bundle per branch. For each dirty worktree it
creates a fresh directory under `<home>/archive/cleanup/<date>/`, with
`tracked.patch` (HEAD to working files), `staged.patch` (HEAD to index),
`status.txt`, `untracked-manifest.json` and `untracked.tgz`. Both patches must apply
in check mode to a scratch index of HEAD. The tarball's exact regular-file list,
sizes, modes and streaming SHA-256 hashes must match the saved manifest. Before
removal it reads the archive back, refreshes process holders, and rechecks HEAD,
status, both patches and the untracked listing/content. Changes, unreadable data,
archive errors or an exhausted inspection budget retain the worktree and report
failure, naming any partial archive; a retry never overwrites an earlier archive.

Manual archiving permits ordinary files only: nested repositories (including
submodules and bare repositories), symlinks and special files are retained.
Ignored files are outside this archive and cause a skip; preserve or move them
before retrying. Inspection allows 60 seconds (or the remaining run budget),
10,000 tree entries and 512 MiB of untracked content; each patch also has a
512 MiB limit. Untracked hashing reads at most 1 MiB at a time. Git patch capture
and individual filesystem operations are not a strict memory or wall-clock cap.
These checks are not an atomic filesystem lock: another writer can still change
a file between its final observation and Git removal. Stop writers before an
explicit manual cleanup. Coverage: `CleanupNeverLosesWork` in
`observatory/engine/tests/test_git_hardening.py`.

Before both automatic and manual worktree removal, cleanup reads index flags
before status and again at the removal boundary. `assume-unchanged`,
`skip-worktree` (including sparse checkouts), unsupported entries or an unreadable
index cause a skip: Git status and diff can omit changes behind these flags.
Cleanup does not clear or rewrite the flags. It also verifies the live HEAD's
retaining branch; a detached HEAD needs an archive ref whose readback equals the
exact commit. A conflicting ref or a failed creation retains the worktree.

Branch cleanup captures its tip before eligibility checks. A unique branch's
verified bundle must advertise that exact tip, and the current tip and worktree
attachments are checked again. Deletion uses Git's expected-old-tip comparison
with `--no-deref`: a newer commit cannot be deleted by a stale cleanup, and a
late symbolic-ref conversion cannot redirect deletion to another branch.
Symbolic branch targets, invalid names and shorthand names are refused during
eligibility checks; only the three known branch cleanup classes can act. A newly attached worktree
still has a small final attachment-check/deletion race; concurrent checkout
writers must be stopped. Each branch bundle gets a new directory. Git branch
configuration is retained when the ref is deleted. Coverage:
`CleanupNeverLosesWork` in `observatory/engine/tests/test_git_hardening.py`.

The legacy `full cleanup` build-artifact action now reports a skip and asks for
`full disk-guard` to replan; only the disk guard owns build-output eligibility.
`CleanupNeverLosesWork.test_legacy_artifact_plan_cannot_delete_newly_tracked_output`
covers this refusal.
The `.wrangler` directory can hold local databases and object storage; it is never
classified as build output, even when an older cleanup configuration lists it.
Cleanup also retains an ignored artifact parent containing a nested repository or
local state, and any tree whose contents cannot be fully inspected within its
bounded check. Symlinks and special files are retained conservatively.
Regression coverage: `observatory/engine/tests/test_disk_guard.py`, `GitTierSafety`.

### A lightweight memory refresh

`project-observatory full machine --memory-only` refreshes RAM, swap and per-process
RSS into `store/raw/machine-memory.json`, without sizing directories or reading process
arguments. Disk-guard passes reserve up to eight seconds for this before planning, even
when automatic cleanup is disabled. This measurement never stops a process or removes data.

The Machine page and `observatory_machine` select memory and process observations separately,
show their timestamps and flag samples older than 30 minutes or with unknown timestamps.
A failed measurement remains null with a reason; zero means a measured zero. RSS totals
can count shared pages more than once and do not measure unique physical memory or
reclaimable disk bytes. Lightweight process groups identify executable basenames only;
project/simulator attribution still belongs to the full survey and is not inferred here.
Native Windows has no new memory adapter in this slice and reports unsupported sources.
Proof: `tests/test_memory_freshness.py` in the engine.

### The disk guard

The tick is long on a busy machine, and its cleanup step comes late: on a loaded
machine it can go unreached for hours, exactly when the disk is filling. So with
`features.auto_cleanup` on, the maintenance job (`full maintain`, every 15 minutes) runs the
**disk guard** first in its pass, before the update and the backup — once an hour while space is
normal, on every pass once free space falls below the `low` line:

```sh
project-observatory full disk-guard                  # the plan at the current pressure; removes nothing
project-observatory full disk-guard --apply          # act now
project-observatory full disk-guard --level critical # plan as if the disk were nearly full
```

The guard reads free space and picks a pressure: `normal`, `low` (under 25 GB),
`critical` (under 10 GB). Pressure moves every threshold in `config/cleanup.json`
under `guard`:

| What | normal | low | critical |
|---|---|---|---|
| The cache tools' own cleanup | `uv cache prune`, `pnpm store prune`, `brew cleanup --prune=14`, Docker build cache older than a week | also `npm cache verify`, `uv cache clean`, Go and Yarn caches, dangling Docker images | also `brew cleanup --prune=all`, all unused Docker build cache, then a trim (`fstrim`) inside each running Colima or Lima VM so the host gets that space back ([why](design/DISK-GUARD.md#space-inside-a-docker-vm-at-critical-obs-305-2026-11-03)) |
| Git-ignored build and dependency output (`node_modules`, `.venv`, `dist`, …) of checkouts nobody worked in | 14 days | 3 days | 1 day |
| Temporary entries untouched for | 3 days | 1 day | 1 day |
| Trash items older than | 30 days | 30 days | 3 days |

Application and tool cache folders (`~/Library/Caches`, `~/.cache`) untouched for 7 / 2 / 1 days go
too, except the downloaded browsers and SDKs a tool does not fetch again by itself (`cache_keep`:
Playwright, camoufox, puppeteer, cargo-xwin and others) and `uv`, which has its own command.
A folder an agent service uses for live state stays at every pressure, in every class that removes
a path (cache folders, build output, temporary entries, Trash): the service writes an empty
`.observatory-keep` file at its top, or declares the folder as state (`paths.data`, `paths.config`
or `paths.logs`) in its Fabric service descriptor. The report and the Machine page list each such
folder under "Kept by the guard" with the reason. A descriptor's `paths.cache` stays removable:
live work there needs the marker. A declaration that covers a whole cache or temporary root, the
Trash, a checkout, `~/Library` or its `Application Support`, the home folder or the disk, or that is
written for another operating system, is ignored and named in `degraded`. When the descriptors folder cannot be
read, no cache folder or temporary entry is removed that run, and `degraded` says why; when one
descriptor `<id>.<instance>.json` cannot be read, only the folders named for that id (the id, the
id followed by `.`, `-` or `_`, or a reverse-DNS name ending in it; ids of at least 3 characters)
are kept that run.
Browser code-sign clones a crashed browser left behind go at any pressure, and
the cleanup's own git auto tier runs after them — with its thresholds lowered by the same
pressure (`pressure` in `config/cleanup.json`): a clean worktree or a merged branch nobody
touched for 3 days at `low`, 1 day at `critical`, instead of 7 (pushed branches 14 and 7 days instead of 30,
build output of idle projects 7 and 3 instead of 30). Only that tier moves, and only for a threshold you did not set
yourself; a worktree with uncommitted edits, or with ignored files that are not build output (`.env.local`, a local
database), and a branch with unique commits never do. `project-observatory full cleanup --level low` previews it.
Every cleanup takes one lock, so the tick, the guard and you never act on the same thing at once. npx's package
cache and macOS diagnostic reports are swept too.

Temporary cleanup preserves repositories and linked worktrees, including nested
Git markers, bare repositories and paths beneath a checkout or bare repository.
Each path plan carries its device/inode identity; a replaced target or a plan
without that identity is skipped and must be planned again. Build cleanup rechecks checkout
activity, ignore rules, tracked files and new writes immediately before acting.
Each path action also makes a fresh, target-scoped holder probe within five
seconds; unknown, partial or timed-out results skip the path. On POSIX this
last probe uses `lsof` for positive holds. An empty listing plus unmatched
rendered `ps` arguments cannot prove a closed script is inactive: spaces, relative
paths and symlink aliases can hide its checkout. Current POSIX production holder
snapshots therefore remain incomplete, and path removal is refused. Windows reports unknown holder coverage and preserves path targets.
See [the preservation contract](design/DISK-GUARD.md#apply-time-preservation-2026-10-13)
and `tests/test_disk_guard.py::TargetRevalidation` / `FreshTargetProbe`.
On POSIX, clean and archived dirty worktrees repeat this target check immediately
before removal, after Git/archive verification. The preliminary admission snapshot never grants final removal. Planning now
requests an explicit unknown snapshot without collecting a full process listing;
per-phase `observation=not-attempted` and `measured_at=null` disclose that choice. Nonzero, diagnostic or undecodable `lsof`/`ps`
output and unparseable global `lsof` names mean
unknown, so cleanup can conservatively skip
work even if a partial survey showed no holder. Cache-tool commands likewise
skip when their process survey is unknown. In particular, a nonempty `busy_if`
request cannot be cleared by an unmatched `ps` display. Commands without holder
preconditions retain their separate configured policy. Worktree idle age is still a planning
condition, not a fresh action-time measurement. A process starting after the
final check remains a race; these checks are not an exclusive filesystem lock.
Windows worktree removal also refuses unknown holder coverage. A successful
rename can coexist with an open FILE_SHARE_DELETE handle; it never proves non-use.

Build planning first refreshes explicit Git worktree registrations within a
reserved budget. Root attempts rotate across passes with a persisted cursor and
a five-second default quantum. Registration lists membership, not abandoned or
safe-to-delete status. Failed path-holder readings remain visible per phase.
See [registration and fairness](design/DISK-GUARD.md#registration-refresh-and-build-root-fairness-obs-87-subset).

**On every system.** Cleanup admission currently skips process-table collection
because its negative result cannot authorize a path action. Explicit positive-hint
readers use bounded `lsof`/`ps` capture: 2 MiB combined pipe output, bounded
parsing and one shared deadline. The unbounded Linux `/proc` fallback is retired.
Limit or capture failures remain unknown; this is not a measurement of RAM saved.
[Capture limits and regression cases](design/DISK-GUARD.md#bounded-explicit-holder-observations)
explain the owned-child cleanup and timing limits. Windows has no supported holder inventory
here, so path actions skip instead of treating rename as proof. The
schedule is the same maintenance job everywhere: launchd,
a systemd user timer, or a Task Scheduler task, every 15 minutes. "Nobody worked in" means no
checkout or commit and no uncommitted file changed since — never the index,
which every `git status` rewrites.

Observed holds and incomplete holder coverage refuse path removal. These
observations do not establish lifecycle ownership or exclude future writers.
Cleanup also refuses symlinks, privacy-guarded folders and another user's files.
It does not stop a process or start an app; daemon-dependent commands require a
positive observation. A configured busy-tool precondition requires proven absence,
which the current rendered process display does not provide. Memory is reported, not
freed: the Machine page names who holds the memory that swap writes to this disk,
and each running simulator with the command that stops it.

Freed space is what the disk reports before and after each action (`freed_mb` in
`store/logs/cleanup.jsonl`), not a `du` estimate: `du` counts an APFS clone in
full, and removing one can free nothing. The delta also counts whatever else wrote to
the disk meanwhile, so a single row can read negative; a run's total (`freed_gb` in
`store/raw/disk-guard.json`) is the figure to trust. The Machine page also lists the largest
files under the home directory, walked once a day within a time budget, and never
removes one. If the guard has not reported for three hours while
`auto_cleanup` is on, the finding `machine.guard_idle` says so.

## Join an organization server (device enrollment)

An organization that runs a `fabric-device/0.1` server (contract DEC-0031) can enroll this machine so that its
activity for that organization (the daily `grant.usage` of agents you share into it) reaches the organization's
server, signed by this machine. An administrator issues a single-use enrollment token; put it in a vault slot, never on
a command line:

```bash
python "$(project-observatory full-path)/tools/vault.py" put my-org prod ENROLLMENT_TOKEN   # the token on stdin
project-observatory full org enroll --server https://org.example --org org_example --token-slot my-org/prod/ENROLLMENT_TOKEN
project-observatory full org status
```

The token is read from the slot for that one request and is never stored, printed or logged. Enrollment creates
device keys for this organization alone (in `node/orgs/<org id>/keys/`: no other organization sees them), declared as
what they are, an owner-only key file (`storage: file`); a server whose policy wants a hardware key refuses with
`key-storage-refused`. It sends a certificate request signed by that `#sig` key, and
keeps the 7-day certificate, the stream epoch and the protocols the server accepts under the workspace's
`node/orgs/<org id>/`, owner-only. From then on the hourly maintenance job runs `full org tick`: it rotates the
certificate at two thirds of its life, checks in, sends share registrations, collects `grant.usage` and empties the
outbox. Every request carries the `fabric-device` HTTP signature (DEC-0056).

- `org status` shows each organization's server, the certificate's expiry date and state (`valid`, `expiring` in its
  last three days, `expired`) and whether rotation is due, the last check-in, the outbox depth and the last batch's
  result, and `degraded` lines: `org:certificate-expiring` (renew it now with `org rotate --org <id>` while the server
  answers), `org:certificate-expired` and `org:needs-enrollment` (the certificate expired or the server asked for
  re-enrollment: enroll again with a new token), `org:removed` (below), `org:share-registration-not-listed` (the
  server does not accept `share-registration/1` yet, so neither registrations nor `grant.usage` are sent),
  `org:share-registration-refused` (a typed refusal that waits for your estate record to change) and
  `org:outbox-retrying`. When you have something to do, `nextStep` says it in words and `command` is the command.
  `full doctor` (`organizations`) and the Health page show the same expiry and state.
- A machine back after its certificate's 7 days renews it by itself with its key on record, no token needed, for 30
  days after the certificate ended (on the server's clock): the next `org check-in` or maintenance tick sends the
  renewal first, and `org status` meanwhile shows the certificate `expired` with `renewUntil` and the `org check-in`
  command. Past those 30 days, or when the organization server refuses the renewal (a server that does not take it
  yet, or its policy), the machine needs a new enrollment token: `org check-in` then refuses with
  `certificate-expired`, the date, the step and the `org enroll` command, and exits 1.
- A machine its organization removed (the server answers `certificate-revoked` or `certificate-unknown` with HTTP
  401, as it does when an administrator removes the machine) is shown as `removed`, with the date and the code.
  Nothing more is sent to that organization, and the events it still held are dropped (they can be accepted nowhere)
  with one count kept. `org leave --org <id>` removes its local state; `org enroll` with a new token starts over.
- An organization that closed (the server answers HTTP 410 `organization-closed` with the date its closing began,
  contract DEC-0103) ends that enrollment on the first answer: `org status`, doctor and Health say the organization
  closed on that date, and nothing more is sent to it. Everything this machine held for it is dropped: the events
  waiting to be sent, the share registrations, and the organization's policy, keys and collection start. Your other
  organizations are not touched. `org leave --org <id>` removes what is left locally. A server that answers
  `unknown-org` to `org enroll` does not know the organization id: check it with the organization's administrator.
- When the organization says it does not collect a kind of event for you under the notice you acknowledged (a
  `purpose` refusal), this machine stops collecting and sending that kind for that organization until you acknowledge
  another notice; it does not try again on a timer. `org status` (`purposeHolds`, `org:purpose-held`), doctor and
  Health name each kind held and the notice version it waits to change. A machine no person is bound to, or whose
  person has not acknowledged a notice yet, holds nothing: it keeps sending, and the organization keeps refusing until
  its notice covers that kind.
- `org check-in`, `org rotate` and `org flush` do one step now; `org tick` does everything due.
- `org leave --org <id>` removes this machine's local state for that organization, its own device keys with it. The
  organization keeps its records until an administrator retires the device.
- Two `org` commands for one organization never run at once: each waits for the other (the maintenance job's tick
  included), so a command can take as long as a tick in flight.
- Share registrations come only from the estate revision this machine verified and applied
  (`org records apply --record FILE`, and `--delta FILE` for revocation-delta pages); a draft is never sent.
- **The harness summary** (`node-summary/1`, contract DEC-0101): what this machine's coding agents run: the coding
  agents of the contract's catalogue installed here with their versions, the MCP servers they load (name, source,
  transport, health), the skills (name, source, digest: Claude Code's, each project's own and Codex's) and enabled
  plugins (name, source, version), and
  how many hooks they load. How much it lists follows the machine's mode (DEC-0118): on your own machine (mode `C`, the
  default and, until the organization's signed policy is applied, the only one) an organization sees the coding agents,
  the hook count, and only the MCP servers, skills (a project's own skill whose project sits in a work folder) and
  plugins installed in its own work folders or named by its
  baseline; the rest stays here, counted as `outOfContext`. Only a machine the organization administers (mode `A`,
  set by an `mdm` profile) lists the whole harness. **It is not shared until you say so, per organization**: run
  `project-observatory full org summary --org ID` to read exactly what would leave (it sends nothing and works before
  you agree), then `project-observatory full org summary --org ID --consent on` (or `--consent off` to stop; the time
  is recorded). Until then `org status` and Health say `harness summary: not shared (consent off)`. Why off: whether
  a person may withhold the summary is still the contract's open question (CT-92's second half). Names can carry a
  client's or a person's name: on a machine the organization administers (mode `A`) every project's own skill names
  leave. To keep names back, list them in
  `config/node-summary.json` as `{"exclude": ["private-*", "plugin:*:mine"]}` (shell-style globs on whole names), mode
  0600 in a folder only you can write to: they are never sent, only counted here (`excluded`); a list that is a
  link, not owner-only, in a folder others may write to or not readable sends nothing, and says which. The consent
  and the summary's rising sequence survive a renewal and a re-enrollment into the same organization. Once on, a server that lists `node-summary/1` gets it in a check-in when it changed and at least once a day.
  Never a path, a command, an argument, an environment value, a URL, a hook's command, a prompt, an output or a file's
  content; a name with a space, a slash or a dashed home folder is left out (counted, not rewritten). It is built from
  the harness and MCP scans, so it needs `integrations.harness` and `integrations.mcp` on and scans younger than
  48 hours that already list plugins, coding agents, project skills and Codex skills: otherwise nothing is sent and `org status` says
  `org:node-summary-unavailable` with the reason. A server that refuses the summary never stops check-ins: the check-in
  goes again without it, the refusal is shown (`org:node-summary-refused`) and the summary is tried a day later.
  `org status` (`nodeSummary`) and Health say when the server last took one and how much it listed, and
  `org:node-summary-not-listed` when the server does not read it. Its drift from the organization's baseline
  (`missing`, `stopped`, `changed`, `outdated`) is computed and shown once this machine applies the organization's
  signed policy (not built yet: the check-in reports policy revision 0, so no baseline applies and the summary carries
  no drift, as the contract asks; a signed policy the server sends is recorded and reported `failed` with its
  revision, never as in force, OBS-231). No agent service is listed yet: nothing on this machine ties a service to an
  organization's context, and a person's other services are never listed.
- **Repairs** (`repair-request/1`, contract DEC-0101): an organization's administrator may ask this machine to run one
  package to repair it, signed by a key of the organization's policy key set. Nothing runs on that alone: the request
  is judged as the contract's `repairDecision` judges it (the signature under a key in its validity window, this
  device and organization, at most 7 days, a policy that has not expired, the package on the policy's
  `repair.packages` list) and runs only as a local action you allow-listed for that package in
  `config/node-repair.json`, mode 0600 in a folder only you can write to:
  `{"schemaVersion": 1, "agents": {"urn:fabric:agent:PUBLISHER:ID": {"action": "restart", "targetId": "TARGET"}},
  "lifecycle": {"bearerSlot": "PROJECT/ENV/NAME"}}`. `action` is `restart` or `ensure_running` of a target you
  enrolled with `full lifecycle` (never a command, a path or arguments), and `bearerSlot` is the vault slot holding the
  bearer of an access binding you issued for the repair runner and granted only those targets and the tools
  `lifecycle.list`, `lifecycle.operation.get` and the action's own (`full access-binding issue`,
  `full lifecycle allow PRINCIPAL --binding ID --target TARGET --tool ...`). **You are told first**: `org status`, doctor
  and Health show the repair (the line the organization wrote, the package, when it starts) at least 15 minutes before
  it starts; `project-observatory full org repair --org ID --decline REPAIR_ID` declines it, and it never runs. It is
  judged again the moment it would start. Every request and run is recorded once by its id in the enrollment's journal
  (no signature, bearer or body) and answered once with `repair-result/1`, the same answer again if the request comes
  again, sent only to a server that lists `repair-result/1`. `project-observatory full org repair [--org ID]` lists
  them and says whether a repair could run here at all. **Today none can**: the organization's signed policy and key
  set are not applied yet (OE-06), so the check-in does not ask for any (it lists `repair-request/1` only while one
  could run, DEC-0117), and a repair a server sends anyway is counted in the journal (`repair-unasked`) and dropped.

Check-ins run as often as the maintenance job (hourly), not at the server's interval: a server that marks a device
offline after three missed intervals shows it offline between passes. Run `org check-in` for a fresh one.

## Organizations and resources

**Which repository owners are yours** is a separate, smaller file,
`config/ownership.json`, read by the merge (`OWNED_ORGS` in `collectors/merge.py`):

```json
{"organizations": ["your-github-user", "your-org"], "work_organizations": ["employer-org"]}
```

A repository whose owner is in neither list is classified `external`. When the
GitHub listing returns an owner the file does not declare, the merge says so in
an `ownership` row (on the board and in Health) instead of declaring it itself —
which owners are yours is your decision. Edit the file and run
`project-observatory full local`.

An estate that serves more than one owner keeps each owner's analytics, design
files and clouds in that owner's accounts. `config/organizations.json` says
which owner a project has and where that owner's accounts are; every project in
the registry then carries `organization`, `organization_source` and
`organization_why`, and `observatory_project` answers with the destination:

```json
{"organizations": {
   "company": {"label": "Company", "ga4_account": "accounts/100",
               "figma": {"team": "Company Design", "project": "Builder"},
               "match": {"github_owners": ["company-org"], "products": ["product:suite"],
                         "digitalocean_teams": ["Company Team"], "cloudflare_accounts": ["<account id>"],
                         "heroku_teams": ["company-apps"], "openrouter_accounts": ["company"]}},
   "person":  {"label": "Person", "default": true, "ga4_account": "accounts/200",
               "ga4_legacy_accounts": {"accounts/300": ["properties/31"]},
               "figma": {"team": "Personal"}}},
 "projects": {"project:partner-app": {"organization": "person", "why": "…"}}}
```

The first answer wins: a declaration (`projects` here, or `organization` in
`project_overrides.json`), then a `match` rule on repository owner, product or
provider account, then `external` when every repository owner is outside the estate
(`ownership.json`), then the `default` organization. Two organizations matching
one project is reported as a `conflict` and assigns neither. An empty file turns
the feature off.

The provider account rules (OBS-412 part 1) are `digitalocean_teams` (a team's
uuid, or its name while no other team here bears it; the emit warns about a name
two teams bear), `cloudflare_accounts` (an account id) and `heroku_teams` (a team
name). A rule that is not a list of strings is ignored and named. A project matches one only through a resource the registry already ties
to it: a DigitalOcean app or a Cloudflare Pages project or zone whose `project`
was measured through a repository or a site, a Heroku app linked by its rule. A
team's name alone never assigns a project. Each account says whose it is in its
own document: `organization`, `organization_source` (`rule`; `conflict` when two
organizations claim it, a shared account that assigns nothing; `none` when no
rule names it, never the default) and `organization_why` on the account rows of
`registry/digitalocean-account.json` and `registry/cloudflare-account.json`, and
`team_organizations` in `registry/heroku-apps.json`; the DigitalOcean and
Cloudflare pages add an Organization column. An account claimed by two
organizations is the shared case of the organization onboarding design's rule
ORG-R04.

`github_owners` is `repository_owners` under the design's name (OBS-412 part 2):
both are read, for an owner on any host, and an owner named by both counts once.
`openrouter_accounts` names an OpenRouter account by the label its provisioning
key was stashed as (`openrouter.py stash --as LABEL`). It decides that account's
organization and the issuer side of the keys it minted, never a project's: a
project's answer comes from its repositories, deploys and zones together
(ORG-R26), and a key is weighed per key, so one personal key in a company
project does not make the project a conflict. A `match` key the engine does not
know is ignored and named with the known ones, on the emit's error output and in
the finding `organization.config_problem`; a file that does not parse is named
there too. A project in `conflict` gets the finding `organization.unresolved`,
so the disagreement is on the board as well as in the session-start line.

Every reader of a project's organization reads it one way
(`organizations.reading`): the session-start line, the analytics findings and
`observatory_project` name an organization's analytics account and design team
only for a decided answer (`declared`, `rule`, `default`). A `conflict`, or any
source this engine does not take for a decision (a joined organization's
`proposed`, once OBS-413 adds it), names none of them and says to ask first.

Two read-only views answer "whose is everything here" (OBS-411, OBS-415; the
organization onboarding design, ORG-W1 and ORG-W6):

```bash
project-observatory full org accounts           # text; add --json for org-accounts/1
project-observatory full org keys --org company  # text; add --json for org-keys/1
```

`org accounts` lists every provider account this space reaches: repository
owners (from the registry and the GitHub scan), DigitalOcean teams (scanned or
stashed) and the `doctl` contexts whose token is not a stashed one (the
DigitalOcean door's own offline comparison, as `digitalocean.py teams` makes
it), Cloudflare accounts, Heroku teams and the Heroku user, and OpenRouter
accounts (stashed, or named by the door's ledger). Each sits under exactly one
organization with its `source`: `rule` (a `match` rule names it), `default` (a
repository owner of yours no rule names: its projects take the default
organization), or `external` (a repository owner outside `ownership.json`: Not
ours); or under `needsDecision`, with the reason (and, for an account not
connected, the door's command that connects it), when it reads `conflict` (two
organizations claim it), `none` (a provider account no rule names, as its
registry row says: it decides no project until a rule does), or it is seen and
not connected (a `doctl` context, an account only a door's record names). The
Heroku user, which no rule kind names, reads `default`. Each says what it holds
(projects, resources, keys its door issued). Without `organizations.json` every
account of yours reads Personal (`implicit`), and a repository owner outside
`ownership.json` still reads Not ours. `state` is `empty` (no account was found;
`degraded` then says what was not read), `partial` (accounts were found and
something in `degraded` was not read) or `complete`; `problems` repeats what
`organizations.json` holds that the engine does not read.

`org keys --org NAME` lists each vault slot of the projects NAME holds (and of
projects whose disagreement includes it), plus each slot issued from one of its
accounts, by name only: no value file is opened. Each slot reads `organization`
(the project and the issuing account are both NAME's), `personal` (issued in a
personal account, in a project whose disagreement includes NAME: it stays where
it is; a personal key of a project NAME does not hold is not listed at all), or
`ambiguous` (they disagree,
the account is shared or has no rule, or the key was put by hand so its issuer is
unknown), with why and the issuer from the door's record (the OpenRouter and
Heroku ledgers, the DigitalOcean records, the movements journal for the
Cloudflare door). `selected` is true only when both agree (ORG-R15), and a slot
in an environment other than `local`, `stage` or `prod` is never selected
(ORG-R22). The doors' admin and provisioning keys are listed under `notMovable`:
never handed over, and for an account of NAME revoked after NAME's own space
stashes its own (ORG-R07). A value present twice and a name NAME already holds
need a comparison of values or NAME's context and are not checked (`notChecked`).
The default organization, an unknown name and a workspace with no organizations
are refused with the command to run. Agents call `observatory_org_accounts` and
`observatory_org_keys` for the same answers. Choosing, applying and handing keys
over (OBS-416, OBS-417) are not built yet.

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
account can read (`analytics.organization_account_unreadable`). Only a project
whose organization is decided is checked: a property of a project in conflict
expects no account, so it is never in the wrong one.

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

The root is chosen in this order:
1. `OBSERVATORY_BACKUPS`;
2. `project-observatory full configure storage backups /absolute/path` (a path under your home is
   stored as `~/...`, like a source, and `doctor` names its `form` under `backups`);
3. the platform default. On macOS that is `~/Documents/Project Observatory/Backups` when
   `~/Documents` exists; iCloud Desktop & Documents can sync it off the machine.
   Elsewhere, or with no `~/Documents`, it is
   `${XDG_DATA_HOME:-~/.local/share}/project-observatory-backups`, beside the workspace and
   never inside it. Deleting the workspace, or reinstalling into it, leaves the copies.

A root inside the
workspace keeps the encrypted copies in the same tree as
`secrets/backup-passphrase`, so one lost disk takes both: `backups status` and
`doctor` warn about it (`inside_workspace: true`) until the root is pointed
elsewhere.
Each workspace writes into its own subfolder (`<home-name>-<instance>`), so two
workspaces sharing one root never rotate each other's files. Three artifacts
per kind are kept (`observatory-db-*.obsdb`, `snapshot-*.obsnap`,
`before-upgrade-*.obsnap`, `before-update-*.obsnap` and `daily-*.obsnap`); a new
one is decrypted in full before anything older is pruned. Local status and migration
use the same snapshot kinds, including snapshots left by engine updates. Migration
exports verified copies under the existing three-per-kind policy; an unverifiable
copy is reported and retained. Unrecognized kinds are outside this inventory and
remain untouched by migration; the count is not a survey of every backup-like directory.

The format is `OBSENC1`: AES-256-GCM in authenticated chunks, key derived from
the passphrase by scrypt (N=2^17, r=8, p=1), header bound as associated data,
last chunk flagged — a truncated, reordered, extended or re-headed file fails
to decrypt instead of decrypting to less. A snapshot includes `secrets/`,
which is why nothing reaches the root unencrypted: **without a passphrase,
copies stay inside the workspace, plaintext, on this disk only**, and `full
doctor` says so under `backups.warnings`.

Each of those sentences has a code under `backups.warning_items`, with its step (`backups-no-passphrase`,
`backups-root-inside-workspace`, `backups-failed-update-copy`, `backups-unencrypted-local`, and
`backups-root-unusable` or `backups-passphrase-unreadable` when the root or the passphrase file is refused).

**A secret store outside the workspace** (`sources.secret_store`, else `sources.secrets`, when either names a
folder outside the workspace) is not part of the workspace, so it travels beside it: an encrypted workspace
snapshot (the daily one, `full workspace-backup` without `--output`, with a backup passphrase set) carries
it in `external/<setting>/`, next to the home in `data/`. Every slot, the movement journal, the leak
register, the estate digest key and the doors' stashes go with it, copied under the vault's own lock and
listed in the snapshot's manifest by relative path, size, mode and SHA-256, never by value. A snapshot
that stays plaintext never holds it: `--output DIR` (reason `plaintext-output`), no passphrase
(`no-passphrase`), an export that failed or was interrupted and left the copy in the workspace
(`export-failed`: the store is removed from that copy before the failure goes on), and the snapshots an
update takes before it installs and upgrades (`update-snapshot`). Each snapshot first removes the store
from any snapshot copy a killed run left under the workspace's `backups/` (a folder named for a snapshot
kind, or whose manifest lists `externals`; a folder of your own there is never touched), and removes that
run's unfinished `.snapshot-*` folders once they are an hour old. A copy it cannot clean (an entry it may
not delete) stops nothing: the snapshot goes on, its answer names the folder under `secret_store_sweep`,
the carry record counts it as `sweep_failed`, and the next snapshot tries again. Nor is a store carried when it is over 256 MB
(`too-large`), kept changing during two copies (`changed-during-copy`), cannot be read (`unreadable`,
`missing`), or holds the workspace itself (`contains-workspace`): each is named in the manifest and the
snapshot itself still succeeds. Lock files, temporaries, a link leaving the store, a socket and a slot kept
here on purpose (below) stay behind; a link inside the store is recorded, not followed. Each snapshot the
backups keep or export leaves `store/raw/secret-store-carry.json` (when, which file, carried or not and
why, how many files and bytes, how many entries stayed behind), the record doctor reads; a record that
cannot be written is said on stderr and in the answer's `carry_record_error`. The daily run also keeps the
state in its maintenance record. The machine bundle skips a store the snapshot carries, naming it in its
`skipped` list, so no second copy can drift; a store the snapshot never carries (`contains-workspace`) is
walked like any other path.

**Putting the store back.** `full restore` writes a carried store into the folder the restored settings
name for it (after the home rewrite, so `~/...` lands under this machine's home), before the home itself
is put in place. What it does when that folder already holds a store is the `--secret-store` policy:

| The folder | `auto` (default) | `keep` | `merge` | `replace` | `skip` |
|---|---|---|---|---|---|
| absent, or empty (lock files and temporaries only, kept aside) | written whole | written whole | written whole | written whole | nothing |
| holds every carried file, the same | `same` | `same` | `same` | `same` | nothing |
| differs | refused, nothing written (`secret-store-not-empty`) | left as it is (`kept-existing`) | the missing files written, the rest kept (`merged`) | moved aside to `<folder>.before-restore-<time>-<hex>`, never deleted, then written whole (`replaced`) | nothing |

Files only in the folder are never removed (only `replace` moves the whole folder aside) and are counted as
`only_here`; a journal (`*.jsonl`) that has grown since the snapshot reads the same. No folder is ever deleted: an
empty one is moved aside to `<folder>.before-restore-<time>-<hex>` like a replaced store, and a folder holding any
subfolder (even one named `x.lock` or `keys.tmp`) is not empty. The restore holds the vault's own lock in that folder
while it writes. It never writes into the home itself, `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.config/gh`,
`~/Library/LaunchAgents` or `~/Library/Keychains` (`secret-store-target-refused`), and `merge` refuses a folder
holding files but no `projects/`. A snapshot taken on another machine or under another home writes its store only
where this machine's own workspace settings name the same folder, or where you confirm it with
`--secret-store-target PATH` (the refusal `secret-store-target-unconfirmed` shows the exact command).
`--same-machine` (at a terminal) under the same home path counts as this machine's and needs no confirmation.
Another spelling of a protected folder (`~/.SSH` on macOS) or a link to one is refused the same way. Under `keep`,
which `full init` passes, none of these stops the restore: the store is answered `skipped` with its reason and a
`next` command, `full init` shows it, and `full doctor` keeps the warning `backups-secret-store-not-restored` with
that command until a store is in that folder again, however it got there. A skip you chose with
`--secret-store skip` raises no warning. A whole store is written
into a sibling folder `.<name>.restore-<hex>` (folders 0700, files 0600, every SHA-256 checked again) and
renamed into place in one step; a merge writes each missing file atomically and never over an existing one;
missing parent folders are created 0700. A store that could not be written is left as it was and the home is
not restored (`secret-store-write-failed`, exit 1); a refusal (`secret-store-not-empty`,
`secret-store-unresolved` when the restored settings name no folder for it, `secret-store-target-inside-home`
when that folder lies inside the new home, `secret-store-manifest-mismatch`) is exit 2 and writes nothing.
`--json` turns either into `{"error": {"code": "...", "message": "..."}}` on stdout. A link inside the store is
recreated as a link (on Windows it is named `link-not-supported` instead); a link leaving the store and a
special file were never carried and are named. A write appends one `restore-store` line to the store's own
movement journal (`vault.py`; names and counts, never a value). The answer's `secret_store` says what
happened (`written`, `same`, `merged`, `replaced`, `kept-existing`, `skipped`, `not-in-snapshot` for a
snapshot that did not carry it, `not-applicable` for a workspace with no external store), with counts;
`--secret-store-names` adds the conflicting and not restored files by relative path. A `.<name>.restore-<hex>`
folder a killed restore left is removed by the next restore once it is an hour old. `full init` on an empty
home restores with `keep`, so a reinstall over a surviving store never fails or overwrites it; an update's
rollback never touches the store (`skip`).

For a workspace an older release restored, or a restore stopped between the store and the home, write the
stores alone into the folders THIS workspace's settings name; the home is not touched:

```sh
project-observatory full restore SNAPSHOT --secret-store-only [--secret-store POLICY] [--json]
```

Without a release that does this, `full backups decrypt FILE DIR` opens the snapshot and
`DIR/external/secret_store/` is the store to copy into place by hand; such a copy must recreate each link the
manifest lists under `externals[].links` (`ln -s TARGET PATH` inside the store: links are recorded, never
stored as links), and then delete `DIR`, which holds every key in plaintext.

**Checking a backup's store without touching the live one.** A restore check (a weekly restore of the newest
backup into a temporary home, say) proves the store opens without writing where the settings point, which on
the machine that took the backup is the store in use:

```sh
project-observatory --home NEW_EMPTY_HOME full restore SNAPSHOT --secret-store-verify-into DIR [--json]
project-observatory full restore SNAPSHOT --secret-store-only --secret-store-verify-into DIR [--json]
```

Each carried store is written into `DIR/<name>/` (folders 0700, files 0600) and read back: every file's size
and SHA-256 against the snapshot's manifest, every folder and link, nothing unlisted. The answer's
`secret_store` reads `verified` with `files`, `bytes`, `links`, `directories`, `skipped` (files the snapshot did
not carry, and links a system without them cannot make), `left_at` and `into`, and `verified`, the number of
stores verified; `stores` lists each when there are several. With `--secret-store-only`, a snapshot that carried
no store answers `status: nothing-to-verify` and writes nothing. The folder the settings name is never written, locked or swept, so a snapshot from another
machine needs no `--secret-store-target`, and the restore record doctor reads is not written: a verification
neither raises nor clears `backups-secret-store-not-restored`. With `--secret-store-only` no workspace is
needed. The verified copy stays in `DIR`, every key in plaintext, for you to remove when the check is done; a copy that fails to verify
is removed at once (`secret-store-verify-failed`, exit 1, and no home is restored), and a restore that fails
after the copy removes it and the folders it made for `DIR`. The temporary home's restored settings still name
the live store, so run no other command against that home, and remove it with `DIR` once the check is done. Refused, exit 2, nothing
written: `secret-store-verify-into-not-empty` (DIR holds something, or is a link or a file),
`secret-store-verify-into-refused` (DIR is, holds or lies inside the new home, this workspace or the default
one, the snapshot file (a DIR beside it, such as `~/obs-transfer/verify` next to a carried `.obsnap`, is fine),
a backups folder, a folder that the snapshot's settings or a
workspace's settings name in `sources.secret_store` or `sources.secrets`, `OBSERVATORY_SECRETS` or
`OBSERVATORY_VAULT_DIR`, or one of the places a store is never written; compared by name and by identity, so a
linked parent or macOS's `/System/Volumes/Data/...` spelling of the same folder is refused too; on a volume
without inode numbers, such as FAT or exFAT, the name alone decides), `secret-store-verify-into-unchecked` (a
workspace on this machine whose settings do not read, so the store folders they name are unknown; repair them
first), and
`secret-store-verify-into-conflict` (beside `--secret-store-target` or a `--secret-store` policy other than
`auto`, which only mean something when the store is written in place). A tampered carried file is refused
before anything is written (`secret-store-manifest-mismatch`).

**A slot that stays on this machine on purpose** (a root seed, an administrator token whose single home is
this machine) is marked once, and no snapshot carries it from then on:

```sh
project-observatory full vault-digests keep-here PROJECT ENV NAME --why "..."   # why, 8 to 200 characters
project-observatory full vault-digests unkeep-here PROJECT ENV NAME            # the next snapshot carries it again
```

The mark lives in the slot's metadata, survives a new value, and is journaled; the snapshot leaves the slot,
its metadata and every archive `vault.py rotate` kept of it (`NAME.retired-*`) behind, names each with the
reason `this-machine-only`, and still reads as carried. A slot whose metadata does not read (not JSON, not
UTF-8, over 64 KiB, not a regular file) stays behind the same way with the reason `unreadable`: a mark that
cannot be read may be a mark. `full vault-digests` shows it as `only here
on purpose` here and `only on <machine> on purpose` on the other machine, neither of which counts as a
difference.

`backups.secret_store` in `full doctor` and `full backups status` names it (`path`, `settings_keys`, `exists`),
the newest record of a snapshot carrying it (`last_carry`, read from `store/raw/secret-store-carry.json`) and of the
last restore of one (`last_restore`, from `store/raw/secret-store-restore.json`), and four warnings follow from it:

| Code | When | Step |
|---|---|---|
| `backups-vault-store-not-carried` | the store exists and no snapshot carried it, the last one did not (its reason), or the last carry is more than 48 hours old | `full workspace-backup --writers-stopped`, or `full backup-passphrase set` when the reason is `no-passphrase`; a release whose snapshots do not carry the store names a copy by hand instead |
| `backups-secret-store-not-restored` | the last restore skipped the store the backup carried (its reason: a folder not confirmed, refused, busy or unwritable) | the restore's `next`, for example `full restore SNAPSHOT --secret-store-only --secret-store-target PATH` |
| `backups-secret-store-missing` | the folder the settings name does not exist on this machine | `full configure sources secret_store PATH` (or `secrets`, the setting that names it) |
| `backups-secret-store-env-override` | the environment moves the vault away from the folder the settings choose: `OBSERVATORY_VAULT_DIR` names another slot folder than `<store>/projects`, or `OBSERVATORY_SECRETS` names another folder while `sources.secret_store` is unset (with it set, `OBSERVATORY_SECRETS` moves nothing) | `full configure sources secret_store PATH`: the daily job may not share the shell's environment, and only the folder the settings name is carried |

Until the first snapshot taken by this release, an external store reads `backups-vault-store-not-carried` with the
reason "no snapshot has carried it yet" and the step `full workspace-backup --writers-stopped`. A store inside the workspace reads `{"external": false}` and is carried with it. With no
setting naming a store and `OBSERVATORY_SECRETS` pointing outside the workspace, the section reports that folder
as external, with `settings_keys: []` and `environment: "OBSERVATORY_SECRETS"`, and only the env-override row. These
codes, except the two a maintenance row already says (no passphrase, unencrypted local copies), join `full maintain
status` and the Health page's maintenance rows, and so doctor's `attention`.

**From 0.17.0 a passphrase exists by default.** When none is configured, the maintenance job
(or `full init` in a terminal) generates one from 32 random bytes. The passphrase lives in
`<home>/secrets/backup-passphrase` (mode 600) and is **also kept outside the workspace**, so
a deleted workspace or a reinstall can still open its backups:

| Platform | Where the copy is kept |
|---|---|
| macOS | the login Keychain, item "Project Observatory backups", account `<home-name>-<instance>`; an account with no login Keychain to write to (a headless one) keeps it in `~/.config/project-observatory/backup-passphrases/<label>` (mode 600), and the answer names `file` |
| Linux | the Secret Service through `secret-tool`; without it, `~/.config/project-observatory/backup-passphrases/<label>` (mode 600) |

The value goes to the Keychain on `security -i`'s stdin and to `secret-tool` on stdin,
never as an argument. A passphrase you set yourself is copied there too. One supplied through
`OBSERVATORY_BACKUP_PASSPHRASE` is yours to keep and is not copied.

`backup-passphrase show` prints it only to a terminal. Keep a copy off the machine as well:
restoring this machine's backups on another machine needs it unless iCloud Keychain carries it
there, and nothing can recover it. Each machine keeps its own passphrase: never set this
machine's on another one to open a backup there (that replaces the other machine's own); give
it to the restore once instead, as below.

**The kept copy is never written over.** If `secrets/backup-passphrase` is lost while the
kept copy exists, the job restores the file from it instead of generating a new passphrase.
When you set a new passphrase, the one kept before moves to `<label>-previous`, because
backups taken before the change open only with it; a restore tries both.

**A reinstall restores.** A full snapshot is taken every day. Run `full init` in a terminal
on an empty home, or `full restore --latest` anywhere. It finds the newest backup a workspace
at the same path left, in every backups root this machine knows, opens it with the kept
passphrase and restores it. `init --fresh` starts empty instead. When backups of more than
one workspace with this name exist (a reinstall that could not open its backups starts a
second, empty workspace with daily backups of its own), nothing is chosen: the answer lists
them and `full restore --latest --label LABEL` picks one. When no kept passphrase opens the
backup, `init` creates the workspace and prints the backup it found and a command to restore
it into another, empty home.

```sh
# On another machine: restore asks for the first machine's passphrase once, at a terminal
project-observatory --home NEW_EMPTY_HOME full restore snapshot-….obsnap
project-observatory full backups decrypt observatory-db-….obsdb ./copy.db
```

**Another machine's backup opens with its passphrase given once, and nothing stores it.** A
restore tries, in order: `OBSERVATORY_BACKUP_PASSPHRASE`, a value read with `--passphrase-stdin`,
this workspace's own passphrase, the ones this machine's credential store keeps for the backups
folder's label, and last, at a terminal, one prompt that names the folder
(`Backup passphrase for <label>: `). A wrong candidate costs one chunk of the file. The value
that opens it is used for that restore only: it is not written to the workspace, to settings, to
the Keychain or any other store, to a log or to the answer, which says only where it came from
(`passphrase_source`: `given`, `environment`, `stdin`, `this-workspace`, `stored`,
`stored-previous` or `prompt`). `full restore --latest` asks once per backups folder when no
kept passphrase opens it; `full machine-restore` takes `--passphrase-stdin` too.

`--passphrase-stdin` reads at most 4096 bytes of UTF-8 from a file or a pipe and removes one
trailing newline. It refuses a terminal (`passphrase-stdin-terminal`: there the prompt is the
route, and it shows nothing typed), an empty value (`passphrase-stdin-empty`), a longer one
(`passphrase-stdin-too-long`) and bytes that are not text (`passphrase-stdin-not-text`). When no
candidate opens the file the refusal is `passphrase-refused` and names the sources it tried, never
a value. Each is exit 2 before anything is written.

An agent with shell access to the first machine hands the passphrase over without seeing it,
redirecting the first machine's passphrase file into the second machine's command over SSH
(never `ssh -t`, so the remote stdin is the file and not a terminal, and never `echo` or an
argument). The default home is `~/.local/share/project-observatory-full`; use the first
machine's `OBSERVATORY_HOME` when it set one:

```sh
ssh SECOND 'project-observatory --home NEW_EMPTY_HOME full restore ~/obs-transfer/daily-XXXX.obsnap --passphrase-stdin' \
    < ~/.local/share/project-observatory-full/secrets/backup-passphrase
```

Without such access, the person at the second machine types it once at the prompt.

**A restore under another home rewrites the old home's paths.** A snapshot records the home it
was taken under (`home_path` in its manifest). Restored under another home, every source and the
backups root (`config/settings.json`) and every `include` and `roots` entry of
`config/machine-backup.json` that named the old home become `~/...`, and each folder declared in
`config/tracked.json` moves to the same place under the new home, still absolute and resolved, as
`full track` writes it. A home behind a symbolic link is matched as recorded and resolved. A value
outside the old home is kept as given; `/Users/ann` never matches `/Users/anna`, and a value that
leaves the home through `..` is outside it. The answer's `paths`
block lists what was rewritten and, in `unresolved`, every declared folder (with
`full track local-folder PATH`), source or backups root that does not
exist afterwards (`reason: missing-after-restore`) with the exact `configure` step that points it
elsewhere; a declaration file that cannot be read is named there too and left as it is. A missing
source is not a refusal: the restore completes and `doctor`'s `coverage_warnings` keeps naming it.
A same-home restore keeps `settings.json` byte for byte. When `restore --latest` finds no backup
of a workspace at this path but the same workspace left backups under another home (its owner
file names `<other home>/<the same path under the home>`), the `no-backup` answer lists them under
`elsewhere`, never chooses one, and `next` names `full restore 'FILE'` for the newest.

A rolled-back update keeps the changed workspace beside the restored one as
`<home>.failed-update-…`: a full, unencrypted copy with `secrets/`. Nothing removes it;
`backups status` and `doctor` name it, and you delete it once the restored workspace is
confirmed.

`workspace-backup --output DIR` writes a plaintext snapshot directory where you name it; it is
not encrypted. Since OBS-389 it refuses when the workspace holds secrets (a secret store inside the
workspace, such as `secrets/`, with a file in it) and names two ways forward: run `workspace-backup`
without `--output`, which exports the snapshot encrypted once a backup passphrase is set, or add
`--plaintext` to write the plaintext copy anyway. With `--plaintext` the answer carries
`"plaintext": true` and a `warnings` line; keep that folder as private as the vault and delete it when
done. A workspace without secrets writes `--output DIR` as before.

### This machine's agent setup, and moving to a new machine

The workspace snapshot carries the engine's own data. Everything around it that makes a
machine an agent machine is backed up separately, in the **same backups root**, encrypted
with the **same passphrase**, as one file per run: `machine-<UTC stamp>-<id>.obmachine`
(`machine_backup.py`, OBS-157). It is taken once a day by the maintenance job, before every
engine update (unless one younger than an hour exists), and when you ask:

```sh
project-observatory full machine-backup            # take one now; prints size, items and what was not carried
project-observatory full machine-backup status     # the newest, its age and size, what it did not carry
```

What one bundle holds, by category:

| Category | What |
|---|---|
| Claude Code | `~/.claude/settings.json`, `settings.local.json`, `CLAUDE.md`, `keybindings.json`, `agents/`, `commands/`, `hooks/`, `skills/`, the plugin registry (`~/.claude/plugins/installed_plugins.json`, `~/.claude/plugins/known_marketplaces.json`) and `~/.claude.json`; `~/.agents/skills` |
| Codex | `~/.codex/config.toml`, `~/.codex/AGENTS.md` |
| Git, SSH, shell | `~/.gitconfig`, `~/.gitignore_global`, `~/.config/git/`, `~/.ssh/`, `~/.zshrc`, `~/.zprofile`, `~/.zshenv`, `~/.bashrc`, `~/.bash_profile`, `~/.profile` |
| CLI logins | `~/.config/gh/`, doctl's configuration, `~/.config/heroku/`, `~/.netrc` |
| Jobs and services | `~/Library/LaunchAgents/*.plist` (macOS) or `~/.config/systemd/user/` (Linux); the Fabric service descriptors directory |
| Inventories | Homebrew (`brew bundle dump`), global npm, pipx and uv tools; this engine's workspace path, version and newest snapshot |
| Checkouts | every Git checkout under the `projects` source and the declared local folders (or `roots`, below): remote URLs without user information, branch, HEAD, dirty and ahead/behind; one `git bundle` per repository of every commit no remote has (on branches, local tags, a detached HEAD of any of its worktrees, and every stash entry) plus its tag names, a patch of uncommitted changes (for a repository with no commit yet, its staged changes) and the untracked files with their modes, each under a cap; a shallow clone keeps its boundary |

A symbolic link is recorded as a link with its target, never followed. Left out on purpose
and named in the bundle's `MANIFEST.json` under `skipped`: agent transcripts
(`~/.claude/projects`, opt-in), plugin caches and marketplace clones, `.DS_Store`, sockets.
Git-ignored files in checkouts (build output, a local `.env`) are not carried: credentials
belong in the vault, which the encrypted workspace snapshot carries, inside the workspace or outside it
(a secret store kept outside the workspace is skipped here and named in `skipped`).

The caps, each a key of `limits` (below): `file_mb` 16 MB (one file), `entry_mb` 128 MB (one
entry, such as `~/.claude/skills`), `total_mb` 320 MB (every file copy together), `bundle_mb`
160 MB (one repository's bundle of unpushed commits, estimated before it is written),
`patch_mb` 64 MB (one checkout's uncommitted changes), `untracked_mb` 32 MB (one checkout's
untracked files together), `checkouts_mb` 768 MB (everything carried for checkouts), and
`min_free_gb` 2 GB left free on the disk that holds the backups root. Anything over a cap is
**not carried** and named, per folder, in `MANIFEST.json`, in the command's answer, in `doctor`
(`machine_backup.not_carried`), on the Health page and as a finding on the board
(`machine.backup_not_carried`, a warning when what is left behind is the only copy of commits
or uncommitted changes). Each such item carries its `fix`: push the branch so a remote holds
the commits, or the exact `limits` value that would carry it, written as the JSON to put in
`machine-backup.json`. The only copies of work are listed first.

Why these caps: on 2026-10-20 a working agent machine (476 checkouts, a bundle of 87 MB in
104 seconds) left 19 items behind under the earlier caps of 16 MB per bundle, 8 MB per patch
or untracked set and 64 MB for all checkouts. Ten were repositories whose unpushed commits
made bundles of 16.7 to 130 MB (and one of 404 MB, a single commit of a large file); two were
patches of 23 and 59 MB. At the caps above all of them but the 404 MB bundle and a 161 MB set
of untracked files are carried, for about 560 MB more per bundle. (Caps count MiB: the bundle
cap is 160, not 128, because the largest of those bundles, 32 commits that were the only copy
of their work, is 130.0 MiB.) A carried part is spooled to
the private stage and streamed into the bundle, so a larger cap costs disk (checked against
`min_free_gb`), never memory. Three bundles are kept.

Every decrypted or temporary copy, during a backup or a restore, lives in one private folder,
`<home>/store/.machine-stage/` (mode 700; before a workspace exists,
`${XDG_DATA_HOME:-~/.local/share}/project-observatory/machine-stage/`). A restore decrypts only
what its current step needs and removes it before the next step; SIGTERM and SIGHUP remove it
too, and whatever a killed run left is swept when the next backup or restore starts. A second
backup or restore while one runs is refused before it touches anything.

Nothing is written without a passphrase: a bundle holds `~/.ssh` and CLI logins, so it is
never written unencrypted, not even inside the workspace. A write is also refused when the
root's disk would keep less than 2 GB free; the refusal is recorded and shown by `doctor`,
`maintain status` and Health (`machine-backup-refused-disk`), and the next attempt is an hour
later. `doctor` warns when the newest bundle is older than 36 hours.

**Your own paths** (a private agent's configuration, a tool this list does not know) go in
`<home>/config/machine-backup.json`, which stays on this machine:

```json
{"include": ["~/.config/my-agent", "/opt/my-agent/etc/agent.toml"],
 "exclude": ["~/.ssh/*.old", "*.log"],
 "roots": ["~/src", "~/work"],
 "transcripts": false,
 "enabled": true,
 "limits": {"total_mb": 160, "entry_mb": 32, "min_free_gb": 2}}
```

Every key is optional; an unknown key or a wrong value is refused by name (`doctor` shows it
as `machine-backup-settings`). `exclude` patterns match a path written `~/…`, or a bare name.
`limits` takes `file_mb`, `entry_mb`, `total_mb`, `bundle_mb`, `patch_mb`, `untracked_mb`,
`checkouts_mb`, `min_free_gb`, `depth` (how deep `roots` are searched, default 4) and
`checkout_seconds` (the checkout inventory's time budget, default 900). `"enabled": false`
stops the daily and pre-update bundles.

**Moving to a new machine, step by step:**

1. On the old machine, keep the passphrase somewhere off the machine:
   `project-observatory full backup-passphrase show` (it prints only to a terminal).
2. Copy the backups root (`full backups status` names it) to the new machine, into the same
   place: `~/Documents/Project Observatory/Backups/` on macOS. With iCloud Desktop & Documents
   on, it is already there.
3. On the new machine install the engine (README, Install), then look before writing:
   `project-observatory full machine-restore --latest` prints every item, where it goes and
   whether it is `missing`, the `same` or a `conflict`. Without a workspace it asks for the
   passphrase at the terminal, or reads `OBSERVATORY_BACKUP_PASSPHRASE`. A bundle copied
   elsewhere is named instead: `full machine-restore /path/to/machine-….obmachine`.
4. `full machine-restore --latest --apply` writes only what is missing, with each file's mode
   (so `~/.ssh` is 700 and a key 600). `--overwrite` replaces a conflict, moving the existing
   file aside as `<name>.before-machine-restore-<stamp>`.
5. `full machine-restore --latest --packages` installs Homebrew's Brewfile (`brew bundle
   install --no-upgrade`) and the npm, pipx and uv tools. Install Homebrew first.
6. `full machine-restore --latest --clone` clones every recorded checkout that is not there
   yet (with your own Git configuration and the SSH keys step 4 put back), adds its other
   remotes, fetches its carried commits and tags, puts back its stash entries in order, checks
   out its branch (or detaches HEAD where it was detached), applies its patch and writes its
   untracked files with their modes. A repository that had no commit yet is initialised and
   its staged changes are staged again. A checkout that exists is never touched; a worktree is
   added to its restored main checkout, detached where it was. A checkout that could not be
   restored, or whose carried files are missing, is named in `failed` and the command exits 1.
7. `project-observatory full init` (or `full restore --latest`) brings back the workspace
   itself from its own snapshot, then `full maintain ensure` schedules the job.

The plan and every answer carry paths, counts and reasons, never a file's contents.

**Which machine a bundle or snapshot came from (0.30.1).** Each bundle and each workspace
snapshot names the machine it was taken on (`machine_id`: `m-` and 24 hex digits of a SHA-256
over the platform's own machine identifier, which is never written; `full machine-move show`
prints this machine's). Restored on the machine that took it, both behave as above. Restored on
another machine, or when they name no machine (taken before 0.30.1), they restore as a **second
machine**, below. `--same-machine` restores whole anyway and is accepted only at a terminal:
use it when you replace a machine and want its identity back, never for a machine that keeps
running. `full machine-restore --second-machine` asks for the second-machine way on any machine.

**Moving to a second machine that runs beside this one** (OBS-283, OBS-284). When both
machines keep running, a whole restore would give the second one this machine's SSH key, start
every one of its launchd jobs at the next login (a bot polling one token on two machines, a
scheduler acting twice) and copy its agent-network identity. A second-machine restore does none
of that:

- **SSH.** `~/.ssh/config` (and `config.d/`), `known_hosts`, `authorized_keys`,
  `allowed_signers` and each `*.pub` whose private half is not in the bundle are written (the
  `.pub` of a withheld key is withheld with it: the new key gets its own). Nothing else of
  `~/.ssh` is, whatever its name, and a file whose text is a private key is withheld under any
  name. The plan shows such an item as `withheld`; only a key (named by an `IdentityFile`, a
  default name such as `id_ed25519`, or private-key text) gets two `ssh-keys rekey` steps (the
  plan, then the same with `--apply`), and a withheld file that is not a key, such as an `Include`d fragment, gets none: `ssh` skips an
  `Include` that matches no file. The second machine makes its own key.
- **Jobs.** Every launchd plist (a systemd user unit off macOS) is written into
  `${XDG_DATA_HOME:-~/.local/share}/project-observatory/machine-move/LaunchAgents/`
  (`systemd-user/` off macOS), which nothing loads at login, and is named in the placement file
  below as running on the machine the bundle came from. A job this machine already defines is
  left as it is. The plan shows a staged item with `staged` and its `label`. This engine's own
  jobs (`org.project-observatory.*`, `project-observatory-*` off macOS) are `withheld` instead:
  every machine installs its own with `full maintain ensure`.
- **The workspace.** `full restore` leaves out `node/device/`, `node/orgs/`, `node/records/`,
  `node/shares/` (a key someone shared with you is sealed to the machine that accepted it, so
  they share it again to the second machine if it needs it; the shares you sent are renewed and
  revoked from the machine that made them), `config/cloud.json` (the second machine deploys its
  own mailbox with `full cloud deploy` if it sends shares), `<home>/store/maintenance.json` and
  `secrets/backup-passphrase` (OBS-348: each machine keeps its own backup passphrase; a
  same-machine restore keeps the snapshot's). Until the second machine makes its own, every
  snapshot it takes, `secrets/` included, stays unencrypted inside the workspace: run
  `project-observatory full maintain ensure`, which the restore's `next` names (a `full init` at a
  terminal does it too; one run over SSH without a terminal does not). Until then `doctor` shows
  `backups.encrypted: false`, `backups.passphrase: missing` and a warning that names the same step.
  The first machine's passphrase does not travel either, so its backups, the snapshot just
  restored included, open only with it: if that machine may be retired, keep its passphrase
  somewhere off both machines now (the restore answer's `machine.notes` says so). It
  turns `features.scheduler` off, holds off every app enrolled `always_on` (the restore names the
  `full lifecycle policy` step that turns it back on, to run on one machine only) and gives the
  workspace its own `instance_id`, so the second machine's daily backups go to their own folder of
  the backups root and never rotate the first machine's. The vault and everything else, the CLI
  logins the machine bundle carries included, are restored: they are your own credentials and
  work on both machines. A vault kept outside the workspace is carried in the encrypted snapshot
  and written where the restored settings name it (see "Putting the store back" above), and a slot
  marked `keep-here` never leaves the first machine. The first machine's own vault digest manifest
  (`<state>/vault-digests/local.json`) becomes a peer here, `peers/<its machine>.json` (or
  `peers/peer-<hex>.json` when its name is this machine's), so this machine's first
  `vault-digests write` makes its own under the carried estate key and the view compares against the
  first machine at once; the answer's `digest_manifest` says where it went.
- **Which machine is which.** Each machine has an id derived from its platform identifier (never
  written). A snapshot or bundle that names another machine, or names none, is restored the
  second-machine way; `--same-machine` at a terminal restores one whole. `full init` on an empty
  home restores a snapshot that names no machine whole and says so, with the step that restores it
  as a second machine. A cloned disk image or virtual machine shares its source's identifier and
  reads as the same machine: restore those with `--second-machine`.

On the second machine, in order:

```sh
project-observatory full machine-restore --latest                 # the plan: `machine.mode` reads second-machine
project-observatory full machine-restore --latest --apply         # nothing private of ~/.ssh written, jobs staged
project-observatory full machine-restore --latest --packages
project-observatory full ssh-keys rekey NAME --fingerprint SHA256:...          # the plan for this machine's own SSH key, as `ssh_keys` names it
project-observatory full ssh-keys rekey NAME --fingerprint SHA256:... --apply  # before --clone: make it, send it to GitHub and each server, verify
project-observatory full machine-restore --latest --clone
project-observatory full restore --latest                         # the workspace, without the node identity; asks once for the first machine's passphrase
project-observatory full node --init                              # this machine's own device id
project-observatory full machine-move show                        # this machine's id and the staged jobs
```

The key comes before `--clone` because the other machine's private key was not written: a
checkout whose remote is `git@github.com:…` clones only once GitHub knows this machine's key.
Use the name the plan gives: when the carried `~/.ssh/config` points a host at a key with
`IdentityFile`, the plan lists that key as withheld with the hosts that use it, its `fingerprint` and
two steps under the same name (OBS-300), so the carried config finds the new key:
`ssh-keys rekey NAME --fingerprint SHA256:...`, which prints rekey's plan and changes nothing, then the
same with `--apply`, which makes the key (OBS-285) and sends it to GitHub for sign-in and signing where
the account gh is signed in to holds the other machine's key, and to each Host that names it,
verifying each, and then lists what still accepts only the other machine's key with the next step for
each. `next` lists both before `--clone`. The fingerprint is a public fact the plan reads from the
bundle (the private file's clear header, else the key's `.pub`, never its private section), and only
keys directly in `~/.ssh` carry it: a second machine never gets `NAME.pub`, so rekey could not match
the key on GitHub without it. When the bundle does not show it, the steps have no `--fingerprint` and
the key's `note` says to take it from `full ssh-keys list` on the other machine; without it nothing goes
to GitHub. rekey reaches a Host through a line naming exactly `~/.ssh/NAME`; a host the plan lists
through another spelling (`~/.SSH/NAME`, another account's absolute path) is added to both steps with
`--server ALIAS`, and the `note` says to point that line at `~/.ssh/NAME` where the path it names is
not this machine's key; and when the restore leaves `~/.ssh/config` out (`--skip ssh-config`) or keeps
this machine's own different `~/.ssh/config` (a conflict, without `--overwrite`) the `note` names
those hosts instead. With `--skip ssh-config` the `note` gives the step that writes the carried config.
With a kept config it says to copy those Host blocks from the other machine's `~/.ssh/config` by hand:
no restore step replaces `~/.ssh/config` alone, and `--overwrite --only=ssh-config` also replaces
`known_hosts`, `authorized_keys`, `allowed_signers` and a lone `.pub` where they differ (the `note`
lists the files it would replace here, each moved aside), and a replaced `authorized_keys` can shut
out a remote login to this machine. A Host whose alias rekey does not take (a `/` in it, say) is
named in the `note` with the `ssh-keys send NAME --to server:ADDRESS --verify` step for the address
its `HostName` gives, which reaches it with ssh's defaults rather than the User, Port or ProxyJump
that Host sets (the `note` says to add `user@`, or to give it a plain Host alias when it needs a Port
or ProxyJump), or, with no such address, the advice to give it a plain Host alias. For a default key such as `id_ed25519` the `note` adds that ssh offers it to
every host whose config names no key, so each such server is added with `--server ALIAS`: rekey names
those hosts as not known, never guesses them. A server whose only access was the other machine's key does not let
this machine in yet: the answer gives the step to run on the other machine, or `--interactive` for a
password. Granting `gh` the `admin:public_key` and `admin:ssh_signing_key` scopes
(`gh auth refresh -h github.com -s admin:public_key,admin:ssh_signing_key`) and typing the new key's
passphrase are yours.

**Moving only part of it** (OBS-282). Every row of the plan names its `category`, and the
answer's `categories` says per category whether it is selected, how many items it holds in each
state and the first destinations an `--apply` would write. `--only` and `--skip` (repeatable, or
comma-separated; not both) limit what `--apply` writes and whether `--packages` runs:

| Category | What it holds |
|---|---|
| `ssh-private` | private SSH keys, by name or by their text, and the public halves of those keys; a second machine never gets them, whatever `--only` names (only `--same-machine` at a terminal does) |
| `ssh-config` | `~/.ssh/config` and its fragments, `known_hosts`, `authorized_keys`, `allowed_signers`, a `*.pub` on its own |
| `launchd` | background jobs: launchd plists on macOS, systemd user units elsewhere (staged on a second machine) |
| `cli-logins` | the `gh`, `doctl` and `heroku` logins and `~/.netrc` |
| `claude`, `codex` | Claude Code and Codex configuration |
| `git`, `shell` | Git settings; zsh and bash profiles |
| `fabric-services` | Fabric service descriptors |
| `packages` | the inventories `--packages` installs; `--packages` with this category left out is refused |
| `agents` | `~/.agents/skills` |
| `operator` | the paths `machine-backup.json` includes |

A category is decided by the path an item is written to, as the second-machine rules are: a key
included from another folder is still `ssh-private`. A left-out row reads `left-out`; a folder that
holds a selected item is written with its own mode even when its category is left out, so a key never
lands in a folder made with the default mode. An unknown category is refused with the list above.
`--projects` limits `--clone` to the named checkouts: a folder name (`alpha-web`), a glob over it
(`alpha-*`), or a path that names every checkout under it (`~/src/work`); a worktree brings its main
checkout, and a name no checkout matches is refused before anything is written. The plan's `next`
repeats the filters, so each step names exactly what you chose:

```sh
project-observatory full machine-restore --latest --skip launchd,cli-logins           # the plan, per category
project-observatory full machine-restore --latest --apply --skip launchd,cli-logins
project-observatory full machine-restore --latest --clone --projects alpha-web,beta-api
```

Each step that writes (`--apply`, `--packages`, `--clone`) records what it did in
`${XDG_DATA_HOME:-~/.local/share}/project-observatory/machine-move/restore.json`: the bundle, the
machine it came from, the categories left out, the counts, the failures, and the fingerprints of the
other machine's keys it withheld. `full machine-move status` reads it.

Then move each job that should run on the second machine, **one at a time, the old machine
first**:

```sh
project-observatory full machine-move leave LABEL --to MACHINE_ID          # on the old machine, with the second's id: stop it, stage it
project-observatory full machine-move cutover LABEL                        # on the second machine: load it here
```

`leave` stops the job, moves its plist into the old machine's staging folder (so the next login
does not start it again) and names it there as running on the second machine. `cutover` runs only
at a terminal: it asks whether `leave` ran on the other machine and moves nothing until you type
the job's label back (a job started here while it still runs there runs twice). It then moves
the staged plist into `~/Library/LaunchAgents`, enables and loads it, and removes it from the
second machine's `elsewhere`; a load that fails puts the plist back in staging. A job that stays
on the old machine needs nothing: on the second machine it stays staged and named as running
elsewhere.

A staged plist can be days old: the restore staged it, and a service released anew since then lives
in a new release folder. Before it moves anything or asks you, `cutover` checks that the staged plist's
`Program`, `ProgramArguments[0]` and `WorkingDirectory` exist on this machine; when one does not, it
refuses that label with `staged-plist-stale`, naming the missing path (OBS-390). Write the job's current
plist into `~/.local/share/project-observatory/machine-move/LaunchAgents/` (replacing the stale one),
then run `cutover` again.

**An account nobody logs in to** (macOS, OBS-361 part 1). An OS account with no graphical login, such
as a second "space" on a shared computer, has no `gui/<uid>` launchd domain, and launchd refuses its
agents even when root asks. When `launchctl print gui/<uid>` answers that the domain does not exist,
`cutover` runs the same checks, then moves the plist into
`~/.local/share/project-observatory/background/LaunchAgents/` (owner-only, the plist made unwritable by
group and others) instead of `~/Library/LaunchAgents`, removes the job from `elsewhere`, and answers
`"state": "waiting-for-root"` with `human_steps` and `next` naming the one command an administrator
runs:

```sh
sudo /usr/bin/python3 -I /usr/local/libexec/observatory-space-daemons apply --user ACCOUNT --label LABEL
```

That command writes the job to `/Library/LaunchDaemons/LABEL.plist` with `UserName` and `GroupName`
set to the account (launchd.plist(5): they apply only in the system domain), so it runs as that
account, never as root, and starts again after a reboot with nobody logged in. Root never runs a file
the account can write, so the administrator first installs a root-owned copy once per computer, from
the release itself, downloaded and verified in the administrator's OWN account (never copied from the
space account's folders, which that account could have changed), with the release's version for
`X.Y.Z`:

```sh
gh release download vX.Y.Z -R passioncode-ai/project-observatory-dashboard -p 'SHA256SUMS*' -p '*.whl'
#   without gh: curl -LO https://github.com/passioncode-ai/project-observatory-dashboard/releases/download/vX.Y.Z/SHA256SUMS
#   and the same for SHA256SUMS.asc and project_observatory-X.Y.Z-py3-none-any.whl
shasum -a 256 -c SHA256SUMS --ignore-missing          # the wheel matches the release's sums
curl -LO https://raw.githubusercontent.com/passioncode-ai/.github/main/release-signing/passioncode-release-signing.asc
gpg --import passioncode-release-signing.asc          # gpg is not on macOS by default: brew install gnupg
gpg --status-fd 1 --verify SHA256SUMS.asc SHA256SUMS | grep -q "VALIDSIG 63B30DC324BD697487AA31944FAFB8AEC803B6A7" && echo signed
unzip -p project_observatory-X.Y.Z-py3-none-any.whl observatory/engine/tools/space_daemons.py > space_daemons.py
sudo install -d -o root -g wheel -m 0755 /usr/local/libexec
sudo install -o root -g wheel -m 0755 space_daemons.py /usr/local/libexec/observatory-space-daemons
```

The signature is checked against the organization's pinned release key, the same fingerprint
`full update` checks (`release_signature.py`, `PINNED`; the key is published in the organization's
[release-signing](https://github.com/passioncode-ai/.github/tree/main/release-signing) folder): a bare
`gpg --verify` would accept any key in the keyring. Without gpg, the administrator's own Observatory
install checks the same pinned key:

```sh
python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import release_signature as r; sys.exit(r.verify(open("SHA256SUMS", "rb").read(), open("SHA256SUMS.asc").read()) != sys.argv[2])' "$(project-observatory full-path)" 63B30DC324BD697487AA31944FAFB8AEC803B6A7 && echo signed
```

`observatory-space-daemons --version` prints the version and the file's sha256 for information only:
the verification is the lines above. `machine-move cutover` names the same steps in `human_steps`.

The `-I` in the command is required: in isolated mode Python leaves the script's folder and the user's
site-packages out of its module path and ignores every `PYTHON*` variable, so nothing the account
wrote is imported as root. `apply` and `remove` refuse without it (`not-isolated`, naming the command)
and from a copy that is not root's alone (owned by root, not writable by group or others, not a link,
in folders only root can write). Still check the folder before the copy goes in (`ls -ld /usr/local
/usr/local/libexec`: owned by root, not writable by group or others); where a package manager owns
`/usr/local`, use another root-only folder and the same command with its path. `plan --user ACCOUNT [--from
background|staged] [--json]` shows what `apply` would write for each plist and why one is refused,
and loads nothing; `status --user ACCOUNT` lists the daemons it wrote, loaded or not, and whether the
file is still as written. A plist is refused, never partly installed, unless it keeps to the allowed
keys (`Label`, `ProgramArguments`, `Program`, `EnvironmentVariables` without `DYLD_*`, `WorkingDirectory`, `RunAtLoad`,
`KeepAlive` (a bool, or `SuccessfulExit`, `Crashed`, `AfterInitialDemand`, and `PathState` with paths
inside the home; not `OtherJobEnabled`), `StartInterval`, `StartCalendarInterval`, `StandardOutPath`, `StandardErrorPath`,
`ThrottleInterval`, `ProcessType`, `Nice` of 0 or more, `LowPriorityIO`, `ExitTimeOut`,
`AbandonProcessGroup`, `Umask` of 0 to 0777, `SoftResourceLimits` of launchd's limit names to
non-negative integers; `LimitLoadToSessionType` and `AssociatedBundleIdentifiers` are dropped, `plan`
noting `stripped`; `HardResourceLimits` stays refused), its program is owned by root, the account or a
trusted owner, in folders nobody else can write, its `WorkingDirectory` stays inside the account's home
(a `WorkingDirectory` of `/` becomes the home, `plan` noting `working-directory-home`),
every integer fits 0 to 2147483647, and the daemon it would become writes as a plist. Its label is
letters and digits, then `. _ @ -` (at most 128; `com.apple.` refused), carried by no other plist in
`/Library/LaunchDaemons` or `/System/Library/LaunchDaemons`; `cutover` checks the same rules before it
moves anything (it names each problem and leaves the plist where it was) and names the moved file
`LABEL.plist`, and refuses when the job is defined both in `~/Library/LaunchAgents` and in staging or
the background folder.
**Logs** go to a folder root owns, never where the plist said: launchd may open
`StandardOutPath` and `StandardErrorPath` with root's rights, and a link in a folder the account
controls could make root write elsewhere. The daemon writes
`/Library/Logs/ProjectObservatory/ACCOUNT/LABEL.out.log` and `LABEL.err.log`; `apply` makes
`/Library/Logs/ProjectObservatory` root:wheel 0755 and `ACCOUNT` in it root:wheel 0700, clears any ACL
on both (`/bin/chmod -N`), adds one inherited entry that lets the account read (`/bin/chmod +a "ACCOUNT
allow list,search,read,readattr,readextattr,readsecurity,file_inherit,directory_inherit"`), checks that
only that entry is there, and refuses,
installing nothing, when such a folder exists as a link or is not root's. It then creates both log
files ahead of launchd (a new file, never through a link), owned by the account, mode 0600. Inside the
root-owned 0700 folder the account can read and append to its files but cannot create, rename, link
or unlink anything there, so they are safe whichever credentials launchd uses to open them, and the
account cannot replace them. A log file already there is kept only when it is a regular file with one
link, owned by the account (or by root, then given to the account) and not writable by group or
others; otherwise that job is refused (`log-file`) and nothing is installed for it. `remove` leaves
the log files in place and says so. `plan` notes `logs-moved` for a plist that named its own log
paths. A file in
`/Library/LaunchDaemons` the tool did not write is never replaced: what it wrote is recorded in
`/Library/Application Support/ProjectObservatory/space-daemons.json` (a stable interface, readable
without root; its format is in COMPATIBILITY.md). One copy only: a label already loaded as an agent in
the account's GUI domain (`loaded-in-gui`), or held by another account's entry, is refused. Once root loaded the job, a
second `cutover` answers `"state": "complete"`, and `machine-move show` lists it under `background`
as `loaded` (`registered` when root wrote it and it is not loaded, `waiting-for-root` before).
`leave` refuses while root's registry names the job for this account, loaded or not, and names `sudo
... remove --user ACCOUNT --label LABEL`; after it, `leave` stages the plist from the background folder
as usual. `apply` writes the registry entry before the plist, so a crash between the two leaves an
entry whose plist is missing: `apply` writes it again, `remove` cleans it, `status` shows it as
`missing`. `remove` deletes only a file whose bytes are what it wrote or whose label is the job's.
An XML comment holding `--` (which launchd and plutil accept and XML forbids) is tolerated: the
comments are removed from the bytes in one pass and the plist parsed again, so nothing inside a comment
becomes a key. This is close to launchd's reading, not proven identical, so a comment that never closes,
or comments beside a CDATA section, are refused as `xml_comment`.

**Trusted owners** (space daemons 1.1.0). On a Mac where Homebrew's prefix is owned by the host's admin
account and its folders are group-writable by `admin`, every program there would be refused
(`program_owner`, `program_dir_writable`). The administrator may name, in a root-owned trust file,
the accounts and groups whose files and folders a program path may belong to besides root:

```sh
sudo /usr/bin/python3 -I /usr/local/libexec/observatory-space-daemons trust set --owner ADMIN --group admin
/usr/bin/python3 -I /usr/local/libexec/observatory-space-daemons trust show --user ACCOUNT
```

Only accounts that already hold root through sudo, and groups whose members all do, belong there:
`trust set` takes only `wheel` or `admin` as groups and only owners that are members of `admin`
(`dsmemberutil checkmembership -U NAME -G admin`), and refuses when membership cannot be told. Every
read checks the same again, so a demoted owner, or a name or group added by hand, stops being trusted at
once, with a warning. **What trusting costs:** any process running as that admin account, even without
its sudo password, can replace the programs in those folders and so run code as every space that uses
them. Trust the admin only when the spaces are less trusted than the admin. A folder writable by
everyone is still refused unless sticky. The space account is never trusted for its own jobs (that entry is
left out, with a warning). A trust file that is not root's alone (owner root, not writable by group or
others, a real file in a root-only folder, at most 64 KiB) is ignored with a warning, never trusted.
`cutover` reads the same file, read-only, so it accepts what `apply` will. One
definition that fails in `apply` is reported as `failed` with its error, and the others go on. Root cannot see the account's
`XDG_DATA_HOME`, so `cutover` refuses, before moving anything, in such an account with a custom one.

**The placement file**, `${XDG_DATA_HOME:-~/.local/share}/project-observatory/placement.json`,
owner-only and written atomically, says which jobs this machine holds for another one, until the
estate record carries placement:

```json
{"schema": "observatory-placement/1", "machine": "m-…", "updatedAt": "2026-10-30T09:00:00Z",
 "elsewhere": [{"label": "com.example.bot", "serviceId": "example-bot.default",
                "placedOn": "m-…", "since": "2026-10-30T09:00:00Z", "by": "machine-restore"}]}
```

`serviceId` is the Fabric service whose descriptor names that launchd label, or `null`. Fabric
Dashboards reads the file to show such an agent as placed on another machine and to hide its
start.

**Checking the move** (OBS-286). `project-observatory full machine-move status` prints one
checklist on the machine it runs on, each item `ok`, `missing` (with the step that fixes it) or
`unknown`, with its evidence (`--json` for the same as JSON; exit 0 only when every item is `ok`):

| Step | Item | `ok` when |
|---|---|---|
| M1 | `release` | the other machine runs the release this one runs |
| M2 | `restore` | the restore receipt here shows `--apply` and `--clone` with no failure and no kept conflict (categories left out on purpose are named, not counted against it); on the machine moved from, the other machine's facts say it restored this one's bundle completely |
| M3 | `ssh-key` | this machine has a private key in `~/.ssh` and none of them is the other machine's (by fingerprint, from its facts and from the keys the restore withheld); the fix is the restore's two `ssh-keys rekey` steps (below) |
| M3 | `device-id` | this machine has a device id (`full node --init`) and it is not the other machine's |
| M3 | `vault` | `full vault-digests` reads every slot `same`; status computes it without writing anything and never creates the estate key |
| M4 | `jobs` | no job defined in this machine's job folder (it loads at the next login) is defined on the other machine, or named in this machine's placement file as running elsewhere; this engine's own jobs and a plist marked `Disabled` are not counted |

The `ssh-key` item's steps are the ones the restore hands out: with no key here, `ssh-keys rekey
NAME --fingerprint SHA256:...` and then the same with `--apply` for each key directly in `~/.ssh` that
the restore receipt lists as withheld (its `other_keys`, whose `in_ssh` says where the key sat; a receipt
written before that field is not used for steps) or the other machine's facts name; when neither names
one, the steps say to read the names and fingerprints with `full ssh-keys list` on the other machine.
A key here that is a copy of the other machine's gets `ssh-keys rekey NAME --name NAME-own
--fingerprint SHA256:...` and the same with `--apply` (rekey does not reuse the other machine's key as
this machine's own; `keys/id` becomes `keys-id-own`, and a name taken by anything but this machine's
own key pair gets a number), then the step to point each Host at `~/.ssh/NAME-own` and remove the copy
by hand. Once `--apply` has made `~/.ssh/NAME-own`, status run again names that pair (rekey reuses it
and sends only what is missing), so the steps do not change. For a default name such as `id_ed25519`
the last step says that the hosts ssh offered the copy to without an `IdentityFile` line got the new
key only if they were added to both rekey steps with `--server ALIAS`, and then to add `IdentityFile
~/.ssh/NAME-own` for github.com and each of those hosts (or once under `Host *`): ssh offers a default
name on its own, the new name only where a line names it.

Status reaches nothing on the other machine. What it knows of it comes from that machine's facts,
public facts only (its id, release, the fingerprints of its SSH keys, its device id, the jobs it
defines, its restore summary):

```sh
project-observatory full machine-move facts --out FILE              # on the other machine: a new owner-only file
project-observatory full machine-move status --peer FILE            # here; or put FILE in machine-move/peers/
```

The backups root both machines read is a convenient way to carry the file. Facts older than
`--max-age-hours` (48 by default), or dated in the future, say nothing about today's release or jobs,
so those items read `unknown`; key fingerprints and device ids do not change by themselves, so an
older file still tells them apart. With no facts at all, every item that compares the two machines
reads `unknown`, never a guess. A file that does not read is named in `degraded` and the rest is
still compared, whatever its shape. Facts hold only what the reader takes: a key whose file name the
format cannot carry keeps its fingerprint under a masked name, a job label it cannot carry is left out,
and `omitted` (and a line on stderr) says how many. Status writes nothing and starts, stops or asks
launchctl about no job; on a machine with no platform identifier and no kept id it does not make one
either, and names this machine `unknown` (`full machine-move show` makes the id).

An agent reads the same checklist over MCP: `observatory_machine` with `section: "move"` returns it under `move`
(with `measuredAt` and `degraded` as every answer), from the same function and the same peers folder, and writes
nothing. Every step it names stays a command a person or an agent runs.

For an original installation whose state lived beside source, set a new home
and run `full migrate-local ORIGINAL_DIRECTORY` to preview. Apply requires
`--apply --writers-stopped`. It leaves the original in place and does not start
services. Verify external source paths, enabled features, credentials, registry
counts and database health before choosing which installation becomes active.

## Second machine

Two operators, or one operator on two Macs, run the same engine version and the
same functional configuration; each machine keeps its own paths, credentials and
estate. To carry one machine's setup, vault and checkouts to a second one that keeps
running beside it, restore from its backups instead, as in "Moving to a second machine that
runs beside this one" above. To start the second machine empty, on the new machine:

1. **Install the same release.** From the release page, save `project_observatory-X.Y.Z-py3-none-any.whl`
   and `SHA256SUMS`, check the digest, and install the wheel
   into the Python environment from [SQLite runtime prerequisite](#sqlite-runtime-prerequisite):

   ```sh
   V=X.Y.Z
   shasum -a 256 -c SHA256SUMS --ignore-missing
   python -m pip install --no-deps "project_observatory-$V-py3-none-any.whl"       # the engine, and the lock it carries
   python -m pip install -c "$(project-observatory full-path)/requirements-full.lock" \
       "project_observatory-$V-py3-none-any.whl[full]"                             # its [full] extra at the tested versions
   ```

   The second `pip` line installs the `full` extra against the `requirements-full.lock` the
   wheel carries, the dependency set this release was tested with (as in the
   [README](../README.md#install)).

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

### Which machine holds which key

Each machine has its own vault. `project-observatory full vault-digests` shows every vault slot per
machine as `same`, `differs`, `only here`, `missing` or `unreachable` (with the time of that
machine's last answer), and one line when all match. Machines compare keyed HMAC-SHA-256 digests
under one estate key, never values, so both need the same key once. The key lives at the top of the
secret store, so a second machine restored from an encrypted snapshot that carried the store has it
already (and the first machine's manifest as a peer); `key export` and `key import` are needed only when the
store did not come from a snapshot:

```sh
project-observatory full vault-digests key export /path/to/new-file    # on the first machine
project-observatory full vault-digests key import /path/to/new-file    # on the second; then delete the file
project-observatory full vault-digests write                          # on each: the manifest, no values
```

Until machines exchange manifests by themselves (OBS-194, step 2), copy the second machine's
`<state>/vault-digests/local.json` into the first machine's `<state>/vault-digests/peers/` under the
second machine's name, for example `machine-b.json`. A slot meant to differ is marked with
`full vault-digests mark PROJECT ENV NAME --why "..."`, and a slot meant to stay on one machine with
`full vault-digests keep-here PROJECT ENV NAME --why "..."`; a `vault.py rotate` makes the other
machines' copies read as stale. `observatory_credentials` names, for each vault row, the machines
that hold it. The design: [docs/design/VAULT-DIGESTS.md](design/VAULT-DIGESTS.md).

## Anonymous usage and error data

Project Observatory sends anonymous usage and error data to its maintainers to help improve it. It is on by
default, for new installs and for installs that update to this release, and you turn it off per workspace:

```sh
project-observatory full analytics status   # on or off, why, what is sent and what never is, the queue and counters
project-observatory full analytics off      # nothing is sent from now on; the queue and the install id are deleted
project-observatory full analytics on       # on again, with a new install id
```

The Mac app and the Windows and Linux app have the same switch in Settings ("Send anonymous usage and error
data"). For one process, a CI job or a shell profile, `OBSERVATORY_ANALYTICS=off` or `DO_NOT_TRACK=1` turns it off
without touching the switch; the environment never turns on what you turned off. An update keeps your choice.

**What is sent:** the name of each `full` command, MCP tool call and tick, with its outcome (ok, refused, error,
failed, skipped), its duration, a command's exit code and how many of a tick's steps failed; the class name of an
error (`OSError`, never its message); whether the install is a release or a development build (`environment`), the
platform (`macos`, or `server` on Linux and Windows), the engine version, the OS name and version (on Linux only the
kernel's major.minor) and the Python version; a number that groups the events of one process (`sessionId`: the
time it started and random digits); and a random install id made only for this, kept in
`store/analytics/install-id`, never derived from your machine, account or name. Every event, property and wire
field is listed in [the event catalogue](analytics-events.md).

**What is never sent:** keys or vault values, paths or file names, repository, project, organisation or person
names, anything from the registry or the store, URLs, prompts, arguments, messages or anything you typed.

**How it travels:** a command or tool call appends one line to a local queue and returns; it never waits on the
network. The maintenance job (every 15 minutes) sends the queue to `https://analytics.sshlg.me` in batches of at most
25 and drops events older than 23 hours; a failure is counted in `store/analytics/counters.json` and never shown as
an error. A count that cannot be written is named in `full analytics status` under `degraded`, whichever process failed to write it (the reason is kept beside the counters until the next update is written). A flush takes at most 30 seconds and stops at the first batch the host does not take; the rest waits
for the next pass. `full analytics send` sends the queue now. The first time a command runs at your terminal after
this release, it prints a one-time notice saying this; the dashboard's Home shows it until you first open Home
through the local server and for a week after that, and Health shows the switch with the command that changes it.
A workspace restored on another machine leaves `store/analytics/` behind, so it gets its own install id and never
resends the first machine's queue; your switch moves with it. A build without an analytics key (a source checkout, for one) collects and
sends nothing, and `analytics status` says `app_key: absent`.

## Staying in step

### Updates install themselves (0.17.0 and later)

Nothing needs to be run after a release. One job per workspace runs every 15 minutes: launchd
`org.project-observatory.<id>.maintain` on macOS, a systemd user timer
`project-observatory-<id>-maintain.timer` on Linux, a Task Scheduler task on Windows. A second,
small job of the same workspace (`.check`, `-check.timer`, the task `check`) asks every five
minutes whether a newer release exists: one read of the newest release's `SHA256SUMS` on
github.com (`releases/latest/download/SHA256SUMS`), never the GitHub REST API and its limit of 60
requests an hour, and nothing else. When it finds one it starts the maintenance job at once. It keeps its own lock and its own record, `update-check.json` in the workspace's store folder, so a maintenance run is never skipped or overwritten because a check is running. A
check that finds what the last one found writes nothing to `store/logs/check.log` and no new line
to the shared update log. Each maintenance run does only what is due:

- With `features.auto_cleanup` on, first the disk guard (see [The disk guard](#the-disk-guard)): hourly while space
  is normal, on every pass below the `low` line.

- Every five minutes it runs `full update --check` (the same static read); at login the first
  check waits 90 seconds after the job starts. When a newer **stable** release exists, it runs `full update --apply
  --unattended`, the verified, reversible transaction described below, at a safe point (see
  [When a new release starts running](#when-a-new-release-starts-running)).
- On macOS, when the installed app is older than the engine, it replaces the app with that
  release's app. The new app must pass the GitHub digest, `SHA256SUMS` and its signature by
  the organization's release key, `codesign`, the organization's pinned signing team
  (`KJ35UYYL22`) and Gatekeeper. It is never replaced while this user has it open: the open app
  offers **Restart to update** (app menu and the dashboard's toolbar), and installs the waiting
  version when you quit it or when it has had no window on screen and no input for 30 minutes.
- Once a day it takes a full encrypted backup, `daily-*.obsnap`, the newest three kept. It
  briefly stops this workspace's tick and server for the copy and starts them again. When a
  tick or an update is running, the backup waits for the next pass; a failed backup is tried again an hour later. Snapshots you take yourself
  (`snapshot-*`) rotate separately and are never removed by the daily ones. See
  [Encrypted backups off this disk](#encrypted-backups-off-this-disk).

After a maintenance pass, `project-observatory full maintain status` and
`full doctor` include `log_retention`: separate engine, updater and hook-log
scope receipts. Each scope has its attempt time, directory-entry count and
counts of completed rotations, refusals and failures. Missing/unreadable scopes
have unknown coverage and `observed_count: null`; an observed empty directory
has zero entries. Action counts describe only attempted work, not remaining
logs or reclaimed bytes. A lost whole-scope receipt also keeps action counts null,
since effects may have occurred before the error. A failed scope does not suppress peer results.

The receipt is saved in the workspace store as `maintenance.json`; its readers retain the original
measurement time and mark evidence older than one hour stale. The Health page
shows the same incomplete/stale warning, and its payload contains the typed
scope result. Reasons are fixed codes, without paths, log contents or exception
text. This adds evidence to the existing five-generation size policy; it does
not enable automatic cleanup or change retention limits. A failed operation may
already have moved older generations; no rollback is claimed. Copy/truncate
still has its concurrent-append race, and external directory writers are not
excluded. Synthetic evidence: `test_lifecycle.LogRetentionEvidence` and the
`test_maintenance.Pass.test_log_retention_*` cases.

**The switch, "Install updates automatically"**, is the file `auto-update` in the workspace
home. Without it, updates are on; only the word `off` in it turns them off. The commands
below write it, and you can write it yourself. An update, a reinstall or an uninstall never
writes it, neither `on` nor `off`. Off stops the downloads and the installs, the Mac app's
included; the daily backup keeps running. Off still tells you when a newer version exists: the
check job keeps reading the release feed every five minutes, never downloads or installs, and
announces each newer version once (the job's log line, `full auto-update status`,
`full doctor`, a calm row on the dashboard's Home and on Health, and the Mac app's menu), with
the command that installs it when you choose, `project-observatory full update --apply`. The
version it found is kept as `available` in `store/update-check.json`: `full update --apply`
removes it when it installs that version or a later one, `full auto-update on` removes it at
once, and otherwise the next check does. Off by the older `updates.auto: false` counts the
same. The Mac app shows its menu item exactly while that key is there. An install that turned updates off before
0.19.0 did it with `updates.auto: false` in `config/settings.json`: that still counts as off,
and the next `auto-update` command moves it into the file and says so.

```sh
project-observatory full auto-update status   # on or off, last check, last result, warnings, a newer version waiting
project-observatory full auto-update off      # writes `off`; new releases wait for you and are announced, the daily backup keeps running
project-observatory full auto-update on
project-observatory full maintain status      # everything the job last did
project-observatory full maintain run         # one pass now
project-observatory full maintain app         # only the app step: swap a staged app (quit the app first)
project-observatory full maintain uninstall   # no job at all; it stays off until `maintain ensure`
```

How the job gets scheduled:

- `install_launchd.py install` schedules it together with the tick.
- `full init` schedules it when you run it in a terminal.
- `full update --apply` makes the new release schedule its own version of the job. It never
  turns back on a job you removed with `maintain uninstall`. When the definition changes
  while a pass is running, the new definition is written and loaded at the next scheduling
  outside a pass, so the job is never stopped under itself.
- `full upgrade --apply` (run by `full update`) schedules the job when it runs from a
  terminal.
- The `observatory-log` plugin's session hook schedules it where it is missing. On Linux
  without a systemd user manager, the hook runs the pass itself, and the check too while
  updates are off.

Every job definition (the pass, the check, the tick, the server) names the engine's virtual
environment interpreter and a program name, `<venv>/bin/python3 -P -m observatory.job maintain run`,
never a folder of one Python release such as `lib/python3.14/site-packages`, so a venv rebuilt on
another Python keeps its jobs running (OBS-393). A definition that differs from the one the
installer would write now is rewritten by `project-observatory full maintain ensure` (all four jobs
under launchd and systemd; on Windows the pass and the check) and by the next pass (the tick and the
server), keeping what the installer does not write; `full doctor` names one whose program, argument
or folder is gone (`job-path-missing`). An account with no GUI session answers `waiting-for-root`
with the administrator's two steps, `remove` then `apply`.

`OBSERVATORY_SYSTEM_SETUP=0` in the environment keeps every one of these away from launchd,
systemd, Task Scheduler and the credential store: `full init` answers `not-scheduled-here`,
`maintain ensure` refuses, and `full update --apply` installs the release but schedules
nothing, reporting `"maintenance": "not-scheduled-here"` with `maintenance_detail` naming the
command that schedules the job. Releases up to 0.29.1 replaced the `0` with `1`, and an update
is run by the release it replaces, so the first update that keeps the `0` is the one after the
release carrying this change. Use it for
a second or test workspace. `OBSERVATORY_SYSTEM_SETUP=1` allows them where no person is at a
terminal; the job and the plugin hook set it.

Only one `full update --apply` runs at a time per workspace. A second one, from the job, the
plugin or a person, is refused before it changes anything; the job then tries again the next
hour. The job runs the update in a process of its own and never stops it: a pass that is
itself stopped (`maintain uninstall`, launchd, systemd stopping the unit) leaves the update
running to its end, and the next pass reads the outcome from `store/logs/update.jsonl`. A
reboot or a power loss is different: it ends the update too, and the next pass records it as
`failed` ("the update ended without a final journal entry"). Health and `doctor` then show
the failure; `doctor` also says whether the engine and its jobs run. Where there are no launchd
jobs (Linux), the job and the plugin pass `--writers-stopped`, because there is nothing for the
update to stop. A check that could not look is retried five minutes later, like every check. A
failed update is rolled back and the same release is tried again six hours later, or at the
next pass when the reason was momentary (a copy changed while it was taken, a busy lock). An update whose
rollback needs a person stops the automatic attempts. Once you have run `full update`
successfully, they resume. Until then, `doctor`, the Health page and `auto-update status` say
so. `doctor` and the Health page also show when automatic updates are off, when the job is
installed but not loaded, the engine and app versions, where the backup passphrase is kept,
and a daily backup that failed.

#### When a new release starts running

Writing a verified release to disk and starting to run it are two moments. The update
restarts this workspace's tick and local server on the new code, and the job does that only
at a safe point: when the local server has answered no page and no agent in the last five
minutes (a host's `/health` probe, the service document and the events feed do not count),
no memory-http client has called in that time, and no tick or other workspace operation is
running (the update would stop the tick half-way). Otherwise the release is recorded as
`deferred`, `auto-update status` and Health show it as ready, and the next pass tries
again. A `full update --apply` you run yourself installs at once.

A running Claude Code session is never stopped by an update. Its MCP server
(`mcp/server.py`) is a Python process that imported the engine when the session started.
`pip` replaces the files on disk in place; the process keeps the code it already loaded, but
a module it imports for the first time afterwards comes from the new release. The server
therefore checks the installed version at each request, and once the code under it changed
it moves itself onto the installed release (OBS-159): it holds that request, lets the
requests already running answer, starts the installed release once to check it
(`--reload-check`), then replaces itself in place with `os.execv`. The process id, the pipes
and the session stay; the new release takes over the handshake the client already made, so
the client sends no second `initialize`, and it answers the held request exactly once. When
the tool list changed, the client receives `notifications/tools/list_changed`. A client on
the 2026-07-28 protocol (Claude Code is one) keeps a `subscriptions/listen` stream open for
the whole session to hear about such changes; that stream is not a request still running.
Just before it moves, the server ends each open stream the protocol's graceful way (its
result, stamped with the subscription id); when the tool list changed, the stream first
carries that change, stamped the same way, so such a client hears it on the stream it holds
(OBS-228). Whether the client then opens a new stream is up to the client: the MCP Python
SDK's own client does not re-listen by itself, and this guide has not observed what Claude
Code does. A client that listens again is acknowledged by the new release and hears later
changes; a client that does not hears of no further change until it listens again or
reconnects, though its requests are still answered by the new release.

Where it cannot move, it answers `stale-server` and names why, and the session gets the new
release when it restarts or reconnects the server: on Windows (`os.execv` starts a new
process there), while a `full update --apply` holds the workspace or the requests already
running take longer than a minute to answer (it tries again the moment the last of them has
answered, with no further request needed, and at a later request), when the installed release fails its check, speaks another MCP revision or no
longer exists, or when the replacement itself fails. Each move and each refusal is one line
in `store/logs/mcp-reload.jsonl` (from, to, pid, and the reason for a refusal) and on the
server's stderr; `full doctor` lists them under `session_servers`. `full update` lists the
servers in its report and never signals them.

#### A release that needs a person

A release whose wheel carries `observatory/engine/RELEASE.json` with its own version and a
`needs_person` step (a data or schema migration to run or confirm by hand) is downloaded and
verified by the job but not installed. `doctor`, Health and `auto-update status` show the step
and the version; the same release is not downloaded again. Do the step, then run
`project-observatory full update --apply`: by hand, the update shows the step and installs.
Releases before 0.19.0 do not read the marker, so such a release must also refuse its own
`upgrade --apply` until the step is done. The format is in [COMPATIBILITY.md](COMPATIBILITY.md).

#### The update log

Besides `store/logs/update.jsonl` (this workspace's journal, step by step), every stage writes
a code to `~/Library/Logs/Project Observatory/updates.jsonl` on macOS, or
`$XDG_STATE_HOME/project-observatory/logs/updates.jsonl` (`~/.local/state/…`) elsewhere:
`update_check` (`current`, `ready`, `check_failed`, `download_failed`, `signature_failed`,
`install_failed`, `needs_migration`), `update_download` (`started`, `done`), `update_install`
(`started`, `installed`, `failed`, `timeout`), `update_restart` (`requested`, `refused`) and
`auto_update` (`on`, `off`). A line holds the event, its code, a UTC time, `engine` or `app`
and the workspace's instance id. It holds no version, path or message.
`OBSERVATORY_PRODUCT_LOG_DIR` moves the folder.

**Installs from 0.7.0 to 0.16.0** have no job of their own. The `observatory-log` plugin,
which the PassionCode launcher keeps current, checks such an engine once a day. When a
newer stable release exists, it runs that engine's own `full update --apply`, unless the
`auto-update` file in the home says `off` or `updates.auto` is `false` in `config/settings.json`. The new release then schedules its job.
The bridge logs to `~/.local/state/project-observatory/update-bridge.log`.

### Updating by hand

To update yourself, or on a machine with no job:

```sh
project-observatory full update --check     # 0 up to date, 10 update available, 3 could not look
project-observatory full update             # preview: current, target, what --apply does
project-observatory full update --apply
```

`--apply` downloads the wheel, `SHA256SUMS` and `SHA256SUMS.asc` from the GitHub release and
installs only when `SHA256SUMS` carries a valid signature by the organization's release key
(pinned in the engine, `release_signature.py`; never a key the release brings) and the wheel
matches both GitHub's published asset digest and its `SHA256SUMS` line. An unsigned release
is refused. The rollback wheel of a release published before signatures existed is checked
on its digests alone; one that carries a signature must verify. A `GH_TOKEN` or `GITHUB_TOKEN`
that GitHub refuses (HTTP 401) is dropped for the rest of that run, said once on stderr, and
the release is read anonymously.
It keeps a verified wheel of the running release for rollback (under
`backups/engine-releases/`, cached by every update, or downloaded from that release) and
refuses without one unless `--no-rollback` is given. It stops this workspace's launchd
tick and server if they are loaded (unless `--writers-stopped` says you stopped your own
scheduler; a foreground `open --serve` is yours to stop), snapshots the workspace, installs
the wheel with its `full` extra using the running interpreter's pip (or `uv pip`),
constrained by the `requirements-full.lock` inside the verified wheel (a rollback uses the
running release's own lock; a wheel without one installs unconstrained and the report's
`degraded` says so, `constraints` names the lock used), runs the
new release's `upgrade --apply --writers-stopped` in a new process, verifies the installed
version, its pinned dependencies and `doctor`, and starts again exactly the jobs it
stopped. Any failure after the install reinstalls the rollback wheel; if the workspace had
already been upgraded, the pre-update snapshot is restored at the same path and the changed
copy is kept beside it as `<home>.failed-update-…`. Every step is a line in
`store/logs/update.jsonl`.

Before it downloads anything, and again after the machine backup and just before it stops a
job, it measures every volume it writes to against what it writes there: the release wheels
(the published size), the installed package beside the one it replaces (twice the installed
size, since pip keeps the old files until it commits), the workspace snapshot (the workspace
as the snapshot copies it), its encrypted copy in the backups root when a passphrase is set,
the cached rollback wheels, room for the store's databases to be written once more while the
new release migrates them, and, in the first check, the machine backup it is about to take
(sized by the newest bundle); plus 1 GB left free. Volumes that report the same free space
(APFS volumes of one container share it) are counted as one. The second check leaves out what
is already on disk. A volume short of that refuses with exit 7: nothing stopped or installed
(and, at the first check, nothing downloaded and no backup taken), the report's `space` lists each volume's writes,
`needed_bytes` and `free_bytes`, and the error names both numbers and how to free space. On
2026-10-20 an automatic update without this check rolled back on "database or disk is full"
half-way through the migration. The preview shows the same measurement and lists a short
volume under `apply_blockers`. The automatic job records it as `refused-disk` and tries again
the next hour, since the disk guard may have freed space by then.

It refuses a downgrade, the same version without `--reinstall`, and a source checkout or
editable install (update those with Git). `--version X.Y.Z` pins the target; `--repository
OWNER/NAME` and `--api-url URL` select a fork or a mirror; the signature is still checked
against the organization's key pinned in the engine. The environment variables
`OBSERVATORY_RELEASE_REPOSITORY` and `OBSERVATORY_RELEASE_API` do the same only in a
development build (a checkout or an editable install): an installed release ignores them
and says so, because a feed the environment can redirect is not a release's (LC-16). Only
HTTPS is accepted, except to this machine.

| Exit | `--check` | `--apply` |
|---|---|---|
| 0 | up to date, or this machine is ahead of the target | updated |
| 1 | — | failed after the install began; rolled back (`rolled_back`, `workspace_restored`) |
| 2 | refused: no such release, bad arguments | refused before any change: verification (digest, signature), downgrade, unsafe state |
| 3 | could not look: network, rate limit, a release without its assets (`degraded`) | could not look (network, rate limit), before any change |
| 4 | — | rollback incomplete; `human_steps` says what to run |
| 5 | — | updated, but a stopped job did not start; `services_not_restarted` has the command |
| 6 | — | `--unattended` only: the release declares a step that needs a person; downloaded and verified, not installed (`status: held`, `needs_person` names the step; do it, then `full update --apply`) |
| 7 | not used | refused before any change: a volume the update writes to has too little free space (`space` names each volume's `needed_bytes` and `free_bytes`); free space and run it again |
| 10 | a newer installable release exists | — |

Anonymous GitHub API requests are limited to 60 an hour per address; a `--check` from a
scheduler every few hours stays far below it.

## Choose the dashboard's language

The dashboard opens in the reader's system language: Russian when the first preferred
language of the system (the browser's `navigator.languages`) is Russian, English otherwise.
Each reader can override it with **System / English / Русский** in the navigation rail; the
choice is stored in that browser, applied before the first paint, kept for pages opened as
local files and across rebuilds and updates, and **System** forgets it again. An unknown
stored value reads as System.

The workspace setting is the language the pages are built in — what a reader without a
browser gets (the CLI, the session hook, the checks) and what a browser that reports no
language shows. It lives in `config/settings.json` as `"interface": {"locale": "ru"}` and is
written by

```sh
project-observatory full configure interface locale ru   # en | ru
project-observatory full open --rebuild                  # pages carry the language they were built in
```

Precedence, first match wins: the reader's choice, the system's first language, then this
setting (`OBSERVATORY_LOCALE` overrides it for one build), else English. `full doctor`
reports it under `interface`. Only `en` and `ru` are accepted; an unknown key or value is
refused. Releases before 0.4.0 ignore the section. The interface and finding titles are
translated; a finding's details and suggested action stay in English. An agent asked to set the language runs the
`configure` command above and never edits other settings to do it.

## Reading the dashboard workspace

The navigation groups work, infrastructure, access and system pages. Projects and
ENV open with rows visible. Search and filters narrow the list; sorting applies
to the entire selection and has a separate control on narrow screens. Project
names open full detail; closing it returns to the list.

Home answers "is everything fine, and what needs me first": six state tiles (sites, apps, disk and
memory, keys, agents, personal cloud) with the time each was measured, "not measured" for a source
that did not run, the three problems to do first with "All problems (N)" to the findings page, and
projects at a glance by organization.
Each finding keeps its title visible; “Evidence and action” opens the evidence
and next step. A direct finding link opens that evidence. Acknowledged history
remains available even if no findings are open.

“Command” means copy for a terminal, not execute. Keys and ENV state their current
mode above the list. Health describes the observer at measurement time, not a
continuous availability promise. Traffic distinguishes missing measurements from
zero and totals are sums across GA4 resources, not deduplicated human visitors.

Behavior contracts and checks: [workspace redesign](ux/DASHBOARD-REDESIGN.md),
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

From 0.17.0 the app updates itself: the maintenance job replaces it with the engine's
release once it is not running (see [Staying in step](#staying-in-step)). The replaced
bundle is kept under `<home>/store/app-previous/`.

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
[macOS app](macos/README.md).

Disk-guard measurement limits: unknown swap remains `null` with a reason and
source time; it is not zero. The largest-file report carries completeness,
error counts and budget exhaustion. An incomplete empty scan is shown as an
incomplete measurement. The MCP overview exposes `guard.largeFilesCoverage`
beside its bounded file list. See [measurement coverage](design/DISK-GUARD.md#measurement-coverage-obs-87-subset).

### Tool configuration coverage

With the harness integration enabled, the scan names credential-shaped fields in
user tool configs and `.mcp.json` files one level below the configured projects
root. It never prints their values. Project-scoped names use a measured registry
id when available and otherwise the directory basename. A malformed config emits
`harness.scan_degraded`; line-based name recovery is marked `parsed: false` and
never establishes full coverage. Unreadable, unsupported or oversized files are
also explicitly unmeasured. Git-tracked plaintext configs raise a critical
finding; failed tracking checks remain unknown. Follow the reason-specific remedy
and rerun `project-observatory full harness`. A tracked key may remain in history:
review exposure and revoke or rotate through its owner before treating it as safe.

### Scheduled measurement freshness

Tick receipts distinguish `disabled`, `not_started`, `stopped`, `failed` and
`ran`; a yes/no probe such as `backup-due` records its "no" (exit 3) as `not_due`,
never as a failed step. Disabled steps preserve the last successful measurement time; they do not
raise starvation. A step that used 95 % of its share of the ceiling in 3 of the last
5 ticks (the leak scan, the `.env` inventory and the production configuration fetch, which resume by design, excepted) is reported as `tick.step_over_share` (the ceiling is the one `tick.json`
`progress.ceiling_seconds` names), and a collector whose same degraded reason stays in
its receipt for more than 24 hours as `collector.degraded_persistent`, unless the reason
is structural (`degradations.STRUCTURAL`, or `structural: true` on the row); its first
sightings are kept in the store as `degraded-since.json`. A probe's "no" is its own exit
(3), so a crash of `backup_store.py --due` is a failed step, never "not due".
Lines in `store/logs/tick.err` start with their UTC time. A collector absent from the last tick's `collector_order` is
not classified as starved. Planned collectors warn after three missed starts or
six hours without a completed measurement (`config/tick.json` `starve_hours`
overrides the age bound); leaks and integrity use critical severity. Interrupted
attempts retain unknown completion. Unsupported rotation schemas are reported as
`tick.rotation_degraded`, never interpreted as a healthy empty queue.

Merge provenance records `inputs` timestamps and degrades old inputs: local
filesystem data after two hours, GitHub/remotes after six, daily inputs after
48. Legacy files without a declared timestamp use file modification time;
invalid declared timestamps stay unknown. A new merge does not refresh them.
Rollback to an older engine can reset schema-2 starvation streaks; allow fresh
receipts before trusting scheduling coverage after rollback.

### Repeated cleanup stops and unmeasured sources

Three consecutive guard summaries reporting stopped Git cleanup produce
`machine.guard_stopping` (warning), with the latest count and pressure level.
The guard's own unavailable sources produce `machine.guard_degraded` (info).
Only a Trash Full Disk Access failure receives the macOS consent instructions:
a person grants the maintenance job's interpreter access in System Settings →
Privacy & Security → Full Disk Access, or leaves that source unmeasured. Other
access, journal and time-budget failures keep their own reason and recovery step.
The compact agent overview already carries Git `partial` and `carried` evidence;
carried measurements are not a new cleanup authorization.

### Inspect checkout candidates beneath explicit roots

```sh
project-observatory full machine --checkouts --root /path/to/checkouts --root /path/to/other-checkouts
```

This read-only command writes `store/raw/checkout-candidates.json` with a persisted
cursor. The default pass allows 20 seconds overall, 2 seconds and 250 entries per
root, depth 8, and at most 10,000 retained candidates. Repeating the command resumes
unfinished roots and rotates the root last attempted before expensive work. Symlinks
are not traversed; Git metadata for a linked checkout may reference its common Git
directory outside these roots. Repository contents are not recursively surveyed.

The receipt distinguishes clones, linked worktrees, bare repositories and unknown
Git results. A fresh registration receipt may establish listed path membership;
missing, partial or old registration evidence leaves absence unknown. Allocated and
reclaimable bytes, local changes, ownership, upstream containment and active use are
not measured here. No candidate is actionable and none enters cleanup planning.
Depth/time/entry limits and source failures keep a pass incomplete. Directory offsets
are resumable hints; a changed directory identity resets its position, and huge
or continually changing directories can need additional passes. Filesystem calls
are checked between operations; a kernel-blocked filesystem read is not a hard
real-time deadline. Git probes run in owned bounded child groups.

A competing inventory writer refuses while the receipt lock is held; it never resets
another scan's cursor. The command is not scheduled automatically.

### Read cache and runtime coverage without cleaning

```sh
project-observatory full machine --cache-coverage
```

The receipt is `store/raw/cache-coverage.json`. It reads machine/cleanup declarations,
checks non-protected root metadata, and locates executable tools without running them.
It does not enumerate directory contents or start application-managed cleanup commands.
Protected app containers and uncovered OS cache areas have unknown presence and size.
A configured `regenerable` label remains an unverified declaration; tool availability
means only that an executable was found, not that its daemon runs or the cache is idle.

Old `du` figures retain their own dates as rounded reported sizes with unknown
completeness. Exact allocated bytes, reclaimable bytes, owner, current use, restoration
cost and regeneration proof remain unknown. Neither command estimates of zero nor
APFS clone/private-size hints are promoted into reclamation promises by this adapter.
The receipt always says coverage is partial. `measurement_complete` describes only
completion of this narrow metadata pass, and never lifecycle safety.

The default pass allows three seconds, capped at ten, and 128 attempted rows. Its
persisted cursor advances before each probe so repeated bounded passes rotate through
locations and tools. Input documents are capped at 1 MiB and source collections at 128
rows; omissions are explicit. A kernel-blocked metadata read is not preempted. A failed
source preserves old observations and dates; a missing current policy leaves those rows
stale and does not authorize probing them again. A single-writer lock protects cursor
progress. Nothing schedules this command or adds its rows to a cleanup planner.


### Read saved checkout, cache and open-deleted-file coverage

On macOS/Linux, the explicit checkout scan also records bounded local Git state.
Its counters are status/index records, not recursive file totals. Hidden index
flags, changed metadata and incomplete reads remain unknown. The saved coverage
view shows current versus recorded state and age; a clean observation does not
authorize removal. Windows currently reports this additional probe as unavailable.

`project-observatory full machine --coverage` summarizes saved inventory
receipts without running their collectors. The agent tool is
`observatory_machine(section="coverage")`; its default overview includes the same
summary. Each source retains its own attempt age and partial state. Snapshot counts
count validated saved rows within the declared scope, never every checkout or cache
on the machine. Current snapshot counts remain null for stale, partial or invalid
sources. Cache lifecycle coverage remains partial even after all metadata probes finish.

The reader caps each source at 4 MiB, each collection at 10,000 inspected rows, and
returns ten rows per source. Its stale threshold is 30 minutes. Oversized or malformed
receipts show an explicit unknown state; malformed rows do not erase valid peers.
Read time is separate from measurement time. Reclaimable/allocated totals remain null,
command arguments are omitted and all rows remain non-actionable. Refresh evidence
with the separate `--checkouts --root ...` or `--cache-coverage` command when needed.

### Missing worktree registration receipts

For a missing worktree, cleanup first proves the directory absent: its parent folder
is listed and lacks the entry. An unplugged drive, a renamed parent folder or a
permission error is `unknown` instead; the survey marks that worktree `unreadable`
and nothing is pruned. When the directory is absent, the worktree's HEAD is kept
reachable first: by its branch, or for a detached HEAD by an
`archive/wt-<sha8>` branch (a symbolic ref there does not count). Then only that
registration's entry, `.git/worktrees/<name>`, found by the path its own `gitdir`
records, is moved to `.git/worktrees-retired/` with its index and reflog: never a
working directory, never a repository-wide `git worktree prune`. A directory that
comes back loses nothing; move the entry back and run `git worktree repair` to
register it again. An unknown, changed or unreadable HEAD, or a locked or ambiguous
entry, moves nothing. A `pruned` journal row names the kept ref (`retained_ref`) and
the moved entry (`retired_entry`); `registration_changes` stays null. A failed or
interrupted move is `failed` with an unknown effect. Reasons omit Git output and exception bodies.
Inspect `git worktree list --porcelain` in the owning repository before retrying.
Proof: `tests/test_git_hardening.py::MissingRegistrationReceipt` in the engine.

### Open files absent from directory scans

`project-observatory full machine --open-unlinked` records files that have been
unlinked but remain open on macOS or Linux when `lsof` is available. It never
stops a process, removes data or changes cleanup settings. Windows and a missing
source return unknown, not zero. There is no scheduled observation.

The bounded receipt `store/raw/open-unlinked.json` stores numeric process IDs and
snapshot-local device/inode identities, logical sizes, source start/end times and
the parser source hash. It collects no paths, process commands or environment.
The command has a five-second deadline, a combined two-MiB output ceiling,
100,000 fields and 10,000 distinct file records. Missing sizes, conflicting sizes
for a shared identity, diagnostics, malformed output and partial execution remain
degraded. Known-size peers survive as a separately labelled subtotal.

`full machine --coverage`, `full doctor` and
`observatory_machine(section="coverage")` read the saved observation only. The
reader exposes up to ten size/PID-count samples and marks snapshots older than
30 minutes stale. Shared files count once, so process totals must not be added.
A successful command does not establish visibility of every process or namespace.
Logical bytes are neither allocated blocks nor reclaimable memory or disk space:
allocated/reclaimable values stay null and every observation remains non-actionable.
Ownership and any supported supervisor action require separate evidence.

Contract tests: `observatory/engine/tests/test_open_unlinked.py`, including a
disposable open-then-unlinked file owned by the test; no host process is stopped.


### Checkout observation does not fetch missing objects

Checkout inventory and local-state Git queries explicitly use the offline
observation profile. Partial clones can omit objects that an ordinary Git read
would download on demand; this observation refuses that download and keeps an
unreadable state unknown. It does not fetch upstream refs or establish remote
backup durability. Explicit fetch and remote-query operations outside this
profile retain their existing behavior. This is not a new cleanup permission.

Proof: `tests/test_checkout_state.py::OfflineGitObservation` in the engine uses
only disposable local file remotes, checks object-store contents before/after,
and includes a positive complete local observation and explicit remote controls.


### Read local upstream graph evidence

An explicit checkout scan saves local-state results before a separately budgeted
upstream graph phase. That phase reserves up to a quarter of the scan's time
budget, at most five seconds, with its own persistent round-robin cursor and
bounded child per checkout. Failures preserve the saved local-state observation.

For a supported complete local graph, the receipt records ahead/behind commit
counts and whether the upstream is another local branch or a remote-tracking
ref. These are local resolved commits only: a remote-tracking ref can be stale,
so `remote_durability` remains `unknown`, and `actionable` remains false. No
fetch, deletion, pruning or owner inference follows. Detached/unborn/missing
upstream, shallow or promisor history, replacement refs, grafts and alternate
object stores produce unknown counts. Those exclusions preserve uncertainty
rather than silently interpreting a different graph.

The saved coverage view uses the graph's own measurement time and source binding;
it never refreshes graph evidence from another phase's success. After 30 minutes,
a malformed receipt or a source/binding mismatch, counts are unknown. Git output,
remote URLs, commit subjects and ref names are absent from this aggregate view.
Proof: `tests/test_checkout_state.py::UpstreamGraph` and `GraphPhase` in the engine.

### Memory pressure observation

The saved memory summary includes an independently dated macOS pressure level.
It reads the platform's read-only sysctl; unknown codes, failed commands and
unsupported platforms stay unknown. Swap usage remains a separate measurement.
Saved pressure observations expire after30minutes and never authorize a lifecycle
action. RAM/swap timestamps are not refreshed by a successful pressure-only read.
