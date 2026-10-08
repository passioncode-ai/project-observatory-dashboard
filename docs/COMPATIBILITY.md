# Compatibility and upgrades

The application release is **0.19.2**. The complete engine and each user's workspace are separate. Updating program files never intentionally replaces configuration, registry data, credentials, history or local dashboards. The previously published portable 0.1 command set remains a compatibility entry point; its smaller data model is not interchangeable with the complete engine's SQLite database.

## SQLite runtime prerequisite

The full engine requires Python 3.11+ with SQLite 3.37+ **and loadable SQLite
extensions**, plus the locked sqlite-vec dependency. Some macOS Python builds
omit `enable_load_extension`; installing sqlite-vec alone cannot add it.
Initialization, doctor, migration and upgrade check this before workspace writes.
The isolated regression is
`observatory/engine/tests/test_workspace_upgrade.py::WorkspaceUpgrade::test_missing_sqlite_extension_support_refuses_before_writes`.

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
| MCP transport | existing declared 2026-07-28 interface, SDK 2.2.0 (`mcp==2.2.0` in the `full` extra) | Preserve existing tool names, camelCase/snake_case aliases and proposal authority; transport negotiation is SDK-owned |
| Tool data | existing published input/output schemas | A closed output schema can reject an added field: version the capability before changing its shape |
| CLI | existing full-engine step names plus workspace management | Keep names/arguments through compatible releases; announce deprecation before removal |
| Profile (`full profile`) | format 1, minor 0 | Refuse an unknown format, a newer engine's profile or unmet `must_understand`; ignore and name unknown sections; never carry or touch `sources`, `storage` or `features.scheduler` |
| Automatic updates and backups (`full maintain`, 0.19.0) | The switch: the file `<home>/auto-update`, on when absent, off only when it holds the word `off`; `updates.auto: false` in settings.json (0.17, 0.18) still reads as off while the file is absent and is moved into it by the next `full auto-update` command; `updates.scheduled` in settings.json, true when absent; the job's record `<home>/store/maintenance.json`; codes in the shared update log `updates.jsonl` (`~/Library/Logs/Project Observatory/`, `$XDG_STATE_HOME/project-observatory/logs/`) | Install only what `full update --apply --unattended` installs (stable releases, signed by the organization's release key, both digests, no `needs_person` step); never downgrade; an update, reinstall or uninstall never writes the switch; an older reader ignores the `updates` key and the switch file; the app is replaced only by a bundle with the same identifier and version, signed by the organization's team `KJ35UYYL22`, with a Gatekeeper pass |
| Release install (`full update`) | GitHub release with `project_observatory-X.Y.Z-py3-none-any.whl`, `SHA256SUMS`, `SHA256SUMS.asc` and GitHub asset digests | Install only when `SHA256SUMS` carries a valid signature by the organization's release key pinned in the engine (`release_signature.py`) and both digests match; an unsigned or malformed signature is refused; never downgrade; roll back to a verified wheel of the running release, which must be signed too for releases from 0.14.0, the first published with a signature; a wheel carrying `observatory/engine/RELEASE.json` as `{"version": "X.Y.Z", "needs_person": "<step or runbook URL>"}` for its own version is held by `--unattended` (exit 6) and installed only by a person's `--apply`, which shows the step first; a marker naming another version is ignored, and the release build refuses such a wheel; an unreadable marker holds |

Semantic versioning applies to the declared public API even before 1.0 as a project policy. A patch fixes behavior within those contracts. A minor release adds compatible behavior and supported migrations. An intentionally incompatible change needs a major release with migration instructions. This does not promise that every past experimental version remains supported forever; each release publishes its tested upgrade matrix. [SemVer specification](https://semver.org/).

## Tested migration boundary

`observatory/engine/tests/test_schema_compatibility.py` covers every prefix of the ten migration IDs (0008 adds the agent-memory workflow tables, 0009 rebuilds the lexical index with search keys, 0010 adds the vector-namespace registry and quarantines the legacy OpenAI index), repeat opens, legacy checksum adoption, unknown IDs, changed migration checksum, future user_version, rollback after injected failure, WAL-only data and concurrent openers. Adopting a checksum for a legacy history records the current implementation; it cannot prove which old implementation originally ran.

`observatory/engine/tests/test_workspace_boundaries.py` covers concurrent initialization, symbolic-link escape attempts, source changes during migration, future writer refusal and preservation of an existing destination. `observatory/engine/tests/test_workspace.py` covers separate homes, optional settings preservation, configuration/registry version refusal and the complete local pipeline through twelve generated pages (`test_complete_local_workflow_and_twelve_pages`). Test execution receipts are recorded separately; listing a test here is not a claim that every release ran it.

## Upgrade and rollback rules

1. Read the target release's minimum Python, SQLite and supported source versions. Full engine requires Python 3.11+ and SQLite 3.37+; macOS/Linux are the supported platform family. Windows file-lock/service support is not claimed.
2. Stop the scheduler and other writers. Old executables cannot honor locks or version checks added later. Do not run two application generations against one workspace.
3. Preview the upgrade. Create a complete private snapshot before applying changes, including config and authored data. SQLite backups use its backup API so committed WAL content is included. [SQLite backup documentation](https://www.sqlite.org/backup.html).
4. Apply supported migrations under a lock, validate, then restart the chosen application version. Interrupted upgrades leave a marker that prevents ordinary runtime from treating partial state as healthy.
5. To roll back, restore a verified pre-upgrade snapshot into a separate home and use its matching application release. An older executable must never silently rewrite a newer database. New observations made after the backup are not present in that backup.

External source directories and externally referenced credential stores are **references**, not bundled backup contents. Their independent backup/recovery policy remains the user's responsibility. Internal credential files, when included in a private snapshot, retain private modes and must never be attached to an issue or public release.

**Encrypted artifacts (0.4.1).** Backups written to the backups root use the `OBSENC1` format, version 1: magic `OBSENC1\n`, a length-prefixed JSON header (`format`, `cipher` AES-256-GCM, `kdf` scrypt with `n`/`r`/`p`/`salt`, `nonce_prefix`, `chunk`, `kind`, `content`, `application_version`, `created_at`), then length-prefixed sealed chunks whose nonce is prefix ‖ counter ‖ final-flag, with magic and header as associated data. A reader refuses any other `format`, `cipher` or `kdf`. `.obsnap` holds a gzip tar of a snapshot directory (manifest verified again after decryption); `.obsdb` holds one SQLite file. Three artifacts per kind are kept. The passphrase cannot be recovered; a lost passphrase means a lost backup.

## Original installation migration

`migrate-local` previews categories and counts. `--apply --writers-stopped` copies into a new destination; it does not activate services or overwrite the original. Curated owner lists move into private ownership configuration. External paths and integration selection must be verified for the destination before activation. SQLite may update its own SHM bookkeeping during a read-only backup; the promise is preservation of logical source data, not byte-identical auxiliary files.

The full source publication deliberately has no ancestry from a private operational Git repository. Release privacy checks examine source, fixtures, package contents and public history independently from runtime migration tests.

## Registry: identity map (0.3.0)

`registry/identity.json` (schema_version 1) records which merge keys and strong anchors belong to
each project id, which ids are retired, and repositories' former names. It is written by the emit
step. A workspace upgraded from 0.2.x gets it on the first emit with every existing id unchanged.
An unreadable map stops the emit step instead of re-minting ids. Contract:
[docs/design/IDENTITY.md](design/IDENTITY.md).

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
