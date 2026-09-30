# Working in project-observatory-dashboard

## Read first

1. The PassionCode.ai knowledge base — `fabric-workspace/knowledge/` in your clone (org-index
   `scripts/clone_all.sh` makes it) or https://wiki.passioncode.ai/knowledge — at least its
   [README](https://github.com/passioncode-ai/fabric-workspace/blob/main/knowledge/README.md),
   vision, principles and how-to-work.
2. This file, then the organization's
   [CONTRIBUTING.md](https://github.com/passioncode-ai/.github/blob/main/CONTRIBUTING.md) and this
   repository's [CONTRIBUTING.md](CONTRIBUTING.md); everything there applies to agents too.

`CLAUDE.md` holds one line, `@AGENTS.md`, so Claude Code reads this same file: it imports
`AGENTS.md` only through that line, and one file cannot drift from a copy of itself.

## What this repository is

`passioncode-ai/project-observatory-dashboard` is **the single home of Project Observatory
code**: the public engine, its Python package `project-observatory`, the companion Claude Code
plugin `observatory-log` and the public website. Engine changes land here first and ship from
here.

The private predecessor repository holds one operator's tooling and data only. It is not developed as an engine: do not port changes to it, do not copy its history,
documents or data here, and do not cite its records.

The organisation's map of repositories is
[passioncode-ai/org-index](https://github.com/passioncode-ai/org-index) (private; readable by
organisation members).

| Path | What it holds |
|---|---|
| `observatory/*.py` | the launcher and the old 0.1 portable CLI, kept working |
| `observatory/engine/` | the complete engine: collectors, findings, store, dashboard, MCP server, tools |
| `observatory/engine/tests/` | engine suites, run by `tests/run_portable.py` in sandboxes |
| `observatory/engine/skill/` | the `observatory-log` plugin; `.claude-plugin/` at the root publishes it |
| `tests/` | root tests: launcher, packaging, release gates, version and plugin consistency |
| `tools/` | release tooling: privacy gate, package checker, source inventory, demo estate |
| `site/`, `docs/site/` | the retired website (a redirect to the product page plus the field-notes article) and its checks; it never reads a workspace |
| `docs/` | onboarding, compatibility, security boundary, UX scenarios, run receipts |

## Commands

| What | Command |
|---|---|
| Install | `python -m pip install -c requirements-full.lock '.[full]'` (in a Python 3.11+ virtual environment) |
| Test (the gate) | `python -m unittest discover -s tests -v && project-observatory full check`, then the rest of the list below |
| Build | `python -m pip wheel --no-deps . --wheel-dir dist && python tools/check_package.py dist/*.whl` |
| MCP (register + proving call) | `claude mcp add observatory --scope user -e OBSERVATORY_HOME="$OBSERVATORY_HOME" -- "$(python -c 'import sys; print(sys.executable)')" "$(project-observatory full-path)/mcp/server.py"`, then `observatory_status` answers with an empty `degraded` list ([README → MCP](README.md#mcp)) |

## Build and test

Python 3.11 or newer with loadable SQLite extensions; choosing the interpreter is in
[docs/ONBOARDING.md](docs/ONBOARDING.md#sqlite-runtime-prerequisite). Node.js 22 and Git 2.17
or newer for the complete checks. These are the commands CI runs
(`.github/workflows/check.yml`), in its order:

```sh
"$PYTHON" -m venv .venv && . .venv/bin/activate
python -m pip install -c requirements-full.lock '.[full]'
python -m compileall -q observatory
python -m unittest discover -s tests -v                # root tests
project-observatory full check                         # every engine suite, each in a fresh sandbox
python tools/update_inventory.py --check               # engine files match SOURCE-INVENTORY.json
python -m pip wheel --no-deps . --wheel-dir dist
python tools/check_package.py dist/*.whl
claude plugin validate --strict .                      # and observatory/engine/skill, and its plugins/observatory-log
python tools/check_public_release.py --history --history-ref HEAD
python docs/site/check.py --self-test
python tools/build_article.py --check
node tools/check_site_interactions.cjs
git diff --exit-code                                   # the checks left no tracked change
```

- The `claude plugin validate --strict` step runs on the ubuntu-latest / Python 3.14 row only,
  with the Claude Code version pinned in the workflow (`npm install -g @anthropic-ai/claude-code@VERSION`).
- One engine suite: `project-observatory full check --suite NAME` (repeatable), `NAME` being
  `test_NAME.py` under `observatory/engine/tests/`.
- A new engine suite is listed in `SUITES` of `observatory/engine/tests/run_portable.py`:
  `LEGACY` for scripts that print `PASS`/`FAIL` and get a synthetic workspace from the runner,
  `BOUNDARY` for self-contained unittest files. A suite not listed there does not run.
- After changing any engine file run `python tools/update_inventory.py` and commit the
  inventory with the change.
- Behaviour changes are test-first: the failing test, then the change, then green.
- CI runs on Python 3.11 as well as 3.14; run `compileall` on 3.11 before pushing when you can,
  since 3.14 accepts syntax 3.11 rejects.

## Privacy rules

This repository is public, and so is its Git history, commit messages included.

- Nothing added may name a private project, person, host, organisation other than
  `passioncode-ai`, machine path (a home directory such as `/Users/<name>/`) or credential
  value, and nothing may cite a decision record kept outside this repository.
- Fixtures are synthetic: `alpha-web`, `beta-api`, `example-org`, `example.com`,
  `*.example.invalid`. Tests use temporary `HOME` and `OBSERVATORY_HOME`, never an operational
  workspace, never live providers and never inherited credentials.
- `python tools/check_public_release.py --history` must report zero findings. A maintainer who
  holds the private predecessor adds `--private-denylist FILE`; that file is never committed.
  Names published on purpose are listed with a reason in `tools/public-identifiers.json`.
- Never commit a workspace, database, registry, dashboard page built from real data, provider
  response or local machine configuration.
- Credential values travel on stdin only. Read the `handling-secrets` skill
  (`observatory/engine/skill/plugins/observatory-log/skills/handling-secrets/SKILL.md`) before
  touching the vault or a provider door.
- Comments and docstrings are the design record: they explain why, under the same rules
  ([CONTRIBUTING.md](CONTRIBUTING.md#comments-and-design-notes)).

## How a change reaches `main`

1. Branch from `main`; commit in small logical commits.
2. Open a pull request against `main`. Three checks are required: the `test` job of
   `observatory-release-check` on each matrix row (ubuntu-latest with Python 3.11,
   ubuntu-latest with 3.14, macos-latest with 3.14).
3. The code is open source under `AGPL-3.0-only OR LicenseRef-PassionCode-Commercial`
   ([LICENSE](LICENSE), [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md); the knowledge base's
   [licensing](https://github.com/passioncode-ai/fabric-workspace/blob/main/knowledge/licensing.md) page), and
   contributions are accepted under [CLA.md](CLA.md): the pull request template's CLA box is
   ticked by the contributor, never by an agent on a person's behalf. Releases up to and
   including v0.9.1 keep the licence they shipped with (PolyForm Noncommercial or Internal Use
   from v0.8.2, MIT up to v0.8.1); describe the current version as AGPL or commercial.
4. `main` keeps a linear history and refuses force-pushes, so a pull request merges by squash
   or rebase, never by a merge commit. `.github/CODEOWNERS` requests the review.

Do not bump the version, edit `CHANGELOG.md` or tag in a feature pull request unless it is the
release itself.

## Decisions and receipts

A unit of work leaves its record in `docs/runs/<date>-<slug>/README.md`: what shipped (PRs,
commits, wheel digests), the checks actually run with their counts, what went wrong and the
check that now catches it, open work and the exact next task. Machine-readable evidence goes
beside it. [docs/HANDOFF.md](docs/HANDOFF.md) points at the current ones and says which next
tasks are the operator's and which a contributor can take.

## Releasing

1. Bump the version in `pyproject.toml`, `observatory/__init__.py`,
   `observatory/engine/configuration.py` and both `COMPATIBILITY.md` files
   (`tests/test_version_consistency.py` fails until they agree), and add the `## X.Y.Z — date`
   section to `CHANGELOG.md`. A plugin change bumps `observatory-log` separately, in its three
   manifests and every `SKILL.md` (`tests/test_plugin_manifests.py`).
2. Merge that pull request through the required checks, then tag the merge commit `vX.Y.Z`.
3. Publish a GitHub release for the tag with the built wheel and `SHA256SUMS`; re-download the
   asset and compare its digest with the one inspected by `tools/check_package.py`.
4. Every machine then runs `project-observatory full update`.
5. Record the release in `docs/runs/<date>-<slug>/`.

## After work

In the same run: update this repository's docs with the change; if a cross-repository fact changed
(a product, a version, a plan row, a principle), update the page in `fabric-workspace/knowledge/`
that owns it; land both; publish (`node scripts/workspace.mjs sync` from a Fabric checkout) or
leave it to the scheduled sync. Leave a handoff with the exact next task — here, the run record
in `docs/runs/<date>-<slug>/README.md` and the pointer in [docs/HANDOFF.md](docs/HANDOFF.md).
