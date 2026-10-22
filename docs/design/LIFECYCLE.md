# `node.lifecycle`: the port plan (OBS-79)

Status: plan, 2026-10-22; steps L1 and L2 built (see "Step L1: what was built" and "Step L2: what was built"). Decided by the operator on 2026-10-12 (organization architecture v4, D5): the
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
| `native_app.swift` | 103 lines | the NSWorkspace helper (status, open, quit, an event stream of launch, terminate and activate) | none | `node/lifecycle/native_app.swift`; a source install compiles it with `swiftc` (L2), and the desktop app's release workflow is to build it into the app and sign it with the app (see "How the helper is built") |
| `lifecycle.py` | 1,952 lines | the supervisor (always_on, idle stop, a 600 s safety sweep, 60 s while a started app runs), the MCP handler and its `instructions` text, the event log, the private log file handler, the admin CLI verbs, identity migration | its fixed port and launchd label, its own install, release, rollback and uninstall machinery (the engine's install and update replace it), its own credential files (the engine's access bindings replace them), and the one trusted signing team its migration names (a configuration value here, empty by default) | `node/lifecycle/service.py` (supervisor and MCP), `tools/lifecycle_cli.py` (verbs under `full lifecycle`) |
| `selftest.py` | 117 lines | a disposable broker on an ephemeral loopback port, authenticated MCP, the second-instance exit | none | a test in `tests/test_node_lifecycle_service.py` |
| `vendor/` | 2 files | none: the engine has its own `fabric_service.py` and Fabric interop | the vendored copies and their provenance | dropped |
| `acceptance.json` | 181 lines | none | one machine's acceptance run: process ids, real clients, its apps | stays |
| `README.md`, `AGENTS.md`, its module page and runbook | about 1,000 lines | the contract and the operator rules, rewritten here | the private gateway, its ports and callers, the list of enrolled apps, incident notes naming them | this document and a section of the operator's guide |
| tests, four files | 203 tests | every behaviour above | real signing teams, caller names and home paths in fixtures, replaced by neutral ones (`ABCDE12345`, `caller-a`, temporary folders) | `tests/test_node_lifecycle*.py` |
| runtime state (its application-support folder, logs, credential files) | n/a | none | everything: enrollments, policies, token hashes, the SQLite store, events | stays on that machine; imported only by the operator's own command (step L5) |

## What changes on the way in

1. **One service, not two.** The tools are served by the engine's existing local server
   (`tools/serverd.py`) under its access bindings, not by a second process on its own port with its own
   token files. The service descriptor says `lifecycle.manager: node`, and the well-known endpoint lists
   a `node.lifecycle` service entry.
2. **The workspace store.** Operations, receipts, the journal and circuits live in a SQLite file of the
   module's own under the workspace's state folder, `<state>/node-lifecycle/lifecycle.db` (owner only),
   under the same retention rules (receipts 24 h, terminal operations 7 days, failed always_on starts
   30 minutes). Not tables in `observatory.db`: that database is the tick's, rebuilt and migrated by its
   schema, while these rows are an effect journal a long-running server writes synchronously, and the
   meter's ledger (`<state>/meter/meter.db`) set the same precedent. Enrollments and policies live in `config/node-lifecycle.json`, owner
   only. The name differs from `config/lifecycle.json`, which configures the lifecycle watch
   (`collectors/scan_lifecycle.py`), a different thing.
3. **The supervisor runs inside the node service** (OBS-198, slice 6) once that exists; until then it
   runs inside the engine's local server, which launchd already keeps up.
4. **macOS first.** The adapter is NSWorkspace and `codesign`. Linux (systemd user units and
   `.desktop` launches) and Windows (the service manager and `ShellExecute`) are later adapters behind
   the same broker; until they exist the tools answer `unsupported_platform` there, never a silent
   success.
5. **Identity migration** keeps its rule (an unchanged bundle adopted under its own team; a changed
   one needs the owner) with the trusted teams read from configuration.

## Pull requests, in order

| Step | What | Release |
|---|---|---|
| L1 | `node/lifecycle/broker.py` with the registry, authority, operations, receipts, journal, circuits and restart budget, and the one-pass supervision decision (`supervise`), against a fake adapter; the broker's tests for these ported with neutral fixtures. **Built**, pull request #342 | 0.29.0 |
| L2 | `node/lifecycle/macos.py` and the Swift helper, compiled by `swiftc` for a source install; the adapter tests (signature parsing, requirement text, process groups). **Built**, pull request #344. The helper's release build inside the signed app is its own pull request before L3's tools are served | 0.29.0 |
| L3 | the six MCP tools in the engine's server, the well-known service entry, `lifecycle.manager: node`, the `instructions` text, and `full lifecycle enroll / allow / policy / hold / revoke / status` | 0.29.0, by 2026-10-31 |
| L4 | the supervisor: the thread that runs `supervise` on its cadence (a 600 s safety sweep, 60 s while a started app runs), the watcher's activation events fed to `note_activation`, and the identity migration if it does not run at L5's import | 0.30.0 |
| L5 | `full lifecycle import --from PATH`: the operator imports their own enrollments and policies from the private broker's state folder on the same machine (read locally, never committed); then the private broker is switched off and its launchd job removed by its own uninstall | 0.30.0, by 2026-11-07 |

