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
[README](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/../README.md#install) installs the wheel `--no-deps` first, then the
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
names with the one canonical id ([design](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/IDENTITY.md)).

**Which environment an app serves** comes from the provider (a Heroku pipeline stage) or from you:
`config/environments.json` holds `{"deployments": {"heroku:<app>": "production"}}`. The names
`production`, `staging`, `review`, `development`, `test` and `local` are recognised (`prod` and
`stage` are read as their full names). An app with neither is listed as `unassigned` in
`registry/environments.json`. Its name is never read as a hint.

`full doctor` also reports whether the scheduled tick is alive (`tick.verdict`): `disabled` while
`features.scheduler` is off (the default), `never` before the first tick, `running` during one and
`ok` after a recent one. A tick that did not finish is `interrupted`, and the reason says how it
ended — stopped at its ceiling (from the supervisor's record) or killed by a restart, a sleep or a
signal. When no tick has finished for six hours the verdict is `stale`, and when one has held the
lock for longer than that it is `running-long`. `tick.recent` counts how the last 10 runs ended
(`store/raw/tick-runs.jsonl` keeps 50); when 3 or more did not finish, `tick.history_warning` says
so even if the last one did.
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
| `cloudflare` | Zones and DNS inventory | User-owned account credentials; token administration is separately scoped |
| `heroku` | Hosting inventory | Local authentication for the intended Heroku account |
| `google` | Analytics property and search inventory | The user's service account files and resource grants |
| `domains` | Domain observations | Explicit domain export and network access |
| `mcp` | The MCP servers the agent configs on this machine declare (Claude Code, Cursor, OpenCode, Codex, Gemini CLI, Kiro), served as `machine.mcp.inventory` | Explicit `sources.mcp_config_root` |
| `harness` | What every agent session loads before the first word: skills and their share of the skill listing, instruction files, the settings that load every MCP tool upfront, and deterministic security rules over skills ([design](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/HARNESS.md)) | Explicit `sources.harness_root` |
| `sessions` | Local agent activity | Explicit `sources.sessions`; `sources.companion_db` is optional, set only where a memory companion (claude-mem) is installed |
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

Apart from an exact SessionStart declaration described below, nothing outside the workspace is read until you name it. Each source is a path set with
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
  actionable.
- **Stop** records the facts of a turn that changed a watched repository as a
  `proposed` ledger record, one per session, revised per turn. It writes the
  record first, then asks for the `why` of that record **once**. It does not ask
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
interpreter that runs the hooks). `install` says "installed" only after Claude Code's
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
127.0.0.1:47311; it answers GET only and changes nothing).

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
events feed. See the [service design](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/FABRIC-SERVICE.md).

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
limit (15 minutes, 20 for the leak scan), the whole tick stops at a 25-minute
ceiling below its 30-minute interval, and `store/raw/tick-run.json` records each
run's start, end, outcome and reason (`store/raw/tick-runs.jsonl` the last 50). A
stopped step is a failed step in the log and on the board, never a hung tick.

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
run least recently started first, so one that was not started goes first on the next tick
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
rules and their reasons: [design/EMBEDDING-POLICY.md](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/EMBEDDING-POLICY.md).

### Who may reach memory: the local agent, and the bindings you issue

The agent your own configuration starts over stdio is the **local agent**. It reads every
project and writes only proposals, as before. Any other caller reaches memory only through a
**binding** you issue at your terminal (PB-137 N-008). A binding names one principal, its
projects, its scopes, the highest class it may read and whether it may write proposals, and
it always expires. It reaches only the memory tools. Everything else (the estate, credentials,
the machine) stays with the local agent.

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
  ([design/FORGET.md](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/FORGET.md)).
- Every memory search leaves a receipt in `store/logs/retrieval.jsonl`. The receipt holds the
  exact results and the scope, and the query only as an HMAC; it holds no text. The caller
  that received a receipt can read it back with `observatory_explain`. Memory answers are
  redacted on the way out too, and say so in `redacted`
  ([design/RETRIEVAL-AUDIT.md](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/RETRIEVAL-AUDIT.md)).
