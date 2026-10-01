# Native assistant flow and screen map

Source: [scenarios](SCENARIOS.md). Scope: one local operator (P-01). JTBD-01 is to
understand current project observations and choose a next action with evidence.
ST-01 asks outside the browser; ST-02 restores conversation context; ST-03 lets
another agent call the same assistant. These are brief-derived requirements,
not observed usability outcomes.

| Flow | Scenarios | State path | Screens |
|---|---|---|---|
| FLW-01 | SCN-001, SCN-005 | disconnected → settings → checking → ready or actionable error; save invalidates prior generation | SCR-01, SCR-02 |
| FLW-02 | SCN-002, SCN-003 | composer → persisted job → working → answer/sources or typed failure | SCR-01 |
| FLW-03 | SCN-004 | working → Stop → cancelled; close/reopen → read persisted job → resume/terminal | SCR-01 |
| FLW-04 | SCN-006 | MCP ask → validated bounded input → shared job → poll/cancel → envelope/history | CLI/MCP, no new screen |

## SCR-01 — Main window

Left: conversation list and connection/version status. Right: transcript with
question, advisory answer, model/cost, suggested steps, missing-source notes and
expandable evidence. Bottom: project scope, composer, Send/Stop and provider
sharing disclosure. New conversation, Refresh and Dashboard are explicit actions.
A missing backend keeps settings accessible. Empty state offers starter questions.
Working disables context-changing selection; accepted jobs remain stoppable.
Text remains selectable. Failed requests preserve the draft or saved question.

## SCR-02 — Settings

Executable and workspace use native pickers and editable absolute paths. Save and
check is the application boundary: editing text alone cannot redirect a running
request. Language selects English/Russian. Failure explains the next configuration
action and leaves the input intact. Changing workspace never cancels an old job.

## Native visual direction

The existing dashboard remains a browser dashboard; this surface uses native
macOS utility conventions: split view, system font, semantic system colors,
compact toolbar and one reading column. No decorative tiles, custom animation or
embedded browser chat. `Design` in `macos/Sources/ObservatoryApp/App.swift` owns the
shared spacing and transcript width. Native controls supply their platform states.
The alternate web-wrapper direction was rejected because it duplicates browser
navigation and does not fulfill the requested separate native application.

Scenarios govern placement and actions; the draft brand pack governs strings.
EN/RU errors and the provider disclosure are in `Model.swift` / `App.swift`.
No visual or accessibility pass is inferred from compilation; the acceptance
matrix is in the [run receipt](../runs/2026-10-01-macos-app/README.md).