Each step is its own pull request with an adversarial and a confirming review round, like every node
slice. The private broker keeps running until L5's import is verified on the machine that uses it, so
agents never lose the tools between steps.

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
  `timeout` before it looks, and a start recovered after its effect is still recorded as the broker's.
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
  lock.
- **The adapter** (`Adapter`, a protocol): `identity_matches(target, *, deadline)`, `probe`, `start`,
  `stop(target, pid, *, force, mode)` and an optional `close`. The tests drive two fakes, a durable file seam
  and a process seam; no installed application is touched.

Notes for L4: the store is pruned only when a broker opens (`prune`); the supervisor's sweep should prune
too. An activation inside the 20 s launch grace is ignored by `note_activation`; the presence check at stop
(frontmost with recent input) still protects the person then, and the watcher may deliver a later activation.

What L1 does not do yet: no MCP tool, CLI verb or event log (L3), no thread runs `supervise` (L4), no adapter
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
  and activate events until stdin closes). The adapter keeps only the fields it knows from an answer.
- **Other systems**: `platform_adapter` returns `UnsupportedAdapter` off macOS; every call answers
  `unsupported_platform`, and a broker over it reports that, never a start.

### How the helper is built

- **Source install (built in L2).** `python3 observatory/engine/node/lifecycle/macos.py build-helper` (or
  `macos.build_helper()`) compiles `native_app.swift` with the `swiftc` and macOS SDK `xcrun` finds (Xcode or the
  Command Line Tools: `xcode-select --install`), with the system PATH only, into
  `<state>/node-lifecycle/bin/lifecycle-native`: an owner-only folder, the file replaced whole, mode 0700. On Apple
  silicon the linker signs it ad hoc. A `--dest` folder that is not this account's alone is refused, never re-moded,
  and the adapter runs the helper only while it is a regular file of this account that no one else may change. `macos.py helper` says where it is and whether it is built. Without it an app
  probe answers `setup_required` (`helper_not_built`) and a start is refused with `setup_required`; launchd targets
  need no helper. Refusals: `swiftc_missing`, `helper_build_failed` (the compiler's last lines on `detail`),
  `unsupported_platform`.
- **Release (next, its own pull request before L3's tools are served).** `macos/scripts/build-app.sh` compiles the
  same source for both architectures (`swiftc -target arm64-apple-macos14` and `x86_64-apple-macos14`, joined with
  `lipo`), places it at `Project Observatory.app/Contents/Helpers/lifecycle-native`, and signs it with the release
  identity and the hardened runtime before it signs the app (nested code first), so `.github/workflows/release.yml`
  notarises it with the app. The adapter then prefers the app's helper when the installed app satisfies
  `requirement_for(app_update.BUNDLE_ID, app_update.TEAM_ID)`, and falls back to a source-built one. It is not in this
  step because a nested executable's notarisation can be proven only by a release run, and the adapter must check the
  app's signature before it runs a helper from inside it.

Notes for L3: the tools construct the broker over `macos.platform_adapter(cache_path=macos.default_cache())`;
`full lifecycle enroll` calls `adapter.fingerprint(kind, path)` and writes what it returns; `full lifecycle helper
build` wraps `macos.main`. Notes for L4: the supervisor feeds `MacAdapter.watch` events to `note_activation` and calls
`stop_watch` and `close` when it stops.

The source's 203 tests, by where they land now: 85 in L1; 50 here; 36 with the tools and verbs in L3; 4 with the
supervisor in L4; 7 with the identity migration at L5's import; 21 dropped. The 21 are the 14 of the private broker's
own install, release, rollback and uninstall, and 7 that L1 counted for L2 but that test how the private broker chose
the interpreter of its own launchd plist: the engine's installer owns its plists (`tools/install_launchd.py`,
`lint_plist`), so they leave with that machinery. The adapter's own rule for a target's interpreter
(`stable_executable`) is ported.

## Acceptance

- The 182 behaviours of the broker's tests that move pass here, with neutral fixtures (the other 21 test
  the private broker's own install, release, rollback and uninstall and the interpreter of its own plist, which the
  engine's install replaces).
- On a test Mac: an enrolled app is woken, renewed, left alone while the person uses it, stopped after
  its idle minutes, and an always_on one comes back after a quit and gives up after three failed starts.
- `observatory_machine` and Health show each target's state and the last operation per caller.
- No file in this repository names an enrolled app, a caller, a signing team or a path of the machine
  the broker came from (the privacy gate before every push).
