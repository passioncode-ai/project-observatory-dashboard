# The machine utility: one node per machine (OBS-OE-00)

status: accepted

Date: 2026-10-26. Owner: this repository. Row: [OBS-76](../backlog.md), packet OE-00. Measured on the engine at
`80b15eb4` (main after #370), against the contract pin `de8cec30` and the contract's decisions to DEC-0136.

The organization plan of 2026-10-08 asked this repository for an architecture decision record of "the machine
utility" (packet OE-00 of
[its Observatory plan](https://github.com/passioncode-ai/fabric-workspace/blob/main/docs/reports/2026-10-08-org-architecture-v2/plans/project-observatory-dashboard.md)):
one utility per machine, a Rust sidecar `observatory-net` supervised by the operating system's service manager, a
local channel with an access list, a transcript watcher with budgets, a revision of access bindings, one owner per
verb, one device workspace, `seq` issued by the journal and acknowledgements in the sidecar's outbox, and Windows in
scope.

Two things happened since. On 2026-10-12 the organization architecture v4 (decision D5, accepted by this repository
and by the operator) replaced the sidecar with **the Observatory node, the engine's own local server in Python under
an OS unit**, Rust only where measured; the
[CROSSWALK-v4](https://github.com/passioncode-ai/fabric-workspace/blob/main/docs/reports/2026-10-08-org-architecture-v2/plans/CROSSWALK-v4.md)
row for OE-00 says so ("the machine-utility ADR becomes the node's ADR"). And the node was built: device keys, the
relay session, the estate record, envelopes, ceremonies, organization enrollment, the harness summary, the repair
runner and `node.lifecycle` with its supervisor (OBS-107 to OBS-114, OBS-133, OBS-161, OBS-192, OBS-193, OBS-79; see
[NODE.md](NODE.md) and [LIFECYCLE.md](LIFECYCLE.md)). This record decides on what exists, not on the 2026-10-08 text.
Where that text and a later contract decision disagree, the contract wins, and each place is named below.

## The decisions

| # | Decision |
|---|---|
| D1 | One machine agent: the node is the engine's local server (`tools/serverd.py`). The `observatory-net` sidecar is retired, its work folded into the node; no Rust until a measurement asks for it. |
| D2 | The operating system's service manager supervises the node and the engine's scheduled jobs; the node supervises only what it starts or was enrolled to manage (`lifecycle.manager: node`). The tick stays its own scheduled job; memory over HTTP and the key server become routes of the node. |
| D3 | The node serves the contract's local channel, `fabric-local-channel/0.1` (DEC-0075), beside its loopback HTTP; the channel's run folder also makes one node per OS account. |
| D4 | The transcript collector is a step of the tick in its own child process, never a thread of the node, with the budgets declared here. |
| D5 | Each organization enrollment's outbox issues its own `seq` and holds its acknowledgements; the local journal gives events stable ids and never a `seq` (DEC-0128 over the plan). |
| D6 | The device workspace is the workspace whose node holds the OS account's run lock; it holds the device keys, the enrollments, the lifecycle state, the meter and the collector's journal. |
| D7 | Access bindings keep a per-instance audience with no device id, gain no organization-server type, and every inbound door refuses before parsing and is rate limited per binding. |
| D8 | One owner per verb on the machine (the RACI table below). |
| D9 | Windows and Linux are in scope for every decision; what is not built there answers `unsupported_platform`, never a silent success. |

## D1. One machine agent: the node is the local server

**Context.** The plan put the network module (device key, enrollment, transport, grants, installs, model access) in
a Rust crate beside the engine, run by a session of its own. Since then:

- v4 rule 3 makes the Observatory node the only machine agent, "the existing always-on Python server under an OS
  unit", Rust "only where measured" (CROSSWALK-v4, rows OE-00, NET-00 and V2-16, "replaced");
- the board row for the sidecar reads `superseded by OBS-161` ([OBS-77](../backlog.md));
- the node's code is in the engine, `observatory/engine/node/` (20 modules and `node/lifecycle/`), and its long-running
  part already lives in the local server: the server builds `lifecycle.Host` after it binds its port and starts the
  supervisor from it ([`tools/serverd.py:865-869`](../../observatory/engine/tools/serverd.py),
  [`node/lifecycle/service.py:581`](../../observatory/engine/node/lifecycle/service.py), LIFECYCLE.md "Step L4");
- the local server is installed under launchd with `RunAtLoad` and `KeepAlive`
  ([`tools/serverd.py:913-920`](../../observatory/engine/tools/serverd.py)), and under Task Scheduler or a systemd
  user unit on Windows and Linux ([`tools/serverd.py:1016`](../../observatory/engine/tools/serverd.py),
  [`osschedule.py:100`](../../observatory/engine/osschedule.py), [`osschedule.py:253`](../../observatory/engine/osschedule.py)).

**Options.** (a) The Rust sidecar of the plan, with its own release channel and a version handshake with the engine.
(b) A second Python process for the node beside the local server, under its own OS unit. (c) The node is the local
server: one process, one unit, one port, one update.

**Decision.** (c). The sidecar is retired, not deferred: every packet it carried either moved into the node (device
keys, enrollment, transport, relay client), was replaced by v4 (grants and the policy decision point, the node
manifest, leased tasks, model access), or moved to its own repository (the relay). Rust arrives only behind a
measurement that names what Python cannot meet; the first native part is the signed key helper (D8), which is Swift,
CNG or tpm2-tss, not a crate.

**Consequences.**

- Slice 6 of NODE.md (OBS-198, `keep_connected` under the OS supervisor) runs as a thread of the local server, as the
  lifecycle supervisor does; it adds no unit. "The node service" in LIFECYCLE.md "What changes on the way in", item 3,
  is the local server.
- The server's budget in AGENTS.md "Lifecycle" (about 0.2 CPU-s a minute, about 40 MB; `AGENTS.md:197`) stays the
  node's budget. Work that would break it runs in a child (D2, D4).
- The plan's packet ids NET-01, NET-02, NET-03, NET-07, NET-08, NET-10 and NET-S01 to NET-S03 keep their v4 meaning
  (CROSSWALK-v4); none of them is a separate process.

## D2. Who supervises what

**Context.** The plan (its review item R07-16) said the sidecar is supervised by the OS service manager, not by
Observatory, and one supervisor holds both the engine and the sidecar. v4 §5 adds: "fold into serverd means
supervision, not one process": the tick's collectors stay a supervised child with its own group and ceiling,
memory over HTTP and the key server become routes of the server. Today:

- the tick is its own scheduled job with its own watchdog and ceiling: 1500 s for the whole tick, a collector at
  most 40 % of it ([`tick_budget.py:52`](../../observatory/engine/tick_budget.py),
  [`tick_budget.py:69`](../../observatory/engine/tick_budget.py), `AGENTS.md:196`);
- memory over HTTP is a separate process a person or a caller's supervisor starts (`AGENTS.md:204`), and the key
  server is run by hand (`AGENTS.md:203`);
- the node already says it manages its enrolled targets with `lifecycle.manager: node` in its well-known entry, while
  the server's own descriptor names launchd, its supervisor
  ([`node/lifecycle/service.py:643-648`](../../observatory/engine/node/lifecycle/service.py); contract DEC-0057,
  DEC-0061).

**Options.** (a) The node starts and watches the tick as its child. (b) The tick stays its own OS job; the node
supervises nothing of the engine's own. (c) Everything in one process.

**Decision.** (b), with the routes of v4. The OS service manager supervises the node, the tick, the maintenance job
and the update check; nothing in the engine supervises the node or restarts it on failure. (The maintenance job does
stop and start the server on purpose, for its daily snapshot and to activate an update, `AGENTS.md:198`,
[`engine_update.py:21-22`](../../observatory/engine/engine_update.py); that is maintenance, not supervision.) The node
supervises what it starts and what the operator enrolled (`node.lifecycle`), and only those carry
`lifecycle.manager: node`. Memory over HTTP and the key server become routes of the node, so the machine has one
listening process for agents ([OBS-255](../backlog.md)).

**The tick is not the node's child: Observatory's reading of v4.** v4 §5 keeps the tick's collectors "a supervised
child" of the node, and the CROSSWALK-v4 row V4-OBS-01 and Observatory's own answer in v4 §19 ("tick stays a
supervised child") say the same. This repository owns V4-OBS-01 and reads the requirement behind those words as: the
tick has its own process group and its own ceiling, so a slow scan never stalls the always-on server. The tick already
meets that as its own OS job, which runs on all three systems through `osschedule`, and keeping it there means a
crashed server does not stop collection or the reverse. So the tick keeps its own job and the node does not spawn it.
This departs from the letter of v4 and of V4-OBS-01, and it is listed under "Where the plans are not followed" below;
the CROSSWALK-v4 cell lives in fabric-workspace, so its wording is open question 6 for that owner.

**Consequences.** `full update` keeps stopping and starting the server and the tick around an install, as today.
A descriptor of a service the node starts as its child says `manager: node` once the node starts children (LIFECYCLE.md
"Step L3"). Fabric Dashboards drives such a service through the node, never through the OS (its FD-58).

## D3. The local channel

**Context.** The plan asked for a local channel with an access list (packet CON-OE-10). The contract has it since
2026-10-12: `fabric-local-channel/0.1` (DEC-0075, `docs/specification/local-channel.md`), a Unix socket in an
owner-only folder checked by peer credentials on macOS and Linux, a named pipe with the DEC-0033 DACL on Windows,
JSON-RPC with `channel.hello` first, the methods `health`, `enroll.status` and `machine.report` (DEC-0132), a
`consent.prompt` the other way, published in the node's descriptor as `channels.local`. The engine serves none of it
(no `channel.hello`, `node.sock` or `channels.local` anywhere under `observatory/`, measured 2026-10-26). Callers reach
`node.lifecycle` over loopback HTTP with an access binding's bearer, checked before the body is read
(`_mcp`, [`tools/serverd.py:751-781`](../../observatory/engine/tools/serverd.py)). The repair runner sends that bearer to
whatever listens on the server's port, with no proof of the server ([OBS-248](../backlog.md)).

**Options.** (a) Loopback HTTP and bindings only. (b) The contract's channel in the node, beside loopback HTTP. (c)
Move `node.lifecycle` onto the channel.

**Decision.** (b). The node serves `fabric-local-channel/0.1` exactly as the contract writes it and publishes
`channels.local`; loopback HTTP stays for the dashboard and for MCP, which the contract keeps off the channel ("it
carries no MCP"). A payload method (`node.lifecycle.*`, `skills.compile`) joins the channel only by its owner's
contract decision (open question 1). The channel's lock is also the rule of one node per OS account (D6): on macOS
`run/node.lock` in the owner-only folder beside `node.sock`; on Linux the same lock in `$XDG_RUNTIME_DIR/<node>/` (or
the contract's `${XDG_STATE_HOME:-~/.local/state}/<node>/run/` when that variable is unset); on Windows there is no
run folder, and the lock is the pipe itself, `\\.\pipe\<node>-<user SID>` created with
`FILE_FLAG_FIRST_PIPE_INSTANCE`, which a second server of the same account cannot create. Built as [OBS-256](../backlog.md).

**Consequences.** Fabric, Switchboard and Fabric Dashboards discover the node from its descriptor, never by a computed
path. The owner-only run folder is one of the two proofs OBS-248 names for the repair runner's door. The hub on the
same machine reads `machine.report` there (DEC-0132, [OBS-253](../backlog.md)).

## D4. The transcript collector and its budgets

**Context.** The plan asked for a transcript watcher as "a declared meter in the server or a separate process, with
budgets" (review item R07-13), and packet OE-01a builds it for 0.31.0 (OBS-76): Claude Code, the desktop app and
Codex transcripts, a cursor per file, usage deduplicated, content dropped. The word "meter" is taken in this engine by
the spend meter of [METER.md](METER.md), so this record calls it **the transcript collector**. Nothing reads
transcripts for activity today: `collectors/scan_sessions.py` reads the retired companion's store read-only
([`collectors/scan_sessions.py:1-40`](../../observatory/engine/collectors/scan_sessions.py)), and the leak scan reads
transcripts for credential shapes only
([`tools/scan_leaks.py:40`](../../observatory/engine/tools/scan_leaks.py)).

**Options.** (a) A thread of the node, reading as files grow. (b) A step of the tick, in its own child, from a
cursor. (c) A child the node starts on a timer. (d) An OS job of its own.

**Decision.** (b). The collector is a step of the tick, run as its own child process under the step watchdog, reading
from a cursor (file, offset, content hash of the last line read), so the node keeps its own budget and a slow or
hostile transcript costs one step. Its budgets, which OE-01a's tests and `full doctor` hold it to. Only the first
exists in code today; every other value is new, set here for OE-01a to build:

| Budget | Value | In code today |
|---|---|---|
| wall time per tick | at most the collector share of the tick ceiling (40 % of 1500 s); what is left is read next tick | yes: [`tick_budget.py:52`](../../observatory/engine/tick_budget.py), [`tick_budget.py:69`](../../observatory/engine/tick_budget.py) |
| CPU per tick | at most 120 CPU-seconds (user plus system) per run, at the tick's low priority (Nice 5, `AGENTS.md:196`); over it, a `degraded` row and the next run reads half as many lines | new |
| memory | at most 256 MB peak resident; over it, a `degraded` row and the next run reads half as many lines | new |
| catch-up | nothing older than 30 days; at most 200 MB of transcript per tick until caught up | new |
| one line | a line over 1 MiB is skipped and counted, never parsed | new |
| content | prompt, output and tool text are dropped at read; only times, model, token counts, project and session ids are kept | new |
| idle | no process between ticks, no timer, no file watcher (organization lifecycle LC-08) | the tick's own rule |

**How CPU and memory are measured.** Nothing enforces memory or CPU in the engine today. The tick's step runner reads
the finished child's peak resident size and CPU time: on macOS and Linux from `os.wait4` on that child alone (its own
`ru_maxrss`, bytes on macOS and KiB on Linux, and `ru_utime` plus `ru_stime`; `getrusage(RUSAGE_CHILDREN)` would report
the largest of all the tick's earlier children, so it cannot give one step's peak), on
Windows from the child's handle (`GetProcessMemoryInfo` `PeakWorkingSetSize`, `GetProcessTimes`) before it is closed.
These are measured and reported, not hard limits: an operating-system cap (`RLIMIT_AS` is not enforced on macOS, a Job
Object on Windows) would kill the step mid-file and lose the cursor's progress, so the budget acts on the next run by
halving its batch, and the wall-time watchdog stays the hard stop. OE-01a's acceptance (catch-up on the operator's
machine within the declared RSS ceiling, reported by `full doctor`) is measured this way.

**Consequences.** An interval reaches the journal within one tick (30 minutes) of being written, which the batched
activity stream of DEC-0030 allows. The `Stop` hook of `observatory-log` may ask for an early run; it never reads a
transcript itself.

## D5. Who issues `seq`, who holds acknowledgements

**Context.** The plan (R07-16) said the journal issues `seq` and the sidecar's outbox alone holds the acknowledgement
state. The node does it otherwise, and the contract decided since:

- each organization enrollment has its own outbox under `node/orgs/<org.id>/`; an event gets the next `seq` of its
  stream's epoch inside the transaction that stores it, under the enrollment's lock
  ([`node/org.py:1484-1511`](../../observatory/engine/node/org.py)); the check-in reports `lastSeq` and
  `bufferedFrom` from the same store ([`node/org.py:1300-1316`](../../observatory/engine/node/org.py)); acks consume
  it there (NODE.md "Organization enrollment", Outbox);
- contract DEC-0128 (UE-1): each enrollment keeps its own "collector ids and counters"; one counter shared by two
  organizations would let them link the machine;
- every enrollment names its collector `observatory-node` ([`node/org.py:92`](../../observatory/engine/node/org.py)),
  a constant every machine running the engine shares: it names the software, not the machine or the enrollment, and
  the epoch and `seq` beside it are already per enrollment. Whether that constant satisfies UE-1's "collector ids" is
  judged over the contract's vectors at the re-pin, in [OBS-252](../backlog.md), which also covers any per-enrollment
  counter the vectors find shared.

**Decision.** Follow the contract: each enrollment's outbox issues its own `seq` and is the one writer of its
acknowledgement state. The local journal (OE-01a) gives each event a stable id, which the outbox uses as its
deduplication key (`enqueue(..., key=...)`), and never a `seq`. The plan's single journal-wide `seq` is not adopted.

**Consequences.** Replaying the journal into an outbox is idempotent by key. A new enrollment starts its own stream.
The estate's own records go to the hub by the machine report (DEC-0132), never in a check-in (DEC-0128 UE-2,
[OBS-252](../backlog.md)).

## D6. One device workspace

**Context.** The plan (R07-10) put the watcher, the enrollment and the sidecar in one "device workspace". Today the
node folder is `OBSERVATORY_NODE`, else `<workspace>/node`
([`node/org.py:451-452`](../../observatory/engine/node/org.py), [`tools/node.py:27-28`](../../observatory/engine/tools/node.py)),
the maintenance job runs `org tick` over that folder ([`maintenance.py:984-993`](../../observatory/engine/maintenance.py)),
and one OS account may hold several workspaces (AGENTS.md "Lifecycle", Logs). Nothing stops two workspaces of one
account from each creating device keys and enrolling.

**Options.** (a) A per-account folder outside every workspace. (b) The workspace whose node runs. (c) Leave it as is.

**Decision.** (b). The device workspace is the workspace whose node holds the account's channel lock (D3). It holds
`node/` (device keys, enrollments, outboxes), `<state>/node-lifecycle/`, the meter's ledger and the transcript
collector's cursor and journal. A node that cannot take the lock serves no node duty and says which workspace holds
it (OBS-256). `OBSERVATORY_NODE` stays the way to point the CLI at that folder.

**Consequences.** A second workspace keeps its own collectors and dashboard; it does not enroll, report, supervise or
meter. Moving a machine's identity stays a ceremony, never a folder copy (NODE.md, OBS-114).

## D7. Access bindings, revised

**Context.** The plan's OE-04 asked for an instance id bound to the device and a binding type for the organization's
server. v4 (CROSSWALK-v4 row OE-04) changed it to: the node's inbound gateway admits a connection only for a current
member or a live binding, before any MCP parsing, with a rate limit. Today a binding's audience is the instance,
`observatory:<instance>` ([`access_binding.py:53`](../../observatory/engine/access_binding.py)); `POST /mcp` refuses
before reading the body; memory over HTTP limits each binding's rate (`AGENTS.md:204`); lifecycle is a separate grant
outside every binding's ceiling ([ACCESS-BINDING.md](ACCESS-BINDING.md)). Since then the contract decided that an
enrollment never shows the estate's identity to an organization (DEC-0128) and that an organization's members reach
one another only over the organization's relay, each T2 to the other (DEC-0134, OR-5).

**Options.** (a) The plan's: a device id in the audience and an organization-server binding type. (b) v4's gateway
rule on today's bindings.

**Decision.** (b), following the contract over the plan. The audience stays the workspace instance and never carries
a device or estate id. There is no binding type for an organization server: the node calls its organization outward
(signed check-ins, DEC-0056) and is reached by members only through a relay, judged by the member list, not by a
binding ([OBS-254](../backlog.md) adopts DEC-0134 on the node). Every inbound HTTP door refuses before it parses a body and limits each binding's rate; `POST /mcp` gains the
rate when OE-04 is built.

**Consequences.** OE-04's acceptance becomes: no door parses a request before admission, each door rates per
binding, and every binding issued before keeps working.

## D8. One owner per verb

**Context.** The plan (R07-4) asked for one owner per verb and proposed: install, check and roll back by the sidecar;
showing and supervising services on macOS by Fabric Dashboards; skills reach a session through one compiler that
Fabric and Switchboard call. v4 §4 and §5 since make the node "the only supervisor and installer", and Fabric
Dashboards' own board says "Dashboards installs nothing; installing and supervision are the Observatory node's" (its
FD-52, FD-58). Where the plan gave supervision on macOS to Dashboards, this record follows v4 and Dashboards' board.

**Decision.** "Owner" does the act and answers for it; one per verb. "Asks" may request it; "Is told" reads its
result.

| Verb | Owner | Asks | Is told | Where it stands |
|---|---|---|---|---|
| install an agent or package on this machine | the node | Fabric, the person | Fabric Dashboards | not built (v4 packet V4-OBS-03, Stage 3) |
| check what is installed against its record | the node | Fabric, an organization (through the summary) | Fabric Dashboards, the organization | harness summary built (OBS-192, DEC-0118 mode `C`) |
| roll back an install | the node | the person, Fabric | Fabric Dashboards | not built (with V4-OBS-03) |
| update the node itself | the maintenance job, an OS job of its own (D2), running the engine's updater | the person | Health, `full doctor` | built (`engine_update.py:18-22`, a rollback wheel kept) |
| supervise a service or app (start, stop, restart, idle stop) | the node (`node.lifecycle`) | Fabric, Switchboard, Dashboards, agents with a grant | Dashboards | macOS built (OBS-79 L1 to L4); Windows and Linux OBS-257 |
| supervise the node | the OS service manager | none | Health | built |
| show this machine's services | Fabric Dashboards | the person | none | Dashboards FD-52 |
| compile skill layers into a session set | the node (`skills compile`, OE-08a) | Fabric, Switchboard | the session | not built (OE-08a) |
| start a session with a compiled set | the program that starts the session | the person | the node | Fabric (FAB-OE-05a) and Switchboard (SB-OE-08) for theirs; open question 3 for others |
| enroll this machine in an organization | the node (`full org enroll`) | the person | the organization | built (OBS-161) |
| issue an event's `seq` | each enrollment's outbox (D5) | the node's collectors | the organization (in the batch) | built (OBS-161) |
| hold the acknowledgement state | each enrollment's outbox (D5) | the organization's acks | the check-in (`lastSeq`, `bufferedFrom`) | built (OBS-161) |
| report this machine to its estate's hub | the node (`machine-report/1`, DEC-0132) | Fabric's hub | Fabric's hub | OBS-253 |
| hold the device key and sign with it | the node (file keys today; the signed key helper next) | Fabric's hub | none | file keys built; helper OBS-258 |
| issue a delegation token | Fabric's hub | an agent | the meter | Fabric's |
| reserve and settle spend (`grant.check`) | the node (the meter) | a callee with a token | the hub | built (METER.md) |
| run an organization's repair | the node (the repair runner) | the organization | the person first, then the organization | built, inert until OE-06 (OBS-193) |

**Consequences.** No second installer or supervisor appears on a machine: a product that needs one asks the node.
The engine still writes the hook environment of its own plugin into the person's Claude Code settings
([`tools/agent_plugin.py:1-25`](../../observatory/engine/tools/agent_plugin.py)); the skills compiler never writes the
person's `~/.claude` (OE-08a's acceptance).

