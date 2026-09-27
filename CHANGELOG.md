# Changelog

All notable changes to Project Observatory. Versions follow [semantic versioning](https://semver.org/);
while the major version is 0, a minor release may change behaviour and says so here.

## 0.6.1 — 2026-09-27

### Fixed

- **The machine survey no longer holds the tick.** Disk places are sized within a time budget per run
  (`disk_budget_seconds`, 90 by default; `disk_location_timeout_seconds` per place), oldest
  measurement first; a place not reached keeps its last number and its own `measured_at`, and one
  that does not fit is reported, never shown as zero. `full machine --disk` uses
  `disk_budget_seconds_manual`. Measured on the machine that found it: the first 0.6.0 survey held a
  tick for 23 minutes under memory pressure; 0.6.1 sizes the same 19 places in 44 seconds.
- `du` runs with `-x`: a mounted simulator image or VM volume is no longer counted as the host disk's
  usage, and a timed-out `du` is no longer run a second time.

## 0.6.0 — 2026-09-27

### Added

- **The Machine page and `full machine`.** Processes grouped by origin — agent session, launchd job,
  simulator device, application, system, detached — with memory, CPU and project attribution; memory
  and swap; free disk and the largest cache, simulator, VM and history locations from
  `config/machine.json`, each with how its space comes back. `full machine --explain PID` says why a
  process runs, using witr when it is installed. A process is never stored with its environment or full
  command line.
- **Git hygiene**: every registered checkout's worktrees and branches, classified (merged, patch-merged,
  pushed, unique; clean, dirty, missing, in use, idle days).
- **Cleanup.** `features.auto_cleanup` removes on each tick only what loses nothing — merged or pushed
  branches, clean idle worktrees, stale worktree records, build output of idle projects — re-checking
  every target when it acts and journalling each removal. `full cleanup --apply --include manual` also
  removes unique branches and dirty worktrees after bundling and archiving them.
- `observatory_machine` (MCP, read-only): the same summary for agents, with `explainPid`.
- Findings: `machine.disk_low`, `machine.memory_pressure`, `machine.heavy_origin`,
  `machine.detached_servers`, `machine.stale`, `git.idle_dirty_worktrees`, `git.idle_unique_branches`,
  `cleanup.done`.
- `features.machine_watch` gates the three new tick steps (`machine`, `git-hygiene`, `cleanup`); both
  features default to off.

## 0.5.0 — 2026-09-27

### Added

- **Organizations.** `config/organizations.json` declares the owners an estate serves, where each
  keeps its Google Analytics properties and Figma files, and how a project is matched to one
  (repository owner, product membership, explicit declaration, `external`, default). Every project
  carries `organization`, `organization_source` and `organization_why`; `observatory_project` adds the
  destination (`ga4Account`, `figmaTeam`, `figmaProject`). Two matching owners is a reported conflict,
  never a guess. An empty file (the new default) changes nothing.
- **Resources.** `observatory_propose` accepts `organization` and `resources` for any project, without
  waiting for an operator to hand-write the first row. A resource has a closed `kind` vocabulary
  (analytics, Firebase/Google Cloud, server, database, DNS zone, cloud/payment/app-store account,
  Figma file, other) and a stable `identifier`; accepting it APPENDS to the project's list.
- **The SessionStart line** names the project's organization, its GA account and Figma team, how many
  resources are recorded, and the duty to report new ones. The `tracking-resources` skill in the
  `observatory-log` plugin (0.12.0) makes that duty explicit.
- Three analytics findings: a property in another organization's account, a new property in a legacy
  account, an organization's account no service account reads.

## 0.4.1 — 2026-09-27

### Added

- **Encrypted backups off this disk.** One backups root for every copy the engine keeps: the daily
  database copy, `workspace-backup` snapshots and the snapshot `upgrade --apply` takes first. On macOS
  it defaults to `~/Documents/Project Observatory/Backups` (iCloud Desktop & Documents carries it off
  the machine); elsewhere to `<home>/backups`. Override with `OBSERVATORY_BACKUPS` or
  `full configure storage backups PATH`. Everything written there is encrypted — AES-256-GCM in
  authenticated chunks, scrypt-derived key — with a passphrase set by `full backup-passphrase set`.
- `full backups status|migrate|decrypt`; `full restore` accepts an encrypted `.obsnap` file, taking the
  passphrase from `OBSERVATORY_BACKUP_PASSPHRASE` or a terminal prompt.
