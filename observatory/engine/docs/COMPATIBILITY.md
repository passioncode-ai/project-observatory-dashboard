# Compatibility and upgrades

The application release is **0.35.16**. The complete engine and each user's workspace are separate. Updating program files never intentionally replaces configuration, registry data, credentials, history or local dashboards. The previously published portable 0.1 command set remains a compatibility entry point; its smaller data model is not interchangeable with the complete engine's SQLite database.

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
| SQLite | ten migration IDs (0001–0010), recorded checksums (`v2:` source digests since 0.35.4; the AST digests earlier releases recorded are read, OBS-387) | Never rewrite a released migration; append a new ID; `full upgrade` proves the migration on a staged copy, then migrates the live store in place in one SQLite transaction (agents' sessions may keep writing), with a verified snapshot taken first; while it is staged only the SQLite databases with their `-wal`, `-journal` and `-shm` files, the append-only logs under `store/logs` and the session hooks' receipts (`store/raw/record-turn.json`, `store/raw/sessions-seen.jsonl`, `store/raw/companion-faults.jsonl`) may change, and settings, secrets, registries and every other file, the rest of `store/raw` included, must hold still (`workspace_upgrade.volatile`) |
| Plugin execution | API 1; absent version means legacy API 1 | Reject unknown versions and escaped script paths before execution; plugins remain trusted executable code |
| MCP transport | existing declared 2026-07-28 interface, SDK 2.3.0 (`mcp==2.3.0` in the `full` extra; the HTTP transport re-measured on it 2026-10-08) | Preserve existing tool names, camelCase/snake_case aliases and proposal authority; transport negotiation is SDK-owned |
| Tool data | existing published input/output schemas | A closed output schema can reject an added field: version the capability before changing its shape |
| CLI | existing full-engine step names plus workspace management | Keep names/arguments through compatible releases; announce deprecation before removal |
| Profile (`full profile`) | format 1, minor 0 | Refuse an unknown format, a newer engine's profile or unmet `must_understand`; ignore and name unknown sections; never carry or touch `sources`, `storage` or `features.scheduler` |
| Automatic updates and backups (`full maintain`, 0.19.0) | The switch: the file `<home>/auto-update`, on when absent, off only when it holds the word `off`; `updates.auto: false` in settings.json (0.17, 0.18) still reads as off while the file is absent and is moved into it by the next `full auto-update` command; `updates.scheduled` in settings.json, true when absent; the job's record `<home>/store/maintenance.json`; the check job's record `<home>/store/update-check.json`, which from 0.34.0 also holds `available` (`version`, `found_at`: the newer version found while the switch is off, announced once per version; removed by `full update --apply` when it installs that version or a later one, by `full auto-update on`, and otherwise by the next check once the engine runs it or the switch is on; the Mac app shows its menu item exactly while the key is there; an older engine ignores the key, OBS-333); codes in the shared update log `updates.jsonl` (`~/Library/Logs/Project Observatory/`, `$XDG_STATE_HOME/project-observatory/logs/`) | Install only what `full update --apply --unattended` installs (stable releases, signed by the organization's release key, both digests, no `needs_person` step); never downgrade; an update, reinstall or uninstall never writes the switch; an older reader ignores the `updates` key and the switch file; the app is replaced only by a bundle with the same identifier and version, signed by the organization's team `KJ35UYYL22`, with a Gatekeeper pass |
| Release install (`full update`) | GitHub release with `project_observatory-X.Y.Z-py3-none-any.whl`, `SHA256SUMS`, `SHA256SUMS.asc` and GitHub asset digests | Install only when `SHA256SUMS` carries a valid signature by the organization's release key pinned in the engine (`release_signature.py`) and both digests match; an unsigned or malformed signature is refused; never downgrade; roll back to a verified wheel of the running release, which must be signed too for releases from 0.14.0, the first published with a signature; a wheel carrying `RELEASE.json` as `{"version": "X.Y.Z", "needs_person": "<step or runbook URL>"}` for its own version is held by `--unattended` (exit 6) and installed only by a person's `--apply`, which shows the step first; a marker naming another version is ignored, and the release build refuses such a wheel; an unreadable marker holds |

**Lessons and the `kinds` filter (0.24.5, additive).** `observatory_lesson` is a new MCP tool with a published input and output schema; no existing tool name, argument or answer field changes. `observatory_search` and `observatory_recall` take an optional `kinds` list and add `kind` to every hit (search already returned rows of every kind; `kinds` absent means today's behaviour). A search answer gains `kinds` when the caller passed it, and a lesson hit gains `lesson`. `retention.json` gains `ledger.kinds` (`lesson`: `proposed_days 365`, `observed_days null`); a workspace whose file predates it ages every kind by the general horizons until the configuration upgrade adds the key, and a hand-edited value wins. `corroboration.json` gains `lessons` (`promoted`, `waiting`, `unresolvable`, `contradicted`) and four `lessons_*` counts, and `counts.proposed` no longer includes lesson rows; `refused` and `unaskable` keep their meaning. The health page's queue digest gains `kind_horizon_days`. Lessons written before as `observatory_record` notes (`statement` starting `lesson:`) stay readable as notes; `tools/migrate_lessons.py --apply` converts them once, as a new revision of the same record. No SQLite migration: a lesson's fields live in the existing `body_json` column.

**Probe receipts name the contract commit (0.29.0, OBS-196, OBS-205; corrected by OBS-224).** A receipt written by `full probes --fixture-home PATH` now carries `contractCommit`, the commit of the engine's contract pin (`fabric-contract.lock.json`); receipts written by 0.28.0 carry `contractCommit: null`. The check mode (`full probes-check --fixture-home PATH`, or `--check` on the runner) compares the stored receipt's commit with the pin's and exits 1 when they differ. Receipts exist only in a maintainer's explicitly synthetic probe workspace (`features.probe_fixture` on, `fabric/FABRIC-CONFORMANCE.md`): the probes never run against a working installation, and without `--fixture-home` they exit 2 with that explanation. An installed copy's `full check` runs the portable suite, not `probes-check`, so it needs no step after an upgrade, also after a release that moves the contract pin; the 0.29.0 and 0.29.1 notes that sent every user to `full probes` after updating were wrong. A maintainer whose fixture workspace holds receipts from before a pin move sees `receipts describe contractCommit=..., it is now ...` once, and rewrites them with `full probes --fixture-home PATH`. The vendored interop schemas (`fabric/interop-schemas/`) move to that pin in the same release; what the server sends is unchanged, and `tests/test_interop.py` validates it against the new copies.

Semantic versioning applies to the declared public API even before 1.0 as a project policy. A patch fixes behavior within those contracts. A minor release adds compatible behavior and supported migrations. An intentionally incompatible change needs a major release with migration instructions. This does not promise that every past experimental version remains supported forever; each release publishes its tested upgrade matrix. [SemVer specification](https://semver.org/).

## Tested migration boundary

`tests/test_schema_compatibility.py` covers every prefix of the ten migration IDs (0008 adds the agent-memory workflow tables, 0009 rebuilds the lexical index with search keys, 0010 adds the vector-namespace registry and quarantines the legacy OpenAI index), repeat opens, legacy checksum adoption, unknown IDs, changed migration checksum, future user_version, rollback after injected failure, WAL-only data and concurrent openers. Adopting a checksum for a legacy history records the current implementation; it cannot prove which old implementation originally ran. The same file pins the `v2:` digest of every shipped migration and checks that the AST digest of the running interpreter is listed (`MigrationChecksums`); hosted CI runs it under Python 3.11 and 3.14, and the release of 0.35.4 ran it under 3.11 to 3.15.

`tests/test_workspace_boundaries.py` covers concurrent initialization, symbolic-link escape attempts, source changes during migration, future writer refusal and preservation of an existing destination. `tests/test_workspace.py` covers separate homes, optional settings preservation, configuration/registry version refusal and the complete local pipeline through fourteen generated pages (`test_complete_local_workflow_and_fourteen_pages`). Test execution receipts are recorded separately; listing a test here is not a claim that every release ran it.

## Upgrade and rollback rules

1. Read the target release's minimum Python, SQLite and supported source versions. Full engine requires Python 3.11+ and SQLite 3.37+; macOS and Linux are the supported platform family. On Windows (Python 3.13+) what CI verifies on every change is claimed and nothing more: the file locks of `oslocks` (shared, exclusive, non-blocking, `fd_path`), owner-only files by ACL, process groups, a whole tick run directly, the server installed as a per-user Task Scheduler task and answering its `/.well-known/fabric-service`, the tick task installed, read and uninstalled with no task left behind, and every suite of the portable engine set except those in the [known list](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/../tools/windows-known.json), each with its backlog row (OBS-278, OBS-279). Not claimed on Windows until a person runs the [Windows live check](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/WINDOWS-LINUX.md#windows-live-check): a tick started by Task Scheduler, the maintenance task, the single-writer lock between two scheduled ticks, the tasks across a sign-out and a restart, the desktop app, an agent's MCP host; not built yet: the local channel's named pipe (OBS-256) and `node.lifecycle` (OBS-257). [What is verified, suite by suite](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/WINDOWS-LINUX.md#verified-on-windows-in-ci-obs-277).
2. Stop the scheduler and other writers. Old executables cannot honor locks or version checks added later. Do not run two application generations against one workspace.
3. Preview the upgrade. Create a complete private snapshot before applying changes, including config and authored data. SQLite backups use its backup API so committed WAL content is included. [SQLite backup documentation](https://www.sqlite.org/backup.html).
4. Apply supported migrations under a lock, validate, then restart the chosen application version. Interrupted upgrades leave a marker that prevents ordinary runtime from treating partial state as healthy.
5. To roll back, restore a verified pre-upgrade snapshot into a separate home and use its matching application release. An older executable must never silently rewrite a newer database. New observations made after the backup are not present in that backup.

External source directories are **references**, not bundled backup contents. Their independent backup/recovery policy remains the user's responsibility. A credential store the settings name outside the workspace (`sources.secret_store`, else `sources.secrets`) is carried inside an encrypted workspace snapshot from 0.34.0 (OBS-350, below), and stays a reference for a plaintext `--output` snapshot, a snapshot taken without a passphrase and the snapshot an update takes before it upgrades. Internal credential files, when included in a private snapshot, retain private modes and must never be attached to an issue or public release.

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


## Registry: derivations (OBS-321)

New optional curated file `config/derivations.json` (schema `derivations/1`), the operator's own and
never shipped: `{"schema": "derivations/1", "derivations": [...]}`, each entry `copy` and `source`
(repository ids as the registry spells them), `kind` (`copy` or `move`), `copy_point` and
`sync_base` (a source commit of 7 to 64 hex digits, or `{source_commit, copy_commit?, date?}`),
and optionally `builder` (`{path_hint, rules_count?, rules_sha256?}`), `source_branch` (default: the
source repository's `default_branch`), `name_prefix` (0.33.0, OBS-332: the prefix the organization a
copy or move went to puts before its names, lowercase letters, digits and hyphens ending in a hyphen,
up to 32; a private value, never written in this repository), `note` and `evidence` (one line each). A copy needs
`copy_point`; a move has no `sync_base`. `last_synced` is read as an alias of `sync_base`, and an
entry giving both is refused (`unknown-field`). Nothing is created for it: without the file there is no
relation, and no reader reports anything missing. A file that is not JSON or not this schema is
refused whole; an entry is refused alone, by code, in `degraded` and `refused`, and never repaired:

| Code | Meaning |
|---|---|
| `unreadable` | the file is not JSON, or not an object of schema derivations/1 |
| `not-an-object` | an entry is not an object |
| `unknown-field` | an entry carries a field this schema does not define |
| `bad-repository` | copy or source is not a `repository:` id |
| `unknown-repository` | copy or source is not a repository in the registry |
| `self-derivation` | a repository is recorded as derived from itself |
| `bad-kind` | kind is neither `copy` nor `move` |
| `bad-point` | copy_point or sync_base is not a commit or {source_commit, copy_commit?, date?} |
| `missing-copy-point` | a copy without copy_point: behind cannot be measured |
| `move-with-sync-base` | a move is never synced, so it carries no sync_base |
| `bad-builder` | builder is not {path_hint, rules_count?, rules_sha256?} |
| `bad-branch` | source_branch is not a branch name |
| `bad-prefix` | name_prefix is not lowercase letters, digits and hyphens ending in a hyphen |
| `bad-text` | note or evidence is not short plain text, or looks like a credential |
| `duplicate-copy` | two entries name the same copy: a copy has one source |
| `cycle` | the entries form a cycle of derivations |

A copy may carry a tracked `.copy/upstream.json` (read at HEAD of its local clone; its size is asked of git
first and a blob over 64 KiB is refused unread; a file that cannot be parsed, a nested-array bomb
among them, is `unreadable`, and one copy's failure is named in `degraded` without removing the other
relations):
`kind` (`rewritten` for a copy whose history a filter rewrote, `export` for a copy made by export,
both read as the curated kind `copy`; or `move`), `copy_point`, `sync_base` (the last source commit
carried into the copy; same shapes as above) and `deny` (relative paths the copy never takes).
A commit there may also be written `<name>@<sha>`, as a copy's sync commit names its source in
`Source-Revision: <name>@<sha12>` (0.33.0, OBS-334): the name (a letter or digit, then up to 63 of
letters, digits, `.`, `_` and `-`) is checked and dropped, and the commit is compared as before.
`last_synced` is read there too, as an alias, when `sync_base` is absent. Another kind value is listed in the relation's `copy_record.problems`. Any other field, a `source`
among them, is ignored. Where both files give a value the curated one is
used and the difference is listed; a value only the copy gives is used and marked as the copy's.

New file `registry/derivations.json` (`schema_version` 1, `schema` `derivations/1`), rebuilt by every
emit, declared by the new source `SRC-0023`: `relations[]` with `copy`, `source`, `kind`,
`source_branch`, `copy_branch`, `copy_point`, `sync_base`, `builder`, `deny`, `provenance` (each
value's side: `curated` or `copy`), `copy_record` (`state`: `read`, `absent`, `no-clone` or
`unreadable`; `copy_kind`, the copy's own kind word; `problems[]`; `differs[]`), `behind` (null for a move; `state` `measured` with `commits`,
`outside_deny` (counted with full history, so commits on merged side branches are not simplified
away), `ref`, `head`, `on_branch`, `counted_from` (the recorded commit, or, when it is off the branch,
the merge base the count starts from), `oldest_on`, `since`, or `unknown` with its `reason`) and
`source_archived`, and `name_prefix` when the entry gives one; `unrecorded[]` (copies that carry `.copy/upstream.json` and no accepted entry);
`refused[]`; `degraded[]`; `totals`. `behind` is counted in the source's local clone (its
`origin/<branch>`, else the local branch), offline: nothing is fetched and nothing is written to any
repository. The findings `copy.behind_source` (the source has commits after `sync_base` outside
the deny paths: warning once the oldest is over 7 days old, info before that or when not measured),
`copy.relation_unrecorded` (a copy that says so with no entry, a recorded copy that does not say so,
or a record that cannot be read), `copy.record_differs`, `copy.moved_source_open` (a move whose
source is not archived) and, since 0.33.0, `copy.name_unprefixed` (info: the entry gives `name_prefix`
and the copy repository's name does not start with it) are rebuilt from it on every tick.

`project.detail` (`observatory_project`) gains `project.derived_from` and `project.copies`: lists
of `{copy, source, kind, copy_point, sync_base, behind}` (commits as the source spells them;
`behind` null when not measured or for a move), present whenever `registry/derivations.json` exists
and absent when no relation was ever recorded. Its published schema leaves the `project` object
open, so no schema changes. `project.resolve` (`observatory_resolve`) is deliberately unchanged and does
not carry them: its published v0.24.0 answer is closed, a field added there fails every host that
validates it, and agents read the relation through `project.detail`. A refusal naming one of the project's
repositories is in `degraded`, and a curated file the emit has not turned into
the registry document yet is named there too. New command `project-observatory full copies list|plan
COPY`, which reads only and prints the sync steps; it writes nothing. An older engine ignores both
files; deleting `registry/derivations.json` loses nothing the next emit does not rebuild.

## Machine placement (0.30.1, OBS-283, OBS-284; 0.31.0, OBS-282, OBS-286)

A machine bundle's `MANIFEST.json` and a workspace snapshot's `manifest.json` gain `machine_id`, the machine they
were taken on (`m-` and 24 hex digits of a SHA-256 over the platform's own machine identifier; `machine_identity.py`).
An older engine ignores the field, and restores a bundle or snapshot whole wherever it runs. This release restores
one that names another machine, or none (taken before 0.30.1), as a second machine's: a bundle writes no private SSH
key and stages its jobs, and a snapshot leaves out `node/device/`, `node/orgs/`, `node/records/` and
`<home>/store/maintenance.json`, turns `features.scheduler` off and writes a new `instance_id` into `workspace.json`.
`--same-machine` at a terminal restores either whole, as before. An update's own rollback restores its snapshot whole.
From 0.34.0 (OBS-348) a second-machine restore also leaves out `secrets/backup-passphrase`: each
machine keeps its own backup passphrase, and the second machine makes its own with `full maintain ensure`, which the
answer's `machine.next` names, beside `machine.notes` (keep the other machine's passphrase off both machines if it
may be retired). This is a behaviour change for second-machine restores only; a same-machine restore keeps the file.

These files live outside the workspace, in `${XDG_DATA_HOME:-~/.local/share}/project-observatory/` on every
platform, so neither a machine bundle nor a workspace snapshot carries them:

| Path | Since | What | Older engines, removal |
|---|---|---|---|
| `placement.json` | 0.30.1 | schema `observatory-placement/1`: `machine` (this machine's id), `updatedAt`, `elsewhere[]` of `label` (launchd label or systemd unit), `serviceId` (`<id>.<instance>` of the Fabric service whose descriptor names that label, or null), `placedOn` (the machine id it runs on, `unknown` for a bundle that named none), `since`, `by` (`machine-restore` or `machine-move leave`), and since 0.35.11 (OBS-480), when a forward is set up, `forward` (the launchd label of the job that forwards the service's local port to where it runs) and `forwardAdopted` (true for a forward set up by hand and adopted); owner-only, written atomically; read by Fabric Dashboards (FD-132) until the estate record carries placement | an older engine neither reads nor writes it; a file that is not this schema is refused by name, never replaced; deleting it forgets which jobs run elsewhere, it moves no job |
| `machine-move/LaunchAgents/`, `machine-move/systemd-user/` | 0.30.1 | the jobs a second-machine restore or `full machine-move leave` staged, byte for byte; nothing loads this folder at login | `full machine-move cutover LABEL` moves one into the job folder and loads it; deleting a file deletes that staged copy only |
| `~/Library/LaunchAgents/org.project-observatory.forward.<id>.<instance>.plist` | 0.35.11 | the port forward `full machine-move leave LABEL --to ID --via USER@HOST` installs (OBS-480): `/usr/bin/ssh -N` with `BatchMode=yes`, `ExitOnForwardFailure=yes`, `ServerAliveInterval=30` and `-L 127.0.0.1:PORT:127.0.0.1:PORT -- USER@HOST` as an argument array, `RunAtLoad`, `KeepAlive`, `ThrottleInterval` 30; mode 0600 | an older engine neither writes nor removes it; `full machine-move return LABEL` unloads and removes it; the engine's prefix keeps it out of a machine bundle and of `machine-move status` M4 |
| `machine-move/forwards-set-aside/` | 0.35.11 | the plist of a hand-made forward that `machine-move return LABEL --remove-adopted` unloaded, moved here (owner-only folder), never deleted | delete a file once you no longer need that job |
| `machine-move/restore.json` | 0.31.0 | schema `observatory-machine-restore/1`: `machine`, `updatedAt` and one part per step that wrote (`apply`, `packages`, `clone`), each with its `at`, the bundle's name, counts and failures; `apply` adds `mode`, `taken_on`, `this_machine`, `only`, `skip`, `left_out` and `other_keys[]` of `key`, `fingerprint` and `in_ssh` (the public fingerprints of the keys a second-machine restore withheld; `in_ssh`, added after 0.31.0, is true when the key sat directly in `~/.ssh`, and only such an entry gets a `rekey` step in status, so an entry written by 0.31.0, without it, gets none); owner-only, written atomically; written by `full machine-restore`, read by `full machine-move status` (OBS-282, OBS-286) | an older engine neither reads nor writes it; one that is not this schema reads as `unknown` in status; deleting it makes the restore item `unknown` |
| `machine-move/peers/` | 0.31.0 | the other machine's facts (`full machine-move facts --out FILE`, schema `observatory-machine-facts/1`: `machine`, `generated_at`, `engine_version`, `platform`, `ssh[]` of `file` and `fingerprint`, `device`, `jobs[]`, `elsewhere[]`, `restore`, `omitted`), placed by the person; written whole through a temporary file and a link, never over an existing name; `full machine-move status` reads every `*.json` here | never written by the engine; a file that does not read is named in `degraded`; deleting one forgets that machine |
| `machine-id` | 0.30.1 | a random machine id, written only where the platform has no machine identifier of its own (macOS, most Linux and Windows have one) | deleting it gives the machine a new id: bundles and snapshots it took before then read as another machine's |

## Background jobs name no Python release (OBS-393, 0.35.6)

No migration and no new workspace file. Until 0.35.5 every job definition named files inside the engine's
`<venv>/lib/python3.X/site-packages/`: the maintenance pass and the five-minute check
(`tools/maintain.py`, with that folder as `WorkingDirectory`), the tick (`tools/tick.sh`, `tools/tick.py` under
systemd), the server (`tools/serverd.py`) and the per-install manifest `<home>/config/fabric-agent.json`
(`mcp/server.py`). A venv rebuilt on another Python minor release (3.14 to 3.15) no longer has that folder, so
launchd failed every run with exit 78, and the repair rewrote only the interpreter.

- A definition written by this release names the venv's stable interpreter and a program name:
  `<venv>/bin/python3 -P -m observatory.job maintain run --first-check-delay 90` (likewise `maintain check`, `tick`,
  `tick-py`, `serverd --run --port N`, and `mcp --stdio --home HOME` in the manifest). `observatory/job.py` is a new
  module of the package; on macOS and Linux it replaces itself with the very program the old definition named
  (`python <engine>/tools/maintain.py run ...`, `/bin/bash <engine>/tools/tick.sh`), with SIGPIPE and SIGXFSZ at
  their defaults, so the process, its arguments, its folder (the engine) and its signals are what they were; on
  Windows it runs the program in its own process. `WorkingDirectory` (launchd), `WorkingDirectory=` (systemd, new in
  the pass's unit) and the Task Scheduler folder are the workspace home. An engine run from a checkout, whose paths
  name no Python release, keeps naming its files as before; so does a Windows venv (`Lib\site-packages` names no
  release). A root-owned `space_daemons.py` accepts the new definitions unchanged: the program is still the venv's
  interpreter, and the workspace home is inside the account's home.
- No definition names an interpreter that cannot start the launcher: when the interpreter is not the engine venv's
  own `bin/python3`, or that is a link to a Python that is gone, installing or repairing a job is refused with that
  reason (`maintain ensure` answers `unscheduled`, a repair answers `skipped`). A `pyvenv.cfg` `home` that is gone
  while `bin/python3` still starts is not refused; doctor names it. `install_launchd.py status|uninstall|run-now` and
  `serverd.py --uninstall` on Linux and Windows need no buildable job, so they work with such a venv.
- Repair compares every key the installer writes with the installed definition. `install_launchd.repair_definitions`
  (the old name `repair_interpreters` still answers) mends the tick and the server on macOS, and
  `install_launchd.repair_systemd` their systemd user units on Linux (then `daemon-reload`); each keeps the tick's
  interval, the server's port, the installed `PATH` (without a Homebrew Cellar folder or one another account can
  write) and probe limit. In a plist every key and variable the installer does not write stays (`Disabled`,
  `HTTPS_PROXY`, `SSL_CERT_FILE`, ...), except keys an earlier installer wrote and this one drops (the server's
  `LowPriorityIO` and `Nice`, the throttling that starved it under load). In a systemd unit only `Environment=`
  variables stay; any other directive of a person's goes in a drop-in (`systemctl --user edit <unit>`, a
  `<unit>.d/*.conf` file), which a rewrite never touches. A found row gains `changed` and `missing`; a drift that cannot be mended here is answered
  under `skipped` with its reason. The maintenance pass applies it: a running tick waits for the next pass, a launchd
  job is reloaded only when it was loaded, and a running systemd server is restarted. `full maintain ensure` now does
  the same for the tick and the server after scheduling its own two jobs, answering `jobs`, and refreshes the hooks'
  settings (`hook_env`). Task Scheduler's tick and server are not migrated: their venv paths name no release.
- For an account with no GUI session, or one whose job root runs as a system daemon, a drifted background definition
  is rewritten and the answer is `waiting-for-root` with root's `remove`, then `apply`, never a bootout of a running
  pass. The answer stays `waiting-for-root` while root's loaded copy (`/Library/LaunchDaemons/<label>.plist`, readable
  by every account) names another program or folder than the current definition, and the background copy is compared
  with a document carrying its own `PATH`, so an ensure from another shell does not ask root again.
- Doctor and `full maintain status` name `job-path-missing` (a launchd definition of the pass, the check, the tick
  or the server, or a systemd unit of any of the four, whose program, an argument or its folder does not exist; Task
  Scheduler's are not read; step `project-observatory full maintain ensure`) and `venv-home-missing` (the engine's
  `pyvenv.cfg` names a `home` that is gone: its interpreter may still start through another link, and if the jobs fail a
  person recreates the venv; no step).
- The plugin hooks: `full agent install` still writes `OBSERVATORY_ROOT` (hooks of every plugin version read it) and
  now names the venv's stable interpreter (`<venv>/bin/python3`, the one the jobs name) in `OBSERVATORY_PYTHON`
  rather than the installer's `sys.executable`. `full maintain ensure` rewrites both when they name a path that is
  gone and `OBSERVATORY_HOME` is this workspace (`agent_plugin.refresh_hook_env`); `full agent uninstall` also
  removes such a stale value an earlier install wrote. The hooks of plugin 0.35.6 (`session-start.sh`,
  `record-turn.sh`) treat a root that does not exist as unset and find the engine under an absolute
  `OBSERVATORY_PYTHON`'s venv, in the `lib/python3.X` its `pyvenv.cfg` names; `handling-secrets` falls back to
  `project-observatory full-path`. The plugin and its skills move from 0.30.0 to 0.35.6 together.
- On a machine that already failed: install this release into the venv (`project-observatory full update --apply`,
  or a reinstall of the package), then `project-observatory full maintain ensure`; where it answers
  `waiting-for-root`, an administrator runs the two `sudo` steps it prints, in order. Then
  `project-observatory full doctor` shows no `job-path-missing`.
- Going back to 0.35.5 or earlier leaves definitions naming `-m observatory.job`, which those releases do not ship.
  Their `full maintain ensure` rewrites the pass and the check; the tick and the server need their installers again
  (`install_launchd.py install`, `serverd.py --install`, run with the older engine's interpreter).

## Workspace restore: a carried secret store put back (OBS-350 part 2, the release after 0.35.4)

Additive; the snapshot format stays 1 and nothing migrates. `full restore` writes each store a snapshot
carried (`externals[].state` `carried`) into the folder the restored settings name for it: the first of its
`settings_keys` present in `sources` after the home rewrite (OBS-347), expanded under this home. New flags,
each optional, so every existing command line answers as before for a snapshot that carried nothing:
`--secret-store {auto,keep,merge,replace,skip}` (default `auto`), `--secret-store-only` (write the stores of
SNAPSHOT into the folders the current workspace's settings name; the home is not touched; refused without a
workspace), `--secret-store-names` (relative paths of conflicting and not restored files in the answer) and
`--json` (a refusal or failure as `{"error": {"code", "message"}}` on stdout, exit 2 or 1; without it the
message stays `Observatory: ...` on stderr) and `--secret-store-target PATH` (confirms where a snapshot taken on
another machine or under another home writes its store; it must equal the folder the refusal shows). The policy decides what happens to a folder that already holds a
store; it is computed after verification and before the home is staged, and the store is written before the
home is renamed into place. Refusals, exit 2, nothing written: `secret-store-not-empty` (`auto` and a store that
differs), `secret-store-unresolved` (no folder named, a relative one, or a link or file at that path),
`secret-store-target-inside-home`, `secret-store-target-refused` (the home itself or a folder holding it,
`~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.config/gh`, `~/Library/LaunchAgents`, `~/Library/Keychains` or a folder inside
one, a start-up file of the home; and under `merge` a folder that holds files but no `projects/`, which does not
look like a vault store), `secret-store-target-unconfirmed` (a snapshot taken on another machine or under another
home, whose target neither `--secret-store-target` nor this machine's own workspace settings name),
`secret-store-manifest-mismatch` (now also a link whose target passes through another link of the store). Under
`keep` (what `full init` passes) none of these is a refusal: the store is answered as state `skipped` with the code
as its `reason`, nothing is written, and the restore goes on. Failure, exit 1, the folder left as it was and the
home not restored: `secret-store-write-failed` (under `keep`, again `skipped` with that reason). The target vault's
own lock (`projects/.vault.lock`, as `tools/vault.py` takes it) is held while the store is read again and written.
A folder holding only lock files and temporaries (regular files named like the ones the copy skips) is empty; any
folder, a folder named like a skipped file included, makes it not empty. A folder that is replaced, empty or not,
is moved aside, never deleted. A journal (`*.jsonl`) that only grew since the snapshot reads `same`: this departs
from the spec's "journals that differ are conflicts", so that the restore's own `restore-store` line, or a vault
write after the snapshot, does not make a rerun refuse. A file another writer creates while a merge writes it is a
conflict and is never overwritten; each merged file is checked against its digest before it is put in place (a
hard link, or an exclusive create where the file system has no hard links). A protected place is recognised by
name, case-folded on macOS and Windows (`~/.SSH` is `~/.ssh` there), and by identity: the target's nearest existing
folder and its parents are compared with each protected folder that exists (`os.path.samefile`). A snapshot whose
machine and home are this machine's, or one restored with `--same-machine` (a person's choice at a terminal) under
the same home path, writes without confirmation. With `--secret-store-only`, `--secret-store-target` is the
confirmation of the folder the current settings name: another folder is refused (`secret-store-target-unconfirmed`),
never ignored. A behaviour change for a snapshot taken by 0.34.0 or later that carried a
store: `auto` now writes it where none is, and refuses the whole restore when a different store is there, where
0.34.0 to 0.35.4 restored the home and left the store in the snapshot; `--secret-store skip` restores as they did.
`full init` on an empty home passes `keep` and an update's rollback passes `skip`, so neither can fail or write
because of a store. `maintenance.restore_latest` takes `secret_store` and `secret_store_names`; a store refusal
other than a manifest mismatch is raised, never turned into "try an older backup". `workspace_upgrade.restore`
gains the keywords `secret_store`, `secret_store_names` and `snapshot_name`; `workspace_upgrade.restore_store_only`
is new; the refusals are `secret_store_carry.CarryRefused` (a `ConfigurationError` with `code`) and the failure
`secret_store_carry.CarryFailed` (a `RuntimeError` with `code`).

The restore answer gains `secret_store` (`state` `written`, `same`, `merged`, `replaced`, `kept-existing`,
`skipped`, `not-in-snapshot` with the snapshot's `reason`, or `not-applicable`; `reason`, `target` in display
form, counts `files`, `same`, `conflicts`, `only_here`, `not_restored`, `stages_removed`, `journal`, `aside` after
a replace, and `stores` when there are several; `conflict_paths` and `not_restored_paths` only with
`--secret-store-names`), and `external_sources_included` is true when every carried store reads written, same,
merged or replaced. A second-machine restore moves `<state>/vault-digests/local.json` to
`<state>/vault-digests/peers/<machine>.json` (or `peer-<hex8>.json`) and answers `digest_manifest` (`state`
`moved-to-peers` with `file`, or `left` with `reason`). New files outside the workspace, both owner-only: the stage
`<parent>/.<folder>.restore-<hex12>` (renamed into place, or removed; one a killed restore left is removed by the
next restore after an hour) and, after `replace` or a write over an empty folder,
`<folder>.before-restore-<UTC stamp>-<hex6>`, which the engine never deletes. The answer's `secret_store` may carry
`notes` (for instance a folder fsync that failed after the store was put in place) and, for a store `skipped` with
a reason, `next`: the exact command that writes it (`full restore SNAPSHOT --secret-store-only`, with
`--secret-store-target` for a folder not confirmed, after `configure sources` for a folder refused or unnamed).
`full init` passes the restore's `secret_store` through in its answer. New workspace state:
`store/raw/secret-store-restore.json`, owner-only, written by every restore and `--secret-store-only` of a snapshot
that carried a store and removed by a restore of one that carried none (`at`, `snapshot` file name, the first store's
`state`, `reason`, `target` in display form, `files`, `next`, and `stores`, the same per store; never a value). It
never travels in a snapshot (`workspace_upgrade.EXCLUDED_FILES`). A skip chosen with `--secret-store skip` is
recorded as `skipped-by-choice`. Doctor's `backups.secret_store` gains `last_restore` from it and, for each store
`skipped` with a reason whose folder holds no store, the warning `backups-secret-store-not-restored` with that
store's `next` as its step (also on Health); a store put in that folder by a later restore, by `vault.py` or by hand
clears it (doctor lists the folder's top level, never reads a file). An older engine ignores the file. The store's `projects/movements.jsonl` gains one `restore-store` row per write (`secret` `store/*`,
`tool` `workspace_upgrade.py`, `snapshot`, `taken_on`, `files`, `state`; never a value), which an older `vault.py`
reads as any other movement. An older engine reading a snapshot this release writes still restores the home only.

## Reloading a repaired job waits for launchd (OBS-410, 0.35.7)

No migration. 0.35.6 rewrote a drifted job definition and reloaded the job with `launchctl bootout` then
`bootstrap`; launchd refuses a bootstrap while the bootout of the same label is still finishing (the server is
given 40 s to exit), so after `full update` to 0.35.6 the server could stay unloaded, and a pass leaves an unloaded
job alone. From 0.35.7 a reload waits until the label is gone (up to 45 s) and retries a bootstrap launchd refuses
for now (exits 5, 37 and 125) up to five times; a reload that still fails answers `jobs.result: reload-failed`,
naming the jobs it left `unloaded` and those still running their old definition (`not_reloaded`); `full update`
checks the jobs it started once `maintain ensure` has run and loads one that is gone, or names it under
`services_not_restarted`. A workspace that was updated to 0.35.6 (its `store/logs/update.jsonl` says so) and whose
server has its launchd definition and Fabric descriptor in place but is not loaded gets the server loaded again,
once, by the first `maintain ensure` or pass of 0.35.7 (the update to 0.35.7 runs `maintain ensure`), answered
under `jobs.recovered`; the new file `store/raw/server-recovery-obs410.json` records the first decided outcome
(`loaded`, `loaded-again`, `no-server`, `disabled`), after which the check never runs again, so a server a person
boots out later stays out; only a load launchd refuses is tried again at the next pass. A server turned off with
`serverd.py --uninstall` (which removes the definition and the descriptor) or stopped in Fabric Dashboards (a
disabled label) is never loaded; one booted out by hand with a plain `launchctl bootout` before the update to 0.35.6
cannot be told from a lost one and is loaded once. A workspace whose schedule was turned off with
`maintain uninstall` has no pass and is not recovered. By hand:
`OBSERVATORY_SYSTEM_SETUP=1 python "$(project-observatory full-path)/tools/serverd.py" --install`.

## Workspace restore: verifying a carried secret store into a disposable folder (OBS-394, 0.35.7)

Additive; the snapshot format stays 1 and nothing migrates. `full restore` takes `--secret-store-verify-into DIR`,
with a whole restore and with `--secret-store-only`: each store the snapshot carried is written into
`DIR/<name>/` (folders 0700, files 0600 through osprivacy) and read back against the manifest (every file's size
and SHA-256, every folder, every link's target on POSIX, nothing unlisted), instead of into the folder the
settings name. That folder is never written, locked, swept or recorded, so no confirmation is asked for a
snapshot from another machine, and `store/raw/secret-store-restore.json` is neither written nor removed (doctor's
`backups-secret-store-not-restored` is not raised or cleared by a verification). The verified copy is left in
`DIR`; the caller removes it. With `--secret-store-only` and the flag, no workspace is needed. The answer's
`secret_store` reads `state` `verified` with `name`, `left_at` and `into` in display form and the counts `files`,
`bytes`, `links`, `directories` and `skipped` (the snapshot's `not_carried` files plus links not made on
Windows), `verified` (the number of stores verified, also beside `not-in-snapshot` and `not-applicable`),
`stores` when there are several, or `not-in-snapshot` / `not-applicable` as before; a whole restore answers
`external_sources_included` false, and `--secret-store-only` answers `status` `store-verified`, or
`nothing-to-verify` (exit 0) when the snapshot carried no store. Refusals, exit 2, nothing written:
`secret-store-verify-into-refused` (DIR is, holds or lies inside the new home or the workspace, the workspace
`OBSERVATORY_HOME` names and the default one (`osprivacy.default_home`) when either is another folder, the
snapshot and its file (not the folder holding the file), each of those workspaces' backups folder, any folder the snapshot's
settings (after the home rewrite) or one of those workspaces' settings name in `sources.secret_store`,
`sources.secrets` or a key a carried store was recorded under, `OBSERVATORY_SECRETS` or `OBSERVATORY_VAULT_DIR`,
or a place `secret-store-target-refused` names; compared resolved and case-folded, and by identity: each
existing folder's device and inode, so a linked parent or macOS's `/System/Volumes/Data/...` view of a folder
is the same folder, except on a volume that reports inode 0 (FAT, exFAT, some network shares on Windows), where
the resolved-name compare decides alone), `secret-store-verify-into-unchecked` (a workspace on this machine,
the destination or one of the live ones, whose settings do not read: its store folders are unknown, so the
verification refuses rather than skipping it),
`secret-store-verify-into-not-empty` (DIR holds anything, or is a link or a file) and
`secret-store-verify-into-conflict` (with `--secret-store-target` or a `--secret-store` other than `auto`); a
tampered carried file stays `secret-store-manifest-mismatch`. The place and the contradiction are checked before a
passphrase is asked (`full restore --latest` included), and DIR is checked again right before the first write;
a missing DIR is made with a plain mkdir (0700, each missing parent too), and each `DIR/<name>/` the same way: one
that appears between the check and that mkdir is refused (`secret-store-verify-into-not-empty`) and left as it
is, never removed by the cleanup. Failure, exit 1, every copy the run
made removed and no home restored: `secret-store-verify-failed` (a written file or link that does not match), or
`secret-store-write-failed` (a write that failed); a restore that fails after the copy (an interrupt
included) removes it and the folders it made, so `--latest` falling back to an older snapshot finds DIR as it
was. `workspace_upgrade.restore` and `restore_store_only` and
`maintenance.restore_latest` gain the keyword `secret_store_verify_into`; `secret_store_carry` gains
`verify_into`, `verify_summary`, `discard_verified`, `check_verify_into`, `verify_conflict` and
`setting_folders`. Without the flag
every command line answers as before.

## Registry and doctor: readable agents page, account organizations, DigitalOcean signals (OBS-317, OBS-412 part 1, OBS-421, OBS-423, 0.35.8)

Additive; nothing migrates and no setting changes. With `features.wiki_projection` on, the projection writes
`inventory/agents.md` beside `inventory/agents.json` in the wiki (generated, overwritten on each projection, built
from `registry/agents.json` alone, with the inventory notes' frontmatter; no path or token), and its report `store/raw/projection.json` gains `pages`.
`registry/digitalocean-account.json` and `registry/cloudflare-account.json`: every resource row gains `account_ref`
(its account row's `id`), DigitalOcean account rows gain `team_uuid` and Cloudflare account rows `account_id`. When
`config/organizations.json` names organizations, those account rows also carry `organization`,
`organization_source` (`rule`, `conflict`, `none`) and `organization_why`, and `registry/heroku-apps.json` carries
`team_organizations` (`team` and the same three fields); without the file nothing new is written. `match` accepts
`digitalocean_teams` (a team's uuid, or its name while no other team here bears it), `cloudflare_accounts` and
`heroku_teams`; an older engine ignores the new keys, so a project they assigned falls back to the older answer there.
A rule that is not a list of strings is ignored and named on the emit's error output. A project assigned through a
provider account reads `organization_source` `rule` with the account and the resource in `organization_why`. With
organizations configured, `registry/projects.json` is written once per emit, after the provider documents, from the
rows as they stood after the projects; an unchanged document keeps its `updated_on`. `full doctor` gains `digitalocean`
(`integration`, `teams_stashed`, or `teams_stashed` null with `reason`) and the `attention` code `digitalocean-off`.
The raw DigitalOcean scan's `degraded` may hold `digitalocean:doctl:<context>` rows. Organization certificates are
rotated earlier (at two thirds of their validity or an hour before the three-day `expiring` notice, whichever comes
first, never in the first half, so before the notice for certificates of about 6 days or more; OBS-423): nothing in
the enrollment's files changes. Downgrade: an older engine reads
every added field as an unknown key and leaves `inventory/agents.md` in the wiki as it was last written.

## Organizations: the remaining account rules, one reading, two read-only views (OBS-412 part 2, OBS-411, OBS-415, 0.35.9)

Additive; nothing migrates and nothing is written. `config/organizations.json` `match` accepts `github_owners` (read
as `repository_owners`) and `openrouter_accounts` (an OpenRouter account by its stash label; it decides that
account's organization and a key's issuer side, never a project's). An older engine ignores both keys: a project a
`github_owners` rule assigned falls back to the older answer there. An unknown `match` key is still ignored; its
message now names the known rules. The findings board gains `organization.unresolved` (a project in `conflict`) and
`organization.config_problem` (a rule the engine does not read, or a file that does not parse), both silent without
the file. `observatory_project`'s `organization` object gains `state` (`decided`, `external`, `unresolved`) and
carries `ga4Account`, `figmaTeam` and `figmaProject` only when it is `decided`; the session-start line and the
analytics findings read the same answer, so a source this engine does not take for a decision names no account.
`full org accounts [--json]` (schema `org-accounts/1`) and `full org keys --org NAME [--json]` (schema `org-keys/1`)
are new, read-only, and need no enrollment; the MCP server serves them as `observatory_org_accounts` and
`observatory_org_keys` (28 `observatory_*` tools). Both schemas are this engine's own and may gain fields; a field
is never renamed without a new schema name. Downgrade: an older engine has neither view nor the two findings, and
reads the configuration as described above.

## Credentials on request: the queue and the provenance beside a slot (OBS-191 parts 2 and 3, 0.35.9)

Additive; nothing migrates. `tools/credential_requests.py` is new: it writes `credential-requests.jsonl` (schema
`credential-queue/1`, one record per line: `requested` with a `credential-request/1` document and the slot,
`issuing` before a door starts, `decided` with a `credential-decision/1` document (an issue's carries the delivery
with the provider's identity of the credential, a key hash or a token id), `withdrawn`, `attempt-failed`,
`revoked`, `settled`) and its lock
`.credential-requests.lock` in the vault's folder (`secrets/projects/`, or `OBSERVATORY_VAULT_DIR`), owner-only, names
only, so whatever backs that folder up carries them. A slot's `meta.json` may gain `provenance` (the
request id, `issuedAt`, `decidedBy`, `issuer`, `recipient` with agent, context and slot, `provider`, `preset`,
`scope`, `limit`, `expiresAt`, `revoke`); `vault.py put` and `rotate` drop it, and the movement journal gains the
events `provenance` and, for a value a revocation removed, `remove` with `tool` `credential_requests.py`.
`observatory_credentials` vault rows gain `issuedOnRequest` for a slot that carries a provenance. The findings board
gains `credential.request_waiting`, `credential.revocation_refused` and `credential.request_queue_unreadable`, all
silent without the queue file. Nothing
is sent: the node's check-in still sends no `credentialRequests` and reads no decision, envelope or revocation (the
`protocols` it lists are unchanged). Downgrade: an older engine ignores the queue file and the `provenance` member, and `vault.py
put` and `rotate` there leave a stale `provenance` beside a new value, which the newer engine drops at the slot's
next put or rotation.

## Organizations: the local selection record (OBS-417, 0.35.9)

Additive. Nothing migrates, and nothing is written until a person (or an agent with the operator's words) changes
or applies a selection: `full org select --org NAME plan|show|apply|undo|confirm-replacement` keeps
`org-selection/1` in the workspace's `store/org-selection/` (one owner-only folder per organization, named by the
digest of its name: `draft.json`, `revisions/NNNNNN.json`, `journals/NNNNNN.json`). A revision verifies against its
own digest and names its predecessor's; one changed by hand or not owner-only is refused (`record-unreadable`), never
read past. Answers are schema `org-select/1`; the MCP server serves the same as `observatory_org_selection` (29
`observatory_*` tools), whose change, apply, undo and confirmation need the operator's words (contract DEC-0121).
The findings board gains `organization.selection_new` (silent without an applied selection), and `full org keys`
lists the keys of a project chosen by hand in the selection. Nothing is sent: projects wait for Fabric's adaptation
plan and keys for the request queue and the rotation (OBS-191, OBS-416). Both schemas are this engine's own and may
gain fields; a field is never renamed without a new schema name. Downgrade: an older engine has no `org select`, no
tool and no finding, and leaves `store/org-selection/` untouched.

## Vault: a slot copied into another store; machine view: the skills section (OBS-476, OBS-477, 0.35.10)

Additive; nothing migrates. `tools/vault.py copy PROJECT ENV NAME --to-store PATH [--to-slot SLOT] [--replace]
[--json]` is a new subcommand (OBS-476): `PATH` is a store, the folder that holds a `projects/` vault, named by a
plain absolute path (`.` and `..` refused as `target-not-plain`), created 0700 when missing; an existing one of this
account, and its vault folder, are set to 0700 before anything goes in. Its receipt is `vault-copy/1` (`outcome` `copied`, `replaced` or `unchanged`; `from` and `to` with
`store` in display form (the resolved path, `~/...` inside the home) and `slot`; `from.kept`, `to.storeCreated`,
`to.archived`; `carried`; `digest` with `algorithm`
`HMAC-SHA-256`, `key` `estate` or `one-time`, `keyId`, `from`, `to`, `match`); a refusal is
`{"schema": "vault-copy/1", "outcome": "refused", "code", "reason"}` with `--json`, exit 2, the target left as it
was (a lock file the copy made in an existing vault is kept); a failed read or write is `outcome: failed`, code
`filesystem-failed`, exit 1; a digest mismatch prints the receipt with `digest.match: false` and exits 1. The codes:
`invalid-slot` (the source's names), `source-empty`, `source-unreadable`, `source-has-open-leak`,
`target-not-absolute`, `target-not-plain`, `target-link`, `target-parent-missing`, `target-not-folder`,
`target-is-vault-folder`, `target-in-repository` (also for a working tree below `projects/`), `target-too-broad`,
`target-not-a-vault`, `same-store`, `nested-stores`, `target-other-account` (the folder's uid against the
effective uid on POSIX; on Windows its owner SID must be this account, SYSTEM or Administrators, so an
administrator's copy does not rewrite another person's store to owner-only, a store an elevated process created
counting as this account's), `target-not-writable`, `target-unsafe`, `target-slot-invalid`, `target-slot-filled`,
`filesystem-refused`; with `--json` a refusal of the source's names is JSON too, and the `project:` sentence goes to
stderr; a command line argparse refuses (a missing `--to-store`) stays argparse's usage text and exit 2.
`target-in-repository` is asked of git and skipped where git cannot run; a git that does not answer within 60 s
is a refusal under that code. An undo after a refusal under the locks
happens while both are held; on Windows a held lock file cannot be removed, so a new store a refused copy made
stays there, empty but for its lock file. A refused copy may leave an existing store and vault folder of this account at 0700 and a lock file in an
existing vault. Unless the outcome is `unchanged`, both
stores' `movements.jsonl` gain a `copied` row (`to_store`, `to_slot` in the source's; `from_store`, `from_slot`,
`archived` after a replace in the target's; `outcome` and `digest_key` in both, and `key_id` and `digest` only
under the estate key), which an older
`vault.py movements` prints as any other row. A copied slot's metadata gains `copied_from` (`store`, `slot`, `at`)
and carries the source's `expires_at`, `expiry_reason`, `expiry_recorded`, `expiry_by` and `provenance`. Module
interface: `vault.copy_slot`, `vault.CopyRefused` (a `VaultBoundaryError` with `code`); `vault._mutation_lock`,
`vault._private_dirs`, `vault._atomic_write`, `vault._append_private` take an optional store and `vault.journal` an
optional `_store`, each defaulting to this machine's vault, so every existing call behaves as before. One change
for every writer: `vault._mutation_lock` now checks, once it holds the lock, that the lock file is still the file
at the lock's path (device and file id, both read from open descriptors) and locks the new one when it is not, so a
lock file removed while a writer waited never lets two writers in; a lock file that changes 20 times in a row is a
refusal, never a wait without end. A store another OS account owns is refused (`target-other-account`); OBS-489
holds that case. An estate key in the source store that cannot be read is `source-unreadable`. The source store
is named as its vault folder's parent when the vault folder is `projects/`, else as the vault folder.

The harness scan (`store/raw/harness.json`) gains `inventory` (OBS-477): `spaces` (one, `this-account`, home `~`)
with `skills` (`name`, `agent` `claude-code` or `codex`, `scope` `user`, `project` or `plugin`, `version`, and
`project` or `plugin`; an enabled Codex plugin's skills included) and `plugins` (`name`, `agent`, `scope` `user`,
`project` or `local`, `project`, `projectKey` (8 hex digits of the project path's SHA-256, never the path; a pseudonym, which a guessed path
confirms, OBS-491), `version`,
`enabled` true, false or null, `marketplace`, `latest`, `latestFrom` `marketplace-catalogue` or `plugin-manifest`,
`marketplaceUpdated`, `behind`), `findings` (`plugin-behind`), `open` and its own `degraded` (`source`
`claude:installed_plugins.json`, `claude:settings.json`, `claude:known_marketplaces.json`,
`claude:marketplace:<name>`, `claude:marketplace:<name>:<plugin>`, `claude:project-settings:<project>`,
`claude:local-settings:<project>`, `skill:<agent>:<scope>:<folder>`, `skills:<agent>:<scope>[:<project or plugin>]`,
`projects-folder`, `codex:config.toml`, `codex:plugins-cache`, `codex:plugins-cache:<marketplace>[:<plugin>]`, code
`unreadable`; `claude:installed_plugins.json`, `claude:installed_plugins.json:<plugin>`, `claude:settings.json`, a
settings label above,
`claude:known_marketplaces.json:<name>` and `claude:marketplace:<name>`, code `unrecognised-shape`;
`codex:plugins-cache:<marketplace>`, code `unrecognised-shape` too; `codex:plugins-cache:<marketplace>:<plugin>`, code
`ambiguous` (several unnamed folders, so the loaded copy is not
known); `inventory`,
code `failed`, when the inventory itself failed and the rest of the scan was kept). A plugin's `scope` is the word
Claude Code recorded (`user`, `project`, `local`, or another such as `managed`, whose `enabled` is null; `other` for a recorded scope of another shape). `latest`
is read from the plugin's manifest inside the marketplace's folder first, then the catalogue entry; a plugin from
another source, whose folder is missing from the marketplace's local copy, or whose
catalogue sets `metadata.pluginRoot`, has none. A home folder that is not a folder adds `harness_root` `not-found`;
Codex's `config.toml` whose `plugins` is not a table adds `codex:config.toml` `unrecognised-shape`. Every answer of
the section carries `page`. The section adds `stale` (source
`harness.json`) to its `degraded` for a scan older than 24 hours. No path is recorded. An older reader ignores the key; the machine view
reads a scan without it as `not-surveyed` until the next scan. `observatory_machine` takes `section: "skills"`
(answer `{measuredAt, degraded, skills}`, `skills` holding `spaces`, `page` (`limit`, `cursor`, `nextCursor`, `order`,
`total`), `counts`, `findings`, `open`; `full machine --skills` prints the same section with its own `degraded` and
every row) with two new optional arguments for that section, `limit`
(1 to 100, default 100 rows of one window over the findings, then each space's plugins, then its skills; `total`
counts those rows; a page also ends once its rows pass 40,000 characters of JSON as `json.dumps` writes them, at least one row a page;
`counts` stay whole; a cursor is `offset@stamp` (the scan's time as digits), and one given for an
earlier scan answers the first page of this one with `invalid-cursor`; a bare offset is accepted; a cursor past the
end answers an empty last page; a skill's `name` is its
front matter's only when it has a name's shape, else its folder's, at most 64 characters, and a plugin recorded
under a name of another shape is left out with `unrecognised-shape`; a version is kept only when release-shaped, at most 40 characters and 9 digits a number,
`marketplaceUpdated` only when ISO-shaped, a project or folder name at most 64 characters with anything that is not
UTF-8 replaced, a Codex cache folder of another name shape left out (`codex:plugins-cache`, `unrecognised-shape`),
and a SKILL.md is read up to its first 64 KiB, so no field a third party wrote makes a row long; the MCP answer's rows leave out their null fields and its `degraded` is at the top
only, not repeated under `skills`) and `cursor` (an invalid one answers the first page and `degraded` code
`invalid-cursor`), and
its overview's `sections` lists `skills`; the tool's other answers are unchanged. `full machine --skills` prints the
same section and cannot be combined with another mode. The board gains the finding type `harness.plugin_behind`
(severity info; subject `harness:plugin-behind:<agent>:<plugin>:<scope>:<project>[.<projectKey>]:<installed>:<latest>`,
`<project>` empty for a user install).

## Space daemons: replacing a loaded daemon (OBS-475, OBS-395 part 2, space daemons 1.2.0, 0.35.10)

No migration: the registry (`observatory-space-daemons/1`), the daemon plists and the log folders keep their format,
and a 1.1.0 copy reads what 1.2.0 writes. The root-owned copy changes (`SPACE_DAEMONS_VERSION` 1.2.0), so an
administrator installs it again from the release with the same verified steps; `--version` prints the new sha256 for
information. Until then the 1.1.0 copy keeps working as before, without `replace`.

- `remove` answers only once `launchctl print system/<label>` answers that it does not know the label (exit 113;
  a print that times out or does not start is not read as gone): up to 45 s
  (`BOOTOUT_WAIT_SECONDS`), or the daemon's own `ExitTimeOut` and 5 s when that is longer, at most 300 s
  (`BOOTOUT_WAIT_MAX_SECONDS`; an `ExitTimeOut` of 0, launchd's "never kill", gets 300 s), plus the time launchctl
  itself takes to answer (each call has its own 60 s timeout). A stop that does not finish
  within the bound answers `outcome` `failed` with the problem `still_loaded` (or `bootout_failed` when launchctl
  refused the bootout), exit 1, and keeps the daemon file and the registry entry, so `remove` can run again; 1.1.0
  deleted both and answered `removed` while launchd still held the label. The answer gains `steps` and
  `waited_seconds`. When the first `launchctl print` does not answer (a timeout, launchctl not starting), `remove`
  answers `failed` with `launchd_unknown` and changes nothing (1.1.0 read it as "not loaded" and deleted the file and
  the entry); when launchd names another plist than `/Library/LaunchDaemons/<label>.plist` as the label's path, or none,
  `remove` refuses (`not-ours`, exit 2) and boots nothing out.
- `replace --user ACCOUNT --label LABEL` is new (root, isolated mode, the root-owned copy, as `apply`): the plan (a
  definition the rules refuse is `skipped` and stops nothing; a label with no definition is `not-found`), then
  `write`, `bootout`, `wait` (the same bound), `bootstrap` (again while launchd answers 5, 37 or 125, up to five
  attempts, as the engine's own jobs since OBS-410; 125 counts as loaded when launchd names this tool's plist as the label's path). When the new daemon
  does not load, the previous file and registry entry go back and the previous daemon is loaded again, and
  `put_back` says which of these held (a label with no previous daemon file is left with no file, its registry entry as it was and, if the
  refused bootstrap loaded it after all, booted out); when the old daemon does not stop in time, nothing new is
  loaded and the previous file goes back. Interrupted while it waits (Ctrl-C), it puts the previous file and registry entry back before it exits. It
  reloads even when nothing changed, and applies a label never applied. Its answer is
  `apply`'s, with `command` `replace`.
- `apply` over a daemon of its own that is loaded replaces it the same way when the definition changed (1.1.0 sent
  the bootstrap while the bootout was still finishing, and then put the old file back), and leaves it running with
  the new outcome `unchanged` when the daemon it would write is byte for byte the one written; `ok` counts
  `unchanged` as done. A label loaded that this tool did not write is still refused (`label_taken`), and so is one launchd reports loaded
  from another plist or from none; a `launchctl print` that does not answer is `launchd_unknown`, refused before anything is
  written. A loaded label is booted out, or a timed-out bootstrap counted as loaded, only when launchd names this
  tool's plist as its path.
- Every result row of `apply` and `replace` gains `steps`, a list of `{step, outcome, detail}` (`plan`, `write`,
  `bootout`, `wait`, `bootstrap`, `put-back`; outcomes `done`, `skipped`, `refused`, `failed`), and their answer
  gains `command`. A launchctl call that times out answers exit 125 (it was 127, which still means launchctl did not
  start). The exit codes of the tool are unchanged.

Not covered yet: a daemon that listens on a port another process still holds (OBS-395 part 1). The engine's
`waiting-for-root` steps for a changed background job still name `remove` then `apply`, which work with every copy
and no longer race with 1.2.0.

## Migration checksums: the same under every Python (OBS-387, 0.35.4)

No migration and no new file. Until 0.35.3 the checksum recorded for each applied migration (`migration_checksums`)
was the sha256 of `ast.dump` of the migration's code, and `ast.dump` prints differently under Python 3.11, 3.12,
3.13 and 3.14 (the same), and 3.15. A workspace recorded under one interpreter was refused under another: `full doctor`
answered "Workspace database is corrupt or incompatible" and `full upgrade` "Applied migration code does not match its
recorded checksum", although nothing was damaged (seen when Homebrew moved a venv from 3.14 to 3.15).

- A new record is `v2:` and the sha256 of the migration's source text with comments, docstrings, trailing spaces and
  blank lines left out, except inside string literals, where every character counts. It is the same under 3.11 to 3.15.
- A record an earlier release wrote is still read: `store/migrate.py` lists, per migration, the AST digests it has under
  each of those interpreters, pinned to the `v2:` digest of the code they were taken from. A recorded digest is never
  rewritten, so an earlier release, on the interpreter that wrote them, still opens such a database.
- A database first created by 0.35.4 or later records `v2:` digests, which 0.35.3 and earlier refuse. Going back to such
  a release means restoring the snapshot taken before the update, as for any downgrade.
- Before 0.35.4, keep each workspace's venv on the Python minor version its database was created under.

## macOS 27: a restricted connection table (peer_owner 1.3.0, 0.35.2)

On macOS 27.2 a process sees only its own sockets in `sysctl net.inet.tcp.pcblist_n` (sometimes none), and
`netstat -anv` no longer prints `process:pid`. `peer_owner` 1.2.0 then read every other process's connection as
`unknown` (`no-row`) and refused it: a client refused its own account's server, and a server refused its own account's
clients. Since 0.35.2 a table with no row of another account (or, read through `netstat`, of another process) is
treated as restricted, and the owner is read from this account's own processes through libproc. A connection found in
one of them answers `this-account` (method `libproc`, with its pid, or pid None and reason `several-processes` when
several processes hold it); anything else answers `unknown` with reason `table-restricted`, which every caller
refuses as before. `listener(port)` on a restricted table answers `this-account` only for this account's exact
`127.0.0.1` bind, else `table-restricted`, never `none`. A process whose real and effective uids differ is refused this
path (`setuid-context`). On a full table (macOS 26 and earlier, Linux, Windows) every answer is unchanged.

Since 0.35.3 (`peer_owner` 1.3.1, contract DEC-0155 RT-1) the reason has two forms: `table-restricted` alone means
this account's sockets were read and none of them is the connection, so a caller may probe whether anything listens;
`table-restricted: <detail>` means the proof could not be read at all (a layout that failed calibration, libproc that
did not load, `setuid-context`), which 0.35.2 reported as `libproc: ...` or `setuid-context`. Both start with
`table-restricted` and are refused as before. A table found restricted is remembered for 30 seconds
(`RESTRICTED_TTL`), so later questions skip the table read; a full table clears it at once. `peer_owner.last_method()`
names the method of the last read (`libproc` included), for a doctor to show what answered.

## Linux: a server end not accepted yet reads its listener's owner (peer_owner 1.2.0, 0.35.0)

A fix on Linux, and one rule change on every POSIX host. `/proc/net/tcp` and `tcp6` print a connection the server has
not `accept()`ed yet with uid 0 and inode 0, because its socket has no owner until then. A client that checks the
server end right after connecting (OBS-355) read that row as root's and refused its own account's server
(`server-not-own`), on a busy server for most connections; a client running as root could take another account's slow
listener for its own uid.

Now, on Linux, the client reads the server end with a hybrid rule:

- A connection row with an inode answers with its own uid, as before. From `accept()` on it carries the accepting
  process's uid, and it is what refuses another account that listened while our server restarted, accepted the
  client, closed its listener and let our server bind again before the client read the table: the row is still the
  other account's, whatever listens on the port now.
- Only a row with inode 0 takes the owner of the listener rows (state `0A`) on the server's address and port: the
  exact bind first, else the wildcard (`0.0.0.0`, `::`, IPv4-mapped included). Rows of one uid (a `SO_REUSEPORT`
  group, or one process's IPv4 and IPv6 sockets) count as that uid; rows of different uids, a listener row without its
  inode, or no listener read as `unknown` (`listeners-disagree`, `listener-no-socket`, `no-listener`), which the
  client refuses as `server-unverified`. The fallback is sound because a queued connection dies with its listener, two
  uids cannot listen on one address and port at once, and the table prints the listening sockets first, so a swapped
  listener shows as `no-row`, never as ours.

macOS and Windows still read the accepting process's own socket. On the server side a client end with inode 0 is a
socket its process already closed: it reads as `unknown` (`no-socket`) and is refused `403 peer-unverified` instead
of `403 peer-foreign-account`; an open client end always has its inode, so nothing else changes there.

The rule change: an owner's uid is now compared with this process's uid before uid 0 reads as the system's, so a
process running as root owns root's sockets. A server or client running as root now admits its own root peer or
listener as `this-account` where it refused it as `system`; pid 1 (launchd's socket-activated jobs) stays `system`. An
ordinary account still refuses root's sockets. `peer_owner.Row` gains `inode` (Linux only, None elsewhere) as its last
field, so positional construction is unchanged. A project that vendors `peer_owner.py` takes this file's 1.2.0 to get
the fix.

Known limits of the Linux listener fallback, each failing closed or left as it was:

- `SO_BINDTODEVICE` is not visible in the table's rows, so two listeners on one address and port bound to different
  devices look alike: rows of one uid count as that uid, rows of different uids are `unknown`.
- The wildcard is family-blind: `0.0.0.0` and `::` both read as the wildcard, so another account's IPv6-only `::`
  listener beside our `0.0.0.0` one on the same port makes a not-yet-accepted connection `unknown`, and the client
  refuses it.
- A systemd system socket unit creates its listener as root and hands it to the `User=` service: before `accept()` the
  connection reads as root's (`system`), so even a client of the service's own account refuses it until the service
  accepts; once accepted, the row carries the service's uid.

## Machine move: a port forward for a service moved to another machine (OBS-480, 0.35.11)

`full machine-move leave LABEL --to MACHINE_ID` takes `--via USER@HOST` (macOS): when a Fabric service descriptor
names LABEL and its origin is `http://127.0.0.1:PORT` (1024 to 65535), leave also installs the launchd job
`org.project-observatory.forward.<id>.<instance>` that forwards that port to the account USER on HOST, where the
service now runs, and records its label in the `elsewhere` entry as `forward` with `forwardAdopted: false`. A job in
`~/Library/LaunchAgents` that already runs `ssh` with `-L [127.0.0.1:|localhost:]PORT:...` to the same USER and HOST
is adopted instead (`forwardAdopted: true`) and never installed a second time. The answer gains `forward`:
`state` `installed`, `unchanged`, `adopted`, `refused` (`reason` `forward-elsewhere` or `port-held`, with
`message`), `failed` (`load-failed`, `unload-failed` or `error`), or without `--via` `kept`, `not-requested` (with
`port` and `hint`) or `not-applicable`. A refused or failed forward leaves the move itself done and exits 1, and
keeps a forward already recorded whose plist is still here (it still holds the port). `--via` that is malformed,
names a label no descriptor with a local origin names, or is given off macOS, is refused before anything moves
(exit 2). Without `--via`, leave moves the job as before and keeps a forward already recorded.

`full machine-move return LABEL [--remove-adopted]` is new: the first step here of the move back. It unloads and
removes the forward this engine installed and clears `forward` and `forwardAdopted` from the entry; the entry
itself stays until `cutover`. An adopted forward is left loaded and keeps its record (it still holds the port, so
`cutover` keeps refusing), unless `--remove-adopted`, which unloads it and moves its plist into `machine-move/forwards-set-aside/` after checking
it is still an ssh forward of a local port (otherwise nothing is unloaded or moved, exit 1). An adopted forward that
is not loaded and has no ssh forward left under its label holds no port: `return`, with or without the flag, answers
`not-running` and clears its record. One that is not loaded but whose plist is still a forward would load again at
the next login: it stays recorded (`adopted-left`) until `--remove-adopted` sets that plist aside. A label not placed
elsewhere is refused (exit 2); off macOS it is refused. `cutover LABEL` now refuses (exit 2, nothing moved) while
the label's entry records a `forward`, naming `machine-move return LABEL`. A placement file without the new fields
reads as before, the schema stays `observatory-placement/1`, and every command line without `--via` or `return`
answers as before, with one new key `forward` in leave's answer.

## Machine move: a space with nobody logged in (OBS-361 part 1, 0.35.0)

On macOS, `full machine-move cutover LABEL` in an account whose `launchctl print gui/<uid>` answers
"could not find domain" (exit 112: nobody is logged in graphically; since 0.35.1 also exit 125, "Domain does not
support specified action", which such an account answers once anything runs as it) no longer tries to load an agent there,
fails and puts the plist back. It runs the same checks, moves the plist into
`~/.local/share/project-observatory/background/LaunchAgents/` (owner-only; group and other write bits cleared),
removes the job from `elsewhere`, and answers with new fields: `domain` (`system`), `state` (`waiting-for-root`,
or `complete` once root loaded it), `load` `waiting-for-root` or `loaded`, `human_steps` and `next` naming
`sudo /usr/bin/python3 -I /usr/local/libexec/observatory-space-daemons apply --user ACCOUNT --label LABEL`. Any other
answer of `launchctl print gui/<uid>` (0, or a code the engine does not know) keeps the earlier path and answer,
byte for byte. Such an account with a custom `XDG_DATA_HOME` is refused before anything moves (part 1). `machine-move
show` gains `background[]` of `label`, `file`, `daemon` (`loaded`, `registered`, `waiting-for-root`); `leave` refuses
while root's daemon of the job runs and names `... remove --user ACCOUNT --label LABEL`; `machine-move status` counts
the background folder's jobs as defined here (M4) and adds one evidence line for each.

`tools/space_daemons.py` (`SPACE_DAEMONS_VERSION` 1.0.0) is new: standard library only, Python 3.9 (macOS's
`/usr/bin/python3`), run by an administrator from a root-owned copy and only in Python's isolated mode,
`sudo /usr/bin/python3 -I /usr/local/libexec/observatory-space-daemons ...`: without `-I`, `apply` and `remove` refuse
(`not-isolated`), so no module from the script's folder, the user's site-packages or a `PYTHON*` variable runs as
root. A daemon's logs never go where the plist said (`plan` notes `logs-moved`): launchd may open them with root's
rights after the check, so they go to `/Library/Logs/ProjectObservatory/<user>/<label>.out.log` and `.err.log`.
It writes files outside any workspace:

| Path | What | Older engines, removal |
|---|---|---|
| `~/.local/share/project-observatory/background/LaunchAgents/` | the account's launchd jobs that run as system daemons; written by `cutover`, read by root's `space_daemons.py` | an older engine neither reads nor writes it; deleting a plist deletes only that copy (root's daemon keeps running until `remove`) |
| `/Library/LaunchDaemons/<label>.plist` | the daemon `apply` wrote: the plist's allowed keys plus `UserName`, `GroupName`, `InitGroups`, HOME, USER, LOGNAME and the two log paths below; root:wheel 0644, written atomically | `sudo ... remove --user ACCOUNT --label LABEL` unloads it, waits until launchd lets the label go (since 1.2.0) and deletes it; `replace` puts a new definition in its place; the engine never writes it |
| `/Library/Logs/ProjectObservatory/<user>/` | the daemons' `<label>.out.log` and `<label>.err.log`; the folder root:wheel 0700 under a root:wheel 0755 `ProjectObservatory`, with one inherited ACL entry (`<user> allow list,search,read,readattr,readextattr,readsecurity,file_inherit,directory_inherit`) so the account reads its logs; `apply` refuses, installing nothing, when either folder exists as a link or is not root's. Each log file is created ahead of launchd (`O_CREAT|O_EXCL|O_NOFOLLOW`), owned by the account, mode 0600: the account reads and appends to it but cannot create, rename, link or unlink in the folder, so the file is safe whether launchd opens it as root or as the account and cannot be replaced. An existing one is kept only as a regular file with one link, owned by the account (root's is given to the account), not writable by group or others; otherwise that job is refused (`log-file`) | `remove` leaves the log files and says so; deleting the folder loses them, and the next `apply` makes it again |
| `/Library/Application Support/ProjectObservatory/space-daemons.json` | the registry of what `apply` wrote, a stable interface (below); root:wheel 0644 in a 0755 folder; the engine only reads it | a file that is not this schema is refused by name, never replaced; deleting it makes the tool refuse to replace or remove the daemons it wrote (they keep running) |

**The registry is a stable interface** that other tools (Fabric Dashboards among them) read without root. Its
format, `observatory-space-daemons/1`, is exactly:

```json
{"schema": "observatory-space-daemons/1",
 "entries": [{"label": "org.example.agent", "user": "space", "uid": 501,
              "source": "/var/empty/space/.local/share/project-observatory/background/LaunchAgents/org.example.agent.plist",
              "sourceSha256": "<hex>", "written": "/Library/LaunchDaemons/org.example.agent.plist",
              "writtenSha256": "<hex>", "at": "2026-11-07T12:00:00Z"}]}
```

`label` is the launchd label; `user` and `uid` the account the daemon runs as; `source` the absolute path of the
definition `apply` read and `sourceSha256` its bytes' SHA-256; `written` the absolute path of the daemon plist and
`writtenSha256` the SHA-256 of what was written; `at` when, RFC 3339 in UTC. One entry per label, sorted by label;
the file is replaced whole (written to a temporary file, synced, renamed). A reader treats a missing file as no
entries, and a different `schema` as a format it does not know. A field is added only under a new schema name.

**Installing the copy.** An administrator downloads the release in their OWN account and verifies it there, never
copying the file from the space account's folders: `gh release download vX.Y.Z -R
passioncode-ai/project-observatory-dashboard -p 'SHA256SUMS*' -p '*.whl'` (or `curl -LO
https://github.com/passioncode-ai/project-observatory-dashboard/releases/download/vX.Y.Z/<asset>` for `SHA256SUMS`,
`SHA256SUMS.asc` and the wheel), `shasum -a 256 -c SHA256SUMS --ignore-missing`, the organization's release key
imported from `https://raw.githubusercontent.com/passioncode-ai/.github/main/release-signing/passioncode-release-signing.asc`
(gpg is not on macOS by default), then `gpg --status-fd 1 --verify SHA256SUMS.asc SHA256SUMS | grep -q "VALIDSIG
63B30DC324BD697487AA31944FAFB8AEC803B6A7"`: the pinned fingerprint of `release_signature.PINNED`, which `full update`
checks, because a bare `gpg --verify` accepts any key in the keyring. Without gpg, the administrator's own
Observatory install checks the same key with `release_signature.verify` (the one-liner is in ONBOARDING). Then `unzip
-p project_observatory-X.Y.Z-py3-none-any.whl tools/space_daemons.py > space_daemons.py`, `sudo
install -d -o root -g wheel -m 0755 /usr/local/libexec`, `sudo install -o root -g wheel -m 0755 space_daemons.py
/usr/local/libexec/observatory-space-daemons`. `--version` is information only. `machine-move cutover` names the
same steps in `human_steps`; `remove` checks the file is its own before it stops the daemon, and an ACL line
`ls -lde` prints that the tool cannot parse is a refusal, never "no entries".

**Accepted residual.** The program and `WorkingDirectory` are checked when `apply` runs, and launchd uses them
later. The program runs as the account, so a program swapped after the check gives the account only what it already
has, and the account changing its own program is expected. Only `ProgramArguments[0]` is checked (owned by root or
the account, in folders only root or the account can write): an interpreter that runs a script elsewhere inherits
that script's folder permissions, which this tool does not read. The log files sit in root's own folder, created ahead
by `apply` and owned by the account, so it does not matter whether launchd opens them as root or as the account;
their layout is decided in one function (`space_daemons.log_paths`).

**Rules added in review.** A label is letters and digits, then `. _ @ -` (at most 128), the same rule the engine's
headless `cutover` applies before it moves anything; a label any plist in `/Library/LaunchDaemons` or
`/System/Library/LaunchDaemons` carries is refused (`label_taken`) unless it is this tool's own file for the same
account. `KeepAlive.PathState` paths stay inside the home, `KeepAlive.OtherJobEnabled` and `DYLD_*` variables are
refused, every integer fits 0..2147483647, and a definition whose daemon would not write as a plist is refused in
`plan` (`not_writable_as_plist`). `apply` writes the registry entry before the plist (an entry whose plist is
missing is written again by `apply`, cleaned by `remove`, shown `missing` by `status`), reports one definition's
error as `failed` and goes on, and clears both log folders' ACLs before adding its one entry. `remove` deletes only
a file whose sha256 is `writtenSha256` or whose label is the entry's. `leave` refuses while the registry names the
job for this account, loaded or not.

One copy only: `apply` refuses a label loaded as an agent in that account's own GUI domain (`launchctl print
gui/<uid>/<label>` exit 0, problem `loaded-in-gui`), and a label another account's entry holds
(`owned_by_other_account`), and writes nothing for it.

**Space daemons 1.1.0 (OBS-361 follow-up).** Measured against 73 real definitions, 44 were refused; this widens
what is accepted without widening what root trusts by default:

- `Umask` (0 to 0777) and `SoftResourceLimits` (`Core`, `CPU`, `Data`, `FileSize`, `MemoryLock`, `NumberOfFiles`,
  `NumberOfProcesses`, `ResidentSetSize`, `Stack`, each a non-negative integer) are allowed and kept;
  `HardResourceLimits` stays refused; `AssociatedBundleIdentifiers` is allowed and dropped (`plan` notes `stripped`);
  a `WorkingDirectory` of `/` becomes the home (`working-directory-home`), any other path outside the home is still
  refused; every `DYLD_*` variable stays refused.
- An XML comment holding `--` is parsed by removing the comments from the bytes in one linear pass and parsing once
  more (never with plutil as root); nothing inside a comment becomes a key, `sourceSha256` stays the hash of the
  original bytes, and a comment that does not close, or comments beside a CDATA section, are `xml_comment`.
- The optional trust file, a new stable format, readable without root:

| Path | What | Older versions, removal |
|---|---|---|
| `/Library/Application Support/ProjectObservatory/space-daemons-trust.json` | `{"schema": "observatory-space-daemons-trust/1", "owners": ["<account name>", ...], "groups": ["<group name>", ...]}`: accounts and groups whose files and folders a program path may belong to besides root (a group-writable folder passes when its group is root's or listed; world-writable never unless sticky). Only root-equivalents: groups `wheel` or `admin`, owners that are members of `admin` (`dsmemberutil checkmembership -U NAME -G admin`, its exact answer; unknown membership is a refusal). Written by `trust set --owner NAME --group NAME` (root, isolated, the root-owned copy; atomically, root:wheel 0644), which refuses anything else; every read drops, with a warning, any group outside wheel and admin and any owner not in admin now, so a demotion or a hand-edited file stops trusting at once. Trusting the admin owner and group means any process running as that admin, even without its sudo password, can replace those programs and so run code as every space that uses them: trust only when the spaces are less trusted than the admin; read with the registry's checks (no link, owner root, not writable by group or others, at most 64 KiB, in a root-only folder), and a file that fails them is ignored with a warning, never trusted. An owner that is the space account itself is left out for that account, with a warning; a name this computer does not know is left out too | 1.0.0 neither reads nor writes it; deleting it trusts nobody but root and the account again |

The engine's headless `cutover` runs the same parse, the same rules and the same trust file (read-only), so it accepts
exactly what `apply` will.

## Workspace snapshot: its home, home-relative paths and another machine's passphrase (OBS-347, OBS-348, 0.34.0)

Additive, with no new file, table or migration. A workspace snapshot's `manifest.json` gains `home_path`, the home
folder it was taken under (`str(Path.home())`); an older engine ignores it. `full configure sources NAME PATH` and
`full configure storage backups PATH` store a path that is the home or lies under it as `~` or `~/<rel>`; every
release reads that form, because the loader has accepted any value whose `expanduser()` is absolute since sources and
the backups root exist (`configuration.load`). A path outside the home is stored as given, and a value already stored
is never migrated. The `configure` answer gains `stored_as` (`home-relative` or `absolute`), and `doctor` gains `form`
for each configured source and, when `storage.backups` is set, under `backups`.

The manifest also gains `home_path_resolved` (the same home with its links resolved), because `config/tracked.json`
keeps resolved paths; both forms are matched, and a value is normalised lexically first, so one that leaves the home
through `..` is outside it. A restore whose snapshot names another home than this one rewrites, in the restored copy before it is checked, every
`sources.*` and `storage.backups` value and every `include` and `roots` entry of `config/machine-backup.json` that is
that home or under it to `~/<rel>`, and every `path` of `config/tracked.json` (`local_folders` and
`dismissed_local_folders`) to the same place under this home, absolute and resolved as `full track` writes it; a
declared folder that does not exist afterwards is named in `unresolved` with `full track local-folder PATH`. Paths are compared part by part, as
Windows parts when the recorded home is a Windows path. A value outside the recorded home is kept; a snapshot without
`home_path` (an older engine's, or one taken by a process with no home folder, such as a Windows service without
USERPROFILE) rewrites nothing, and neither does a restore run without a home. A file is written only when something in it changed, so a same-home
restore is byte for byte as before. The restore answer gains `paths` (`home_from`, `home_to`, `rewritten[]` of
`file`, `setting`, `to`, and `unresolved[]`: every source or backups root that does not exist afterwards, with
`value`, `exists`, `reason` `missing-after-restore` and its `fix`, or `{file, reason: unreadable}` for a declaration
that does not read) and `passphrase_source` for an encrypted file. `full restore --latest` answers `no-backup` with
`elsewhere[]` (`label`, `owner_home_relative`, `newest`, `snapshots`) and a `next` step when the same workspace left
backups under another home; nothing is chosen.

`full restore` and `full machine-restore` gain `--passphrase-stdin`. A restore tries `OBSERVATORY_BACKUP_PASSPHRASE`,
the stdin value, this workspace's own passphrase, the OS store's passphrases for the backups folder's label, then
one terminal prompt; the value that opens the file is kept nowhere. The refusals `passphrase-refused`,
`passphrase-stdin-empty`, `passphrase-stdin-too-long`, `passphrase-stdin-not-text` and `passphrase-stdin-terminal`
exit 2 before any write (`backup_vault.BackupError.code`). `full restore --latest` asks once per backups folder at a
terminal when no kept passphrase opens it; `full init` never asks, and names the backups and the steps as before.

## Workflow memory: a session Fabric starts for a board request (OBS-323, 0.34.0)

Additive, with no new file, table or migration. A checkpoint body's `artifacts` may carry the packet refs of
Fabric's on-demand sessions (`board-request:`, `board-thread:`, `capability:`, `fabric-project:`, `deadline:`,
`run-ends-at:`, each `{kind: "other", ref}`); a body that carries one is refused when a ref has another shape or
member, a prefix is named twice or any artifact holds an absolute path. A body without those refs is accepted as
before. The redactor keeps `board-request:`, `board-thread:` and `fabric-project:` refs whole when the UUID ends the
token: an older engine scrubs their UUIDs. The packet is frozen at its first write: a later checkpoint that changes a
ref or the credentials is refused with the new workflow error `PacketFrozen`, and one that leaves them out keeps
them; a packet is accepted only in the checkpoint that starts the workflow, so a later one that brings packet refs
into a workflow that started without is refused with `PacketNotFirst` (a subclass of `PacketFrozen`), and a row whose
packet first appears in a later revision is no packet (no `dispatch`, keys refused with `packet-not-first`; a dispatched
workflow opened under the previous build whose packet was written after its first checkpoint gets no keys after the
upgrade, so let it finish first or start it again); a packet whose deadline is more than seven days after that write is refused with `DeadlineTooFar`. A workflow
that never carried a packet is unchanged. `observatory_checkpoint_latest`, `observatory_workflow_list` and
their `memory/0.1` twins add the member `dispatch` (their output schemas are open). `use_secret.py run` takes
`--workflow wf_…` and reads `FABRIC_DISPATCH_WORKFLOW`, `OBSERVATORY_WORKFLOW_LEASE` and
`OBSERVATORY_WORKFLOW_LEASE_FILE` (a private file holding the lease): with a workflow named, a value is released only
under its packet ([agent memory](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/AGENT-MEMORY.md#a-session-fabric-starts-for-a-board-request-obs-323));
without one, nothing changes. That gate guards a cooperating run; containment stays the vault's
`slotsRequireGrant: always` with a delegation token. The secret-use journal's rows gain `workflow`, `request` and
`dispatch` (a refusal code).

## Capability: products' daily operations digests (OBS-330, 0.34.0)

Additive. One new capability, `ops.digest.record` (effect `draft`, idempotency `supported`), served as the MCP tool
of that name (`ops_digest_record` where a client turns dots into underscores) with schemas published at `v0.34.0`;
0.34.0 declares it in the manifest as revision 10 (before that release a build of `main` served it from
`fabric/unreleased-capabilities.json`, because a manifest may not pin a tag newer than its
release). No existing tool name, argument or answer field changes, and `contractVersion` stays `0.1.0`. Its input is `digest`, one `ops.digest.daily@1` line of fabric-agent-contract
DEC-0153 as a JSON object (the contract's closed schema inline under `$defs/daily`), `source` (default `app`) and an
optional `service {id, instance}`. It has no project field: over stdio the project is the one project of the access
binding the server runs under (the new server option `--binding-token-file PATH`, or `OBSERVATORY_BINDING_TOKEN_FILE`;
without it the answer is `binding-required`), and on the memory-http door it is the `project` claim of a delegation
token minted for `project-observatory` with the scope `ops.digest.record` (without one, `delegation-required`): a
`project:<slug>` the registry holds or a `fabric:<uuid>` Fabric's attribution records, never a curated alias. The
review policy's `registry.aliases-clean` no longer accepts a proposal that names a `fabric:` alias: it waits for the
operator. The
contract's schemas, vectors and fixtures are vendored at its release tag `v0.1.0+2026-11-06.1` in
`fabric/ops-digest/`, which the engine's contract pin predates (`contract_lock` lists the folder).

access-bindings/1 gains the scope `ops.digest.record` (`config/access-bindings.json`, `defaults/access-binding.schema.json`):
an older engine refuses a registry that holds it (an unknown scope is a broken registry, served to the local stdio agent
alone), so issue such a binding only after the upgrade. The built-in `local:stdio` binding does not hold it. New
workspace state: `<state>/ops-digests/<project slug>/<YYYY-MM>.json`, owner-only, one record per (project, service,
source, UTC day) with every revision that arrived and the channel that sent it (`binding:<id>` or
`delegation:<agent>`), never a token, written compact; a reader takes the first revision of a day. A record keeps at
most 16 revisions, a project's day at most 8 source labels and a month file at most 64 MiB, each refused with its own
code. Each write drops days older than 400. An older engine ignores the folder; nothing migrates and no SQLite table is added. In a digest's counters, the
`ok` of a host's `retro-after-work` operation counts the retro requests a retro the host saw written answered (RW-4),
never the retros written.

## Servers: another OS account's connection refused (OBS-355 part 1, 0.34.0)

A behaviour change on hosts with more than one person's OS account only. There, the local server, memory over HTTP
and the keyserver answer a connection from another account's (or root's, or launchd's) process
`403 {"error":"peer-foreign-account"}`, and one whose owner cannot be read `403 {"error":"peer-unverified"}`, both with
`Connection: close` and before reading the request, on every path including the dashboard pages and
`/.well-known/fabric-service`. A client of another account that used to read this account's pages, `/health` or the
well-known document gets the 403 instead. A one-account host (`host_accounts.count()` is 1) answers exactly as before
and reads no socket table. The count joins the local accounts that can log in (hidden administrators and an enabled
Guest included) with the people's uids running processes; a host bound to a directory (a non-local search node on
macOS, `sss`, `ldap`, `winbind` or `nis` for `passwd` on Linux) or an unreadable count reads as several.
`config/local.json` may set `{"loopback": {"peerCheck": "always"}}` to switch the check on with one account; `auto` is
the default, there is no `off`, and any other value or a file that does not read reads as `always`. Memory over HTTP
now runs uvicorn with `proxy_headers` off (no proxy stands in front of it) and refuses WebSocket upgrades. Nothing writes that
file yet (the `full configure loopback` section arrives with the per-account settings file); an older engine ignores
it. Additive elsewhere: the receipt `store/raw/serverd.json` gains `peer` (`setting`, `active`, `accountsOnHost`,
`method`, `refused` with a count per code and `since`), outside its change comparison, and `full doctor` gains
`loopback`: `accountsOnHost`, `accountsReason` (`local`, `directory-bound`, `unreadable`), `peerCheck` (`setting`,
`active`, `method`, and `problem` when the setting did not read), `refused` (the local server's counters from that
receipt, or null; memory over HTTP and the keyserver count in their own process only) and `clientCheck`, null until
the clients check their server (OBS-355 part 2). The relay is unchanged.

## Doctor: one verdict and the secret store (OBS-349, 0.34.0)

Additive, with no migration. `full doctor` gains `attention` (rows `{area, code, severity, text, step}`, area
`backups`, `machine_backup`, `maintenance`, `sources` or `tick`) and `clean` (no row is a `warning`;
`update-available`, `auto-off`, `deferred` and `schedule-off` are `info`; a `tick` verdict of `interrupted`, `stale`
or `running-long` is a `tick-<verdict>` warning; the other sections are not read by the verdict yet). Its exit code is unchanged. `backups` (also `full backups
status`) gains `secret_store` (`{"external": false}`, or `external`, `path` in its home-relative form,
`settings_keys`, `exists`, `env_override`, `last_carry`, and `environment` when only `OBSERVATORY_SECRETS` names it) and `warning_items`, one per sentence of `warnings`, in the
same order and with the same text: `backups-root-unusable`, `backups-passphrase-unreadable`, `backups-no-passphrase`,
`backups-root-inside-workspace`, `backups-failed-update-copy`, `backups-unencrypted-local`, then
`backups-secret-store-env-override`, `backups-secret-store-missing` and `backups-vault-store-not-carried`, which are
new sentences. `full maintain status` and `maintenance.warning_items` gain those codes, except
`backups-no-passphrase` and `backups-unencrypted-local`, which a maintenance item already says. Doctor reads
`store/raw/secret-store-carry.json` when it exists (written by a release whose workspace snapshot carries an external
store; OBS-350, below), and a file that does not read is `{"state": "unknown"}`. The MCP tool
`observatory_machine` accepts `section: "move"`, the `machine-move status` checklist under `move`; an older server
refuses the value through its schema, and `full machine-move status --json` answers the same.

## Workspace snapshot: an external secret store carried encrypted (OBS-350 part 1, 0.34.0)

Additive; `format_version` stays 1 and nothing migrates. A snapshot's manifest gains `externals` (one row per
store the settings name outside the workspace: `name`, `settings_keys`, `path` in its home-relative form,
`state` `carried` or `not-carried`, `reason` `plaintext-output`, `no-passphrase`, `export-failed`,
`update-snapshot`, `too-large`, `changed-during-copy`, `unreadable`, `missing` or `contains-workspace` (a setting
naming a folder that holds the workspace itself), and for a carried store `files` (relative path, SHA-256, size,
mode), `directories`, `links` (relative path and a relative target inside the store, recorded and never written),
`not_carried` (relative path and `link-outside-store`, `special-file`, `unreadable` or `this-machine-only`; a held
slot's metadata and rotation archives are listed beside it, and a slot whose metadata does not read is held as
`unreadable`), `size`, `vault_slots` and `kept_here`) and `external_warnings` (`secret-store-env-override` when the
environment moves the vault away from the folder the settings name). `external_sources_included` is true only when
a store outside the workspace exists and every one was carried; external source folders such as `sources.projects`
are never carried. A carried store sits in `<snapshot>/external/<name>/` beside `data/`, files 0600 and folders
0700, only inside an `OBSENC1` artifact: an explicit `--output`, a snapshot without a passphrase, a failed or
interrupted export kept in the workspace and the update's snapshots never hold it. Each `snapshot()` first strips
`external/` from every snapshot copy under the workspace's `backups/` (named `<kind>-` for a snapshot kind, or with a
manifest listing `externals`) and removes `.snapshot-*` stages older than an hour; a folder it cannot clean is named
in the answer's `secret_store_sweep.failed` (folder name, error class) and counted in the carry record's
`sweep_failed`, and never stops the snapshot;
`workspace_upgrade.snapshot` gains the keyword `update` (the pre-update snapshot passes it). `verify_snapshot`
refuses a link, a special file, an unlisted file or folder, a changed file or a folder of a store not carried, with
the code `secret-store-manifest-mismatch` (a `ConfigurationError`, so exit codes are unchanged). An older engine
verifies and restores `data/` alone and ignores `external/`; so does the `full restore` of 0.34.0 to 0.35.4, which
does not write the store into place (the restore side is part 2, below). New workspace state: `store/raw/secret-store-carry.json`,
owner-only, written atomically after each snapshot the backups keep or export when a store is external (`at`,
`kind`, `file`, `state`, `reason`, `files`, `size`, `not_carried`, `sweep_failed`; never a value), read by doctor
since OBS-349 (doctor's `last_carry` gains `not_carried` and `sweep_failed`); a write that fails is said on stderr and as `carry_record_error`. The
daily run's maintenance record gains `snapshot.secret_store` (`state`, `reason`); the snapshot answer gains
`secret_store` (`state`, `reason`, `files`, `size`, `kept_here`, `not_carried`, or `{"state": "not-applicable"}`).
The machine bundle skips any path inside a store the snapshot carries (`skipped`, reason `carried encrypted in the
workspace snapshot (sources.<setting>)`), and its `inventories/observatory.json` gains `secret_store` (`external`,
`carried_in`, `newest_snapshot_carried`; or `carried_in: null` with `reason` for a store no snapshot carries).
Slot metadata gains `this_machine_only` (`why`, `at`), set by `full vault-digests keep-here PROJECT ENV NAME --why
TEXT` and cleared by `unkeep-here`; a `vault-digests/1` manifest row may carry `"this_machine_only": true`, which an
older reader ignores and this one refuses with any other value; the view's cell may carry
`"on_purpose": "this-machine-only"` (a string, where a difference on purpose carries an object).

## Workspace state: the daily learning review (OBS-331 part a, 0.34.0)

Additive. A new command, `project-observatory full learning-review show|list|run`, and two new tick entries after
`rollup`: the probe `learning-review-due` and the step `learning-review` (both optional, never among the steps that
must finish). New workspace state under `<state>/learning-review/`: `state.json` (`observatory-learning-review-state/1`,
the last report written and the newest 30 runs, codes and counts only), `.lock`, and one report per UTC day,
`<YYYY-MM>/<YYYY-MM-DD>.json` (`observatory-learning-review/1`) with its page `<YYYY-MM-DD>.html`, and a revision
`run --again` replaced kept beside it as `<YYYY-MM-DD>.r<n>.json`, and a report file that no longer read moved aside as
`<YYYY-MM-DD>.damaged-<n>.json`; folders and files owner-only, reports older than 400 days pruned. A new optional settings section, `learning_review` with `settle_hour_utc` (a whole hour from 0 to 23, 6
when absent), set with `full configure learning_review settle_hour_utc`: an older engine never reads it, and this one
refuses another key or value in it, as it does in `updates`. `ops_digest_store.first_lines` also answers the first
revision's `receivedAt`, and `first_lines_of(project, month)` reads one month. `tools/corroborate.py`
`corroborate_lessons` takes three optional keyword arguments (`keys`, `deadline`, `revalidate`); without them it behaves
as before. No MCP tool, finding type, Health or doctor key changes in this part; the review writes no ledger row itself
(the corroborator it runs may write promotions, never revalidation archives); nothing migrates and no SQLite table is
added. An older engine ignores the folder.

## Service: the connections declaration (OBS-326, 0.34.0)

The always-on server's `/.well-known/fabric-service` gains, under `extensions`, the key
`https://fabric.passioncode.ai/agent-contract/extensions/connections/0.1` with `{connections: [...]}`
(fabric-agent-contract DEC-0096, rule FAC-SEM-127): `id`, `kind` (`service` or `connector`), `target`,
`capability` (the hub only), `state` (`ok`, `degraded`, `down`, `unknown`), `label`, `purpose`, `reason`,
`lastAt`. Additive: a host that does not know the key ignores it, and `contractVersion` stays `0.1.0`. The
ids are stable: `fabric-hub` (target `fabric`, capability `project.resolve`), `release-feed`, one per
provider integration that is on (`github`, `bitbucket`, `cloudflare`, `heroku`, `digitalocean`, `google`,
`openrouter`, `domains`, `mcp-probe`) and `usage-analytics`. The list is checked against the contract's
schema, vendored at the contract pin (`fabric/interop-schemas/service-connections-extension.schema.json`),
each id once and no credential-like string; a list that fails is not published and `degraded` names
`connections`. No file is added; `store/analytics/counters.json` gains `last_send`
(`at`, `ok`), the state of `usage-analytics`, which an older engine ignores. Not declared yet (OBS-338): git remotes (`ls-remote`), remote environments, GA4, Search Console,
Cloudflare analytics, the assistant's model provider, the organization server, relay and mailbox, and release
downloads (only the feed is declared). The hub reads `down` when it is dead or unverified and `unknown` when no hub is installed.

In the same release (OBS-339 to OBS-342): a collector receipt older than three times its collector's interval in
the tick, and never less than six hours (`service_connections.STALE_FACTOR`, `STALE_FLOOR_HOURS`,
`INTERVAL_HOURS`: 6 hours for a collector that runs every tick, 18 for Bitbucket, 36 for Google, whose collector skips a scan younger
than 12 hours, 72 for a daily one), reads
`degraded` with a reason saying its collector has stopped running, and keeps its old `lastAt`; the local server
measures the age again at least every ten minutes. With `OBSERVATORY_OFFLINE=1` in the server's environment no
provider is declared, the collectors' own rule (`configuration.enabled`); the server cannot see a tick's
environment, so an offline tick beside an online server still declares what is configured. The hub reads
`degraded` when its receipt holds any other refusal beside `hub-absent`, and `unknown` only for `hub-absent`
alone. `store/analytics/counters.json` is now written under a lock, the new empty file
`store/analytics/.counters.lock`; an older engine ignores it and writes without it.

## Workspace state: anonymous usage data (OBS-324, 0.33.0)

New files only; no earlier file changes shape. An upgrade writes none of them: the switch file is
absent until a person runs `full analytics on|off` (absent reads as on, so an install that predates
this release sends anonymous usage data after the update unless a person turned it off), and the
folder appears the first time an event is queued, which happens only in a build that carries an
analytics App-Key. A downgrade leaves them in place, unread by the older engine, which sends nothing.
What is sent and what never is: [the event catalogue](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/analytics-events.md).

| Path | Since | What it holds | Upgrade and downgrade |
|---|---|---|---|
| `analytics` (in the workspace home) | 0.33.0 | the switch: `on` or `off`, owner-only; absent is on, only `off` is off, like `auto-update` | written by `full analytics on\|off` and by the apps' Settings switch; deleting it turns collection back on |
| `store/analytics/install-id` | 0.33.0 | a random UUID made for analytics only, never derived from the machine, an account or a person | deleted when the switch is turned off; a new one is made when it is turned on again. A snapshot restored on another machine leaves the whole `store/analytics/` folder behind (`ANOTHER_MACHINE_LEAVES`), so that machine makes its own id and never resends this one's queue; the switch file `analytics` moves |
| `store/analytics/queue.jsonl` | 0.33.0 | events waiting to be sent: name, catalogued properties, time, version; at most 512 KiB, sent by the maintenance job, dropped after 23 hours | deleted when the switch is turned off; deleting it loses only unsent events |
| `store/analytics/counters.json` | 0.33.0 | counts of what was sent, refused, expired, rejected or failed; never an event | deleting it resets the counts |
| `store/analytics/.counters.lock` | 0.34.0 | empty; its lock serialises every update of `counters.json` (OBS-341) | deleting it is harmless; it is made again at the next update |
| `store/analytics/notice.json` | 0.33.0 | when the first-run notice was shown on the terminal, and when a person first opened Home through the local server, and in which version | deleting it shows the notice again |

Settings outside the workspace: `OBSERVATORY_ANALYTICS=off` (also `0`, `false`, `no`) and `DO_NOT_TRACK`
(any value but empty or `0`) turn collection off for the processes that carry them, without touching
the switch file; `OBSERVATORY_ANALYTICS_APP_KEY` supplies the App-Key to a build without one;
`OBSERVATORY_ANALYTICS_ENVIRONMENT` (`production` or `sandbox`) overrides the install check. A
release build may carry the App-Key in the file `analytics-app-key` beside `usage_analytics.py`; the
repository never does.

## Workspace state added in 0.25.0 to 0.27.0

Each family below is new; no earlier file changes shape. An upgrade creates none of them: each
appears the first time its command or collector runs. A downgrade leaves them in place, unread by
the older engine. `secrets/` here means the configured `sources.secret_store`, `secrets/` in the
workspace by default; `node/` is `OBSERVATORY_NODE`, the workspace's `node/` by default. A workspace
snapshot carries a family only while it lives inside the workspace (the defaults): a node folder
configured elsewhere is a reference and needs its own backup; a secret store configured elsewhere is
carried by an encrypted snapshot from 0.34.0 (OBS-350) and is a reference for any
other snapshot, as the rollback rules above say. Restoring a snapshot taken before the upgrade drops these files, as
for every later observation. The repository's `StateFamilies` test refuses a family the code writes
that is not classified, and one from 0.26.0 on that has no row here; it finds the families from the
code's path constants, so a path built another way is not seen (OBS-182).

| Path | Since | What it holds | Upgrade and downgrade |
|---|---|---|---|
| `store/raw/fabric-attribution.json` | 0.26.0 | Fabric's id for each project (`schema_version` 1): `aliases` (`fabric:<uuid>` per `project:` id with its provenance), `conflicts`, `unattributed`, per-folder answers and a `history` of at most 500 changes, written by `full fabric-attribution` and the tick | `project.resolve` and the findings read it; without it they answer from the registry alone, as before 0.26.0. Deleting it loses only the history: the next pass asks Fabric again |
| `secrets/heroku-admin/` | 0.26.0 | the Heroku CLI session token the door stashes as the admin, one owner-only file per account | read by `tools/heroku.py` alone; an older engine has no Heroku door and never opens it |
| `secrets/heroku-issued.json` | 0.26.0 | the ledger of authorizations the Heroku door issued: ids, scopes, slot, team, app, dates; never a value | as above; removing it makes the door forget what it issued, not revoke anything at Heroku |
| `store/raw/digitalocean.json` | 0.27.0 | per DigitalOcean team: balance, Droplets with their monthly price, apps, databases without credentials, volumes, domains and named `degraded` (401, 403, 429), written by `full scan-digitalocean` | the findings read it; absent, they skip DigitalOcean |
| `secrets/digitalocean-admin/` | 0.27.0 | each team's own token stashed by `digitalocean.py stash`, owner-only, never handed out | read by `tools/digitalocean.py` alone |
| `secrets/digitalocean-read.d/` | 0.27.0 | each team's installed `observatory-read` token, which the collector prefers over the stashed one | the collector falls back to the stashed token when a team has none |
| `secrets/digitalocean-issued/` | 0.27.0 | one `<label>.meta.json` per installed narrow token (team, preset, declared scopes, slot, dates; never a value) and its lock | removing a record makes the door forget the token, not revoke it |
| `store/raw/cloudflare_account.json` | 0.30.0 | per Cloudflare account: Workers, Pages projects with their source repository, R2 buckets, D1 databases, zones with plan and price, DNS record count and certificate expiry, and named `degraded`, written by `full scan-cloudflare-account` | `emit` turns it into `registry/cloudflare-account.json` for the Cloudflare page; an older engine never reads it, and absent, the page says the account inventory is not measured |
| `secrets/cloudflare-inventory.d/` | 0.30.0 | one `inventory-read` token per account, owner-only, with its `<account>.meta.json` (account, groups, dates, expiry; never a value) | read by `collectors/scan_cloudflare_account.py` and managed by `tools/cloudflare.py`; an older engine never opens it, and deleting a file only stops the scan of that account, the token stays live at Cloudflare until `revoke` |
| `secrets/sentry-issued.json` | 0.31.0 | the ledger of Sentry tokens the Sentry door issued or installed (kind, scopes, organization, project, hosts, token id, dates, last verification) and the broad tokens it retired, each with a keyed digest of the value a Sentry accepted; never a value or a plain hash. Beside it, `sentry-proof.key`: the random key of those digests, owner-only | written and read by `tools/sentry.py` only; deleting the key makes every recorded proof stale, so a broad token needs a new one before its revocation counts; an older engine never opens it, and removing it makes the door forget what it recorded, not revoke anything at Sentry |
| `node/device/` | 0.25.0 | this machine's agent-network device keys from `full node --init`: `device.json`, `sig.pem`, `kem.pem`, owner-only; never replaced | an older engine ignores it; deleting it gives the machine a new device id at the next `--init` |
| `node/records/` | 0.27.0 | the estate revision and revocation-delta state this machine verified and applied (`estate-record.json`, `delta-state.json`) | share registrations derive from it; absent, none is derived |
| `node/orgs/<org>/` | 0.27.0 | one organization enrollment (`full org enroll`): `enrollment.json` (server, certificate serial and window, epoch, check-in state; no token), its own device keys in `keys/`, the signed activity outbox `outbox.sqlite`, a bounded `journal.jsonl`; locks in `node/orgs/.locks/` | `full org leave` removes the folder; an older engine neither checks in nor sends the outbox, which keeps its pending batches until a 0.27.0 or later engine runs again |
| `node/relay/` | 0.31.0 | the interim relay's spool from `full relay serve` or `full relay enable` (OBS-287): `spool.sqlite` (queued messages sealed under the relay's own key, their tombstones, each mailbox's next number) and `spool.key`, owner-only | read only by the relay; an older engine never opens it. Deleting it drops the queued messages, which their senders' ttl would have ended anyway; a new key makes the old spool unreadable, and the relay drops what it cannot open |
| `node/shares/sent/<id>/share.json` | 0.30.0 | a share this machine sent; its `usage` holds what the recipient reported. From 0.31.0 (OBS-291, contract DEC-0147): each report's day and slot replaces the one held, but only inside that report's window (its `iat` day and the 35 before it, not before `claimedAt`, the time the claim was read; a record without it uses `createdAt`), read with the window of the latest `iat` already read (`lastIat`) when a report's own is earlier, and what falls outside, or names a slot the share does not carry, is counted in `outOfWindow` (`backdated`, `unknownSlots`); at most 40,000 day and slot entries; `exportTimes` is the set of `{slot, at}` the reports named, at most 400 of one slot on one day and 10,000 in all (past that the ones older than the window are dropped and counted in `exportsDropped`), `consumers` at most 1,000, and `exports` the set's size plus `exportsDropped`, where 0.30 added each report's exports to a running count | a record 0.30 wrote reads as it is (0.30 had no export button, so its counts are 0); an older engine reading a record this one wrote keeps its numbers and adds the exports of each new report again, so a restated export would count twice there (none exists while no export button ships) |
| `node/shares/received/<id>/` | 0.30.0 | a share sent to this machine: its record, keys, sealed copies, `uses.jsonl`. From 0.31.0 (OBS-291): `uses.jsonl` keeps the 35 UTC days before the last report's day, which each report restates; `exports.jsonl` (the export button's journal; nothing writes it yet); the states `handed-over`, `lapsed` and `refused` (the person's `differ`) with `handedOverAt` and `lapsesAt`, at most 20 handed over at once, and a `link` file (owner-only, the link an agent handed over until the claim spends it, it lapses 7 days later or the person refuses it) | an older engine reports only the days since its last report and trims `uses.jsonl` to today, which loses nothing it needs; it cannot accept a `handed-over` share (its pass records that share as failed and its `accept` answers `already-opened`): accept it, or remove its folder, before going back |
| `<state>/secret-requests/decisions.jsonl` | 0.31.0 | the node's own audit record of the operator's decisions relayed to `secret.decline` and `share.open` (contract DEC-0121 AP-2): the decision with the operator's words, a share link's fragment replaced by the share id, one line per decision acted on (a repeated `share.open` adds none), kept two days (a decision's 24 h validity and a day's margin) and pruned at each write; owner-only, never in the journal, an answer or the events feed. A queue request answered `declined` carries `declinedBy` (`person` or `operator`) | an older engine ignores the file and the member, and reads every `declined` as the person's |