- `project-observatory full memory-http` serves memory over loopback HTTP (port 47313) to
  callers holding a binding. A client sends its bearer as `Authorization: Bearer …` from the
  file you gave it. Without a valid binding every request is refused before it is read
  ([design/HTTP-TRANSPORT.md](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/HTTP-TRANSPORT.md)).

Rules and reasons: [design/ACCESS-BINDING.md](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/ACCESS-BINDING.md).

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
  to `inventory/agents.json`. Nothing of a descriptor's `auth` is copied, and no row is guessed
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
`tests/test_git_hardening.py`.

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
`CleanupNeverLosesWork` in `tests/test_git_hardening.py`.

The legacy `full cleanup` build-artifact action now reports a skip and asks for
`full disk-guard` to replan; only the disk guard owns build-output eligibility.
`CleanupNeverLosesWork.test_legacy_artifact_plan_cannot_delete_newly_tracked_output`
covers this refusal.
The `.wrangler` directory can hold local databases and object storage; it is never
classified as build output, even when an older cleanup configuration lists it.
Cleanup also retains an ignored artifact parent containing a nested repository or
local state, and any tree whose contents cannot be fully inspected within its
bounded check. Symlinks and special files are retained conservatively.
Regression coverage: `tests/test_disk_guard.py`, `GitTierSafety`.

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
| The cache tools' own cleanup | `uv cache prune`, `pnpm store prune`, `brew cleanup --prune=14`, Docker build cache older than a week | also `npm cache verify`, `uv cache clean`, Go and Yarn caches, dangling Docker images | also `brew cleanup --prune=all` |
| Git-ignored build and dependency output (`node_modules`, `.venv`, `dist`, …) of checkouts nobody worked in | 14 days | 3 days | 1 day |
| Temporary entries untouched for | 3 days | 1 day | 1 day |
| Trash items older than | 30 days | 30 days | 3 days |

