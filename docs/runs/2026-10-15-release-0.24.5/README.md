# 2026-10-15 — release 0.24.5

The public record of one release; the changes are in [CHANGELOG.md](../../../CHANGELOG.md). Release node
REL-ENG of release iteration 1. Cut on 2026-10-14 (UTC), installed and checked on 2026-10-15.

## State now

0.24.5 is published, verified and installed on a maintainer machine. Open from this run:
[OBS-104](../../backlog.md) (two engine suites read the host session under `full check`) and
[OBS-105](../../backlog.md) (a broken wiki link is logged as a failed tick step). Next task: OBS-104.

## The release

- **Pull request:** #287, squash-merged as `7e984a90` with `--match-head-commit 02caee8e`. Its CI was green
  on every required check (Linux 3.11 and 3.14, macOS 3.14, each twice; the Windows and Linux desktop builds).
  The merge commit's tree is identical to the gated `02caee8e`.
- **Tag:** annotated `v0.24.5` on `7e984a90`.
- **Release run:** [38648444708](https://github.com/passioncode-ai/project-observatory-dashboard/actions/runs/38648444708),
  every job green: guard, wheel, manifest, macos, windows x64/arm64, linux x64/arm64, feed, publish.
- **Guard verdict (approval by checks):** pass. The guard read "v0.24.5 (7e984a9) is on main" and `check.yml`
  green on that commit ([run 38648444310](https://github.com/passioncode-ai/project-observatory-dashboard/actions/runs/38648444310)).
  Nobody clicked.
- **Published** 2026-10-14T23:25:48Z with 19 assets: the wheel, the Mac app zip, the Windows setup programs and
  the Linux AppImage and `.deb` for x64 and arm64 with their updater signatures and receipts, `latest.json`,
  `SHA256SUMS` and `SHA256SUMS.asc`.

The release commit moves the version in the files every release changes, and moves the companion plugin
`observatory-log` 0.22.0 → 0.24.5 in its three manifests and three `SKILL.md` files: its `handling-secrets`
skill had changed in #195 and #256 without a version.

**CHANGELOG entries added at release.** Three pull requests merged since 0.24.4 had no entry, and the 0.24.5
section adds one for each:

- #241, the Russian copy of the dashboard and the apps;
- #255, cleanup preservation, worktree registration and kept suite receipts;
- #257, the coverage findings and `full machine --memory-only`.

Each claim was checked against the code before it was written: the command in `workspace.py`, the finding
types in `finding_types.py`, and the input ages in `collectors/merge_input_age.py`.

## Checks on the release commit's tree (before merge)

| Check | Result |
|---|---|
| `tests/run_portable.py --jobs 6` (maintainer gate, Python 3.14.7, macOS) | PASS, 247 suites |
| Root tests (`python -m unittest discover -s tests`) | ran 214, OK |
| `project-observatory full check` (development environment) | exit 0, 247 suites |
| `tools/check_public_release.py` with the maintainer's private list | passed: 938 files, 0 findings |
| `tools/check_backlog.py`; `check_manifest_urls.py --release v0.24.5`; `sync_engine_docs.py --check` | 0 problems; 25 URLs, 0 problems; nothing stale |

The tree privacy check first went red on one untracked, git-ignored local settings file that the agent
host keeps in every worktree; it is not part of the tree, and the check passed with the file moved out.

**History, not rewritten.** The same check over Git history with the maintainer's private list reports
findings in older blobs and Git metadata. Three commit messages merged to `main` before this release contain
names on that list: `e5ded968` (#277), `62ec3d4d` (#281) and `43a0ac46` (#195). This release's own diff and
commit message scan clean. A published history is never rewritten; the names are recorded here by commit
only.

## Artefact checks (downloaded from the release)

- `shasum -a 256 -c SHA256SUMS`: OK for the wheel, the Mac app zip and `latest.json`.
- `gh attestation verify … --signer-repo passioncode-ai/.github`: passed for the wheel and the Mac app zip.
- `gpg --verify SHA256SUMS.asc SHA256SUMS`: good signature from the organization's release key.

## Installation and byte check

`project-observatory full update --apply` moved the maintainer machine 0.24.4 → 0.24.5 with status `updated`.
It verified the wheel against GitHub's digest, `SHA256SUMS` and `SHA256SUMS.asc`, stopped the tick and the
server, upgraded the workspace and started both again. Nothing was degraded and there was no rollback.
`project-observatory --version` prints `0.24.5`.

The installed wheel's sha256 (`3b413a85…`) is listed in the release's `SHA256SUMS`. All 640 installed files
match the installed RECORD, and that RECORD is identical to the released wheel's.

## After the release

- **Tick.** The first ticks on 0.24.5 finished and wrote the schema-2 per-collector receipt
  (`store/tick-rotation.json`).
  - `leaks` hit its time limit, which the 0.24.5 notes expect for the first passes after the upgrade
    ("Upgrade cost").
  - The tick that `full update` stopped is reported as interrupted, as designed.
  - `links` exits 1 on every tick (19 times on 2026-10-14, before this release). Its receipt
    (`store/raw/vault-links.json`) reads `broken-links`: one wikilink in the user's wiki resolves to nothing
    (1 of 5636 links in 1102 notes). The tick logs this as a failed step: [OBS-105](../../backlog.md).
- **MCP.** A session's Observatory server started on 0.24.4 answers `stale-server`, as `full update` said it
  would. The installed server, called directly over stdio, reports 0.24.5: `machine.mcp.inventory` returns 34
  servers with nothing degraded, and `observatory_overview` answers.
- **`full check` on the installed copy from the user's own HOME, inside an agent session: FAIL.**
  `test_mcp_inventory` failed under load and passes alone. `test_lifecycle` (its fake `claude` never writes
  `claude-child.pid`) and `test_secret_header` (`test_the_value_never_enters_a_child_argv`: the `ps` shim is
  never called) fail again alone. Both pass with an isolated `HOME`, and in CI on every platform, so the
  shipped code is not in question; the tests read the host session: [OBS-104](../../backlog.md).

## Coordination

The edits to `CHANGELOG.md` and `docs/backlog.md` were made under the agent-sync git lease. Those leases
expired once during a pause and were taken again before any edit. The board ids OBS-104 and OBS-105 were
reserved through the `OBS` register.
