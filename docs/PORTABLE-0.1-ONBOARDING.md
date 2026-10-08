> Historical 0.1 compatibility CLI only. For the complete engine, read [ONBOARDING.md](ONBOARDING.md). Keep the two workspace formats separate.

# Agent-led onboarding

The first useful result should require no credentials: an isolated demo, a visible dashboard and an explanation of what will be read when a real folder is enrolled. Every command below is implemented by [`observatory/cli.py`](../observatory/cli.py); the end-to-end flow is covered by [`tests/test_observatory.py`](../tests/test_observatory.py).

## Give this prompt to your agent

```text
Set up Project Observatory portable edition from
https://github.com/passioncode-ai/project-observatory-dashboard

Read README.md, SECURITY.md and docs/PORTABLE-0.1-ONBOARDING.md first
(docs/ONBOARDING.md is the full engine's guide, not this edition's).
Use an isolated Python environment and run the synthetic tests/demo.
Explain the local state directory, the read-only dashboard and the exact
scope of this edition. Do not scan my home folder or enroll projects
implicitly. Preview discovery only inside the project root I select.
Register only the directories I authorize, then scan and explain findings,
last scan time, partial results and local-only Git tracking freshness.

Do not ask me to paste keys into chat. If optional known-value checks are
useful, explain the local hidden prompt or stdin-based secret put command.
Never read a value back into tool output, an argument, a message or a file
in the repository. Do not install a scheduler, contact providers, fetch
remotes, scrub other tools' stores or rotate credentials in this setup.
Show me how to repeat a scan, read status, open the localhost dashboard,
run an explicit-target leak check and export aggregates. Keep real state
and the generated dashboard private. Finish with the commands I used and
the remaining optional integration work from docs/MIGRATION.md.
```

## 1. Install and verify

```sh
git clone https://github.com/passioncode-ai/project-observatory-dashboard.git
cd project-observatory-dashboard
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
python -m unittest discover -s tests -v
```

Python 3.11+ is required. Git enables repository metrics. No cloud credential, local agent CLI, agent gateway, node runtime or paid model is required. The runtime itself uses only Python's standard library; pip may obtain setuptools to build the package. An offline alternative, from the cloned checkout, is `python3 -m observatory` with the same arguments.

## 2. Start with fictional data

```sh
project-observatory --home "$HOME/.local/share/observatory-demo" demo
project-observatory --home "$HOME/.local/share/observatory-demo" serve
```

The demo's state and sibling `observatory-demo-demo-projects` directory contain only freshly created synthetic data. It refuses to overwrite a populated setup. Stop the server with Ctrl-C. Inspect the project's state, findings and the two synthetic exposure occurrences. They demonstrate exact matching and are not measured customer incidents.

## 3. Select real scope

```sh
project-observatory init
project-observatory doctor
project-observatory project discover "$HOME/projects" --depth 2
project-observatory project add example-app "$HOME/projects/example-app"
project-observatory scan
project-observatory status
project-observatory serve
```

Replace the example path with a directory you control. Discovery only reports candidates based on familiar repository/project markers. It does not run code, infer ownership or add anything. Project roots cannot contain the state directory or be contained by it. No default command traverses your entire home directory.

Scanning reads Git metadata, a bounded file inventory, root `package.json` dependency counts and `.env`-style files. Environment values are read transiently to compute per-installation salted equality fingerprints; snapshots retain names/presence/fingerprints, not values. This still creates sensitive metadata, so the state directory stays private. Dotenv parsing is deliberately literal: no shell expansion, substitution, evaluation or multiline interpolation.

Git ahead/behind uses existing local tracking references. Run your own authorized fetch workflow separately when current remote state is required. An unavailable source yields partial-observation evidence rather than an all-clear.

## Optional secret input

Use a **local terminal**, outside an agent tool transcript:

```sh
project-observatory secret put EXAMPLE_TOKEN
```

Type the value into the hidden prompt. Alternatively, when a value is already in a private local file, redirect it without displaying it:

```sh
project-observatory secret put EXAMPLE_TOKEN < /absolute/path/to/private-input
project-observatory secret list
```

Never use `echo ACTUAL_VALUE ...`, shell command substitution containing a literal value, a command-line `--key`, an agent message or a screenshot. Do not create a temporary value file in this repository. Observatory stores plaintext local slots with restrictive file permissions; it is not an encrypted OS keychain. Read the threat boundary in [SECURITY.md](../SECURITY.md).

To use the slot in a process you intentionally trust:

```sh
project-observatory secret run --env API_TOKEN EXAMPLE_TOKEN -- your-command
```

The process receives the value in its environment. Observatory suppresses its stdout and stderr because either could echo the value. Its exit code is returned. The child remains capable of its own file or network writes; this command does not sandbox arbitrary software. Do not inject secrets into an untrusted command.

No provider key is required for this release. Cloud-provider setup is future, optional migration work; do not add a broad API key just to make onboarding appear complete.

## Explicit-target exposure checks

```sh
project-observatory leaks scan --file /absolute/path/to/a/local-transcript.jsonl
project-observatory leaks scan --sqlite --file /absolute/path/to/a/local-memory.sqlite3
```

Choose each file deliberately. Repeated `--file` arguments scan multiple targets. Each gets an opaque `target-N` identifier so receipts omit private paths and snippets. Keep the invocation locally if you need to resolve a label later. SQLite opens in read-only mode; it is not scrubbed or vacuumed. Snapshot a busy database using its owner's approved method when needed.

Only previously stored slot values of at least eight bytes are compared. At most 32 explicit targets may be selected. Files over 16 MB and SQLite inputs exceeding 100,000 inspected cells or 16 MB of inspected content return partial/degraded results. SQLite 3.37+ is required; virtual/shadow tables and generated columns are skipped with degraded evidence. A five-second SQLite statement deadline and length limit bound query work. No values known means the scan cannot establish absence. Duplicate slot aliases can produce separate named findings; do not call the sum a count of unique credentials or independent incidents.

Project metadata scanning is bounded to 20,000 files and directories per project, 2 MB of dotenv input and 2,000 variables across a scan. The local secret inventory is bounded to 1,000 slots and 2 MB in total. A reached limit is explicit rather than an implied complete inventory.

Exit `1` means a match; exit `2` means partial/degraded or invalid operation. Review the artifact and issuer-specific rotation procedure. Deleting an artifact does not invalidate a credential. This edition does not rotate provider keys or rewrite other tools' memories.

## Sharing, updating and removing

`export` emits aggregates only; counts themselves can still be sensitive. The regular `status`, project listing, local database and generated dashboard are private. Never upload the dashboard to a static host. The repository's `site/` is a separate, public marketing artifact.

To update source, review changes and rerun tests before reinstalling with `python -m pip install .`. This edition installs no scheduled service or background hook, so there is nothing to disable at login. Stop `serve` with Ctrl-C. Remove the chosen state and demo folders only after reviewing whether their private slots or history need backup; deleting source code alone does not delete state.