Application and tool cache folders (`~/Library/Caches`, `~/.cache`) untouched for 7 / 2 / 1 days go
too, except the downloaded browsers and SDKs a tool does not fetch again by itself (`cache_keep`:
Playwright, camoufox, puppeteer, cargo-xwin and others) and `uv`, which has its own command.
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
See [the preservation contract](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/DISK-GUARD.md#apply-time-preservation-2026-10-13)
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
See [registration and fairness](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/DISK-GUARD.md#registration-refresh-and-build-root-fairness-obs-87-subset).

**On every system.** Cleanup admission currently skips process-table collection
because its negative result cannot authorize a path action. Explicit positive-hint
readers use bounded `lsof`/`ps` capture: 2 MiB combined pipe output, bounded
parsing and one shared deadline. The unbounded Linux `/proc` fallback is retired.
Limit or capture failures remain unknown; this is not a measurement of RAM saved.
[Capture limits and regression cases](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/DISK-GUARD.md#bounded-explicit-holder-observations)
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

The root is chosen in this order:
1. `OBSERVATORY_BACKUPS`;
2. `project-observatory full configure storage backups /absolute/path`;
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

**From 0.17.0 a passphrase exists by default.** When none is configured, the maintenance job
(or `full init` in a terminal) generates one from 32 random bytes. The passphrase lives in
`<home>/secrets/backup-passphrase` (mode 600) and is **also kept outside the workspace**, so
a deleted workspace or a reinstall can still open its backups:

| Platform | Where the copy is kept |
|---|---|
| macOS | the login Keychain, item "Project Observatory backups", account `<home-name>-<instance>` |
| Linux | the Secret Service through `secret-tool`; without it, `~/.config/project-observatory/backup-passphrases/<label>` (mode 600) |

The value goes to the Keychain on `security -i`'s stdin and to `secret-tool` on stdin,
never as an argument. A passphrase you set yourself is copied there too. One supplied through
`OBSERVATORY_BACKUP_PASSPHRASE` is yours to keep and is not copied.

`backup-passphrase show` prints it only to a terminal. Keep a copy off the machine as well:
restoring on another machine needs it unless iCloud Keychain carries it there, and nothing
can recover it.

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
# On another machine: restore asks for the passphrase at a terminal
project-observatory --home NEW_EMPTY_HOME full restore snapshot-….obsnap
project-observatory full backups decrypt observatory-db-….obsdb ./copy.db
```

A rolled-back update keeps the changed workspace beside the restored one as
`<home>.failed-update-…`: a full, unencrypted copy with `secrets/`. Nothing removes it;
`backups status` and `doctor` name it, and you delete it once the restored workspace is
confirmed.

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
   V=X.Y.Z
   shasum -a 256 -c SHA256SUMS --ignore-missing
   python -m pip install --no-deps "project_observatory-$V-py3-none-any.whl"       # the engine, and the lock it carries
   python -m pip install -c "$(project-observatory full-path)/requirements-full.lock" \
       "project_observatory-$V-py3-none-any.whl[full]"                             # its [full] extra at the tested versions
   ```

   The second `pip` line installs the `full` extra against the `requirements-full.lock` the
   wheel carries, the dependency set this release was tested with (as in the
   [README](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/../README.md#install)).

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

### Updates install themselves (0.17.0 and later)

Nothing needs to be run after a release. One job per workspace runs every 15 minutes: launchd
`org.project-observatory.<id>.maintain` on macOS, a systemd user timer
`project-observatory-<id>-maintain.timer` on Linux, a Task Scheduler task on Windows. Each run does only what is due:

- With `features.auto_cleanup` on, first the disk guard (see [The disk guard](#the-disk-guard)): hourly while space
  is normal, on every pass below the `low` line.

- Every six hours it runs `full update --check`; at login the first check waits 90 seconds
  after the job starts. When a newer **stable** release exists, it runs `full update --apply
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
writes it, neither `on` nor `off`. Off stops the checks, the downloads and the installs, the
Mac app's included; the daily backup keeps running. An install that turned updates off before
0.19.0 did it with `updates.auto: false` in `config/settings.json`: that still counts as off,
and the next `auto-update` command moves it into the file and says so.

```sh
project-observatory full auto-update status   # on or off, last check, last result, warnings
project-observatory full auto-update off      # writes `off`; new releases wait for you, the daily backup keeps running
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
  without a systemd user manager, the hook runs the pass itself.

Only one `full update --apply` runs at a time per workspace. A second one, from the job, the
plugin or a person, is refused before it changes anything; the job then tries again the next
hour. The job runs the update in a process of its own and never stops it: a pass that is
itself stopped (`maintain uninstall`, launchd, systemd stopping the unit) leaves the update
running to its end, and the next pass reads the outcome from `store/logs/update.jsonl`. A
reboot or a power loss is different: it ends the update too, and the next pass records it as
`failed` ("the update ended without a final journal entry"). Health and `doctor` then show
the failure; `doctor` also says whether the engine and its jobs run. Where there are no launchd
jobs (Linux), the job and the plugin pass `--writers-stopped`, because there is nothing for the
update to stop. A check that could not look is retried once within the hour, then every six
hours. A failed update is rolled back and retried at the next check, or the next hour when
the reason was momentary (a copy changed while it was taken, a busy lock). An update whose
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
therefore checks the installed version on each call and answers `stale-server` once the code
under it changed, and the session gets the new release when it restarts or reconnects the
server. `full update` lists those servers in its report and never signals them.

#### A release that needs a person

A release whose wheel carries `RELEASE.json` with its own version and a
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
[macOS app](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/macos/README.md).

Disk-guard measurement limits: unknown swap remains `null` with a reason and
source time; it is not zero. The largest-file report carries completeness,
error counts and budget exhaustion. An incomplete empty scan is shown as an
incomplete measurement. The MCP overview exposes `guard.largeFilesCoverage`
beside its bounded file list. See [measurement coverage](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/DISK-GUARD.md#measurement-coverage-obs-87-subset).

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
`ran`. Disabled steps preserve the last successful measurement time; they do not
raise starvation. A collector absent from the last tick's `collector_order` is
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

Contract tests: `tests/test_open_unlinked.py`, including a
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