## D9. Windows and Linux in scope

**Context.** Decision OD-4 of the plan puts Windows on the path to the demonstration; v4 rule 13 stages it: the
person's own Windows or Linux machine at Stage 3, an organization's Windows host at Stage 4. Today the local server,
the tick and maintenance install on both ([`tools/serverd.py:1016`](../../observatory/engine/tools/serverd.py),
[WINDOWS-LINUX.md](WINDOWS-LINUX.md) W4), the engine suites run on Windows (OE-09, shipped in 0.22.0), and the
lifecycle adapter answers `unsupported_platform` off macOS
([`node/lifecycle/macos.py:31`](../../observatory/engine/node/lifecycle/macos.py)).

**Decision.** Every decision above holds on all three systems; the one-node-per-account lock is the run folder's lock on
macOS and Linux and the first pipe instance on Windows (D3). Workstations use per-user units (LaunchAgent, user
systemd, Scheduled Task); an organization's host uses what v4 §5 names (a LaunchDaemon through SMAppService, a system
unit with `DynamicUser=`, a Scheduled Task with an S4U or SYSTEM principal). The channel is a named pipe with the
DEC-0033 DACL on Windows. The device key moves to CNG or a TPM on Windows and to a TPM on Linux through the key helper;
a Linux workstation without TPM access gets no organization device certificate (v4 §5). What is not built on a system
answers `unsupported_platform` with the command or row that changes it.

