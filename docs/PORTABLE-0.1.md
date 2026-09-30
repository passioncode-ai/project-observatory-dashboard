# Portable 0.1 compatibility reference

**A local observation layer for agent-operated projects.** Register the folders you own, measure their current state, and give your agent a concrete next action. Project Observatory belongs to the [ssheleg harness](https://skills.sshlg.me/harness/): skills guide the work; Observatory reports what changed around it.

These are the retained **0.1 compatibility commands**. Their smaller workspace remains separate from the full engine added in 0.2. They include local project observation, credential metadata and explicit known-value exposure checks. For the complete engine, use the [current onboarding guide](ONBOARDING.md) and [migration map](MIGRATION.md).

## Try it without accounts or keys

Python 3.11+ and Git are the prerequisites. The runtime has no third-party Python dependencies. The documented shell commands target macOS and Linux; Windows support has not been validated.

```sh
git clone https://github.com/passioncode-ai/project-observatory-dashboard.git
cd project-observatory-dashboard
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
python -m unittest discover -s tests -v
python tools/check_public_release.py --history
project-observatory --home "$HOME/.local/share/observatory-demo" demo
project-observatory --home "$HOME/.local/share/observatory-demo" serve
```

Open the localhost URL printed by `serve`. The demo creates one fictional project and a fictional transcript containing a known synthetic value twice. These are demonstration occurrences, not real incidents. Use a new demo home if that sample directory already exists.

Prefer agent-led setup? Copy the complete [onboarding prompt](PORTABLE-0.1-ONBOARDING.md#give-this-prompt-to-your-agent). It starts with the demo, explains the scope, and keeps credentials out of chat.

## Observe your own projects

```sh
project-observatory init
project-observatory project discover "$HOME/projects" --depth 2
project-observatory project add example-app "$HOME/projects/example-app"
project-observatory scan
project-observatory dashboard
project-observatory serve
```

Discovery is a preview: it enrolls nothing. `project add` records a directory you explicitly selected. `scan` reads only registered roots; it never fetches from Git remotes, calls providers or runs project build commands.

| What you can ask | What this edition measures | Evidence |
|---|---|---|
| Is work still only on this machine? | Git dirty files, upstream presence, ahead/behind against local tracking refs | [`git_metrics`](../observatory/core.py), synthetic Git E2E test |
| How large is this project? | Bounded source-file count and bytes; root npm dependency counts | [`observe_project`](../observatory/core.py) |
| Where are environment variables reused? | Variable names, presence, file permissions and machine-local HMAC fingerprints | [`env_pairs` and `observe_project`](../observatory/core.py) |
| Did a known secret get copied into an artifact? | Exact byte matches against local slots in explicit files or SQLite targets | [`scan_leaks`](../observatory/credentials.py) |
| What needs attention next? | Deterministic findings, a remedy, partial-observation warnings | [`findings`](../observatory/core.py), [local dashboard](../observatory/dashboard.py) |
| Can I share a summary? | Aggregates with project names, paths, variable names and fingerprints omitted | [`public_export`](../observatory/core.py) |

`status` prints the most recent snapshot and its timestamp; it does not secretly run a new scan. The local SQLite history retains the most recent 100 snapshots. Files inside dependency/build/cache directories and symlinked files are excluded from observation. Git external filters are disabled and submodules ignored for safety, so dirty-file counts may differ from a customized Git workflow.

## Credentials stay local

The local workflow needs no API keys. Optional known-value exposure scanning uses secrets you deliberately add to private local slots:

```sh
project-observatory secret put EXAMPLE_TOKEN
project-observatory secret list
project-observatory leaks scan --file /absolute/path/to/a/local-artifact.txt
```

`secret put` prompts invisibly in a local terminal or reads standard input. Never place a value in a command argument, an agent message or a Git file. Stored values are redacted from CLI results, findings, exports and the dashboard, including when a known value appears inside a metadata label. See [safe local input and child-process injection](PORTABLE-0.1-ONBOARDING.md#optional-secret-input) and the [threat model](../SECURITY.md).

This is **known-value matching**, not universal secret detection. A zero-match result says nothing about unknown values, transformed/encoded copies, skipped targets or earlier versions. Local artifacts containing copied credentials are not evidence that an upstream vendor was breached.

## Commands and exit status

| Command | Behavior |
|---|---|
| `init`, `doctor` | Create empty private state; check prerequisites without provider calls |
| `demo` | Create isolated fictional examples; scan and build the dashboard |
| `project discover ROOT` | Preview candidates up to a bounded depth |
| `project add NAME PATH`, `project list` | Explicitly enroll and inspect local scope |
| `scan`, `status` | Measure scope; read the last recorded snapshot |
| `dashboard`, `serve --port 47311` | Build private HTML; serve a read-only loopback view |
| `secret put NAME`, `secret list` | Store through local input; list names only |
| `secret run --env ENV_NAME NAME -- COMMAND` | Inject a slot into one process; suppress its output |
| `leaks scan --file PATH [--sqlite]` | Scan explicit artifacts against known slots |
| `export` | Print de-identified aggregate project statistics |

Global `--home PATH` must precede the command. Otherwise `OBSERVATORY_HOME` or `~/.local/share/project-observatory` is used. Exit codes: `0` completed, `2` invalid/degraded leak scan or operational error; leak scan returns `1` for matches. `secret run` returns the child exit code. Ordinary project findings do not make `scan` fail.

## Build and contribute

```sh
python -m unittest discover -s tests -v
```

The tests create temporary projects, a local bare Git remote, fictional credentials and SQLite stores. They do not use cloud accounts or the user's project inventory. See [CONTRIBUTING.md](../CONTRIBUTING.md), [SECURITY.md](../SECURITY.md), [migration acceptance criteria](MIGRATION.md), and the [handoff](HANDOFF.md). Public marketing lives in `site/`; generated dashboards belong in private state and must never be deployed.

Open source under the [GNU AGPL-3.0](../LICENSE); a [commercial license](../COMMERCIAL-LICENSE.md) is available for use that does not meet the AGPL's terms — contact@passioncode.ai. Versions up to and including v0.9.1 were released under PolyForm Noncommercial or Internal Use (v0.8.2–v0.9.1) and the MIT License (v0.8.1 and earlier); those releases keep their licence. Source and website examples are synthetic unless explicitly identified as a separately reviewed observation.
