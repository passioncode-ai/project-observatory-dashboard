# `node.lifecycle`: the port plan (OBS-79)

Status: plan, 2026-10-22; steps L1, L2, L2r, L3 and L4 built, and the engine side of L5 (see "Step L1: what was built",
"Step L2: what was built", "Step L2r: what was built", "Step L3: what was built", "Step L4: what was built" and "Step L5: what was built"). Decided by the operator on 2026-10-12 (organization architecture v4, D5): the
node wakes, stops and restarts enrolled desktop apps and background services for agents, and the
mechanics (code, protocol, design) move in from an existing private broker under the operator's standing
permission. Its records, logs, keys, the names of the apps it manages and its private paths and
configuration do not move. This document says, file by file, what is mechanics and what is data, where
each piece lands in this engine, and in which order the pull requests come. Row
[OBS-79](../backlog.md) holds the dates.

## What the broker does today

A per-user macOS service, started at login, that agents ask over authenticated MCP instead of launching
or killing apps themselves:

- six tools: `lifecycle.list`, `lifecycle.status`, `lifecycle.ensure_running`, `lifecycle.stop`,
  `lifecycle.restart`, `lifecycle.operation.get`; each effect is a durable, idempotent operation
  (admitted synchronously, polled by its receipt);
- only explicitly enrolled targets: installed apps pinned by their code-signing designated requirement
  (signing team and bundle id, so same-team updates keep matching) and LaunchAgents whose interpreter
  sits behind a stable path;
- a lease per caller: `ensure_running` again within the idle window renews it; an app the broker
  started and nobody renews is stopped after its idle minutes;
- the person comes first: `in_use_by_person` when the person opened, took over or is looking at the app
  (an activation seen by the watcher, or frontmost with input in the last minute), and `held_off` when
  the operator switched a target off on purpose;
- `always_on` targets are started at login and again when they exit, under a restart budget (three
  failed starts in 30 minutes stop the relaunching until someone acts);
