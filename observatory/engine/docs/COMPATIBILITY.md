# Compatibility and upgrades

The application release is **0.26.0**. The complete engine and each user's workspace are separate. Updating program files never intentionally replaces configuration, registry data, credentials, history or local dashboards. The previously published portable 0.1 command set remains a compatibility entry point; its smaller data model is not interchangeable with the complete engine's SQLite database.

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

## Contracts with separate versions

| Surface | Current supported contract | Change policy |
|---|---|---|
| Workspace | format 1, minimum reader/writer application versions | Refuse unknown formats or newer required versions before writing |
| Main configuration | schema 1 | Preserve unknown optional fields; refuse unsupported `must_understand` capabilities |
| Registry | projects/repositories/relations 1–2; other documents 1 | Refuse future versions; preserve optional top-level extension fields on atomic writes |
| SQLite | ten migration IDs (0001–0010), recorded AST checksums | Never rewrite a released migration; append a new ID; `full upgrade` proves the migration on a staged copy, then migrates the live store in place in one SQLite transaction (agents' sessions may keep writing), with a verified snapshot taken first; while it is staged only the SQLite databases with their `-wal`, `-journal` and `-shm` files, the append-only logs under `store/logs` and the session hooks' receipts (`store/raw/record-turn.json`, `store/raw/sessions-seen.jsonl`, `store/raw/companion-faults.jsonl`) may change, and settings, secrets, registries and every other file, the rest of `store/raw` included, must hold still (`workspace_upgrade.volatile`) |
| Plugin execution | API 1; absent version means legacy API 1 | Reject unknown versions and escaped script paths before execution; plugins remain trusted executable code |
| MCP transport | existing declared 2026-07-28 interface, SDK 2.3.0 (`mcp==2.3.0` in the `full` extra; the HTTP transport re-measured on it 2026-10-08) | Preserve existing tool names, camelCase/snake_case aliases and proposal authority; transport negotiation is SDK-owned |
| Tool data | existing published input/output schemas | A closed output schema can reject an added field: version the capability before changing its shape |
| CLI | existing full-engine step names plus workspace management | Keep names/arguments through compatible releases; announce deprecation before removal |
| Profile (`full profile`) | format 1, minor 0 | Refuse an unknown format, a newer engine's profile or unmet `must_understand`; ignore and name unknown sections; never carry or touch `sources`, `storage` or `features.scheduler` |
| Automatic updates and backups (`full maintain`, 0.19.0) | The switch: the file `<home>/auto-update`, on when absent, off only when it holds the word `off`; `updates.auto: false` in settings.json (0.17, 0.18) still reads as off while the file is absent and is moved into it by the next `full auto-update` command; `updates.scheduled` in settings.json, true when absent; the job's record `<home>/store/maintenance.json`; codes in the shared update log `updates.jsonl` (`~/Library/Logs/Project Observatory/`, `$XDG_STATE_HOME/project-observatory/logs/`) | Install only what `full update --apply --unattended` installs (stable releases, signed by the organization's release key, both digests, no `needs_person` step); never downgrade; an update, reinstall or uninstall never writes the switch; an older reader ignores the `updates` key and the switch file; the app is replaced only by a bundle with the same identifier and version, signed by the organization's team `KJ35UYYL22`, with a Gatekeeper pass |
| Release install (`full update`) | GitHub release with `project_observatory-X.Y.Z-py3-none-any.whl`, `SHA256SUMS`, `SHA256SUMS.asc` and GitHub asset digests | Install only when `SHA256SUMS` carries a valid signature by the organization's release key pinned in the engine (`release_signature.py`) and both digests match; an unsigned or malformed signature is refused; never downgrade; roll back to a verified wheel of the running release, which must be signed too for releases from 0.14.0, the first published with a signature; a wheel carrying `RELEASE.json` as `{"version": "X.Y.Z", "needs_person": "<step or runbook URL>"}` for its own version is held by `--unattended` (exit 6) and installed only by a person's `--apply`, which shows the step first; a marker naming another version is ignored, and the release build refuses such a wheel; an unreadable marker holds |

