# Native assistant scenarios

Persona P-01: operator responsible for the local project estate. Evidence kind:
brief; decision status: accepted by explicit implementation request; validation
status: unvalidated. JTBD-01: ask what changed and act from traceable evidence.
Story ST-01: use the agent without a browser; ST-02: retain context across launches;
ST-03: another agent invokes the same capability. Product outcome: unobserved.

## Index

| ID | Scenario | Status |
|---|---|---|
| SCN-001 | First launch and connection recovery | draft |
| SCN-002 | Evidence-bound answer | draft |
| SCN-003 | Disabled provider or failed request | draft |
| SCN-004 | Stop, close and resume | draft |
| SCN-005 | Change workspace without stale output | draft |
| SCN-006 | CLI/MCP caller obtains the same answer | draft |
| SCN-007 | Opening the app shows the dashboard | draft |
| SCN-008 | No server: saved pages, then Start server | draft |
| SCN-009 | Window closed, Dock click brings it back | draft |

These scenarios are specified from the autonomous implementation brief, not human usability validation;
coverage below names the regression tests and the native walkthroughs run on 2026-10-02 and 2026-10-03. See [receipts](../runs/2026-10-01-macos-app/README.md).

## SCN-001 — First launch and connection recovery
Status: draft
Product: unobserved
Persona: P-01
Traces: ST-01, JTBD-01, FLW-01
Preconditions: app installed, backend may be absent or old.
Trigger: user opens app.
Steps: open → the engine is looked for where README → Install puts it → no engine:
«Observatory is not installed yet» with the path, Settings and an installation guide;
a folder that is not a workspace: the `full init` command → choose program/workspace in
Settings → Save and check connection → compatible status → empty conversation.
Expected result: no automatic dependency install, no model call, usable next action.
Errors & recovery: invalid path/protocol/timeout shows retry/settings, keeps selections.
Coverage: core/bridge/model regression tests (`testMissingExecutableIsNamedMissingNotMisconfigured`,
`testDefaultEngineIsFoundWhereTheReadmeInstallsIt`, `testFirstRunMessagesNameTheInstallAndInitSteps`);
native walkthroughs 2026-10-02 ([receipt](../runs/2026-10-02-app-agent-audit/README.md)) and
2026-10-03 on synthetic workspaces: no engine, no workspace, unbuilt dashboard ([report](../reports/2026-10-03-observatory-audit-run-1/README.md)).

## SCN-002 — Evidence-bound answer
Status: draft
Product: unobserved
Persona: P-01
Traces: ST-01, JTBD-01, FLW-02
Preconditions: compatible backend and configured provider.
Trigger: user sends a question, optionally scoped to a project.
Steps: type → Send → persisted pending turn → working indicator/Stop → answer,
model/cost and expandable evidence → follow-up uses bounded previous context.
Expected result: cited facts and degraded sources visible; no implicit mutation.
Errors & recovery: invalid reference/provider schema refuses the result, retains question.
Coverage: core/bridge/model regression tests; native walkthrough run 2026-10-02 against a workspace copy ([receipt](../runs/2026-10-02-app-agent-audit/README.md)).

## SCN-003 — Disabled provider or failed request
Status: draft
Product: unobserved
Persona: P-01
Traces: ST-01, JTBD-01, FLW-02
Preconditions: missing key, disabled agent, budget limit or unavailable source.
Trigger: attempt Send.
Steps: preflight → distinct error and next action; repair configuration → deliberate retry.
Expected result: no fake response, no automatic spend loop, input/history preserved.
Errors & recovery: full disk refuses before spending; errors do not expose raw provider text.
Coverage: core/bridge/model regression tests; native walkthrough run 2026-10-02 against a workspace copy ([receipt](../runs/2026-10-02-app-agent-audit/README.md)).

## SCN-004 — Stop, close and resume
Status: draft
Product: unobserved
Persona: P-01
Traces: ST-02, JTBD-01, FLW-03
Preconditions: accepted job.
Trigger: Stop or close app then reopen.
Steps: Stop → cancellation recorded; reopen → stored terminal state. Close without
Stop → job continues; reopen → pending or completed answer recovered.
Expected result: cancelled cannot become completed; dead runner becomes interrupted.
Errors & recovery: missing/corrupt job is named, not recreated; retry is a new request.
Coverage: core/bridge/model regression tests; native walkthrough run 2026-10-02 against a workspace copy ([receipt](../runs/2026-10-02-app-agent-audit/README.md)).

