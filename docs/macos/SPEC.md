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
usable, never with a character of a key; `key_status` and `key_source` say whether the
key is usable and where it comes from, the environment included; `model_status` and
`next` name the steps still missing), `project_count`, `degraded` and
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

**Decision, 2026-10-02 (operator):** opening Observatory shows the dashboard, not the
assistant. The first version put the conversation in the main window and kept the
dashboard in the browser; the operator expected the dashboard. Supersedes the
"no WebView, dashboard stays in the browser" choice of 2026-10-01.

Dashboard window (main, a single `Window` scene; opens at launch and on Dock reopen whenever
the dashboard itself is not on screen, even with the Assistant open — the app opens it
itself, because AppKit's reopen and a launch restored with no window do not recreate a
SwiftUI scene window, and AppKit's `hasVisibleWindows` counts the Assistant): the workspace's
dashboard in a WKWebView. Live when `assistant dashboard` verifies a loopback server
for THIS workspace; otherwise the built pages from `docs/dashboard/`, which are
self-contained, under a banner that names their build time and offers **Start
server** (`assistant serve`: an installed always-on server is restarted through its
own launchd job, otherwise the detached `full open --serve` start). No pages yet →
**Build the dashboard** (`assistant build`, local, no provider call). A port held by
another workspace is named and Start is disabled. Navigation stays inside this
workspace's pages (`DashboardOrigin`); other sites, other local services, mail and
mail links open in the default browser or mail app; any other scheme is refused, so a page
cannot launch an app.
The page's `confirm`/`prompt` are native sheets. Toolbar: back, forward, overview,
reload, open in browser, assistant. The app's language is written once per change to
the page's own `observatory.locale`; a choice made on the page is read back after
each load and adopted. Re-checked when the app becomes active.

Assistant window (⇧⌘A, toolbar): native split view, conversation sidebar, toolbar with
new conversation, refresh and dashboard (the project scope sits above the composer), central transcript, evidence
disclosure, composer and Send/Stop. Returning users see stored history. Empty state
explains the job and offers concrete starter questions; loading/error states preserve
input. Settings: program and workspace fields with pickers, Save and check connection,
language (System/English/Русский), and visible backend/version status. A first launch looks for
the engine at `~/.local/bin/project-observatory`, then in the virtual environment README →
Install creates (`~/.local/share/project-observatory-venv/bin/`), then Homebrew's prefixes;
no engine is `backend-missing` with the path and an installation-guide link, a folder
without `workspace.json` names `project-observatory full init`. Absolute executable path, argv
array, no shell interpolation. The app never edits other agents' MCP configs. No hidden
enrollment or automatic service takeover: a server starts only from Start server.

**Language (2026-10-06, the organization's L10N-01…06).** The app's own interface — menus,
alerts, both windows, Settings and the update affordance — speaks Russian when the first
preferred system language is `ru`/`ru-*` and English otherwise; Settings → Language
(*System / English / Русский*) overrides it, persisted in the app's defaults (`language`; the
0.18 Bool `russian` is still read when no choice is stored, an unknown value is System). An
explicit choice is also written as the app's `AppleLanguages`, so the menu items macOS draws
itself (Quit, Hide, Window) follow at the next launch; System removes it. English is the
source and the key: views call `t("English text", ["name": value])`, the Russian lives in
`macos/Sources/ObservatoryCore/Resources/ru.lproj/Localizable.strings`, counts go through
`plural(…)` with forms in `Localizable.stringsdict` (Russian one/few/many, English
one/other), values in named `{placeholders}`, never concatenated fragments. A missing entry
shows in English. `build-app.sh` copies both `.lproj` folders into the bundle's Resources
(`CFBundleLocalizations` en, ru); `ObservatoryCore.Localizer` reads them there, or from
SwiftPM's resource bundle for `swift run` and the tests. The resolved language reaches the
dashboard through its own `observatory.locale` key, as before. Dates and numbers follow the
app's language. Checked by `LocalizationTests` (every key the app uses has its Russian with the
same placeholders, no stale entries, three Russian plural forms, the language resolution for
`ru-RU`, `ru`, `en-US`, an empty list and an unknown stored value).