- `storage` in `config/settings.json` (`{"backups": "/absolute/path"}`). Releases before 0.4.1 ignore it.
- `full doctor` reports `backups`: root, the rule that chose it, whether it is encrypted, the newest
  artifact per kind, and warnings — never the passphrase.

### Changed

- **Snapshots rotate.** Three per kind are kept, locally and in the root. Before this release
  `<home>/backups` grew by one snapshot per upgrade and per manual backup, with no limit.
- Without a passphrase nothing leaves the workspace: copies stay where they were, unencrypted, and
  `doctor` warns. A plaintext copy never reaches the backups root.
- The tick asks `tools/backup_store.py --due` whether a daily copy is due, instead of searching one
  directory for one file name; the copy may now live in either place.
- `cryptography` is a declared dependency of the `full` extra (it was already in the lock file).

## 0.4.0 — 2026-09-26

Project Observatory is now a PassionCode.ai product. The repository lives at
[passioncode-ai/project-observatory-dashboard](https://github.com/passioncode-ai/project-observatory-dashboard);
GitHub redirects the previous address, and no workspace migration or key rotation is needed.

### Added

- **The dashboard speaks English and Russian.** English is the default. A workspace chooses its
  language with `project-observatory full configure interface locale ru` (or `en`); `full doctor`
  reports it. Each reader can switch between EN and RU in the navigation rail. The choice is kept in
  the browser, is applied before the first paint and works for pages opened as local files. Every
  interface string is an English message id; `observatory/engine/dashboard/locales/ru.json` holds the
  Russian, with CLDR plural forms (1 проект, 2 проекта, 5 проектов) and locale number grouping. Page
  titles, navigation, tables, filters, counters, confirmations and action outcomes are translated, as
  is the session-start line of the Claude Code plugin. Finding texts written by the finding rules stay
  in English: they are data, not interface.
- `interface` in `config/settings.json` (`{"locale": "en" | "ru"}`). Releases before 0.4.0 ignore it,
  so a workspace that sets it stays readable by them. An unknown key or value is refused.
- `tools/demo_estate.py` builds the real dashboard over a fictional estate for screenshots and demos.
  It reads nothing from the machine that runs it.

### Changed

- **PassionCode design system 1.0.0.** The dashboard and the website use the PassionCode tokens,
  vendored byte for byte and pinned by commit and SHA-256 (`dashboard/brand/manifest.json`): one fixed
  dark theme, gold for action, selection and focus, and a product glyph — an observing lens with a gold
  point — on the family's dark tile. The rail names the family, "by PassionCode.ai". The light/dark
  theme switch is replaced by the language switch.
- Stats in the dashboard payload use stable ids (`commits_7d`, `projects_active_7d`,
  `unpushed_commits`, …) instead of Russian labels; the page supplies the words.
- Metric captions in the shipped plugin manifests are English message ids (`dependencies`,
  `on disk`, `git branches`, …), translated by the page. A third-party plugin's caption reads as its
  author wrote it.
- The store's degradation note reaches the page as a message with arguments, so it is translated too.
- The website shows the real dashboard rendered over the fictional estate, names PassionCode.ai in
  its header, metadata and footer, and lists Observatory beside Switchboard as the PassionCode tools
  available today.
- `full agent install` moves a marketplace that still points at the previous repository address;
  `full agent status` names that case instead of calling it a non-GitHub source. Companion plugin 0.11.2.

### Maintainer tooling

- `tests/test_i18n.py` proves that the dashboard sources carry no Russian, that every message id has a
  translation with matching placeholders and complete plural forms, that the builder's plural rules
  equal `Intl.PluralRules`, that English pages hold no Russian, and that the vendored design tokens
  match their pin. Render harnesses run the page's own language runtime in both languages.

## 0.3.12 — 2026-09-26

### Changed

- The ten-page dashboard has persistent navigation, visible list rows and a compact
  project summary with full linked detail. ENV metadata is visible by default;
  revealing a value remains an explicit protected action.
- Sorting compares all selected rows across accounts and groups. A separate sort
  control remains available on narrow screens; opposite filters replace each other.
- Overview shows a bounded preview across all severities. Findings retain visible
  titles, searchable evidence and acknowledged history even when none remain open.
- Copy-only actions say they copy a command. Hosting cost and traffic coverage sit
  above their lists; Health starts with the observer's measured state.

### Fixed

- Repeated observations of the same GA4 resource through different credentials no
  longer duplicate rows or traffic totals. Provenance is retained, conflicting
  measurements are marked unknown, and cached registries normalize when rendered.
  Totals describe sums across properties, not unique people across products.
- Unknown traffic no longer matches the measured-zero filter. Findings search uses
  its own keyboard shortcut, and detail links restore focus when closed.

### Maintainer tooling

- Source checks parse each file once per metric-vocabulary guard. Private-name
  checks factor common prefixes while preserving longest-first boundary matching;
  a reference-matcher regression covers case, punctuation and Unicode.

## 0.3.11 — 2026-09-24

### Fixed

- The leak scan counted a file it could not open as read and clean. An unreadable file, a transcript
  whose `stat` failed, and an engine that isn't a git checkout are now listed as unread, and an
  unreadable file raises a warning. The report gains a `coverage` block.

### Added

- Leak suppressions with a reason and an expiry (`config/leak_suppressions.json`). A suppressed
  sighting is still shown along with its reason. An expired or incomplete rule is not applied and is
  reported.

## 0.3.10 — 2026-09-24

### Fixed

- A dashboard action whose answer was lost said "не вышло" (failed) and let you try again, even
  though the provider may already have revoked or minted. A timeout (60 s), a dropped connection, an
  unreadable answer to success, or a server fault now says "исход неизвестен · проверьте" (outcome
  unknown, check) and keeps the button locked until a rescan. A refusal the server explains is still
  "failed" and can be retried.

### Added

- [docs/design/ACCESS.md](docs/design/ACCESS.md): the access model for the dashboard server, the
  keyserver, MCP and the CLI. Each rule names the test that proves it, and each gap is stated as a
  gap. There are new tests showing that a declared caller name is a label and never an authority,
  that another workspace's token is refused, and that a cross-origin preflight is never granted.

## 0.3.9 — 2026-09-24

### Fixed

- The event collector read its retention horizon from `store/retention.json`, a file no workspace
  has. Retention reads `config/retention.json`. Without a horizon the collector inserted every commit
  within its depth, and retention deleted the old ones again, about 29,000 events on every tick of
  one installation. The collector now reads the same file. T25's guard is new: it builds a real git
  history with one commit past the horizon and one fresh, and proves that only the fresh one is
  collected. The old end-to-end check passed on any machine without an estate.
- `tools/trap_efficacy.py` ran in no public installation. It crashed when the private trap registry
  (`docs/knowledge-pack.md`) was absent, when a trap id was not `T<n>`, and when a self-driving suite
  was missing. It also wrote its report into the engine directory. It now says what it can't measure,
  sorts both id populations, and writes to `store/raw/trap-efficacy.json`. In this distribution it
  measures 3 caught, 1 missed (T25, whose guard is vacuous without a real estate) and 3 self-driven,
  and it reports 32 as inconclusive because their guards are not shipped.
- The T20 anchor now points at the public `tests/test_retention.py`, and T33, whose subject and
  guard are private, is no longer declared. `tests/test_trap_anchors.py` fails the day a source
  anchor goes stale.

## 0.3.8 — 2026-09-24

### Fixed

- The local OpenRouter and embedding key files followed `store/` even when `OBSERVATORY_STATE`
  redirected the workspace's state, unlike the token and salt. They now follow the selected state.
  A key left at the old location is still read with a note, and nothing is moved. A key in both
  places refuses instead of choosing silently. The installer refuses while a legacy copy exists,
  before it reads the key or asks the provider anything. Both locations are scanned for leaks.

## 0.3.7 — 2026-09-24

### Added

- The projects table groups each project's Heroku apps by the environment they serve, with production
  first and the account shown on hover. An app with no known environment is shown as "окружение не
  указано" (environment not specified), never guessed from its name. This is the last slice of
  [docs/design/DEPLOYMENTS.md](docs/design/DEPLOYMENTS.md).

## 0.3.6 — 2026-09-24

### Added

- A credential edge says where its value is read. Every `credential_used_by` edge carries a
  `binding`:
  - `local`: a vault slot, a destination file, or a file inside the project, all on this machine.
  - `unknown`: a curated edge that says the project uses the key but not where.
  - `run`: a production config var whose salted fingerprint equals a vault slot's current value, so
    that slot is read at run time by that deployment. The edge names the deployment and the variable.

  An edge also carries `environment` when a real slot is filed under one. `run` edges need a
  `remote-env` scan made with 0.3.6 or later, because the scan now also fingerprints the vault's
  current values. `build` is part of the contract but nothing scans CI secrets yet. This is the third
  slice of [docs/design/DEPLOYMENTS.md](docs/design/DEPLOYMENTS.md).

## 0.3.5 — 2026-09-24

### Added

- Environments are entities, scoped by project. `registry/environments.json` lists
  `environment:<project>/<name>` from explicit evidence only. There are four kinds:
  - an override in `config/environments.json`;
  - the Heroku pipeline stage, which the scan now reads;
  - the environment a project's vault slots are filed under;
  - a `.env.<name>` file in its checkout.

  Only the first two bind an app to an environment, as a `serves` edge that carries its rule. An app
  with neither is `unassigned`, and its name is not taken as a hint. So two projects' production apps
  no longer look alike. This is the second slice of [docs/design/DEPLOYMENTS.md](docs/design/DEPLOYMENTS.md).

## 0.3.4 — 2026-09-24

### Fixed

- The dashboard server reported version 0.1.0 in `/health`, in its receipt and in its `Server` header
  in every release. It now reports the application version, and a test refuses a second version
  literal anywhere in the engine.

## 0.3.3 — 2026-09-24

### Added

- Provider accounts are entities. `registry/accounts.json` lists each Heroku team (or a personal
  account) and each Cloudflare account under the provider's own id, and every app and zone the provider
  attributed gets an `in_account` edge that carries its rule. A resource whose account wasn't stated
  is listed as `unattributed` with the reason, never attached to the only account known. The Heroku
  scan now records the team id and, for a personal app, the owner's user id. Apps from an older scan
  stay unattributed until the next Heroku scan. This is the first slice of
  [docs/design/DEPLOYMENTS.md](docs/design/DEPLOYMENTS.md).

## 0.3.2 — 2026-09-24

### Fixed

- The leak scan read the memory companion's whole database on every tick. On a busy disk that step
  alone took more than half an hour. It now reads rows added since its last pass, as it already did
  for transcripts. A complete pass returns when the set of known values changes, weekly, when a table
  is recreated, or with `--full`. The scrub and the leak scan share this rule (`tools/sqlite_scan.py`).
- The engine's comments and docstrings are back. The source export had blanked about 8,300 lines that
  mentioned private context. They are restored where that was safe and rewritten in neutral words
  where it was not, so no line of spaces is left in their place. Code is unchanged: each file's AST,
  with docstrings removed, is identical.
- `check_public_release.py --private-denylist` is fast with a long list. It compiles the list once,
  where before it built one pattern per value for every file and blob.
- The 0.3.1 notes named the scrub's mark `state/scrub-watermark.json`. It is `store/scrub-watermark.json`.

### Added

- The tick's health, judged from outside the tick: `ok`, `running`, `running-long`, `interrupted`,
  `stale`, `never` or `disabled`. It appears in `full doctor`, in the server's `/health` and, when
  the data may be older than it looks, in every MCP answer's `degraded`. A tick killed by a restart
  used to leave the previous report in place, looking current.
- `tools/public-identifiers.json`: names this repository publishes on purpose, each with a reason.
  The privacy check subtracts them from a maintainer's private list and refuses an entry without a
  reason.
- `docs/design/DEPLOYMENTS.md`: the contract for accounts, environments, deployments and credential
  bindings (planned for 0.4).

## 0.3.1 — 2026-09-23

### Fixed

- Every derived relation names the rule that produced it (`rule` on `implemented_by`, `deployed_to`
  and `credential_used_by`), so an edge can be traced to its evidence; `validate` now refuses one
  without it.
- Two Cloudflare accounts holding a zone of the same name produced one zone id, and one of the zones
  vanished from the registry. Such zones are now `zone:<name>@<account>`; unique names keep their id.
- The ENV page showed the production verdict of only the first Heroku app sharing a checkout. It now
  lists every app's verdict for each variable.
- The memory-companion scrub re-read every row of both stores on every tick, testing each cell
  against each known value in turn; on a 5 GB store that step alone ran past half an hour. It now
  screens whole pages at once and, after one complete pass, reads only rows added since
  (`store/scrub-watermark.json`). A complete pass returns when the value set changes, weekly, when a
  table is recreated, or with `--full`. It also reads each store once instead of twice.

### Added

- `deployed_commit` on each Heroku app: the commit its last code release states it deployed, with the
  release number and source. Rollbacks, promotions and branch-only descriptions give `null`, never a
  guess from the configured branch. The Heroku page shows the short commit under the deploy date.

## 0.3.0 — 2026-09-23

### Added

- **Project ids survive renames.** `registry/identity.json` persists which names and strong anchors
  (a repository, a repository's former name, the root commit of a local Git history, a checkout path)
  belong to each project id. A renamed wiki folder, a transferred repository or a renamed local Git
  folder keeps its id, so history, notes and curation stay together. Ids are never reused; a project
  gone from a complete scan is retired, and a partial scan retires nothing. Anything ambiguous gets a
  new id and an `identity.ambiguous` finding instead of a guess. `identity_overrides.json` still
  wins. Upgrading changes no id. Contract: [docs/design/IDENTITY.md](docs/design/IDENTITY.md).

## 0.2.9 — 2026-09-23

### Fixed

- Projects whose names slug to one key (a wiki folder `Foo-Bar` and a repository `foo/bar`, or
  local folders `a b` and `a-b`) are all kept. 0.2.8 kept the last and dropped the others silently;
  now the highest-precedence anchor keeps the key, the others get a numeric suffix, and the collision
  is reported as degraded so you can pin names in `identity_overrides.json`.
- Commits in every checkout of a repository are recorded, not only the primary clone's: a worktree or
  second clone on another branch held work that never reached the event history.
- Relation ids use the project id. A project pinned with `identity_overrides.json` kept its id across
  a rename, but its `implemented_by` and `public_domain_of` edges were renamed with the merge key.

## 0.2.8 — 2026-09-23

### Changed

- MCP SDK 2.2.0 (`mcp`, `mcp-types`), with `requirements-full.lock` refreshed as one tested set:
  httpx2/httpcore2 2.13.1, starlette 1.7.0. The full offline matrix, including the MCP wire and
  contract suites, passes on the new set. The build backend may use setuptools up to 84.

## 0.2.7 — 2026-09-23

### Fixed

- The `ledger` step of every scheduled tick failed with `ValueError`: `export_ledger.py` printed the
  exported file relative to the program directory, and in a workspace installation the registry is
  elsewhere. The ledger was written; the step reported failure. It now names the file wherever it is.

## 0.2.6 — 2026-09-23

### Fixed

- launchd jobs keep Homebrew's `/opt/homebrew/bin`: 0.2.3's PATH filter dropped every
  group-writable directory, and Homebrew's is user-owned and admin-writable, so `heroku` and other
  Homebrew tools vanished from scheduled runs. Group-writable directories are kept when this user or
  root owns them; world-writable ones are still dropped.
- `full agent install` brings an already installed older plugin to the version the engine ships;
  `claude plugin install` succeeds without upgrading, so 0.2.5 left 0.11.0 in place.

## 0.2.5 — 2026-09-23

### Fixed

- The scheduled tick and the plugin hooks run with the Python that installed the engine
  (`OBSERVATORY_PYTHON`, written by `install_launchd.py` and `full agent install`). An installed
  package has no `.venv`, so tick fell back to the first `python3` on PATH, which lacks the `[full]`
  dependencies: the vector index reported "sqlite-vec is not loadable here". Companion plugin 0.11.1.

## 0.2.4 — 2026-09-23

### Changed

- The engine's comments and docstrings are back: 3,049 lines restored from the original sources
  that the first public export had blanked, filtered for private identifiers, with every file's
  syntax tree unchanged.

### Added

- `full doctor` reports `coverage_warnings`: an enabled integration or feature whose source is not
  configured or does not exist. A migrated installation without a `sessions` source had its leak
  scan narrowed from every agent transcript to four files without any warning. ONBOARDING now lists
  every source.

## 0.2.3 — 2026-09-23

### Fixed

- launchd jobs written by `install_launchd.py` and `serverd.py --install` carry the installing
  user's safe `PATH` directories, so collectors find `claude`, `heroku` and similar tools installed
  outside the system directories; before, the MCP inventory degraded with "claude CLI not on PATH".

## 0.2.2 — 2026-09-23

### Fixed

- **`full migrate-local` works again for installations that have a fingerprint salt or keyserver
  token.** 0.2.1 created fresh identities while staging the new workspace and then refused to copy
  the originals over them, so the migration stopped (`File exists`) and nothing was written. The
  original identities now move unchanged, and only a missing one is created, so fingerprints stay
  comparable across the move.
- Two processes opening a new database at the same moment no longer fail one of them with a
  `DatabaseError`: the version check that runs before the upgrade lock ignores a file another
  process is still creating, and the check under the lock decides.

## 0.2.1 — 2026-09-23

A correctness and security release. An audit of the published 0.2.0 wheel found fixes that had been
made in the private predecessor but never reached the public engine. All of them are carried here,
each with a regression test that fails on 0.2.0.

### Security

- **A local `rotate` no longer marks a leaked credential as closed.** In 0.2.0, `vault.py rotate`
  settled every open leak of the slot it replaced, although replacing a local slot proves neither
  that the provider revoked the old value nor that its consumers moved. `settle` (and
  `moved --settle`) now require `--revocation-evidence` and `--consumer-evidence`, recorded as manual
  attestations. **Upgrading reopens** every leak that 0.2.0 closed only by a local rotate: it shows
  as "closed only by a local rotate" at warning severity until you settle it with evidence.
- **The fingerprint salt and keyserver token are never created by a read.** A missing, empty,
  malformed, loosely permissioned or symlinked identity file is refused and left untouched.
  Identities are created only by `full init` (race-free), and re-running `full init` on an existing
  workspace keeps them and restores only a missing one.
- **Fingerprints taken under different salts are not compared.** Scans record a
  `fingerprint_namespace`; a comparison across namespaces answers `not_compared` with a
  `namespace_withheld` finding instead of a misleading "differs".
- **A private home inside the installed code or its checkout is refused.** 0.2.0 accepted an
  `OBSERVATORY_HOME` inside `site-packages`, where an upgrade or uninstall deletes it, or inside a
  source checkout, where it can be committed.

### Fixed

- A corrupt `finding_acks.json` is preserved: `ack` refuses to write, and the board builds without
  acknowledgements and names the unreadable file, instead of overwriting every saved decision.
- A former project id claimed by two projects is left unresolved rather than handing one project's
  history to the other by row order.
- A session that matches two projects equally is left unattributed (`ambiguous-project`) instead of
  being credited to whichever came first; a stale link is withdrawn by an event.
- The dashboard reads wallet and provider health from `OBSERVATORY_STATE`, where providers write
  them; ENV-page filters open the matching groups; counts use correct plural forms.
- The dashboard root `/` redirects to `/dashboard/index.html`, so its stylesheet and script load.
  Served at `/`, 0.2.0's pages opened unstyled and without data.

### Added

- `project-observatory full open [--serve]` builds the dashboard if needed and opens it, as local
  files or from the loopback server; it refuses to reuse a port that serves another workspace.
- `project-observatory full agent install|status|uninstall` installs the `observatory-log` Claude
  Code plugin from this repository with **auto-update on by default** (`--no-auto-update` opts out)
  and points its hooks at your workspace. The repository root is now a plugin marketplace.
- `tools/update_inventory.py` keeps the engine source inventory current; CI runs it with `--check`.
- CI installs the built wheel without the lock file and runs the dependency-sensitive suites.
- New regression suites: audit regressions, runtime identity, remote env, project identity,
  sessions, ENV page, metric labels, agent plugin.

### Upgrade

```sh
python -m pip install -U -c requirements-full.lock '.[full]'   # or the 0.2.1 release wheel
project-observatory full upgrade                                # preview, then --apply --writers-stopped
project-observatory full init                                   # creates any missing runtime identity
project-observatory full agent install                          # optional: plugin with auto-update
```

A 0.2.0 workspace is read by 0.2.1 as it is. A workspace created by 0.2.1 records 0.2.1 as its minimum reader.
The companion plugin is 0.11.0.

### Not established by this release

Live provider revocation and rotation, admission by external MCP hosts, and off-device restore were
not exercised; checks run on synthetic offline fixtures. Known-value scanning cannot prove that no
secret remains.

## 0.2.0 — 2026-09-21

First public release of the complete engine. See [docs/RELEASE.md](docs/RELEASE.md).

## 0.1.0

Portable CLI. See [docs/RELEASE-0.1.md](docs/RELEASE-0.1.md).
