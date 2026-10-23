# `node.lifecycle`: the port plan (OBS-79)

Status: plan, 2026-10-22; steps L1, L2 and L3 built (see "Step L1: what was built", "Step L2: what was built" and
"Step L3: what was built"). Decided by the operator on 2026-10-12 (organization architecture v4, D5): the
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
| L3 | the six MCP tools in the engine's server, the well-known service entry, `lifecycle.manager: node`, the `instructions` text, and `full lifecycle enroll / allow / policy / hold / revoke / status`. **Built**, pull request #347 | 0.29.0, by 2026-10-31 |
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
  `already_running` forever. Off macOS the adapter answers `unsupported_platform` to everything.
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
  is refused), `policy` (`--desired`, `--idle-minutes`, `--background-launch`, `--readiness`), `hold [--release]`,
  `revoke`, and `build-helper` (`macos.main`). Every verb that changes the document needs a terminal, like
  `full access-binding`, and edits under a lock (`<state>/node-lifecycle/config.lock`). The terminal check is the
  engine's existing convention (`sys.stdin.isatty()`): it keeps an agent's ordinary tool calls out, not a process that
  allocates a pseudo-terminal on purpose; the owner-only document is the boundary that holds against that.

Notes for L4: the supervisor runs inside the server (`lifecycle.Host`) and must also join the event log into the events
feed; the Health page and `observatory_machine` show each target (acceptance below). For L5: the `unenroll` verb with its
retired revisions comes with the import. The release build of the helper inside the signed app is still its own pull
request; until it lands, a source install runs `full lifecycle build-helper`.

The source's 36 tests for this step, by where they went: 28 ported here (21 with neutral fixtures and a binding's bearer in place of the broker's token, 7
adapted to the engine: the owner's token and the tools it gained at start, the host's token twice, the disposable log
folder, the reserved names, re-enrolling); 5 wait for the `unenroll` verb
(L5); 3 are the server's own behaviour, already tested where it lives: a second copy exits 75 and touches nothing
(`tests/test_fabric_service.py`), the launchd output files are rotated (`serverd.rotate_own_logs`), a client that went
away is not a server failure (`tests/test_serverd.py`). 24 new cases cover the binding authority (the bound binding only, rebinding,
revocation before the effect), the unsupported platform, the inert host, a damaged store (a degraded row, the server up,
the lock released), the config document's refusals and the verbs the source did not have.

## Acceptance

- The 182 behaviours of the broker's tests that move pass here, with neutral fixtures (the other 21 test
  the private broker's own install, release, rollback and uninstall and the interpreter of its own plist, which the
  engine's install replaces).
- On a test Mac: an enrolled app is woken, renewed, left alone while the person uses it, stopped after
  its idle minutes, and an always_on one comes back after a quit and gives up after three failed starts.
- `observatory_machine` and Health show each target's state and the last operation per caller.
- No file in this repository names an enrolled app, a caller, a signing team or a path of the machine
  the broker came from (the privacy gate before every push).