**Updates (2026-10-06, LC-16 "Install vs activation").** The engine's hourly maintenance pass
verifies and stages a new app and swaps it only while the app is not running, recording
`app.result = "waiting-for-quit"` with the `pending` version in
`<workspace>/store/maintenance.json`. The app reads that record at launch and every 15 minutes
(and again before acting on it; every 6 h before 0.20.0, which put the prompt hours behind the
staged update); a pending `X.Y.Z` strictly newer than its own `CFBundleShortVersionString` shows
**Restart to update** in the app menu and as a toolbar button in the dashboard window, and a
badge (`↑`) on the Dock icon, so it is seen without opening a window — never a modal. Activation happens only at a safe point:
the person's *Restart to update*, the person's quit (⌘Q), or 30 minutes with no titled window
on screen, the app not frontmost, no key press, click or scroll, and no question, build or
start in flight. Each starts one detached helper (`/bin/sh`, its own session, values as
arguments only) that waits ≤ 60 s for the app to exit, runs the single documented command
`OBSERVATORY_HOME=<workspace> <program> full maintain app` (the engine's app step alone, under
the maintenance lock; retried while another pass holds it, ≤ 5 min; ≤ 15 min in all), then
opens the app again — in front after *Restart to update*, with `--background` (no window, no
focus) after an idle restart, not at all after a quit. When the swap does not happen the old
app is opened again and the reason is logged. `~/Library/Logs/Project Observatory/app.log`
holds one JSON line per event, UTC, codes only (`update_restart` `requested`/`refused`,
`update_install` `started`/`installed`/`failed`/`timeout`, with `trigger`, `reason` and
`version` codes), 0600 under a 0700 folder, under 1 MB with one previous generation. A build
that is not inside an `.app` (`swift run`) refuses a restart (`not-a-bundle`). Checked by
`AppUpdateTests`/`UpdatesTests` (version order, the record, the idle decision, the plan, the
real helper against a stand-in engine and `open`, the detached session, the log) and the
engine's `tests/test_maintain_app.py`.

Icon: the product mark (`dashboard/brand/observatory-mark.svg`, pinned in its
manifest) rasterized on the macOS grid by `macos/scripts/make-icon.swift` at every
iconset size — vector rasterization of the reviewed mark, not generated imagery.

Native process bridge uses bounded output, timeout, cancellation and exit status,
spawning the CLI in its own process group with an empty signal mask and default
dispositions, so a timeout or Stop ends what the CLI started and a server it starts
can be stopped; a write to a child that exited is EPIPE, never SIGPIPE. Malformed or
incompatible protocol is a recoverable configuration error: an engine without the
assistant is `backend-incompatible` with its first stderr line, any other non-JSON
failure `backend-failed` with its first stderr line. One request id per draft until
accepted, so a retry after a timeout replays it instead of spending again. Settings
changes invalidate in-flight UI callbacks; late results cannot paint a different
workspace. Closing the window does not kill a detached accepted job; reopening
can resume it. Stop explicitly cancels the job, not unrelated processes.

Design (2026-10-03): one PassionCode product. Every window — dashboard, assistant,
Settings, alerts and menus — is dark (`NSApp.appearance` is Dark Aqua) and drawn from
the design system's own colour roles, the values the dashboard's pages use
(`dashboard/brand/passioncode-tokens.css`). They are defined once, in
`macos/Sources/ObservatoryCore/Palette.swift`, and the views read them through
`macos/Sources/ObservatoryApp/Theme.swift`: gold (`--pc-accent`) for the primary action,
the selected conversation and the focus ring, with `--pc-on-accent` text on it; semantic
roles for state only (warning: a person is needed; negative: a failure; positive: ready;
info: running). No system blue: buttons, fields, the language switch and the conversation
list draw their own states. `PaletteTests` compares every value with the vendored CSS,
holds every text pair the app draws to WCAG AA (4.5:1), and the focus ring and a control's
resting edge to 3:1 (WCAG 1.4.11), and refuses a system colour in any view. That edge is the
design system's `--pc-border-strong`, `#6f5e77` since PassionCode 1.1.0 (3.26:1 on panel; the
dashboard's inputs use the same token); fields also carry their label and AA placeholder and
get the gold ring on focus.
System font, keyboard shortcuts, selectable answer text, VoiceOver labels and the
selected state as an accessibility trait. Motion is colour on hover and press, 120 ms,
and none under Reduce Motion; no other animation. Main actions remain reachable at
900×620. Native UI inspection is required; screenshot review alone is not an
accessibility certification.

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