**Consequences.** The Windows and Linux lifecycle adapters are rows of their own ([OBS-257](../backlog.md)); Fabric
Dashboards' supervision on those systems waits for them (its FD-58).

## Open questions, for other owners

These need another repository's owner; they are not decided here.

1. **Payload methods on the local channel** (owner: fabric-agent-contract). The shapes of `node.lifecycle.*` and
   `skills.compile` if they are to be served on the channel, and whether `node.lifecycle` stays MCP over loopback HTTP
   for agents. Until decided, both stay where they are (D3).
2. **The vendored service kit** (owner: fabric-agent-adapter). The engine's vendored `fabric_service.py` admits
   `launchd`, `systemd`, `task-scheduler` and `none` as a supervisor, not `node`, and no `channels.local`; the node
   writes neither until the kit carries DEC-0061 and DEC-0075 and is vendored again (CT-18 there).
3. **Sessions the person starts directly** (owners: the `passioncode` launcher, with Fabric and Switchboard). Fabric
   (FAB-OE-05a) and Switchboard (SB-OE-08) call the compiler for the sessions they start. Which program points a
   session started outside them at the compiled set, through a managed `CLAUDE_CONFIG_DIR` or a plugin folder, is
   theirs to decide; this record proposes the launcher, which already installs the family's plugins.
