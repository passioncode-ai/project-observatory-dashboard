# Changelog

All notable changes to Project Observatory. Versions follow [semantic versioning](https://semver.org/);
while the major version is 0, a minor release may change behaviour and says so here.

## 0.15.0 — 2026-10-04

A minor release: a vault bearer for HTTP MCP servers through Claude Code's `headersHelper`, and
agent memory carried further — credentials through Observatory, recovery after a lost session,
sessions, the Agents page, an evaluation set and better search (#133, #136). The companion plugin
`observatory-log` is 0.15.0: its `handling-secrets` skill names the new commands. A new dependency,
`snowballstemmer` 3.1.1 (BSD-3-Clause), is in `[full]` and both locks; migration `0009-search-stems`
rebuilds the lexical index from canon on upgrade.

### Added

- **A vault bearer for an HTTP MCP server, through Claude Code's `headersHelper`.**
  `tools/use_secret.py header [--env ENV] [--name Authorization] [--scheme Bearer] PROJECT NAME`
  prints one JSON header object, `{"Authorization": "Bearer <value>"}`, which Claude Code puts
  into the request on each new connection. It reads the vault only and refuses, printing nothing,
  when stdout is a terminal, when `CLAUDE_CODE_MCP_SERVER_URL` is unset or names a scheme, host or
  port other than the slot's binding, when the slot is not bound, when `--name` or `--scheme` is
  not an RFC 7230 token, or when the value carries a control character. Every call, served or
  refused, is a row in the `secret-use.jsonl` audit, without the value.
- **`tools/vault.py bind PROJECT ENV NAME --header-for <https URL or host>`** records, in the
  slot's metadata and never with its value, the one server the header door may serve it to;
  `--clear` removes it. Only https is accepted (a bare host means https on the default port).
  Both are journalled in the movements journal, and `vault.py list` shows the binding.

- **Agents get every credential from Observatory, by name** (OBS-10): declared credentials,
  slot-naming redaction, `--vault-only`, `use_secret.py serve`, consumers named on rotate.
- **Agents find and recover their work** (OBS-09): `full workflow list/show/handoff/close`, and a
  self-handoff after a lost token.
- **Sessions are linked to workflows**, with derived stalls (OBS-11), and **the live Agents page**
  shows lanes of executors and handoffs, what needs you, and sessions, in EN and RU (OBS-12).
- **Credential checks that report** (OBS-13): known values in agent memory, keys outside the vault,
  `.env` fallbacks.
- **Search** (OBS-03): RU/EN word forms, checkpoint bodies, a measured coverage floor that abstains,
  indexing on write. On the gated evaluation set (OBS-07), recall@5 rose from 0.975 to 1.0 and MRR
  from 0.931 to 1.0; unanswerable questions abstain 20/20.

### Fixed

- `observatory_record` redaction was quadratic on long statements (about an hour on 2 MB, now
  0.26 s); the length bound is checked before redaction.

## 0.14.0 — 2026-10-03

A minor release: the organisation's product lifecycle contract applied to the engine (#125,
#131), agent memory for workflows and its hardening (#127, #130), and releases built, signed and
published in CI.
Behaviour changes: the scheduled tick no longer probes MCP servers, and background disk sizing
no longer opens privacy-guarded places. After updating, run
`python "$(project-observatory full-path)/tools/install_launchd.py" install` and
`python "$(project-observatory full-path)/tools/serverd.py" --install` once, so the background
jobs pick up the new plists. The companion plugin `observatory-log` is unchanged (0.14.0).

### Changed

- **The scheduled tick starts no MCP server.** It reads the agents' MCP configs only.
  `claude mcp list` runs on request (`full scan-mcp`, `machine.mcp.refresh`) in a process group
  that is killed when it answers or times out, so no server it started outlives it and no
  background job refreshes your Claude login. The last verdict is carried with its time
  (`liveness_at`).
- **Background disk sizing skips privacy-guarded places** — Documents, Downloads, Desktop, the
  media folders, iCloud Drive and other apps' containers. `full machine --disk` sizes them.
- **The server idles.** It re-reads its inputs only when they change, writes its heartbeat on
  change or every five minutes, and slows its beat to two minutes when no client asks.
  `/health` carries `silent_after_s`.
- **Background jobs carry a minimal `PATH`** (the directories holding the tools the engine
  calls, then the system ones) and name the interpreter by its virtual-environment or Homebrew
  `opt` path; the installers refuse a plist naming a versioned Homebrew `Cellar` path.

### Added

- **The tick is bounded.** Every step runs in its own process group under a wall-clock limit,
  the whole tick stops at a 25-minute ceiling below its interval, and
  `store/raw/tick-run.json` records each run's start, end, outcome and reason. The tick plist's
  `ExitTimeOut` is 30 seconds.
- **One log policy** for every log the engine writes: five generations of 5 MB, mode 0600.
- **`stale-server`.** An MCP server whose code an update replaced answers every call with
  `stale-server` instead of running the previous release; `full update` reports the session
  servers it could not reach.
- **The lifecycle watch** (`lifecycle.*` findings): orphaned product processes, session servers
  on replaced code, jobs past their interval, and logs past their cap or readable by others,
  each reported against the product that owns it.
- **Retention owns the pre-upgrade database copies** in `store/migration-backups/`: the newest
  two and any younger than 30 days stay.
- **Agent memory for workflows.** A checkpoint after every step, one executor per workflow by
  lease token, and an immutable handoff pack, so a workflow continues on another account, model
  or session when the one that leaves cannot answer. Five MCP tools
  (`observatory_checkpoint_write`, `_checkpoint_latest`, `observatory_handoff_create`,
  `_accept`, `_get`); migration `0008-agent-memory-workflows`; design in
  `docs/design/AGENT-MEMORY.md`.
- **Agent memory, hardened (#130).** Twelve findings of an adversarial review fixed: checkpoints
  and handoff packs stay out of recall, notes and the observer prompt; search serves only a
  record's latest revision; workflows never cross projects; a handoff needs the lease token (or a
  limit, crash or restart after two silent minutes) and acceptance is bound to the accepting
  session; identifier fields refuse credential shapes; the committed ledger export carries
  workflow rows without their body; an open workflow's checkpoint cannot be tombstoned; a
  credential store is never read into a pack. Details in
  `docs/runs/2026-10-03-agent-memory-hardening/README.md`.
- **Builds clean up after themselves.** `tools/prune_builds.py` keeps the current and previous
  release in `dist/`; `macos/scripts/build-app.sh` runs it.

- **A notarized Mac app per release.** `macos/scripts/notarize.sh` notarizes the
  Developer ID-signed bundle with an App Store Connect API key or a `notarytool` Keychain
  profile, staples the ticket, assesses it with `spctl` and packages
  `ProjectObservatory-<version>-macos.zip`, which the release attaches and lists in
  `SHA256SUMS`. It refuses before uploading when credentials are missing or the bundle is ad hoc
  or lacks the hardened runtime. Apple's log is printed on a rejection. The key never reaches
  the output. 0.13.0 carries the first such download.
- **Releases are built, signed and published in CI.** `.github/workflows/release.yml` runs on a
  `vX.Y.Z` tag. It builds the wheel and the Mac app, signs the app with the organization's CI
  Developer ID, notarizes and staples it, attests every file (Sigstore), and publishes the
  release with `SHA256SUMS` and a GPG signature, `SHA256SUMS.asc`. Nothing runs until a person
  from `release-approvers` approves it; that may be whoever pushed the tag, and an agent never
  approves. A published release is never rewritten. To rehearse, push an `-rc` tag and dispatch the workflow with `publish=false`.

## 0.13.0 — 2026-10-03

A minor release: three audit-and-fix runs over 0.12.0 (2026-10-03) — 173 defects found, 167 fixed (two
of them in part), the rest open and named. The findings, the checks and what is still open are in
`docs/reports/2026-10-03-observatory-audit-run-{1,2,3}/README.md`. The companion plugin
`observatory-log` is 0.14.0: its hook and its `handling-secrets` skill text changed.

### Changed

- **Every engine git call goes through one hardened door** (`safe_git.py`): no credential helper,
  askpass, pager, fsmonitor, hook, signer, external diff, textconv or filter driver runs, only the
  https, http, ssh, git and file transports are allowed, and ssh runs in BatchMode. Plain values
  of your git config (`url.*.insteadOf`, `safe.directory`, excludes, proxies) still apply. A
  consequence: a collector sees an LFS file as its pointer. SECURITY.md lists what remains.
- **The wheel carries the tested dependency lock**; README → Install is two steps (the wheel, then
  its `[full]` extra under that lock), and `full update --apply` installs with it.
- **A vault `PROJECT` is the project's folder name**; a registry id (`project:local-alpha-web`) or
  name is accepted and normalised to that folder, and two projects claiming one string are refused.
- **`full agent install` checks that Claude Code really installed the plugin** before saying so,
  and `uninstall` leaves `settings.json` as it was before the install.
- **No key is not "ready"**: `model_status` is `no-key`, `next` names `install_key.py`, and doctor
  and `assistant status` show where the key comes from (an inherited `OPENROUTER_API_KEY` too).
- **Bare `observatory` without a workspace exits 2** (it printed its guidance and exited 0).
- **The registry guard** refuses a swing over ±25% only above a floor (more than 5 projects added
  or more than 1 lost), so a small estate can grow; an empty registry is always refused.
- **Every `full <command> --help` prints help and runs nothing**, `full google --force` and
  `full plugins --only ID --force` are accepted, and doctor lists every known setting.
- **PassionCode design system 1.1.0**: the dark control edge is `#6f5e77` (3:1 on every surface),
  on the dashboard, the website and the Mac app.
- **The Mac app**: one dashboard window; a window reopened after a workspace change shows that
  workspace; Start Server is offered only where it can start; no model or budget is a named step;
  the assistant and Settings in the dashboard's tones; ⌥⌘↑/↓ walk the conversations.
- `configure model chain` and `configure budget` set what the assistant needs; `vault.py remove`
  removes a slot or its retired archives on the record.

### Fixed

- **Keychain and prompts:** `git ls-remote`, scheduled commits, the remotes probe (a `gcrypt::`
  remote started its helper), collectors and the Stop hook could run a credential helper, signer,
  hook or filter of the user's git config; the dashboard window cancels every password or
  certificate challenge.
- **Lost work:** `full cleanup --apply --include manual` could remove a dirty worktree after saving
  an empty or unappliable patch; it now proves the patch applies first.
- **Keys:** a second sighting of one leaked slot stopped the whole board; a value typed as a name,
  owner or note reached the registry and the pages; the keyserver's leak route could be turned into
  vault options; refusals over MCP and the keyserver quoted the caller's value; a vault slot
  reached the Keys page only after an OpenRouter scan; a key with no limit read as spent.
- **New user:** `full local` with no projects source ended in a traceback; the first board said the
  dashboard was unverified; a folder whose remote is on another host had no history and its
  unpushed commits were reported nowhere; seven handed-over commands were refused by the step
  runner; messages named source-tree paths; a missing `[full]` extra was blamed on the interpreter.
- **Dashboard:** keyboard focus was lost after a sort or a reset; the project panel said "no
  commits" for a project with commits; empty pages gave no command; demo pages carried the
  builder's paths; the server answered HEAD with 501; the Russian copy had meaning errors.
- **CLI:** a bare `full open --stop` looked on the default port, not this workspace's.

## 0.12.0 — 2026-10-02

A minor release: the Mac app opens on the dashboard, has its icon, always comes back to a window,
and what it starts can be stopped again.

### Changed

- **The app opens on the dashboard.** Its main window is the workspace's dashboard: live when the
  workspace's own server is verified on 127.0.0.1, the built pages from disk when it is not (they
  are self-contained), under a banner with **Start server**; an unbuilt workspace offers **Build
  the dashboard**. Navigation stays inside this workspace's pages and everything else opens in the
  browser; the page's confirm and prompt are native sheets; the app's language and the page's
  follow each other; the window's chrome is dark like the pages; rebuilt pages reload in place.
  The assistant moved to its own window (⇧⌘A).
- `project-observatory full assistant dashboard` names the server state (`verified`, `absent`,
  `other-workspace`) and the built pages; `serve` and `build` are the explicit actions behind the
  app's buttons (an installed always-on server is restarted through its launchd job).

### Fixed

- **What the app started could not be stopped.** posix_spawn hands the calling thread's signal
  mask to the child and a Swift worker thread blocks the asynchronous signals, so every process the
  0.11.0 app started inherited a blocked SIGTERM: a server it started ignored `full open --stop`,
  and an assistant runner kept waiting on the model after Stop. The app now spawns with a clean
  mask, and `serverd` and `run_job` unblock their stop signals whoever starts them.
- **No window after closing it.** Clicking the Dock icon, or launching again, did not recreate the
  closed main window; the app now always ends with the dashboard on screen.
- **No icon.** The app shows the product mark, rasterized on the macOS icon grid at every size.
- `macos/scripts/install-app.sh` installs the app and forgets Launch Services registrations of the
  same bundle left by builds elsewhere, which Spotlight could open instead.
- An engine that answers the dashboard action in the 0.11.0 shape is named incompatible instead of
  being shown as a workspace without a dashboard.
- **Finding titles read in Russian.** Every finding carries a message id and its arguments
  (`title_id`, `title_args`) beside the English `title`, which is rendered from them; the
  dashboard shows the title in the reader's language with correct plural forms, and a test fails
  when a rule's title has no id or no translation. Several English titles change wording: "(s)" is
  replaced by the proper singular or plural, and two titles were reworded so a second count needs no
  agreement. Anything matching on the old title text sees the new wording. Details and actions stay
  as their rules write them.
- An estate with no scan yet no longer reads "Measured not measured" ("Измерено не измерено") in
  every page's header.

## 0.11.0 — 2026-10-02

A minor release: the native Mac app and its shared assistant, and an agent channel an agent can
actually read. Behaviour changes for MCP callers are listed under **Changed**.

### Added

- **Project Observatory for macOS** (`macos/`, `docs/macos/`): a SwiftUI window that asks the
  workspace's configured model about bounded local evidence and shows the facts each answer used.
  `project-observatory full assistant status|ask|get|job|cancel|delete|list|dashboard` is the same
  workflow for the CLI, and `observatory_assistant_status`, `observatory_assistant_ask` and
  `observatory_assistant_conversation` for MCP hosts (#112, #114). An engine without the assistant
  is reported by the app as incompatible, with the update command.
- `observatory_findings` takes `projectId`, `limit` and `cursor`; `observatory_machine` takes
  `section`; `observatory_assistant_status` takes `includeProjects`.

### Changed

- **`observatory_overview`**, the agent's entry point: counts, activity tiers, the most recently
  active projects, the most severe findings and this machine's disk, about 5 KB on an estate whose
  unpaged survey is 165 KB. `observatory_status` keeps the published "no limit means everything"
  (the v0.2.0 probes rely on it) and gains `detail: "summary"` (about 400 characters a project) and
  `detail: "full"`; by default every row now carries only the published survey fields, and the
  organization and recorded resources come with `detail: "full"`.
- `observatory_machine` answers an overview with the top ten of each section; a named `section`
  in full; `explainPid` alone with its explanation. It was 168,000 characters every time.
- Smaller defaults: `observatory_findings` 20 (most severe first), `observatory_recall` 10,
  `observatory_timeline` 25. Text copies of `observatory_*` answers are compact JSON.
- Server instructions fit under 1,800 characters with the SPENDS, WRITE and SECRETS rules first; a
  host keeps about 2,048 and cut the old text at the WRITE rule.

### Fixed

- **`estate.survey` failed on every call** on an estate that records organizations: survey rows
  carried `organization` and `resources`, which the closed v0.2.0 item schema does not list. The
  capability now meets its published schema and names what it omits; `observatory_status` keeps
  both fields.
- `observatory_credentials` and `observatory_timeline` name an id the registry does not hold in
  `degraded` instead of answering an empty list that reads as clean.
- Findings reach a project through one shared matcher (its repositories and clones, its sites'
  domains, the secrets and env files in its folders), in `project.detail` too.
- A client that disconnects mid-answer no longer leaves a traceback in the server log (#107).
- Dashboard: every subject link lands on a row that is rendered, and a subject without one stays
  text; env links resolve folders to registry ids and an unknown project says so; the domains
  tile, nav badges and pages count the same sets; every grouped table folds; a linked row is no
  longer hidden under the sticky header; finding types are labelled in English and Russian;
  units, plural agreement, enum words, the health queue and the machine page at 900 px are fixed
  (#114). Finding titles, details and actions are still written in English by their rules.
- The `observatory-log` plugin is 0.13.1: an example in `tracking-resources` carried a real
  analytics account id and now carries a synthetic one (#107, #114).

## 0.10.1 — 2026-10-01

A patch release: the always-on server is no longer scheduled as background work, and Fabric
Dashboards is the one notification channel where it is installed.

### Fixed

- **The server is scheduled as a standard process.** Its launchd job carried `ProcessType
  Background`, `Nice 5` and low-priority I/O; under load macOS starved it, and on 2026-10-01 Fabric
  Dashboards recorded it "not answering" 30 times in a day while the process ran without a single
  restart. `tools/serverd.py --install` now writes `ProcessType Standard` with neither; the
  scheduled tick stays background work. Re-run `--install` to apply it to an installed server.
- **One notification channel.** When the server's Fabric descriptor is installed and Fabric
  Dashboards is on the machine, `tools/notify_findings.py` raises no banner of its own: the host
  delivers `finding.opened` from the events feed, titled with the agent and what it wants (Fabric
  Dashboards ADR-0010). Before, the operator got each finding twice. Without the app it notifies
  as before.

## 0.10.0 — 2026-10-01

A minor release: the licence changes, and collectors report sources that do not apply here
without degrading the service.

### Changed

- **Licence.** This release is the first under `AGPL-3.0-only OR
  LicenseRef-PassionCode-Commercial`: open source under the GNU AGPL-3.0, with a commercial
  licence from PassionCode.ai (`LICENSE`, `COMMERCIAL-LICENSE.md`, every manifest and `SKILL.md`).
  v0.8.2–v0.9.1 keep PolyForm Noncommercial or Internal Use, v0.8.1 and earlier MIT (#90, #91).
- `google-auth` 2.58.1, with the lock (#83).

### Added

- Cloudflare door presets, each delivering into a vault slot only after its new token passes
  its own probe: `email-send`, `email-routing` and `workers-edit`, and `cloudflare.py groups
  --account <slug> --match "<words>"`, a read-only listing of permission-group names (#89);
  `r2-bucket`, one bucket's S3 key pair, with the bucket and its lifecycle made by a setup token
  deleted before the command returns (#94); `fabric-inbox-server`, `fabric-inbox-account` and
  `workers-observability-read` (#93).

### Fixed

- **Sources that do not apply here held the service `degraded`.** A collector now puts a
  source it measured as not applicable on a `not_applicable` list beside `degraded`: shown on
  the board as one `collector.not_applicable` info row per receipt, never counted as degraded
  coverage. It covers a TLD whose registry runs no RDAP service (absent from the IANA bootstrap)
  for a domain DNS shows is held; a companion tool that is not installed, told apart from one
  installed without its store or key; a Google credential refused by an API its Cloud project
  never enabled, when another credential reads that surface; and a receipt left behind by an
  integration the workspace has switched off, which was read as current for days (#104).
- **An OpenRouter key was reported gone because a bounded listing did not reach it.** The listing
  reads the newest keys only, and an account where another product mints hundreds a day pushed a
  consumer's key past that bound. Missing keys are now searched for once past the bound and
  remembered by hash; the provisioning key, which no listing includes, is confirmed with
  `GET /key`; a key is reported gone only on a 404 for its hash or a listing walked to its end (#104).
- **A repository whose remote has one branch had "no default branch".** With no `origin/HEAD`
  and no `main` or `master`, the remote's only branch is its default; with several, the reason
  names `git remote set-head origin --auto` (#104).
- **One slow answer from an external command degraded a whole collector.** `git` calls of the
  filesystem scan and `heroku auth:token` are asked again with a longer limit after a timeout
  (`slow_command.py`), and a command that misses every attempt is reported with each duration
  and the load average (#104).
- **A leak-scan suppression could hide a rotated value or another file.** It matched only the
  credential name and a path substring; it is now bound to an exact location and the value's
  identity, malformed or legacy rules are refused with a warning, and evidence is replayed when a
  decision expires (PB-032, #62).
- **A spent key's reset date moved with the clock.** It was counted from today, so a key measured
  as spent in one month read as still blocked in the next; it is now counted from the
  measurement's `checked_at` (#98).
- `test_leak_coverage.py` built its dates from the local clock while the scanner uses UTC, so it
  failed between local and UTC midnight east of Greenwich (#94).

### Companion plugin 0.13.0

- Licence metadata in `plugin.json`, both marketplace entries and every `SKILL.md` front matter
  is `AGPL-3.0-only OR LicenseRef-PassionCode-Commercial`; 0.12.3 shipped the PolyForm text.
- `handling-secrets` documents the new Cloudflare presets (`r2-bucket`, `email-send`,
  `email-routing`, `workers-edit`, `fabric-inbox-server`, `fabric-inbox-account`,
  `workers-observability-read`) and the read-only `cloudflare.py groups` listing.

## 0.9.1 — 2026-09-30

### Fixed

- **Claude Code could not list any of the MCP server's tools in 0.9.0.** It refuses a whole
  `tools/list` answer ("tools fetch failed — Handler returned an invalid result") when one
  tool's `outputSchema` is not an object schema at its root, and two were rooted in `oneOf`:
  `project.record`'s published v0.2.0 output schema and the job-tool union of
  `machine.mcp.refresh`. Both are now served with `"type": "object"` added at the root; every
  branch of both is an object, so they accept exactly what they did. A test holds every served
  tool to an object-rooted input and output schema. Restart Claude Code sessions after the
  update.

## 0.9.0 — 2026-09-30

### Added

- **`machine.mcp.inventory`**: every MCP server the agent configs on this machine declare —
  Claude Code (user and project scopes, plugins, claude.ai connectors), Cursor, OpenCode,
  Codex, Gemini CLI and Kiro — by name, with the configs that declare it, its transport
  (`stdio`, `streamable-http`, `sse`) and whether it answered the last probe, and when. Names
  and verdicts only: no URL, command line, header or environment value is read into it. An
  absent or unreadable config and an inventory older than the tick allows are named in
  `degraded`. Enable it with `full configure sources mcp_config_root "$HOME"` and
  `full configure integrations mcp true` (#84).
- **`fabric-interop/0.1`** on the MCP server: every Fabric capability is also a tool of its
  own name — `estate.survey`, `project.detail`, `project.timeline`, `project.record`,
  `machine.mcp.inventory`, `machine.mcp.refresh` — taking and returning exactly its
  published schemas; `_meta.traceparent` continues as a child span; `machine.mcp.refresh`
  takes a new inventory as a job followed with `fabric.job.get` and stopped with
  `fabric.job.cancel`, whose result is the contract's full result envelope with its trace.
  Built against fabric-agent-contract `9cd778e` (DEC-0017) and fabric-agent-adapter 0.5.0,
  whose kit and conformance probe are vendored. The ten `observatory_*` tools are
  unchanged (#85). Design and limits: `docs/design/FABRIC-INTEROP.md`.
- `tools/serverd.py --install` writes a per-install manifest (this interpreter, the server,
  the workspace) and the service descriptor points at it; the server keeps it current after
  an update. `mcp/server.py` accepts `--home PATH`.
- The README quick start registers the MCP server with Claude Code (`claude mcp add …`).

### Changed

- The Fabric manifest is revision 7 (profile 1.2.0). The v0.2.0 schema identifiers are
  unchanged; the new schemas are published at `v0.9.0`, and the contract lock pins each file
  to the release that publishes it.

### Fixed

- **A URL's `user:password@` part reached the MCP scan file.** Only the query string was
  dropped; the user-info now is too, and it counts as a key in the URL.
- `observatory_status` skipped the value check for a `scope` object: `{"scope": {"kind":
  "owner"}}` surveyed the whole estate instead of answering `missing value`.
- `publish_contract.py --local-manifest` named the virtual environment's base interpreter
  (resolved through its symbolic link), so the server it described could not import its
  packages.

## 0.8.2 — 2026-09-29

### Changed

- **License.** From 0.8.2 Project Observatory is source-available, not open source. It is
  offered under `PolyForm-Noncommercial-1.0.0 OR LicenseRef-PolyForm-Internal-Use-1.0.0`,
  at the user's choice. Individuals and noncommercial organisations may use, change and
  share it for noncommercial purposes. Any company may use it, and change it, for its own
  internal operations. Distributing it commercially, or building it into a product or
  service provided to others, needs a separate commercial license (contact@passioncode.ai).
  Copyright: Siarhei Sheleh. **Versions up to and including 0.8.1 were released under the
  MIT License and remain available under MIT.** Contributions are accepted under
  [CLA.md](CLA.md).
- Package, plugin and marketplace metadata name PassionCode.ai as author and owner; the
  project homepage is https://passioncode.ai/observatory/. Security and conduct reports go
  through GitHub private vulnerability reporting or contact@passioncode.ai.
- observatory.sshlg.me is no longer a product page: `/` redirects to
  passioncode.ai/observatory/, and the field notes stay where they were.

### Companion plugin 0.12.3

- License and author metadata as above; no behaviour change.

## 0.8.1 — 2026-09-29

### Fixed

- **`full agent` fought a launcher that installs the same plugin.** `status` looked only for
  `observatory-log@observatory-log`, so a copy installed under another marketplace id (the
  PassionCode launcher's `observatory-log@passioncode`) read as "plugin not installed", and
  `install` added a second copy: every hook fired twice until the launcher removed it again.
  Any installed and enabled `observatory-log@<marketplace>` is now the plugin. `status` is ok
  and names the managing channel, `install` writes only the hook environment
  (`OBSERVATORY_ROOT`, `OBSERVATORY_HOME`, `OBSERVATORY_PYTHON`) and adds no second id,
  `uninstall` keeps the environment that copy still reads, and two enabled copies are
  reported with the command that removes one. `full update` suggests `full agent install`
  only when the engine's own channel manages the plugin.
- **`full doctor` was silent when a configured source had been deleted.** The `sessions`
  integration reads the companion database, but `companion_db` was not among its needs, so a
  deleted database left `coverage_warnings: []` while every tick logged DEGRADED. A missing
  path, or a directory where the database file should be, is now a warning, and every warning
  carries `fix` (`full configure sources NAME PATH`) and `disable`
  (`full configure integrations|features NAME false`).
- **`scan-mcp` timed out on machines with many MCP servers.** `claude mcp list` health-checks
  each server before printing. The limit is now 180 s (was 120), `OBSERVATORY_MCP_PROBE_TIMEOUT`
  raises it (1–3600 s), and `install_launchd.py` carries it into the tick's plist. A probe that
  still runs out of time is degraded: servers reported before the cut keep their verdict, the
  rest are `not-probed`.
- The dashboard builder no longer uses the deprecated `datetime.utcnow()`.

### Companion plugin 0.12.2

- `tracking-resources` no longer triggers on creation requests ("new Figma file", "add
  analytics"). It is the record-after step, and names the skills that create or wire instead.
- `plugin.json` points at a schema that resolves (`claude-code-plugin-manifest.json`).
- The engine's plugin suite checks every shipped SKILL.md front matter as strict YAML. An
  unquoted `: ` in a description, which `claude plugin validate --strict` accepts, now fails it.

### CI

- `claude plugin validate --strict` runs on the root marketplace, the engine marketplace and
  the plugin (Claude Code pinned; ubuntu-latest / Python 3.14 row of the required `test` job).

## 0.8.0 — 2026-09-29

### Added

- **The always-on server speaks `fabric-service/0.1`**, the Fabric Agent Contract's local service
  extension, so a host such as Fabric Dashboards can find and watch it without knowing it in
  advance ([design](docs/design/FABRIC-SERVICE.md)):
  - `GET /.well-known/fabric-service` — who answers (`project-observatory`, instance `default` or
    `ws-<sha16>` per workspace), which build (commit or package digest), pid and start time,
    `status` and `degraded` from the collectors and the tick verdict, and four tiles: projects,
    open findings, open leaks, last tick. Answered from memory; `/health` is unchanged.
  - `GET /fabric/v1/events?after=&limit=` — the event store as sentences with a cursor: grouped
    commits ("3 commits in Fabric by …"), agent sessions, findings opened (`notify: true`) and
    resolved, each linking to its project or finding. Requires the workspace's new
    `service.token` (mode 600) as `Authorization: Bearer`.
  - **One copy per workspace.** `serverd.py --run` locks `service.lock` in the workspace before
    anything else; a second copy exits 75 naming the holder. `full open --serve` on another port
    now says which address already serves the workspace.
  - `serverd.py --install` writes the service descriptor (claiming the port, refusing one another
    service declares) before the plist, uses bootout-wait-bootstrap with retries and waits until
    the server answers as itself; the plist gains `ThrottleInterval`, `ExitTimeOut` and
    `ProcessType`. `--uninstall` removes the descriptor too. SIGTERM now drains and exits 0.
  - `fabric-agent.json` revision 5 names the descriptor under the extension key.

### Fixed

- **Both servers bind without a reverse DNS lookup.** `http.server`'s `server_bind()` calls
  `socket.getfqdn()` between `bind()` and `listen()`; with a slow resolver (the macOS CI runner)
  the port stayed bound but silent for longer than 20 s, so a probe neither connected nor was
  refused. `serverd` and the portable `observatory` dashboard now name themselves `127.0.0.1`.
- **A failed heartbeat no longer leaves the server `starting` forever.** A full disk or a failing
  collector becomes a `heartbeat` or `health` row in `degraded`, and the snapshot is still
  refreshed.

## 0.7.1 — 2026-09-28

### Fixed

- **`full profile export` carried no model chain from a real workspace.** A policy file
  was excluded whole when any value held a machine path, and the operator's `models.json`
  had one in two prose fields (`wallet.source_note`, `embedding.contract_source`). The
  second machine then got an empty chain and zero spending ceilings, and its agent layer
  was off without saying so. Now, when only documentation fields (keys ending in `_note`
  or `_source` with a string value, which the engine never reads) hold a path or a token,
  those fields are dropped and listed in `excluded`, and the file travels. A path in any
  other field still excludes the file.

## 0.7.0 — 2026-09-28

A second machine can now run exactly the same Observatory as the first and stay in step with it.
Every install is a tagged release of this repository; the engine code that still lived only in
the private predecessor has come across.

### Added

- **`full update`** installs a GitHub release with no manual steps. `--check` exits 0 (up to
  date), 10 (a newer release) or 3 (could not look: never reported as up to date). `--apply`
  installs the wheel only when it matches both the release's `SHA256SUMS` line and GitHub's own
  asset digest. It then stops this workspace's launchd jobs, snapshots the workspace, installs
  with the running interpreter (the `[full]` extra included), runs the new release's `upgrade
  --apply` in a fresh process, checks version, pins and `doctor`, and restarts only the jobs it
  stopped. On failure it reinstalls the previous verified wheel, which is kept under
  `backups/engine-releases/`, and restores the snapshot. It refuses downgrades, source
  checkouts and editable installs. Steps are logged to `store/logs/update.jsonl`.
- **`full profile export | import`** moves the functional configuration between machines:
  integrations, features (except the scheduler), interface language, the model chain and the
  machine-independent policy files. It never carries sources, paths, secrets, registry, store,
  account ids or credential annotations, and a value shaped like a path, email or token is
  refused on both export and import. Import previews by default, applies atomically under the
  workspace lock, refuses a profile from a newer engine, and lists which enabled integrations
  still need sources or logins on this machine.
- **`full open --stop`** ends the dashboard server that `open --serve` started for this
  workspace, and only that one.
- **Cloudflare `dns-edit` preset**: `tools/cloudflare.py issue --preset dns-edit --zone Z --vault
  P/E/N` mints a token scoped to one zone (Zone Read + DNS Write), verifies it against that zone
  and delivers it into a vault slot over stdin. The handling-secrets skill routes to it
  (`observatory-log` 0.12.1).
- **The historical engine suites.** 130 suites exported with the engine on 2026-09-23 but left
  behind now run in `full check`, adapted to synthetic workspaces: 192 suites, up from 62.
  `test_step_references` fails if the step list names a file that does not exist (124 of 206
  did) or a suite the portable runner does not run.
- `AGENTS.md` for agents, with `CLAUDE.md` importing it, and a link to the organisation map.

### Fixed

- Operator messages named `collectors/<file>`; curated files live in the workspace's `config/`.
- keyserver audit rows now state that the caller is a token holder, not a verified identity.
- Trap T31 mutates the merge rule that decides whether a remote-less checkout is dropped.
- `mcp.own_unregistered` suggests a registration command that runs as written.
- Findings had no remedy for the merge's `github`, `wiki` and `identity` degradations.
- `skill_check` kept YAML quotes around the shipped version, so it never reported OK.
- `trace_opens` seeded its sandbox from the source tree instead of the workspace.
- Plugin `secret:NAME` readers and the default meaning of an unknown activity tier.
- `dashboard/audit_pack.py` could not import `paths` when run as a script.
- `review accept-proposal` named a checkout-relative file instead of the one it wrote.
- `skip_sites.survey()` crashed on a directory outside the program tree.
- `registry_shape` described `degraded` notices instead of records on a small estate.
- The dashboard purity allowlist was missing the machine page added in 0.6.0.
- README selects a supported interpreter explicitly; stock macOS `python3` (3.9) stops with a
  message instead of a misleading resolver error. Every ONBOARDING command runs as written.
- CODEOWNERS named an organisation, which GitHub rejects; it names the `contributors` team.

## 0.6.3 — 2026-09-28

### Added

- **`observatory`**, the short name: the package installs it beside `project-observatory`, same
  commands.
- **With no arguments, either name opens the dashboard** of the complete engine's workspace
  (`OBSERVATORY_HOME`, or the default one; `--home PATH` selects another) — `full open`, which
  builds the pages if needed. With no workspace it prints where to start (`full init`, `demo`)
  instead of an argument error. Every existing command parses as before.

## 0.6.2 — 2026-09-27

### Added

- **Swap is a disk row.** On macOS the swap files live on the VM volume, which shares the APFS
  container with the data volume, so memory pressure shrinks free disk. The machine survey now
  measures them (one `stat` per file; `/proc/swaps` on Linux) as "Swap files (memory written to
  disk)" on the Machine page, and `machine.disk_low` says how much of the shortfall is swap. Found
  the evening 0.6.0 shipped: 15 GB of swap was most of an 11 GB fall in free space that no measured
  place explained.

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
