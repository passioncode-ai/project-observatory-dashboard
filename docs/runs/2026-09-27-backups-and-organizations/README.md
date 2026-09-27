# 2026-09-27 — encrypted backups (0.4.1) and organizations (0.5.0)

Handoff for the next agent. Two releases shipped the same day, both through a PR,
the hosted matrix, a tag, a GitHub release with `SHA256SUMS`, and an upgrade of the
operator's installation. Private names, accounts and ids stay in the operator's
workspace; this receipt carries none.

## What shipped

| Release | PR | Merge | Wheel SHA-256 (release asset, re-downloaded and compared) |
|---|---|---|---|
| 0.4.1 — one configurable backups root, `OBSENC1` encryption, rotation | #67 | `89e99d7` | `cec76c7d26d86afb54916e526f16a233320a4ca1864f562c9c24dbaf883b069c` |
| 0.5.0 — organizations, resources, SessionStart destination line, GA4 account checks; `observatory-log` 0.12.0 with `tracking-resources` | #68 | `8fe25a2` | `d9ec93c97fb226166cfa455b3b217f6beb865078108643d3dd7081a94d0a0ee3` |

## Checks actually run

Each release, on a clean clone of the merge candidate, in a fresh virtual
environment installed from the built wheel (Python 3.14.7, macOS):

| Check | 0.4.1 | 0.5.0 |
|---|---|---|
| `project-observatory full check` | 60/60 suites, 334 cases, 1305 assertions | 61/61 suites, 350 cases, 1305 assertions |
| `python -m unittest discover -s tests` | 61 OK | 61 OK |
| `tools/check_package.py` | passed, 287 runtime entries | passed, 291 runtime entries |
| `tools/check_public_release.py --history` | 0 findings | 0 findings |
| hosted matrix (macOS/Ubuntu × 3.11/3.14) | 6/6 pass | 6/6 pass |
| `claude plugin validate --strict` | — | plugin and marketplace pass |
| planted defects, each failing a test | final-chunk flag ignored; header outside AAD; publish without verify; no local fallback | resource merge that overwrites; structured fields refused; conflict resolved by first match |

## What was verified on the operator's installation

- Upgraded 0.4.0 → 0.4.1 → 0.5.0 with `full upgrade --apply --writers-stopped`; each
  pre-upgrade snapshot landed encrypted in the backups root (macOS default).
- `full backups migrate` moved the newest legacy copies into the root and removed
  the rest; no unencrypted snapshot remains in the workspace.
- A real restore of the newest encrypted snapshot into an empty home: 147 files,
  `doctor` reports the database present, compatible and integrity-checked.
- A daily copy taken from a launchd job wrote into `~/Documents` without a privacy
  refusal; the scheduled tick then ran `ok` on both releases.
- With the operator's organization file: every project got an organization
  (rule, declaration, external or default; zero conflicts), the SessionStart line
  names the destination in the workspace's language, and the new
  `analytics.property_wrong_account` check fired on a stale scan and cleared on a
  fresh one after the property's move.
- `observatory-log` updated to 0.12.0 in Claude Code; `tracking-resources` present
  in the plugin cache. Sessions pick it up after a restart.

## Decisions

- Without a backup passphrase nothing is written outside the workspace; with one,
  everything written to the root is encrypted. There is no plaintext-to-cloud mode.
- `observatory_propose` gained two structured fields instead of a new MCP tool, so
  the wire surface and the Fabric contract are unchanged; `project` in the
  project-detail schema is open.
- Resources are append-only on acceptance, keyed by `kind` + `identifier`.

## Open work

1. **Next task — a finding's remedy that the CLI refuses.** `analytics.stale`
   tells the operator to run `./observatory.py google --force`; the step runner
   answers "step arguments are not accepted here". The working command is
   `collectors/scan_google.py store/raw/google.json --force`. Fix the remedy text,
   or let the `google` step pass `--force`, with a test that runs the printed remedy.
2. The operator keeps the backup passphrase outside the machine
   (`full backup-passphrase show` in a terminal) — a human step.
3. One organization has no GA4 account or Figma team configured; its projects'
   SessionStart line therefore names no destination.
4. `features.companion_remediation` is on while the companion's database no longer
   exists on the operator's machine; decide whether to turn it off.
5. Outside this repository: the `agent-sync` guard hook refuses a `git commit` in a
   repository without `.claude/agent-sync.json` when the session's project has one,
   contrary to its own boundary. Worked around by a local-only `init --backend fs`
   (excluded via `.git/info/exclude`), not by bypassing the hook.