4. **Which descriptor a client trusts as the node's** (owners: Fabric, Switchboard, Fabric Dashboards). The contract
   leaves it to each client's configuration (local-channel.md, "Not decided here").
5. **The key helper's peer check and who signs for the hub** (owner: Fabric). Fabric told v4 §19 it will decide
   separately, for its 0.5.0, "the hub holds no private key, the node signs with the device key", and the contract leaves the
   helper's check that its peer is the signed Fabric binary to platform code signing. OBS-258 builds the helper
   against whatever Fabric decides.
6. **The CROSSWALK-v4 cell for V4-OBS-01** (owner: fabric-workspace). It says the tick becomes a supervised child of
   the node; D2 keeps the tick as its own OS job. The cell's wording is that file's owner's to change or to keep.

## Where the plans are not followed

| Plan item | Here | Why |
|---|---|---|
| a Rust sidecar `observatory-net` | the node is the local server (D1) | v4 D5, OBS-77 superseded |
| Dashboards shows and supervises services on macOS | Dashboards shows, the node supervises (D8) | v4 §4 and §5, Dashboards FD-52 |
| `seq` issued by the journal | issued by each enrollment's outbox (D5) | contract DEC-0128 UE-1 |
| an instance id bound to the device | the audience names no device (D7) | contract DEC-0128 |
| a binding type for the organization's server | none; members meet over the relay (D7) | contract DEC-0056, DEC-0134 |
| a policy decision method on the local channel | none | contract DEC-0075, v4 §7 (no policy decision point) |
| v4 §5 and V4-OBS-01: the tick a supervised child of the node | the tick stays its own OS job (D2) | Observatory's reading, as owner of V4-OBS-01: its own group and ceiling already keep a slow scan off the server, and the two fail independently |

The contract decisions after the engine's pin (`de8cec30`) that this record relies on, DEC-0128, DEC-0132 and DEC-0134,
are adopted by their own rows at the re-pin past DEC-0121 ([OBS-237](../backlog.md), [OBS-252](../backlog.md),
[OBS-253](../backlog.md), and [OBS-254](../backlog.md) for DEC-0134). DEC-0118 is inside the pin and built (OBS-192).