## SCN-005 — Change workspace without stale output
Status: draft
Product: unobserved
Persona: P-01
Traces: ST-02, JTBD-01, FLW-01
Preconditions: a request to workspace A is outstanding.
Trigger: select workspace B in settings.
Steps: apply → invalidate UI generation → connect B → show B history.
Expected result: late A response never enters B; old draft and project scope are cleared when the backend/workspace changes; A accepted job persists independently. Dashboard opens only after the backend verifies the selected workspace.
Errors & recovery: failed B connection offers settings; no fallback into A disguised as B.
Coverage: core/bridge/model regression tests; native walkthrough run 2026-10-02 against a workspace copy ([receipt](../runs/2026-10-02-app-agent-audit/README.md)).

## SCN-006 — CLI/MCP caller obtains the same answer
Status: draft
Product: unobserved
Persona: P-01
Traces: ST-03, JTBD-01, FLW-04
Preconditions: compatible host, initialized workspace and provider.
Trigger: call observatory_assistant_ask.
Steps: validated input → durable handle → fabric.job.get → result envelope and
shared conversation; fabric.job.cancel → terminal cancellation.
Expected result: advertised MCP input contract, trace propagation and shared Fabric job envelope; no duplicate spend on replay.
Errors & recovery: changed input under same request id rejected; different busy request
is not silently joined; unknown ids never create jobs.
Coverage: core/bridge/model regression tests; native walkthrough run 2026-10-02 against a workspace copy ([receipt](../runs/2026-10-02-app-agent-audit/README.md)).

## SCN-007 — Opening the app shows the dashboard
Status: draft
Product: unobserved
Persona: P-01
Traces: ST-01, JTBD-01, FLW-05
Preconditions: installed app; a compatible engine and an initialized workspace.
Trigger: the operator opens Project Observatory (Dock, Launchpad, Spotlight, Finder).
Steps: launch → the dashboard window comes forward → its overview loads, live when the
workspace's server answers → toolbar offers back/forward, overview, reload, browser, assistant.
Expected result: the dashboard, never the assistant, is what opens; the window title is the page's.
Errors & recovery: an unreadable engine or workspace shows the reason with Retry and Settings.
Coverage: Model and navigation tests; native walkthroughs 2026-10-02 ([receipt](../runs/2026-10-02-dashboard-first-app/README.md)) and 2026-10-03, EN and RU, default size and 900×620 ([report](../reports/2026-10-03-observatory-audit-run-1/README.md)).

## SCN-008 — No server: saved pages, then Start server
Status: draft
Product: unobserved
Persona: P-01
Traces: ST-01, JTBD-01, FLW-05
Preconditions: built pages exist; the workspace's server is not running.
Trigger: the app opens or comes back to the front.
Steps: saved pages shown under a banner naming their build time → Start server → the
window switches to the live dashboard; an installed always-on server is restarted instead.
Expected result: reading never waits for a server; a server starts only on request.
Errors & recovery: a port held by another workspace is named and Start is disabled; a failed
start names its log; no pages at all → Build the dashboard.
Coverage: engine `serve`/`build` tests, Model tests; native walkthroughs 2026-10-02 and 2026-10-03:
port held by another workspace → Start disabled with the reason; unbuilt → Build → saved pages;
spare port → Start server → «Live» ([report](../reports/2026-10-03-observatory-audit-run-1/README.md)).

## SCN-009 — Window closed, Dock click brings it back
Status: draft
Product: unobserved
Persona: P-01
Traces: ST-02, JTBD-01, FLW-05
Preconditions: the app runs with no window open.
Trigger: Dock icon click, or launching the app again.
Steps: reopen → the dashboard window is created and comes forward.
Expected result: the app is never running without a way back to its window.
Errors & recovery: none expected; ⌘1 (Window → Dashboard) does the same.
Coverage: native walkthroughs 2026-10-02 and 2026-10-03 (launch 1 window → closed 0 → reopen 1; the
Window menu lists Dashboard and Assistant once each).