**Lessons and the `kinds` filter (unreleased, additive).** `observatory_lesson` is a new MCP tool with a published input and output schema; no existing tool name, argument or answer field changes. `observatory_search` and `observatory_recall` take an optional `kinds` list and add `kind` to every hit (search already returned rows of every kind; `kinds` absent means today's behaviour). A search answer gains `kinds` when the caller passed it, and a lesson hit gains `lesson`. `retention.json` gains `ledger.kinds` (`lesson`: `proposed_days 365`, `observed_days null`); a workspace whose file predates it ages every kind by the general horizons until the configuration upgrade adds the key, and a hand-edited value wins. `corroboration.json` gains `lessons` (`promoted`, `waiting`, `unresolvable`, `contradicted`) and four `lessons_*` counts, and `counts.proposed` no longer includes lesson rows; `refused` and `unaskable` keep their meaning. The health page's queue digest gains `kind_horizon_days`. Lessons written before as `observatory_record` notes (`statement` starting `lesson:`) stay readable as notes; `tools/migrate_lessons.py --apply` converts them once, as a new revision of the same record. No SQLite migration: a lesson's fields live in the existing `body_json` column.

Semantic versioning applies to the declared public API even before 1.0 as a project policy. A patch fixes behavior within those contracts. A minor release adds compatible behavior and supported migrations. An intentionally incompatible change needs a major release with migration instructions. This does not promise that every past experimental version remains supported forever; each release publishes its tested upgrade matrix. [SemVer specification](https://semver.org/).

## Tested migration boundary

`tests/test_schema_compatibility.py` covers every prefix of the ten migration IDs (0008 adds the agent-memory workflow tables, 0009 rebuilds the lexical index with search keys, 0010 adds the vector-namespace registry and quarantines the legacy OpenAI index), repeat opens, legacy checksum adoption, unknown IDs, changed migration checksum, future user_version, rollback after injected failure, WAL-only data and concurrent openers. Adopting a checksum for a legacy history records the current implementation; it cannot prove which old implementation originally ran.

`tests/test_workspace_boundaries.py` covers concurrent initialization, symbolic-link escape attempts, source changes during migration, future writer refusal and preservation of an existing destination. `tests/test_workspace.py` covers separate homes, optional settings preservation, configuration/registry version refusal and the complete local pipeline through twelve generated pages (`test_complete_local_workflow_and_twelve_pages`). Test execution receipts are recorded separately; listing a test here is not a claim that every release ran it.

## Upgrade and rollback rules

1. Read the target release's minimum Python, SQLite and supported source versions. Full engine requires Python 3.11+ and SQLite 3.37+; macOS/Linux are the supported platform family. Windows file-lock/service support is not claimed.
2. Stop the scheduler and other writers. Old executables cannot honor locks or version checks added later. Do not run two application generations against one workspace.
3. Preview the upgrade. Create a complete private snapshot before applying changes, including config and authored data. SQLite backups use its backup API so committed WAL content is included. [SQLite backup documentation](https://www.sqlite.org/backup.html).
4. Apply supported migrations under a lock, validate, then restart the chosen application version. Interrupted upgrades leave a marker that prevents ordinary runtime from treating partial state as healthy.
5. To roll back, restore a verified pre-upgrade snapshot into a separate home and use its matching application release. An older executable must never silently rewrite a newer database. New observations made after the backup are not present in that backup.

External source directories and externally referenced credential stores are **references**, not bundled backup contents. Their independent backup/recovery policy remains the user's responsibility. Internal credential files, when included in a private snapshot, retain private modes and must never be attached to an issue or public release.

**Encrypted artifacts (0.4.1).** Backups written to the backups root use the `OBSENC1` format, version 1: magic `OBSENC1\n`, a length-prefixed JSON header (`format`, `cipher` AES-256-GCM, `kdf` scrypt with `n`/`r`/`p`/`salt`, `nonce_prefix`, `chunk`, `kind`, `content`, `application_version`, `created_at`), then length-prefixed sealed chunks whose nonce is prefix ‖ counter ‖ final-flag, with magic and header as associated data. A reader refuses any other `format`, `cipher` or `kdf`. `.obsnap` holds a gzip tar of a snapshot directory (manifest verified again after decryption); `.obsdb` holds one SQLite file. `.obmachine` (kind `machine`, OBS-157) holds a gzip tar of a machine bundle: `files/`, `abs/`, `inventories/`, `checkouts/` and `MANIFEST.json` (`format` `observatory-machine/1`); a reader refuses another `format`. Three artifacts per kind are kept. The passphrase cannot be recovered; a lost passphrase means a lost backup.

## Original installation migration

`migrate-local` previews categories and counts. `--apply --writers-stopped` copies into a new destination; it does not activate services or overwrite the original. Curated owner lists move into private ownership configuration. External paths and integration selection must be verified for the destination before activation. SQLite may update its own SHM bookkeeping during a read-only backup; the promise is preservation of logical source data, not byte-identical auxiliary files.

The full source publication deliberately has no ancestry from a private operational Git repository. Release privacy checks examine source, fixtures, package contents and public history independently from runtime migration tests.

## Registry: identity map (0.3.0)

`registry/identity.json` (schema_version 1) records which merge keys and strong anchors belong to
each project id, which ids are retired, and repositories' former names. It is written by the emit
step. A workspace upgraded from 0.2.x gets it on the first emit with every existing id unchanged.
An unreadable map stops the emit step instead of re-minting ids. Contract:
[docs/design/IDENTITY.md](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/IDENTITY.md).

## Registry: provenance and account-qualified ids (0.3.1)

- Every derived relation (`implemented_by`, `deployed_to`, `credential_used_by`) carries a `rule`
  naming the evidence that produced it; `validate` rejects one without it.
- A Cloudflare zone name held by two or more accounts gets the id `zone:<name>@<account>`. A name
  held by one account keeps `zone:<name>`, so only duplicated zones change id on upgrade.
- `heroku-apps.json` rows gain `deployed_commit` (`{sha, release, source}`), taken only from a
  release description that states a commit, never from the configured branch; `null` otherwise.

## Workspace state: incremental store scans (0.3.2)

`store/raw/leak-scan-state.json` gains a `sqlite` key, and `store/scrub-watermark.json` is written
by the scrub. Both hold a per-table rowid mark and an HMAC of the value set, never a value. A
workspace upgraded from 0.3.1 makes one complete pass and is incremental from the next tick on.
Deleting either file only forces a complete pass.

`full doctor` output gains `tick` (`verdict`, `why`, `last_started`, `last_finished`). The server's
`/health` gains `tick.health` with the same shape. A survey's `degraded` may carry
`{"source": "tick"}`. These are additions, and existing fields are unchanged.

## Registry: accounts (0.3.3)

New file `registry/accounts.json` (schema_version 1) and a new relation type `in_account`
(resource → `account:<provider>/<id>`), derived on every emit with a `rule`. Heroku scan rows gain
`team_id` and `owner_id`. The emit accepts a Heroku scan without them and marks its apps
unattributed. Existing ids and fields are unchanged.

## Registry: environments (0.3.5)

New file `registry/environments.json` (schema_version 1) and a new relation type `serves`
(deployment → `environment:<project>/<name>`), derived on every emit with a `rule`. Heroku scan
rows gain `pipeline` and `pipeline_error`. Apps from an older scan are listed as unassigned. The
optional `config/environments.json` is new, and nothing is created for it.

## Registry: credential bindings (0.3.6)

`credential_used_by` edges gain `binding` (`run`, `build`, `local` or `unknown`) and optionally
`environment`, `deployment` and `variable`. `validate` refuses an edge without a binding, and a `run`
edge without a deployment. One credential and project pair may now have more than one edge: one
`local` edge, and one `run` edge per deployment that reads it. The gitignored
`store/raw/remote-env.json` gains `current`, the salted fingerprints of the vault's current values.
The registry's `remote-env.json` apps gain `vault_in_use`, which holds slot names and never a value.

## Workspace state: provider key files (0.3.8)

With `OBSERVATORY_STATE` unset, which is the default, key files stay at `store/.openrouter-key` and
`store/.openai-key`, so nothing changes. With it set, they belong under the selected state, and a
key at the old location is read with a note until you move it.

`store/raw/leak-scan.json` gains `coverage`, `suppressed` and `suppression_problems`. An entry in
`not_scanned` may carry `"unreadable": true`. The optional `config/leak_suppressions.json` is new, and
nothing is created for it.

## Leak scan: suppression identity (0.10.0, PB-032 follow-up)

Leak sightings gain an optional `version_id` (HMAC under the workspace fingerprint salt). Suppressions require this identifier and an exact location; 0.3.11 name/substring-only rules are refused with a warning until explicitly replaced. No existing suppression is broadened or silently migrated. Without the salt, detection still reports sightings and suppression is unavailable. File offsets are reset when known values change. Changes to effective suppression rules, including expiry, replay files and the companion database. The private state gains `values_digest` and `suppressions_digest`; old state causes a complete first pass.

## Leak scan: coded degradations and coverage (ENG-001)

Each entry of `store/raw/leak-scan.json` `degraded` gains `code` — `no-values` (the scan knew no
values), `deadline-targets` or `deadline-store` (the step's deadline cut the pass), `source-unreadable`
(a target or a store would not open) — plus `source` (`leak-scan:<code>`) and `reason` (the `why`), the
fields `/health` and `collector.degraded` read. `coverage` gains `targets_read` (the targets the pass
reached) and `stores_unread` (store labels, `~`-relative). `partial` now means a deadline cut only; an
unreadable source alone no longer sets it. Readers: only a scan that knew no values is
`secret.leak_scan_unmeasured`; every other document keeps its sightings, and a cut one adds
`secret.leak_scan_partial`. Every coded entry, a `source-unreadable` hole included, holds `/health` as
degraded: an unread source is unmeasured, not clean; the hole counts every `not_scanned` note marked
`unreadable` (a session folder that will not list is one, named `~`-relative), every `skipped` entry marked
`unreadable` (an env file or a vault slot that will not read) and every store that would not open.
The env inventory and every file it lists are read as regular files only (`lstat`, then an
`O_NOFOLLOW|O_NONBLOCK` open, then `fstat`): a FIFO, a device, a symbolic link, a NUL in a path, a
`path` that is absolute, climbs with `..` or resolves outside the data folder through a linked
folder, a wrong shape or a document nested past the parser's depth is a hole, never a hang or a
crash; a byte-order mark is accepted. A listed env file is checked AFTER it is opened: the open
descriptor's canonical path (`F_GETPATH` on macOS, `/proc/self/fd` on Linux) must have an ancestor that
IS the data folder — the same device and inode, the ancestor read with `lstat` so a link is never
followed (a folder this user may traverse but not list still compares) — so a folder swapped for a
link before or after the open is a hole, and a case variant of the data folder's name is not. An inventory string with a lone surrogate or a NUL is a shape hole. A hard link to a file outside the data folder cannot be told
apart by any path check and is read like any other regular file. A document from an older scanner (no `code`, no `targets_read`) still reads:
without `known` it is unmeasured as before; with `known` its sightings stand and the partial row says the
scanner recorded no coverage rather than inventing a count.

## Leak scan: every agent's transcripts (I-5, PB-032 remainder)

The leak scan now also reads Codex, Hermes and opencode transcripts and the Claude Code and Codex
prompt histories (`paths.transcript_homes`). A sighting's `where` can name a file under `~/.claude`,
`~/.codex`, `~/.hermes` or `~/.local/share/opencode`, or a `<database>#<table>.<column>` in a Codex
`*.sqlite`, Hermes's `state.db` or opencode's `opencode*.db`.

`coverage` gains:

- `transcript_homes`: `[{agent, home, state}]`, with `state` one of `absent`, `scanned`, `partial`
  (the step's deadline left something under the home unread) or `unreadable`, and `home` `~`-relative;
- `transcript_stores`: `[{store, read, rows_read}]`, one entry per transcript database.

**Findings.** A SQLite store that would not open is now marked `"unreadable": true` in
`not_scanned`. The `source-unreadable` count is unchanged, but `secret.leak_scan_blind` for such a
store rises from `info` to `warning`, because the finding grades on unreadable entries.

A store with a table that could not be read was reported clean before. It is now such an entry
too, its `why` starting with "a SQLite store read only in part" and listing each table, or each
`table.column` whose name is not UTF-8, with the error's type only. Its other tables' sightings
still appear, and it raises `source-unreadable` and `secret.leak_scan_blind` (warning). A name
that was not UTF-8 appears with `\udcXX` escapes.

A store that raised anything else is an entry whose `why` is "a SQLite store stopped while
reading (<Type>)"; its sightings counted before the stop still appear, and the scan still writes
its report. A store that would not open reads "a SQLite store that would not open: <Type>". It
used to be followed by SQLite's message, which is no longer copied anywhere.

**Private state.** `store/raw/leak-scan-state.json` gains:

- `signatures`: `[size, mtime_ns]` per non-`.jsonl` file. Such files are no longer resumed from an
  offset but read whole when they change.
- `sqlite_signatures`: per store, the file's inode. Per table it keeps:
  - the highest rowid and the row count;
  - the sum of the rowids modulo 2147483647 (`sum`), and of their squares modulo 1000000007 (`sq`);
  - the highest update-column value;
  - a digest of the newest row, and digests of up to 64 rows at the update maximum (`at_max`), or
    past 64 rows one digest of the whole set (`at_max_set`).

  Never any row text. A BLOB maximum, or text that is not UTF-8, is kept as `{"sha256": …}`.
  These tables have their rows below the mark read once again: a table marked before the upgrade,
  which has no signature yet, and one whose signature lacks `sum` or `sq`.

**Upgrade cost.** The first passes after the upgrade read much more than an ordinary tick:

- the tracked tree and the built page, once;
- the companion and Observatory stores, once more;
- every Codex rollout in the 14-day window, and both prompt histories;
- every Codex, Hermes and opencode database, whole.

On a busy machine the first tick or two may stop at the step's deadline and raise
`secret.leak_scan_partial` until the marks catch up. Nothing is removed; older readers ignore
the new keys.

## Registry: agents (OBS-83)

New file `registry/agents.json` (schema_version 1, `schemas/agents.schema.json`),
written by the emit from `store/raw/agents.json` (new, collector `collectors/scan_agents.py`, tick step
`scan-agents`, run with `features.machine_watch`) and `store/raw/mcp.json`, and validated by
`validate`. It declares a new source, `SRC-0020`. Rows are `agent:<service-id>.<instance>`, one per
fabric-service/0.1 descriptor (`agent:file:<name>` when a descriptor's own id is unusable), and carry no
`auth` field, no absolute home path and no URL user-info or query (`key_in_url` says one was dropped). The
document's `stale` is true when the collector's scan is older than one tick. `updated_on` is the day the emit
wrote it; `scanned_on`, `first_seen_on` and `last_seen_running_on` are the scan's.
Rows kept from an unreadable services directory carry `carried_since` and are left out of the supervisor
totals. `totals.mcp` counts `attributed`, `ambiguous` (an origin two agents claim), `unattributed_http`, `stdio`
(declarations whose transport is stdio) and `other` (neither stdio nor an HTTP(S) origin, such as `ws://` or a
malformed target). URLs fail closed: `origin` and `source_repository` are
kept only as a strict `scheme://host[:port][/path]` and are otherwise null (with "origin unreadable" in
`degraded`); in every other string a URL-shaped token that is not strict (`@`, `:/`, `:\`, or `?`/`#` after a
`scheme:`) is replaced whole by `[url]`, and the home directory is written `~` as a whole path segment, including doubled separators, `./` and `../`
segments before it and, on macOS, the `/System/Volumes/Data` firmlink (`_`, `-` and any punctuation before it
are boundaries). `validate` refuses the home directory anywhere it ends a path, independently of that rewrite, so
a form the rewrite misses makes the scan stale instead of reaching the registry. This
covers scheme URLs with user-info, a query or a fragment; a schemeless `token=…`, a bare `?key=…` or a
percent-encoded form is not recognised as a URL. A Windows drive path outside the home (`C:\work\x`) holds
`:\` and is written `[url]` too. When the scan is missing or cannot be used (including a scan row without a valid agent id, and any
scan that would produce a document `validate` refuses), the previous document is kept with `stale: true` and a
degraded line naming the error and the day its rows are from; rows are rebuilt from the schema's keys only, so an
undeclared key such as `auth` is never written; a descriptor file whose
name is shaped like a URL is not read. A scan dated after the emit is stale
(`clock-skew` in `degraded`). The wiki projection
mirrors it as it mirrors every registry document. A workspace upgraded from an earlier release gets the
file on its first tick with the feature on. Until then it is absent, and no reader requires it.