- readiness kinds: `process`, `fabric-service` (the well-known endpoint answers), `fabric-hub`
  (Fabric's `hub.json` names the running pid and its loopback origin answers) and
  `switchboard-runtime` (Switchboard's bundled CLI, verified under the enrolled team, reports online);
- an operator CLI: enroll, credential, allow, policy, revoke, hold, bootstrap, service control.

It makes no model calls, sends no messages, installs no target, and never accepts a command, an
executable, a path, arguments or environment from MCP. About 4,400 lines of Python, a 100-line Swift
helper and 203 tests in four files.

## File by file

| Broker file | Size | Mechanics (moves) | Data or private wiring (stays) | Lands in this engine |
|---|---|---|---|---|
| `broker.py` | 1,387 lines | the closed registry schema and its validation, the authority (callers, per-tool scopes, token hashes compared in constant time), admission, durable operations, idempotency receipts, the effect journal, crash circuits, the restart budget, idle and takeover rules | the names of its callers; nothing else is hard-coded beyond the public bundle ids of Fabric and Switchboard, which stay as built-in readiness profiles | `node/lifecycle/broker.py` |
| `native.py` | 911 lines | `codesign` reading, the designated requirement built from team and bundle id, trusted paths, child processes in their own process group and killed with it, the NSWorkspace adapter (status, open with `--background`, quit, watch), the Fabric hub and Switchboard probes | none | `node/lifecycle/macos.py` |
| `native_app.swift` | 103 lines | the NSWorkspace helper (status, open, quit, an event stream of launch, terminate and activate) | none | `node/lifecycle/native_app.swift`; a source install compiles it with `swiftc` (L2), and the Mac app's build script compiles it into the app, which the release signs and notarises with it (L2r; see "How the helper is built") |
| `lifecycle.py` | 1,952 lines | the supervisor (always_on, idle stop, a 600 s safety sweep, 60 s while a started app runs), the MCP handler and its `instructions` text, the event log, the private log file handler, the admin CLI verbs, identity migration | its fixed port and launchd label, its own install, release, rollback and uninstall machinery (the engine's install and update replace it), its own credential files (the engine's access bindings replace them), and the one trusted signing team its migration names (a configuration value here, empty by default) | `node/lifecycle/service.py` (supervisor and MCP), `tools/lifecycle_cli.py` (verbs under `full lifecycle`) |
| `selftest.py` | 117 lines | a disposable broker on an ephemeral loopback port, authenticated MCP, the second-instance exit | none | a test in `tests/test_node_lifecycle_service.py` |
| `vendor/` | 2 files | none: the engine has its own `fabric_service.py` and Fabric interop | the vendored copies and their provenance | dropped |
| `acceptance.json` | 181 lines | none | one machine's acceptance run: process ids, real clients, its apps | stays |
| `README.md`, `AGENTS.md`, its module page and runbook | about 1,000 lines | the contract and the operator rules, rewritten here | the private gateway, its ports and callers, the list of enrolled apps, incident notes naming them | this document and a section of the operator's guide |
| tests, four files | 203 tests | every behaviour above | real signing teams, caller names and home paths in fixtures, replaced by neutral ones (`ABCDE12345`, `caller-a`, temporary folders) | `tests/test_node_lifecycle*.py` |
| runtime state (its application-support folder, logs, credential files) | n/a | none | everything: enrollments, policies, token hashes, the SQLite store, events | stays on that machine; imported only by the operator's own command (step L5, `full lifecycle import`), which reads it there and copies no token hash |

## What changes on the way in

1. **One service, not two.** The tools are served by the engine's existing local server
   (`tools/serverd.py`) under its access bindings, not by a second process on its own port with its own
   token files. The well-known endpoint lists a `node.lifecycle` service entry that names the node as the
   targets' lifecycle manager (`lifecycle.manager: node`); the server's own descriptor stays `launchd`, the
   supervisor of that process (see "Step L3: what was built" for why).
2. **The workspace store.** Operations, receipts, the journal and circuits live in a SQLite file of the
   module's own under the workspace's state folder, `<state>/node-lifecycle/lifecycle.db` (owner only),
   under the same retention rules (receipts 24 h, terminal operations 7 days, failed always_on starts
   30 minutes). Not tables in `observatory.db`: that database is the tick's, rebuilt and migrated by its
   schema, while these rows are an effect journal a long-running server writes synchronously, and the
   meter's ledger (`<state>/meter/meter.db`) set the same precedent. Enrollments and policies live in `config/node-lifecycle.json`, owner
   only. The name differs from `config/lifecycle.json`, which configures the lifecycle watch
   (`collectors/scan_lifecycle.py`), a different thing.
3. **The supervisor runs inside the node, which is the engine's local server** under its OS unit (launchd,
   systemd, Task Scheduler); the node service of OBS-198 is that same process, not a second one
   ([MACHINE-UTILITY.md](MACHINE-UTILITY.md) D1, D2).
4. **macOS first.** The adapter is NSWorkspace and `codesign`. Linux (systemd user units and
   `.desktop` launches) and Windows (the service manager and `ShellExecute`) are later adapters behind
   the same broker; until they exist the tools answer `unsupported_platform` there, never a silent
   success.
5. **Identity migration** keeps its rule (an unchanged bundle adopted under its own team; a changed
   one needs the owner) with the trusted teams the operator names (`full lifecycle import --trusted-team`,
   none by default).

## Pull requests, in order

| Step | What | Release |
|---|---|---|
| L1 | `node/lifecycle/broker.py` with the registry, authority, operations, receipts, journal, circuits and restart budget, and the one-pass supervision decision (`supervise`), against a fake adapter; the broker's tests for these ported with neutral fixtures. **Built**, pull request #342 | 0.29.0 |
| L2 | `node/lifecycle/macos.py` and the Swift helper, compiled by `swiftc` for a source install; the adapter tests (signature parsing, requirement text, process groups). **Built**, pull request #344. The helper's release build inside the signed app was planned before L3's tools were served; it was not built, and 0.29.0 serves the tools with the source-install helper (`full lifecycle build-helper`); the release build is step L2r | 0.29.0 |
| L2r | the helper's release build inside the signed app (below, "Release"), and the adapter preferring the app's helper when the installed app satisfies its signing requirement. **Built** in the L2r pull request; that a notarised release carries it is proven by the first release run (the release's own requirement check) | 0.30.0, by 2026-11-07 |
| L3 | the six MCP tools in the engine's server, the well-known service entry, `lifecycle.manager: node`, the `instructions` text, and `full lifecycle enroll / allow / policy / hold / revoke / status`. **Built**, pull request #347 | 0.29.0, by 2026-10-31 |
| L4 | the supervisor: the thread that runs `supervise` on its cadence (a 600 s safety sweep, 60 s while a started app runs), the watcher's activation events fed to `note_activation`, and the identity migration if it does not run at L5's import. **Built** in the L4 pull request; the identity migration runs at L5's import (below) | 0.30.0 |
| L5 | `full lifecycle import --from PATH`: the operator imports their own enrollments and policies from the private broker's state folder on the same machine (read locally, never committed); the identity migration runs inside the import (a legacy record can exist only in the private broker's state); then the private broker is switched off and its launchd job removed by its own uninstall. **Built**, the engine side, in the L5 pull request; the `unenroll` verb the L3 notes gave this step is OBS-251; running the import on the machine and the private broker's uninstall are the operator's (below) | 0.30.0, by 2026-11-07 |

Each step is its own pull request with an adversarial and a confirming review round, like every node
slice. The private broker keeps running until L5's import is verified on the machine that uses it, so
agents never lose the tools between steps: the operator runs the import, checks `full lifecycle status`, moves each
agent to the local server's `/mcp` with its access binding, and only then runs the private broker's own uninstall
(the engine never switches it off or touches its launchd job).

## Step L1: what was built

`observatory/engine/node/lifecycle/broker.py`, tested by `observatory/engine/tests/test_node_lifecycle_broker.py`
(suite `node_lifecycle_broker`, 102 cases: 85 ported, 17 new).

- **The closed registry** (`validate_registry`): every field required but `idleStopMinutes` and
  `backgroundLaunch`; an unknown field, a cycle among dependencies, an idle stop on an always_on target or a
  relative path refuses the whole document. An app's identity is only the requirement scheme
  (`designated-requirement/1`: bundle id, a ten-character signing team, the requirement text); a background
  job's is its label, the hash of its definition and the hashes of what it runs. Readiness is `process` or
  `fabric-service` (a loopback origin and nothing else); the product profiles `fabric-hub` and
  `switchboard-runtime` come with their probes in L2 and are refused until then.
- **The authority** (`Authority`): a caller is the SHA-256 of its credential, compared in constant time
  against every grant; a grant names its tools and targets, and only an owner's `*` covers every target. The
  name `supervisor` is the broker's own pass and is refused as a caller.
- **One broker per state folder.** The broker takes an exclusive lock on `broker.lock` beside its store
  (`oslocks`, across processes) before it opens SQLite or recovers anything; a second broker, in the same process
  or another, is refused with `already_running`, so two can never take over the same running operations.
- **Durable operations**: admitted synchronously, run by a worker; the admitting call answers with the receipt
  (no `lifecycle.operation.get` grant is needed to see it), and `get` reads it later by id. A process that dies
  leaves them `running`, the next one takes them over under a new generation, and an old generation's worker
  cannot finish them; one recovered past its deadline gets the target's own budget again instead of ending
  `timeout` before it looks, but only within `RECOVERY_WINDOW_SECONDS` (600 s) of that deadline: one recovered later
  (a machine that slept for hours) launches and stops nothing and ends with what is known, `delivery_unknown` when it
  had dispatched a start (the journal entry stays for observation to settle) and `timeout` otherwise, both with
  `reason: recovered_too_late`. A start recovered after its effect is still recorded as the broker's within the recovery window; a start recovered too late ends `delivery_unknown` and the app counts as the person's: observation settles the dispatch without recording a launch, so the node never idle-stops it (`_recovered_too_late`, `_resolve_dispatch`).
  **Idempotency receipts** answer a retry for 24 hours, before the policy and grant revisions are compared; the
  same request id with another meaning is `idempotency_conflict`. Admission reads and writes its receipt in one
  `BEGIN IMMEDIATE` transaction.
- **The effect journal** (`effects`): committed before the only effect of a start, so a crash in between is
  `delivery_unknown`, never a second launch, until a live process or 120 s of a stopped target settles it.
  The entry is read again inside the transaction that writes it, so another operation's dispatch found there
  is never followed by a second start.
- **Circuits and the restart budget**: two counted starts in 60 s open a five-minute circuit; three failed
  supervisor starts of an always_on target in 30 minutes stop the relaunching until a caller asks for it, the
  owner changes its policy, or a probe stronger than a pid sees it serve.
- **Idle and takeover**: an agent stops only what the broker started, that the person has not taken over
  (`note_activation` after a 20 s grace) and is not in front of (frontmost with input in the last minute):
  otherwise `in_use_by_person`. `held_off` never starts; killing and holding an always_on target off are the
  owner's (or a caller the owner marked `operatorProxy`). An idle stop reads the lease again under the target's
  lock. A stop is fenced again before SIGTERM and before a forced kill, and a broker that starts closing while it
  waits for a quit or a signal to take effect sends nothing further: the operation stays `running`, and the next
  process recovers it, probes again and gives the quit its grace.
- **`requiredCapability`** is checked at admission, not waited on separately: a start or restart accepts only a
  capability the target's own readiness proves (none, the enrolled `readiness.capability`, `process`, or its
  profile's `fabric-hub` or `runtime-online`), and a target is answered `ready` only when the adapter reports that
  readiness, so the capability asked for is ready when the operation is. Any other value is `capability_missing`
  before anything is admitted.
- **The adapter** (`Adapter`, a protocol): `identity_matches(target, *, deadline)`, `probe`, `start`,
  `stop(target, pid, *, force, mode)` and an optional `close`. The tests drive two fakes, a durable file seam
  and a process seam; no installed application is touched.

Notes for L4: the store is pruned only when a broker opens (`prune`); the supervisor's sweep should prune
too. An activation inside the 20 s launch grace is ignored by `note_activation`; the presence check at stop
(frontmost with recent input) still protects the person then, and the watcher may deliver a later activation.

What L1 does not do yet: no MCP tool, CLI verb or event log (L3), no thread runs `supervise` (L4, built since), no adapter
acts on a real app (L2). Nothing in the engine calls the broker, so no reader-facing surface changes in this
step.

The source's 203 tests, by where they land: 85 ported here; 57 with the macOS adapter in L2 (signature and
requirement checks, trusted and interpreter paths, the product probes, stop by signal, the verification cache);
36 with the tools and verbs in L3 (the HTTP and MCP surface, the admin CLI, the event log, the host credential);
4 with the supervisor in L4 (the watcher's wake-ups, the sweep); 7 with the identity migration at L5's import;
14 dropped with the private broker's own install, release, rollback and uninstall.

## Step L2: what was built

`observatory/engine/node/lifecycle/macos.py` and `observatory/engine/node/lifecycle/native_app.swift`, tested by
`observatory/engine/tests/test_node_lifecycle_macos.py` (suite `node_lifecycle_macos`, 73 cases: 50 ported, 23 new).

- **The designated requirement** (`requirement_for`): `anchor apple generic and identifier "<id>" and certificate
  leaf[subject.OU] = "<team>"`, built from a bundle id and a ten-character team that are validated first, never
  copied from the bundle or the registry. Enrollment (`MacAdapter.fingerprint('app', path)`, `inspect_app`) reads
  `codesign -d --verbose=2` and refuses an ad hoc or unsigned bundle, a signing identifier other than the
  Info.plist bundle id, and a bundle that changes while it is checked; it runs `codesign --verify --deep --strict
  -R =<requirement>` and records the bundle's own designated requirement for audit only. A same-team rebuild keeps
  matching; another team, another bundle id or a broken seal does not.
- **Trusted places** (`trusted_path`): the path is its own resolution; it and every folder above it belong to root or
  this account and nobody else may write them. `/Applications` as macOS ships it (`root:admin 0775`) is the one
  admin-writable parent an app may have, and Homebrew's `Cellar` the one a launchd job's interpreter may have. An app
  outside `/Applications` and `~/Applications` is `not_installed`.
- **Background jobs** (`launchd_fingerprint`): a LaunchAgent of this account, its label, the hash of its plist and of
  every file it runs; an interpreter behind a stable path (Homebrew's `opt` link, a sealed system folder) is pinned by
  that path, so `brew upgrade` is not a new identity. Start is `launchctl kickstart` (loaded) or `bootstrap` (not
  loaded); stop is `bootout` (SIGTERM, its ExitTimeOut, unloaded) or, on the owner's force, `kill SIGKILL`; a job the
  operator disabled is `held_off` and is never enabled here.
- **Process groups** (`Runner`): every tool runs with the system PATH only, in a group of its own
  (`osproc.new_group`), and the whole group is killed on a timeout or when the adapter closes. A `codesign` that times
  out is `verification_timeout`, one that cannot start `verification_unavailable`; neither is a false
  `identity_mismatch`.
- **The verification cache**: one daemon verifier runs the deep checks in turn, concurrent callers of one bundle wait
  on one check, and a caller waits only to its operation's deadline (`verification_pending`, then the result is
  cached). The key is a walk of the bundle's metadata (5 s budget; out of time is `verification_pending`). The cache
  holds at most 256 entries (`CACHE_LIMIT`, oldest dropped) and is persisted to an owner-only file that is ignored,
  not trusted, when it is readable by others or a link.
- **Readiness probes**: `process`; `fabric-service` (the well-known document names this service, instance, build
  and pid); `fabric-hub` (Fabric's `hub.json`, read only when it is an owner-only regular file, names the running pid
  and its loopback origin answers; the door token it names is never opened); `switchboard-runtime` (Switchboard's
  bundled CLI, checked against the enrolled team's requirement before it is run, reports its runtime online while the
  enrolled app runs). The registry now accepts the two profiles, each only on its product's app
  (`ai.passioncode.desktop`, `ai.passioncode.fabric-switchboard`) and with no field of its own; a caller may require
  the capability a profile proves (`fabric-hub`, `runtime-online`).
- **The helper** (`native_app.swift`): `status`, `open [--background]` (no activation; `--background` only for a
  target enrolled with `backgroundLaunch`), `quit <app> <pid>` (the app's own quit path) and `watch` (launch, terminate
  and activate events until stdin closes). The adapter keeps only the fields it knows from an answer. It runs the
  helper only while the helper's folder and the file are this account's, not links, and writable by nobody else.
  After a `quit`, only `quit_refused` falls through to SIGTERM; `identity_mismatch` (the pid is no longer the
  enrolled bundle's) signals nothing and is answered as such, and any other answer is `failed`.
- **Shapes it does not expect**: a `hub.json` whose `origin` is not a string is `hub_invalid`; a well-known document
  whose `service`, `process`, `build`, `surfaces` or `mcp` is not an object is read as empty (so it is
  `identity_mismatch`), and a `capabilities` that is not a list names none. None of them raises.
- **A disabled list it cannot read**: `launchctl print-disabled` is read once more when it fails; a second failure
  is `failed` with `reason: launchd_disabled_state_unknown`, never `held_off`, which would say the operator switched
  the job off.
- **Other systems**: `platform_adapter` returns `UnsupportedAdapter` off macOS; every call answers
  `unsupported_platform`, and a broker over it reports that, never a start.

### How the helper is built

- **Source install (built in L2).** `python3 observatory/engine/node/lifecycle/macos.py build-helper` (or
  `macos.build_helper()`) compiles `native_app.swift` with the `swiftc` and macOS SDK `xcrun` finds (Xcode or the
  Command Line Tools: `xcode-select --install`), with the system PATH only, into
  `<state>/node-lifecycle/bin/lifecycle-native`: an owner-only folder, the file replaced whole, mode 0700. On Apple
  silicon the linker signs it ad hoc. A `--dest` folder that is not this account's alone is refused, never re-moded,
  and the adapter runs the helper only while it is a regular file of this account that no one else may change. `macos.py helper` says where it is, whether it is built and its `state` (`current`, `outdated`, `not_built`, `unsafe` for a link or a file or folder others may change, `source_missing` when the engine's `native_app.swift` cannot be read), decided by the same ownership check the adapter applies (`macos.helper_safe`) before anything is run. Without it an app
  probe answers `setup_required` (`helper_not_built`) and a start or a stop is refused with `setup_required`; launchd
  targets need no helper.
- **The helper's version (OBS-222).** A helper built from an older `native_app.swift` than the engine ships is not
  run either. The source declares `let helperVersion = "..."`: the first 16 hex digits of the SHA-256 of the file with
  that value left empty (`macos.source_version`), and `--version` prints it (`{"state": "version", "version":
  ...}`) at once and touches nothing. Before it runs the helper the adapter asks it, with a 5 s timeout, and compares
  the answer with the version of the source beside it (`macos.expected_helper_version`). Another value, an
  `invalid_argument` from a build that predates the flag, a timeout or an answer of another shape is
  `setup_required` (`helper_outdated`), repaired by `full lifecycle build-helper`. A helper that fails the ownership
  check is `helper_unsafe` and is not asked; when the engine's own `native_app.swift` cannot be read no version is
  known, so no helper is asked and the reason is `helper_source_missing` (reinstall the engine). Known gaps are row
  OBS-249: a stop is gated like a start, a hanging helper costs its timeout on every call, and `watch` does not
  restart after a rebuild. A helper that answered the expected
  version is not asked again until its file changes; a mismatch is never remembered. The version cannot stay behind
  its source unnoticed: `test_the_source_declares_the_version_the_engine_expects` fails on any change to the Swift
  file until its declared value is the new digest, which the failure prints. `full lifecycle status` names an
  outdated helper beside the not-built one, and the `setup_required` hint of the six tools names both. Refusals: `swiftc_missing`, `helper_build_failed` (the compiler's last lines on `detail`),
  `unsupported_platform`.
- **Release (step L2r, built; see "Step L2r: what was built").** `macos/scripts/build-app.sh` compiles the same
  source for every architecture of the app (`swiftc -target arm64-apple-macos14` and `x86_64-apple-macos14`, joined with
  `lipo`), places it at `Project Observatory.app/Contents/Helpers/lifecycle-native`, and signs it with the release
  identity, the hardened runtime and the identifier `ai.passioncode.observatory.lifecycle-native` before it signs the app
  (nested code first), so `.github/workflows/release.yml` notarises it with the app. The adapter prefers the app's helper
  when the installed app satisfies `requirement_for(app_update.BUNDLE_ID, app_update.TEAM_ID)`, and falls back to a
  source-built one.

Notes for L3: the tools construct the broker over `macos.platform_adapter(cache_path=macos.default_cache())`;
`full lifecycle enroll` calls `adapter.fingerprint(kind, path)` and writes what it returns; `full lifecycle
build-helper` wraps `macos.main`. Notes for L4: the supervisor feeds `MacAdapter.watch` events to `note_activation` and calls
`stop_watch` and `close` when it stops.

The source's 203 tests, by where they land now: 85 in L1; 50 here; 36 with the tools and verbs in L3; 4 with the
supervisor in L4; 7 with the identity migration at L5's import; 21 dropped. The 21 are the 14 of the private broker's
own install, release, rollback and uninstall, and 7 that L1 counted for L2 but that test how the private broker chose
the interpreter of its own launchd plist: the engine's installer owns its plists (`tools/install_launchd.py`,
`lint_plist`), so they leave with that machinery. The adapter's own rule for a target's interpreter
(`stable_executable`) is ported.

## Step L2r: what was built

`macos/scripts/build-app.sh`, the macOS job of `.github/workflows/release.yml`, and the helper choice in
`observatory/engine/node/lifecycle/macos.py` (`MacAdapter._choose_helper`, `check_app_helper`, `helper_choice`), shown by
`full lifecycle status` and `full doctor`; tested by `observatory/engine/tests/test_node_lifecycle_helper.py` (suite
`node_lifecycle_helper`, 35 cases, every tool and the app faked, nothing compiled) and five cases of
`tests/test_keychain_and_app_scripts.py` (`TheAppCarriesTheLifecycleHelper`).

- **The build.** `build-app.sh` compiles `observatory/engine/node/lifecycle/native_app.swift` once per architecture in
  `ARCHS` (`xcrun --sdk macosx swiftc -O -target <arch>-apple-macos14`), joins the slices with `lipo` into
  `Contents/Helpers/lifecycle-native` (mode 0755) and stops when a slice is missing, like the app's own executable
  (PL-01). On a machine that can run one of its slices it asks the result `--version` and stops unless it answers the
  `helperVersion` its source declares. The unsigned CI build signs both ad hoc; with `OBSERVATORY_SIGN_IDENTITY` the
  helper is signed first with `--options runtime --timestamp --identifier ai.passioncode.observatory.lifecycle-native`,
  then the app, and `codesign --verify --strict --deep` checks the app with its nested helper.
- **The release proves it.** After notarisation the release's macOS job checks the stapled bundle against the
  requirements the adapter evaluates, with the team read from `TEAM_ID` in `app_update.py` (not the workflow variable):
  the app deep and strict for `ai.passioncode.observatory`, the helper for its own identifier. A release they refuse
  stops before its zip is packaged, so no release ships a helper no engine would run. Notarisation of nested code can be
  proven only by a release run; the first 0.30.0 run is that proof.
- **The choice.** The adapter built by the local server (`platform_adapter(cache_path=...)`, no helper injected) looks
  for the installed app on every choice (`app_update.installed_app`: `/Applications`, else `~/Applications`). Its helper
  runs when, cheapest first: the helper is a regular file at a trusted place (`trusted_path`: no link, every folder root's
  or this account's and no one else may write it, `/Applications` as macOS ships it the one exception); the app
  satisfies `requirement_for(app_update.BUNDLE_ID, app_update.TEAM_ID)`, deep and strict, through the same single-flight
  verifier and persisted cache as an enrolled app; the helper's own signature satisfies the requirement of its identifier
  under the same team; and only then it answers `--version` with the version of the source this engine ships (OBS-222).
  A bundle whose helper passed is not checked again until the metadata walk over the bundle (the helper included)
  changes; one refused for a lasting reason (another team, an outdated helper) is not checked again for
  `APP_REFUSAL_SECONDS` (300 s) unless it changes, and a pending or failed check is never remembered. Otherwise the source-built helper serves, as before (`helper_not_built`, `helper_unsafe`,
  `helper_source_missing`, `helper_outdated`), and the reason the app's was not used is kept: `app_not_installed`,
  `app_helper_missing` (an app older than 0.30.0; no `codesign` runs), `app_untrusted`, `app_signature_invalid` (a
  local QA build signed ad hoc, or another team), `app_helper_signature_invalid`, `app_verification_pending`,
  `app_verification_failed` or `app_helper_outdated` (the app and the engine are different releases).
- **One call, one choice.** Each call (probe, start, stop, the watcher) runs the helper its own choice returned
  (`MacAdapter._choose_helper`, a `HelperChoice`), never one read back from the adapter afterwards, so a call never runs
  a helper another call checked or refused at the same time. `last_choice` is kept for reports and tests only.
- **How long a call waits.** When a source-built helper could serve, the app's first deep check holds a call at most
  `APP_CHECK_GRACE_SECONDS` (2 s) and the source helper serves if it has not landed; the check finishes in the
  background and the next call takes the app's helper. With no source helper the deep check may hold the call up to
  `VERIFY_WAIT_SECONDS`, as an enrolled app's check does. Once the deep check passed, the helper's own plain signature
  check (one small file, bounded by `PLAIN_VERIFY_TIMEOUT`, 60 s) and its `--version` (`HELPER_VERSION_TIMEOUT`, 5 s)
  run in the call, once per bundle state. Deciding whether to wait asks the source helper `--version` once, and nothing
  else.
- **What says which one runs.** A `setup_required` probe carries `appHelper` (the app's reason) beside `reason` (the
  source helper's). `full lifecycle status` prints whether apps are driven by the helper inside the signed app or by
  the source build and why, names the build-helper repair only when neither can run, and its JSON carries
  `helperInUse` (`macos.helper_choice`: `inUse`, `path`, `app {path, state, reason}`, `source {path, state}`); with
  `--no-probe`, or with nothing enrolled, it checks nothing (`checked: false`) and runs no `codesign`. `full doctor`
  carries the same as `lifecycle_helper` once `config/node-lifecycle.json` enrolls a target, and `{"state": "inert"}`
  before (no document, or one with no target); a document the tools refuse is `unknown` with its code. `macos.py helper` adds `choice`.
- **Injected helpers stay pinned.** An adapter given a `helper` looks at no installed app unless it is also given an
  `app` (`MacAdapter(app=...)`: a path, `INSTALLED` or None), so the adapter's tests never reach an app of the machine
  that runs them.

Not in this step: the first release run that notarises and checks the nested helper (0.30.0); a source install keeps
`full lifecycle build-helper`.

## Step L3: what was built

`observatory/engine/node/lifecycle/service.py` (the door), `observatory/engine/tools/lifecycle_cli.py` (the verbs under
`full lifecycle`) and the `POST /mcp` route of `observatory/engine/tools/serverd.py`, tested by
`observatory/engine/tests/test_node_lifecycle_service.py` (suite `node_lifecycle_service`, 52 cases: 28 ported, 24 new).

- **One server, one port, no token files.** The six tools are served at `POST /mcp` on the engine's local server
  (127.0.0.1:47311 by default), the only route there that changes anything. A caller proves itself with the bearer of an
  access binding of this workspace's audience (`full access-binding issue`; hashed at once, never logged or passed on),
  and the binding's principal is the caller. What it may do is a second grant, `callers.<principal>` in
  `config/node-lifecycle.json`: `{bindingId, revision, revoked, allowedTools, allowedTargets, operatorProxy?}`. The
  grant names ONE binding (`full lifecycle allow PRINCIPAL --binding ID`): a binding's scopes, projects and effect
  ceiling are memory's and stop at `propose`, while stopping an app is destructive, so lifecycle is a separate grant
  outside the binding's ceiling ([ACCESS-BINDING.md](ACCESS-BINDING.md#a-binding-outside-memory-nodelifecycle)) and a
  second binding of the same principal, issued to read memory, opens nothing here. No binding is 401 with the command
  that issues one, a binding without a grant or not the bound one 403, the descriptor's service token (which opens the
  events feed) 401. `BindingAuthority.grant` asks again for both the grant and the bound binding at admission and before
  every effect, so revoking either refuses the next call and stops an admitted start before it launches.
- **No owner over the wire.** A grant cannot name an owner, a token or its hash (the schema refuses the whole document);
  enrolling, holding a target off and forced stops are the operator's at a terminal, and `operatorProxy` (may hold an
  always_on target off) is the most a caller holds. So the broker's owner-only branches (`*` targets, force, the person's
  raw input-idle time) are unreachable from MCP in this step.
- **`config/node-lifecycle.json`**, owner-only, closed schema: the broker's registry under `targets` and the grants under
  `callers`. It is read on every call; a document others may read, a link, or one that fails the schema refuses every call
  (`unsafe_config`, `invalid_config`). The broker's store is `<state>/node-lifecycle/` as planned.
- **Inert until enrolled.** `lifecycle.Host` builds the broker over `macos.platform_adapter(cache_path=macos.default_cache())`
  only once `config/node-lifecycle.json` exists (at the server's start or its next heartbeat); before that no state is
  created and `/mcp` answers 403 `nothing is enrolled`. A start that fails, whatever raised it (a corrupt or locked
  `lifecycle.db` raises `sqlite3.DatabaseError`), is a `lifecycle: unavailable, <class>` row in the well-known
  `degraded` and a 503 on `/mcp`, never a crash of the server or of its heartbeat thread: `Host.service`, the server's
  start and every heartbeat catch it. The broker releases its instance lock when its construction fails at any point
  (the store's open, recovery or pruning), so the next attempt starts once the store is repaired instead of answering
  `already_running` forever; when recovery or pruning fails it first ends the adapter's children and waits for any
  recovery worker already running, then closes the store under its lock, so the next broker never recovers an
  operation this one is still acting on. Deleting `config/node-lifecycle.json` after a failed start clears the fault:
  `/mcp` answers 403 `nothing is enrolled` again, not a stale 503. Off macOS the adapter answers `unsupported_platform` to everything.
- **The MCP surface**: `initialize` (with the `instructions` text: how to wake an app, renew the lease every 10 minutes,
  what `in_use_by_person`, `held_off`, `denied` and `unsupported_platform` mean; neutral, at most 1,800 characters),
  `ping`, `tools/list` (only the tools the caller's grant names; stop and restart carry `destructiveHint`) and
  `tools/call`. A malformed argument is `isError` with `invalid_argument`; any other refusal is a typed answer `{error,
  hint}` naming the command that changes it. Results carry `_meta.traceparent`, a child of the caller's.
- **The well-known document** declares `surfaces.mcp` (`/mcp`, `streamable-http`, `auth: own`, contract DEC-0024) and an
  extension keyed by this document's URL: `{service: node.lifecycle, lifecycle: {manager: node}, path, capabilities,
  platform, supported, state: inert | serving | failed}`. Two decisions here. The surface carries no `capabilities`
  list, because the manifest is that list's authority (the conformance probe's `interop.well-known-capabilities`) and
  the six tools are not manifest capabilities yet; the entry names them. And `lifecycle.manager: node` is in the entry,
  not in the server's descriptor: in the contract (DEC-0057, DEC-0061) a descriptor's `lifecycle.manager` names the
  supervisor of THAT service, the server is supervised by launchd, `label` and `plist` are refused beside `node`, and
  the vendored fabric-service kit (`fabric_service.validate_descriptor`, pinned bytes) does not admit `node` yet. The
  descriptors of services the node itself starts as children will say `node` when the node starts children.
- **What happened**: one sentence per admitted operation and per result in `<state>/node-lifecycle/events.jsonl` (it names
  the app, and why when it failed; a correct refusal wakes nobody, a failure sets `notify`; appends are serialized), and
  one structured line in `<state>/node-lifecycle/logs/lifecycle.jsonl` (5 x 5 MB, owner-only). `full lifecycle status`
  shows the last ten. They are not in the server's `/fabric/v1/events` feed yet, which is a view over the tick's store;
  joining them comes with the supervisor (L4), whose gave-up event is the one a host must be told.
- **The verbs**: `status` (targets with a live probe that starts nothing, callers with their bindings in force, the
  helper, the last events), `enroll` (an absolute path the operator names, `adapter.fingerprint(kind, path)`, which
  verifies an app's signature; the operator's `--startup-effect` sentences; a new `policyRevision` past the old one),
  `allow` (creates or changes a grant; `--remove`; `--operator-proxy on|off`; never the wildcard; an unenrolled target
  is refused, and so is a `--binding` that is revoked, expired, another principal's or another workspace's, which the
  runtime would refuse), `policy` (`--desired`, `--idle-minutes`, `--background-launch`, `--readiness`), `hold [--release]`,
  `revoke`, and `build-helper` (`macos.main`). Every verb that changes the document needs a terminal, like
  `full access-binding`, and edits under a lock (`<state>/node-lifecycle/config.lock`). The terminal check is the
  engine's existing convention (`sys.stdin.isatty()`): it keeps an agent's ordinary tool calls out, not a process that
  allocates a pseudo-terminal on purpose; the owner-only document is the boundary that holds against that.

Notes for L4: the supervisor runs inside the server (`lifecycle.Host`) and must also join the event log into the events
feed; the Health page and `observatory_machine` show each target (acceptance below). The `unenroll` verb with its
retired revisions, which this note gave L5, is row OBS-251. The release build of the helper inside the signed app was its own pull
request, step L2r (built since); a source install with no signed app runs `full lifecycle build-helper`.

The source's 36 tests for this step, by where they went: 28 ported here (21 with neutral fixtures and a binding's bearer in place of the broker's token, 7
adapted to the engine: the owner's token and the tools it gained at start, the host's token twice, the disposable log
folder, the reserved names, re-enrolling); 5 wait for the `unenroll` verb
(OBS-251); 3 are the server's own behaviour, already tested where it lives: a second copy exits 75 and touches nothing
(`tests/test_fabric_service.py`), the launchd output files are rotated (`serverd.rotate_own_logs`), a client that went
away is not a server failure (`tests/test_serverd.py`). 24 new cases cover the binding authority (the bound binding only, rebinding,
revocation before the effect), the unsupported platform, the inert host, a damaged store (a degraded row, the server up,
the lock released), the config document's refusals and the verbs the source did not have.

## Step L4: what was built

`observatory/engine/node/lifecycle/supervisor.py`, started by the local server's `lifecycle.Host`, tested by
`observatory/engine/tests/test_node_lifecycle_supervisor.py` (suite `node_lifecycle_supervisor`, 29 cases) and seven
cases of `tests/test_node_lifecycle_service.py` (`SupervisionTests`).

- **Where it runs.** In the engine's local server, as planned ("What changes on the way in", item 3): `Host.service()`
  builds the service and calls `Service.start_supervision()`; `Host.close()` stops it with the service, and
  `Service.close()` stops the supervisor before the broker, so no pass starts on a closing store. A service built
  anywhere else (a test, a terminal) runs no pass of its own. A supervisor that cannot start is a degraded row
  (`lifecycle: supervisor unavailable, <class>`) while the tools keep serving. `start` is idempotent and the broker
  holds the state folder's exclusive lock, so two supervisors never act over one state folder.
- **The cadence.** One thread (`lifecycle-supervisor`) passes once at its start, then waits on a wake-up event for
  `interval()`. The safety sweep has a monotonic deadline of its own, every 600 s (`SWEEP_SECONDS`), checked at every
  wake-up whatever caused it, so a stream of launch and quit events never postpones it. The sweep reads the enrolled
  bundles again, runs the warm-up and `Broker.prune` (the store was pruned only when a broker opened; a server runs for
  weeks), starts the watcher again if it ended, then passes over every target. Before the deadline the loop wakes every
  60 s (`IDLE_CHECK_SECONDS`) only while an idle lease can run out (`Broker.idle_launches`: a launch of the broker's,
  not taken over by the person, of an approved target with `idleStopMinutes` that is not held off), and that wake-up
  passes over those targets only; a launch the idle pass never looks at (an on_demand target without idle minutes, a
  held-off or unapproved one, an adopted one) never holds the loop at one minute. A wake-up from an event passes only
  over the targets that event named. The waits run on the monotonic clock, which macOS pauses while the machine
  sleeps: after a wake the next sweep can come up to about 10 minutes later.
- **The watcher.** The adapter's `watch` runs on its own thread (`lifecycle-watch`). An `activated` event of an enrolled
  bundle goes to `Broker.note_activation` for every target of that bundle (the broker ignores one inside the 20 s
  launch grace); a `launched` or `terminated` event wakes a pass over those targets at once. Events are matched against
  the enrolled bundles read at the last sweep, so an event of another app reads no file; an app enrolled since is
  matched from the next sweep. A watcher that ends (no
  helper built, a helper built from older source as OBS-222 makes `watch` return, a crash past its own backoff) or
  raises is started again by the next sweep, never in a tight loop, and is a log line (`supervision.watch_ended`).
  Until it runs again, activations are not seen; the presence check at a stop (frontmost with input in the last
  minute) still protects the person.
- **The warm-up.** On its start and on each sweep it asks the adapter whether each enrolled target's identity still
  matches, with a deadline of now: the macOS adapter queues the deep `codesign` check and answers
  `verification_pending` at once, so the result is cached before an agent's first call instead of inside it.
- **It decides nothing.** Every rule stays the broker's: `in_use_by_person`, `held_off`, leases, the takeover grace,
  the crash circuit and the restart budget. The cases over the real broker show the supervisor admits an idle stop
  only when the broker would, never stops an app the person is in front of or took over, never starts a held-off
  target, and brings an always_on target back on the sweep.
- **A pass that fails is one pass.** An exception in `supervise`, `prune`, the warm-up, an activation or the watcher is
  caught; every one is a log line by its class (`supervision.pass_failed`, never its message, which may carry a
  path), and the first failed pass after a good one is one `lifecycle.supervision_failed` event with `notify`, not one
  a minute. Once the supervisor is stopping, a pass cut short by the closing store is not a failure. The event is in
  `events.jsonl` and `full lifecycle status`; no host is told until OBS-250 joins the events feed.
- **Other systems.** Over the adapter that answers `unsupported_platform` (Linux and Windows) the supervisor runs no
  pass, no warm-up and no watcher; the sweep only keeps the store pruned.
- **The identity migration** is not in this step: the engine's registry admits only `designated-requirement/1`
  identities (`broker.app_identity` refuses any other record), so a legacy record can exist only in the private
  broker's state, and its migration runs inside L5's import, before the imported document is written (built:
  `importer.migrate_identity`, "Step L5: what was built").
- **Not in this step** (OBS-250): the lifecycle event log joining the server's `/fabric/v1/events` feed, which is a view
  over the tick's store with a rowid cursor and needs a cursor that orders two sources, and the Health page and
  `observatory_machine` showing each target (acceptance below).

The source's four supervisor cases (the watcher's wake-ups, the sweep) are not in this repository; their behaviours are
covered by new cases here (`test_a_launch_or_termination_wakes_a_pass_over_that_apps_targets_only`,
`test_an_activation_goes_to_the_broker_for_every_target_of_that_bundle`,
`test_the_sweep_warms_prunes_and_passes_over_every_target`,
`test_an_always_on_target_that_exited_is_started_again_by_the_sweep`).

## Step L5: what was built

`observatory/engine/node/lifecycle/importer.py` and the `import` verb of `observatory/engine/tools/lifecycle_cli.py`,
tested by `observatory/engine/tests/test_node_lifecycle_import.py` (suite `node_lifecycle_import`, 47 cases: the 7
identity-migration cases of the source, adapted to an import, and 40 new). This is the engine side only: nothing in
it switches the private broker off.

```sh
project-observatory full lifecycle import --from FOLDER --dry-run    # what would change; writes nothing
project-observatory full lifecycle import --from FOLDER              # terminal only; one write, journaled
```

- **What it reads.** The private broker's state folder holds `registry.json` (`{schemaVersion: 1, targets}`, the
  target record this engine's registry validates, with callers named by their bare names and its `owner` always
  among them), `callers.json` (`{schemaVersion: 1, callers: {name: {tokenHash, revision, revoked, owner?, host?,
  operatorProxy?, allowedTools, allowedTargets}}}`, the token hash being the SHA-256 hex of the bearer) and
  `lifecycle.sqlite3`. Of the store only two counts are read: operations still running and processes it started that
  the person has not taken over. No row of it is imported: the engine's store is its own (`<state>/node-lifecycle/`)
  and another broker's operation ids mean nothing here.
- **Read-only on the source.** `--from` is absolute; the folder must be a real folder of this account that nobody
  else may change, with no link on the way (`check_folder`). Each document is opened through `broker.private_json`
  (owner-only regular file, no link followed). The store is not opened in place: even a `mode=ro` connection to a
  database in WAL mode creates `-shm` beside it, so the store and its write-ahead log are read as bytes into a private
  temporary copy, queried there `mode=ro` with `query_only`, and the copy is removed. A live broker's last writes are
  in its log, so they are counted. The two files are read one after the other, so a checkpoint between the reads
  could pair them inconsistently: both are stamped (inode, size, times) before and after, a copy whose stamps moved
  is taken again once, and a second that moved leaves the counts `degraded`. So does a store whose `meta` version is
  not 1 (never guessed at) and a write-ahead log others may read. A case holds the source folder's bytes, modes and
  times unchanged across an import while the source's writer keeps its rows in the log.
- **Through the engine's own validation, item by item.** Each target is checked alone by `broker.validate_registry`
  (its dependency list too: ids, at most 16), then against its dependencies: the targets on a dependency cycle are
  refused `invalid_dependencies` and a target depending on a refused one `dependency_refused`, the others come; each grant by the rules of `config/node-lifecycle.json`. A refusal is one item with
  a code (`invalid_registry`, `startup_effects_required`, `no_callers`, `caller_unmapped`, `dependency_refused`,
  `reenroll_required` with its reason, `already_enrolled`; for grants `invalid_grant`, `wildcard_refused`,
  `unknown_tool`, `already_granted`), and the rest is validated whole (`service.validate_config`) and written in one
  replacement of the owner-only document, under the operator's lock. A write that fails leaves the document and the
  journal as they were. The exit code is 1 when any item was refused.
- **Idempotent.** A target or grant the engine already holds with the same content is `unchanged`; one it holds with
  other content is refused and never overwritten. A second run writes nothing, journals nothing and says
  `changed: false`; `--dry-run` reports `wouldChange` and writes nothing.
- **always_on comes over as on_demand.** The local server rereads the document on every call and its supervisor
  (L4) acts on it at once, while the private broker runs until the operator switches it off last. Each would take
  the other's process for the person's, so a crashed always_on job would be restarted twice, under two restart
  budgets. So an always_on target is imported as on_demand, and the report names `full lifecycle policy TARGET_ID
  --desired always_on` for each, to run once the private broker is off. `--keep-always-on` keeps the state and says
  why it is risky until then.
- **Callers.** A source name becomes the principal `agent:<name>` (or itself when it already is a principal);
  `--principal NAME=PRINCIPAL` names another. The source's `owner` and its Fabric host credential are not callers
  here (the operator's authority is a terminal; Fabric reaches the local server through its own descriptor): both are
  `skipped` with a code and dropped from the targets' callers, and so is a revoked grant.
- **No token hash is copied.** The engine's grants hold no hash: each names one access binding (step L3). The
  source's hash is the same scheme as a binding's credential (`bearer-sha256`), so it is only compared: a grant is
  imported when a binding in force for this workspace, of that principal, carries that very digest. Otherwise the
  caller is `reissue_required` and the report prints the two commands, `full access-binding issue` and `full
  lifecycle allow` with its tools and targets. A grant keeps the targets that came and names the ones that did not
  (only valid ids are named; any other string of the source is counted, never echoed).
- **The identity migration** (the L4 note). A legacy app record (no `scheme`, a `codeHash`) is verified now, by the
  same check enrollment runs (`macos.inspect_app`): an unchanged bundle (its metadata hash equals the enrolled one)
  is adopted under its own signing team; a changed one only under a team the operator names with `--trusted-team`
  (none by default); a changed bundle id never. A migrated target's `policyRevision` goes up by one. Bundles are
  verified once, before the operator's lock is taken, and the plan made under the lock replays the answers. Off
  macOS a legacy record is `reenroll_required` (`unsupported_platform`). A `switchboard-runtime` or `fabric-hub`
  readiness that repeats its profile's own capability loses that field, which the engine's profiles do not carry.
- **The report and the journal** name target ids, callers, counts and codes, and the one folder the operator gave:
  never an app's path, a signing team, a hash or a token. A write appends one `lifecycle.imported` event to
  `<state>/node-lifecycle/events.jsonl` (shown by `full lifecycle status`).

What the operator runs next, on the machine that uses the private broker: `import --dry-run`, then `import`; issue a
binding to each caller the report names and `allow` it as printed; enroll again any `reenroll_required` target;
`full lifecycle status`; point each agent's MCP client at `POST http://127.0.0.1:47311/mcp` with its binding's bearer
and check one `lifecycle.list`; then switch the private broker off with its own uninstall, and only then turn each
target the report names back to always_on with `full lifecycle policy TARGET_ID --desired always_on`. Processes the private
broker started are, from then on, the person's to the node: it never idles them out.

Not in this step: the `unenroll` verb (OBS-251), which removes a target while the local server runs and needs a door
of the broker's own for its store rows.

Known limits, from the review of #369: if the source broker removes its write-ahead log between the stamp and the read,
the import reports the generic "cannot be read" reason instead of retrying; and importing again after the operator has
turned a target back to always_on reports it `already_enrolled`, with no hint that the difference is that policy.

## Acceptance

- The 182 behaviours of the broker's tests that move pass here, with neutral fixtures (the other 21 test
  the private broker's own install, release, rollback and uninstall and the interpreter of its own plist, which the
  engine's install replaces).
- On a test Mac: an enrolled app is woken, renewed, left alone while the person uses it, stopped after
  its idle minutes, and an always_on one comes back after a quit and gives up after three failed starts.
- `observatory_machine` and Health show each target's state and the last operation per caller.
- No file in this repository names an enrolled app, a caller, a signing team or a path of the machine
  the broker came from (the privacy gate before every push).
