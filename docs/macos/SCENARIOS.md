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
| SCN-010 | An update is waiting: restart, quit or idle installs it | draft |
| SCN-011 | The app speaks the person's language | draft |
| SCN-012 | The app opens at login unless the person turned it off | draft |
| SCN-013 | A permission the app needs is missing at launch | draft |
| SCN-014 | The person pairs this machine with their network | draft |

These scenarios are specified from the autonomous implementation brief, not human usability validation;
coverage below names the regression tests and the native walkthroughs run on 2026-10-02 and in the three 2026-10-03 audits. See [receipts](../runs/2026-10-01-macos-app/README.md).

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
Settings → Save and check connection → compatible status → the dashboard (or Build the
dashboard); the assistant shows its empty conversation when opened (⇧⌘A).
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
Coverage: core/bridge/model regression tests; engine tests `test_a_full_disk_refuses_as_disk_full_before_any_job_or_spend`
and `test_a_provider_failure_never_carries_the_providers_text` (`observatory/engine/tests/test_assistant.py`); native walkthrough run 2026-10-02 against a workspace copy ([receipt](../runs/2026-10-02-app-agent-audit/README.md)).

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
Coverage: core/bridge/model regression tests (`testLateWorkspaceResponseCannotReplaceNewState`,
`testChangingWorkspaceDuringABuildLeavesTheNewWorkspaceUsable`); native walkthrough 2026-10-03 with a
stub provider ([run 2](../reports/2026-10-03-observatory-audit-run-2/README.md)): a question outstanding in A, Settings switched to B → B's
state and none of A's turns, A's job completed in A's own store. The dashboard half is settled by
`DashboardWindowTests`, which hosts the window over a real web view with two built workspaces: a
window open during the switch shows B's pages; a window closed during the switch and reopened kept
showing A's until [run 3](../reports/2026-10-03-observatory-audit-run-3/README.md) (R2-APP-5, fixed:
the window shows the current mode when it opens).

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
Coverage: core/bridge/model regression tests and the MCP wire suite; a raw MCP stdio walk
2026-10-03 ([run 1](../reports/2026-10-03-observatory-audit-run-1/README.md)); a malformed
argument or unknown tool is a typed `invalid-input` / `unknown-tool` ([run 2](../reports/2026-10-03-observatory-audit-run-2/README.md)).

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
Errors & recovery: a port held by another workspace is named and Start (the banner's button and
Dashboard → Start Server alike) is disabled; a failed start names its log; no pages at all → Build
the dashboard.
Coverage: engine `serve`/`build` tests, Model tests (`testStartServerIsOfferedOnlyWhereItCanStart`);
native walkthroughs 2026-10-02 and 2026-10-03: port held by another workspace → Start disabled with
the reason; unbuilt → Build → saved pages; spare port → Start server → «Live»
([run 1](../reports/2026-10-03-observatory-audit-run-1/README.md)); opened through Launch Services,
Dashboard → Start Server started the workspace's server and the window's next load answered HTTP 200
from it ([run 3](../reports/2026-10-03-observatory-audit-run-3/README.md)).

## SCN-009 — Window closed, Dock click brings it back
Status: draft
Product: unobserved
Persona: P-01
Traces: ST-02, JTBD-01, FLW-05
Preconditions: the app runs and the dashboard window is not on screen — no window open, or only the
Assistant open, or the dashboard minimised.
Trigger: Dock icon click, or launching the app again.
Steps: reopen → the dashboard window is created (or un-minimised) and comes forward; an Assistant
window that is open stays open. A dashboard already on screen is left as it is.
Expected result: the app is never running without a way back to its window.
Errors & recovery: none expected; ⌘1 (Window → Dashboard) does the same.
Closing a window never quits the app (operator rule, 2026-10-16): with every window closed it keeps
running in the Dock, keeps reading its update record and keeps its idle restart; it quits only
through its own Quit (⌘Q) or a shutdown or logout. `AppLifecycleTests`
`testClosingTheLastWindowNeverQuitsTheApp` pins `applicationShouldTerminateAfterLastWindowClosed` to
`WindowClosePolicy` (false).
Coverage: native walkthroughs 2026-10-02 and 2026-10-03 (launch 1 window → closed 0 → reopen 1). Run 2
found that ⌘1, Window → Dashboard and Dashboard → Overview each ADDED a window (a WindowGroup); the
dashboard is one `Window` now — Close All → 0, reopen → 1, again → 1 — and the Window menu lists
Dashboard and Assistant once each ([run 2](../reports/2026-10-03-observatory-audit-run-2/README.md)).
The decision is the dashboard's own window, not AppKit's `hasVisibleWindows`: that flag counts the
Assistant, so with only the Assistant open a Dock click brought nothing back (audit 2026-10-05, A47).
`DashboardReopenTests` (`macos/Tests/ObservatoryCoreTests/DashboardWindowTests.swift`) asserts the
decision — Assistant only → open, minimised → restore, dashboard visible → nothing; the single-window
source check is `testTheDashboardIsOneWindowThatEveryCommandBringsForward` (`ModelTests.swift`). The
reopen itself (SwiftUI creating the window) is verified by the walkthroughs only.

