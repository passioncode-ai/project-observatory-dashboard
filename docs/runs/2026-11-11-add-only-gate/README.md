# 2026-11-11: add only, improve only (OBS-515)

Objective: adopt the operator's rule of 2026-11-11 (rule R28 of the organization's agent templates), asked of this
project on the project board (capability `agents.improve`): what an agent shipped is never changed or removed under
its name, only proven improvements are taken, and every change is logged with its reason.

## What shipped

Pull request [#504](https://github.com/passioncode-ai/project-observatory-dashboard/pull/504), branch `r28-add-only`:

- `AGENTS.md`, section "Add only, improve only": the rule, what counts as shipped here (MCP tools, their parameters,
  Fabric capabilities and their effect, released schemas, released changelog sections), how a thing is retired, and
  the two commands.
- `tools/check_add_only.py` and its lock `tools/shipped.lock.json`: 29 MCP tools declared by `mcp/server.py` with
  their parameters and required ones, 9 capabilities of `fabric-agent.json` with their effect. `check` refuses
  `missing`, `input-removed`, `required-added`, `effect-changed`, `retired-incomplete`, `retired-unknown`,
  `retired-served`, `unlocked`, and `lock-rewritten` against the lock on `origin/main`; `lock` adds rows and never
  forgets one.
- `tests/test_add_only.py` (10 cases), run by the root tests.
- `docs/backlog.md`: OBS-515.

Decision: the templates' reference hashes whole entries, which would refuse every edit of a tool's description or an
added optional field; this gate compares only what a caller depends on (names, parameters, requirements, effect).

## Checks actually run (this machine, Python 3.14; 3.11 `py_compile` of the new files)

| Check | Result |
|---|---|
| `python -m unittest tests/test_add_only.py tests/test_backlog_board.py tests/test_git_background_maintenance.py tests/test_docs_drift.py` | 116 tests OK, 1 skipped |
| `python tools/check_add_only.py check` | 29 tools and 9 capabilities keep their promises (no lock on `origin/main` yet: `lock-rewritten` starts with the merge) |
| `python tools/check_backlog.py` | 0 problems (no private denylist on this machine: shapes only) |
| `python tools/update_inventory.py --check` | passed |
| `python -m unittest discover -s tests` | 4 failures in `test_demo_estate` and `test_j1_evidence`, the same 4 on a clean `origin/main` checkout here (package not installed); CI runs them on an installed wheel |

## Open

- Merge #504 once its three required checks pass (squash), then answer the board request with the merge commit.
- The `CHANGELOG.md` line comes with the next release (feature pull requests do not edit it).

Next: merge #504 on green checks.
