# `node.lifecycle`: the port plan (OBS-79)

Status: plan, 2026-10-22. Decided by the operator on 2026-10-12 (organization architecture v4, D5): the
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
| `native_app.swift` | 103 lines | the NSWorkspace helper (status, open, quit, an event stream of launch, terminate and activate) | none | `node/lifecycle/native_app.swift`, built by the desktop app's release workflow and signed with it; a local `swiftc` build only for a source install |
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
2. **The workspace store.** Operations, receipts, the journal and circuits live in the workspace's
   SQLite store under the same retention rules (receipts 24 h, terminal operations 7 days, failed
   always_on starts 30 minutes). Enrollments and policies live in `config/node-lifecycle.json`, owner
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
| L1 | `node/lifecycle/broker.py` with the registry, authority, operations, receipts, journal, circuits and restart budget, against a fake adapter; the broker's tests for these ported with neutral fixtures | 0.29.0 |
| L2 | `node/lifecycle/macos.py` and the Swift helper, built and signed by the app's release workflow; the adapter tests (signature parsing, requirement text, process groups) | 0.29.0 |
| L3 | the six MCP tools in the engine's server, the well-known service entry, `lifecycle.manager: node`, the `instructions` text, and `full lifecycle enroll / allow / policy / hold / revoke / status` | 0.29.0, by 2026-10-31 |
| L4 | the supervisor: always_on, idle stop, takeover through the watcher, the presence check at stop | 0.30.0 |
| L5 | `full lifecycle import --from PATH`: the operator imports their own enrollments and policies from the private broker's state folder on the same machine (read locally, never committed); then the private broker is switched off and its launchd job removed by its own uninstall | 0.30.0, by 2026-11-07 |

Each step is its own pull request with an adversarial and a confirming review round, like every node
slice. The private broker keeps running until L5's import is verified on the machine that uses it, so
agents never lose the tools between steps.

## Acceptance

- The 203 behaviours of the broker's tests pass here, with neutral fixtures.
- On a test Mac: an enrolled app is woken, renewed, left alone while the person uses it, stopped after
  its idle minutes, and an always_on one comes back after a quit and gives up after three failed starts.
- `observatory_machine` and Health show each target's state and the last operation per caller.
- No file in this repository names an enrolled app, a caller, a signing team or a path of the machine
  the broker came from (the privacy gate before every push).