## SCN-010 — An update is waiting: restart, quit or idle installs it
Status: draft
Product: unobserved
Persona: P-01
Traces: ST-02, JTBD-01, FLW-05
Preconditions: the app is open; the engine's maintenance pass verified a newer app and recorded
`waiting-for-quit` with its `pending` version in the workspace's `store/maintenance.json`.
Trigger: the app's launch or its check every 5 minutes (operator rule, 2026-10-17) reads the record.
Steps: once per version, while a window of the app is on screen (or at the next minute one is), a
prompt asks «Restart to finish updating» with **Restart now** and **Later**; Restart now is the same
act as Restart to update below, Later is remembered for that version and asks no more. **Restart to update** appears in the app menu and in the dashboard's toolbar (its help
names the version) → the person chooses it → the app quits, a detached helper waits for it,
runs `full maintain app` and opens the new app in front. Or: the person quits → the same swap,
nothing reopens. Or: no window on screen, the app in the background, no input and no work for
30 minutes → the same swap, the app reopens with no window and no focus.
Expected result: a person who keeps the app open is never left on an old version, and nothing
they are doing is interrupted: an open window, input in the last 30 minutes or a running
question or build keeps the idle restart from happening.
Errors & recovery: a swap that does not happen (refused bundle, another pass holding the lock
for > 5 min, an engine failure) opens the old app again and logs `update_install failed` with
its reason code; a helper that cannot start keeps the app running, logs `update_restart refused`
and the toolbar's help says the update installs at quit; no newer pending version → no affordance.
Coverage: `AppUpdateTests` (`testOnlyAStrictlyNewerReadableVersionIsOffered`,
`testAStagedUpdateIsReadFromTheMaintenanceRecord`, `testIdleRestartNeedsAStagedUpdateNoWindowNoInputAndNoWork`,
`testRestartWaitsForTheAppSwapsAndOpensTheNewOne`, `testARefusedSwapReopensTheOldAppAndSaysWhy`,
`testAFailedEngineInTheBackgroundReopensWithoutAWindow`, `testAQuitSwapsAndOpensNothing`,
`testTheHelperRunsInASessionOfItsOwn`, `testTheLogHoldsCodesOnlyInUTC`), `UpdatesTests`
(restart, refusal, quit, no bundle) in `macos/Tests/ObservatoryCoreTests/AppUpdateTests.swift`;
`AppLifecycleTests` (`testAReadyUpdateAsksOncePerVersionAndOnlyWhenSeen`,
`testTheUpdatesWatchAsksThroughItsPresenterAndRemembersTheVersion`, `testTheUpdateRecordIsReadEveryFiveMinutes`);
engine `observatory/engine/tests/test_maintain_app.py`. Not yet walked on a real Mac with a
signed release: the menu item and toolbar button on screen, the prompt's sheet on the dashboard
window, the real swap and relaunch, and `--background` keeping the reopened window hidden.

## SCN-011 — The app speaks the person's language
Status: draft
Product: unobserved
Persona: P-01
Traces: ST-01, JTBD-01
Preconditions: none.
Trigger: launch; or Settings → Language.
Steps: the app opens in Russian when the first preferred system language is Russian, in English
otherwise → Settings → Language offers System / English / Русский → a choice re-words every window
at once, reaches the dashboard page through `observatory.locale`, survives updates, and the menus
macOS draws itself follow at the next launch.
Expected result: one language across menus, windows, alerts and the dashboard; counts in real
Russian plural forms; dates in the chosen language.
Errors & recovery: an unknown stored value is System; a string with no Russian entry shows in
English, never as a key; choosing a language on the dashboard page is adopted by the app.
Coverage: `LocalizationTests` (`testTheSystemLanguageDecidesWhenNothingIsChosen` — `ru-RU`, `ru`,
`en-US`, empty; `testAnUnknownStoredValueIsSystemAndTheOldSwitchIsStillRead`;
`testTheChoicePersistsAndPinsTheMenusLanguage`; `testEveryKeyTheAppUsesHasARussianEntryWithTheSamePlaceholders`;
`testEveryPluralEntryHasEachFormOfItsLanguage`; `testACountReadsInTheRightFormInBothLanguages`;
`testTheBuiltResourcesAreTheSourceDictionaries`); `ModelTests` for the page round-trip. Not yet walked
on a real Mac: the AppKit menus in Russian after a relaunch with Русский chosen on an English system.

