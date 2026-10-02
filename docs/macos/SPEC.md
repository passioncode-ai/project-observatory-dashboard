# Native application and assistant specification

Spec date: 2026-10-01. MCP revision 2026-07-28, independently checked against
https://modelcontextprotocol.io/specification/latest. Existing MCP SDK owns the
wire. Fabric interop remains fabric-interop/0.1 with durable job handles.

## Ownership and components

`macos/` owns a SwiftUI application (macOS 14+, arm64 build on Apple silicon).
`observatory/engine/agent/assistant.py` owns bounded conversational reasoning,
retrieval and local dialogue records. CLI, native app and MCP call this same module.
The existing observer remains its scheduled delta interpreter. No chat route or
agent widget is added to the HTML dashboard. The registry, event store and ledger
remain canonical facts; assistant dialogue is interaction history, never approved
project truth. No Claude Mem dependency or integration is introduced.

The assistant is a bounded advisory workflow: collect a limited set of typed
project/machine facts, recent findings and conversation context; call the configured
structured-output model through `agent/providers.py`; validate references; record
answer and evidence. It cannot execute shell, delete data, approve proposals,
change policy, rotate credentials or deploy. A request for such work is answered
with the measured prerequisite and next action. Existing explicit cleanup controls
remain their own surface. This scope is what MAC-03 verifies.

## Runtime and state machine

`project-observatory full assistant <action>` accepts a bounded JSON object on stdin
for actions with input and emits one JSON response on stdout. No user question or
credential goes in argv. Errors are typed codes, never raw provider responses.
`status` reports protocol `observatory-assistant/1`, `workspace_available`,
agent feature/provider configuration (`provider_status` names why no provider is
usable, never with a character of a key), `project_count`, `degraded` and
conversation summaries without spending; a path that is not a workspace answers
`unknown-workspace`. `ask` starts `agent.ask`; `get` reads a conversation and stores
any reconciled turn state; `job` polls and `cancel` stops **assistant jobs only**;
`delete` removes a conversation that has no open turn, with its request ids and job
records; `list` lists conversations; `dashboard` verifies the selected workspace
before returning a loopback URL. Unknown actions/fields/ids fail closed. A full
disk is `disk-full`, an unwritable workspace `workspace-unwritable`.

`agent.ask` accepts question (1..6000 chars), optional conversation id, optional
project id, and caller request id for deduplication. The CLI and additive `observatory_assistant_ask` MCP tool return a
Fabric-compatible job view plus conversation id; `agent.ask` is the internal job
kind, not a newly published manifest capability. Pending → working → completed | failed | cancelled. Cancellation
is terminal; a late answer cannot replace it. Repeating an identical request id
reuses its job, never spends again. Reusing it with changed input is an error.
A different request while this assistant is working is busy, not a join to an
unrelated answer. Existing no-argument inventory jobs keep their join behavior.
A detached runner survives the app/MCP process. Heartbeat reconciliation marks a
vanished runner interrupted. Provider requests retain bounded retry/timeouts; an
assistant job has its own wall-time limit and no infinite conversation loop.

The core stores private conversation records below `store/assistant/`, modes
700/600, with atomic writes and a workspace lock around mutations. Conversations
have opaque ids. Truncate model context to recent turns and a fixed evidence
budget spent machine → findings → projects, naming every trimmed source with a
`code` and its counts; retain original history locally. Limit a conversation to 32
turns and bound message lengths and document retention; never silently delete
history to make disk space — a conversation leaves only by an explicit delete.
Terminal assistant jobs follow the generic job retention; a terminal turn is the
record and is never re-derived from a pruned job. A full/unwritable
store refuses the operation before model spend. Interrupted turns stay visible.

## Evidence and provider boundary

Read only allowlisted registry fields and snapshot summaries. A missing source
is degraded, never a fabricated empty estate. Do not read environment values,
credential stores, session transcript trees or arbitrary paths for retrieval.
Each provided evidence item has an opaque id, title, source path and measured time.
The model returns answer, referenced ids and suggested next steps. Reject unknown
ids and malformed results; no fabricated link is shown as a source. Render model
text as text/Markdown without executable HTML or automatic external navigation.

Reuse `providers.complete` for configuration, model fallback, wallet accounting
and guardrails. Tests inject a fake provider; production has no fake-success mode.
First-run copy states that sending uses the configured model and selected local
facts. No credential textbox: configuration uses the existing private setup.
Disabled agent, no credential, budget reached, provider failure, disk full and
unknown workspace remain distinguishable. Status and local navigation spend nothing.

## Native screens and lifecycle

Main window: native split view, conversation sidebar, toolbar with new dialogue,
project scope and dashboard action, central transcript, evidence disclosure,
composer and Send/Stop. Returning users see stored history. Empty state explains
the job and offers concrete starter questions; loading/error states preserve input.
Settings: CLI executable picker, workspace directory picker, connection test,
language (EN/RU), and visible backend/version status. Absolute executable path,
argv array, no shell interpolation. The app never edits other agents' MCP configs.
No hidden enrollment or automatic service takeover.

Native process bridge uses bounded output, timeout, cancellation and exit status,
spawning the CLI in its own process group so a timeout or Stop ends what the CLI
started; a write to a child that exited is EPIPE, never SIGPIPE. Malformed or
incompatible protocol is a recoverable configuration error: an engine without the
assistant is `backend-incompatible` with its first stderr line, any other non-JSON
failure `backend-failed` with its first stderr line. One request id per draft until
accepted, so a retry after a timeout replays it instead of spending again. Settings
changes invalidate in-flight UI callbacks; late results cannot paint a different
workspace. Closing the window does not kill a detached accepted job; reopening
can resume it. Stop explicitly cancels the job, not unrelated processes.

Design: native controls and system semantic colors; one central token map for
spacing, widths and accent. System font, keyboard shortcuts, selectable answer text,
VoiceOver labels, clear focused controls, reduced-motion-compatible static layout.
No custom animation or WebView chat. Main action remains reachable at 900×620.
Native controls are the component layer, not a web component kit. Visual direction
is quiet macOS utility; observable target is readable evidence beside an answer,
not a tile dashboard. No Figma publication requested. Native UI inspection is
required; screenshot review alone is not an accessibility certification.

## Distribution and linked repositories

Build Swift package, wrap executable/resources into `Project Observatory.app`,
validate Info.plist and ad-hoc signature for local QA. A release workflow can use
Developer ID signing/notarization when credentials are configured; absence is
NOT_RUN, not a signed distribution. The app connects to a compatible tagged engine;
source development uses an explicitly selected fixture backend and separate HOME.
Do not hotpatch the operational installed engine. No version bump in this feature PR.

Update owner README/onboarding, UX index, Fabric conformance and runtime inventory.
Update org-index's owner row for the new app/build/test paths, and fabric-workspace
knowledge product entry linking this spec. Leave Fabric and Fabric Dashboards
runtime unchanged unless an actual integration defect is reproduced: existing
MCP/service discovery is reused. Do not move their submodule pins to unmerged work.