## SCN-012: The app opens at login unless the person turned it off
Status: draft
Product: unobserved
Persona: P-01
Traces: ST-02, JTBD-01
Preconditions: none (operator rule, 2026-10-16, for every product that ships an app).
Trigger: every launch; Settings → Open at login.
Steps: the first launch registers the app as a login item (`SMAppService.mainApp`) and stores the
choice (`openAtLogin`, on) → at every later launch the stored choice decides: on registers again a
registration the system lost, off removes a leftover one and never registers → Settings → Open at
login switches it, with the line that the app keeps working with its window closed and quits from
its menu (⌘Q).
Expected result: the app runs from login without being asked; an update keeps the person's choice,
because the choice is stored, never re-defaulted. At login it opens as a launch from the Dock does,
with the dashboard window on screen: macOS starts a `SMAppService` main-app login item with no
argument the app could tell apart from a person's launch, so it is not started hidden.
Errors & recovery: a registration that fails, or one waiting for the person's approval, is SCN-013;
each act is logged by code in `~/Library/Logs/Project Observatory/app.log` (`login_item`
`registered`/`unregistered`/`requires_approval`/`failed`).
Coverage: `AppLifecycleTests` (`testTheFirstLaunchTurnsOpenAtLoginOnAndStoresTheChoice`,
`testAPersonsOffIsNeverOverriddenAndALeftoverRegistrationIsRemoved`,
`testAnUpdateKeepsTheChoiceAndALostRegistrationIsRestored`,
`testTheLoginItemGlueAppliesTheDefaultOnceAndLogsIt`); `AccessibilityTests`
`testSettingsReadsEveryControlByItsOwnName` (the switch is named in both languages). Not yet walked on
a real Mac: the app in System Settings › General › Login Items after a first launch, and opening at the
next login.

## SCN-013: A permission the app needs is missing at launch
Status: draft
Product: unobserved
Persona: P-01
Traces: ST-02, JTBD-01
Preconditions: none. Today the app needs no privacy (TCC) permission; the one grant it depends on is
the login item's approval (SCN-012). A new requirement joins `AppPermissions` with its test.
Trigger: every launch, and every change of Settings → Open at login.
Steps: the launch checks what the app needs → for each missing grant the dashboard window shows a
banner naming it and how to grant it («Project Observatory cannot open at login»: allow it in System
Settings › General › Login Items, or turn off Open at login in Settings) with **Open System
Settings**, which opens Login Items.
Expected result: nothing the app needs is missing silently: the banner, and an `app_permission
missing` line with the permission's code in `app.log`.
Errors & recovery: granting it in System Settings, or turning Open at login off, removes the banner at
the next check (the next launch or the Settings switch).
Coverage: `AppLifecycleTests` (`testAMissingLoginItemApprovalIsNamedWithHowToGrantIt`,
`testNothingIsMissingWhenGrantedOrNotWanted`, `testARegistrationThatFailsIsShownAndLoggedNeverSilent`).
Not yet walked on a real Mac: the banner on screen after switching the app off in Login Items, and the
button opening System Settings there.

## SCN-014: Pairing this machine with the network
Status: draft
Product: unobserved
Persona: P-01
Traces: OSS-94 (the portable scenario, which owns the states), NODE slice 5 (OBS-197)
Preconditions: a compatible engine (one that answers `full assistant ceremony`).
Trigger: File → Pair this machine….
Steps: the Pair this machine window shows the engine's screen for this machine's pairing session and
reads it again every five seconds while open: Start pairing → "Waiting for the hub" while no hub runs
pairing (nothing sent, nothing added) → with a hub, the six-digit code and the network fingerprint, The
codes match or They differ → the passkey prompt on the trusted machine (the new machine and its key
fingerprints, Sign with passkey disabled while the hub serves no passkey page) → Pairing complete.
Expected result: the window decides no ceremony rule and shows no progress the engine did not report.
Errors & recovery: a refusal shows Pairing stopped with its sentence and reason, an expiry The pairing
expired; Start again drops the ended session; a step the engine refuses is named in a banner.
Coverage: `CeremonyTests` (`testEveryStateTheEngineAnswersIsReadAsIs`,
`testEveryStateHasItsWordsAndOnlyTheButtonsItOffers`, `testTheWaitOnTheHubIsSaidAsSuchNeverAsProgress`,
`testAStepGoesThroughTheBridgeAndARefusalIsWorded`). Not yet walked on a real Mac.
