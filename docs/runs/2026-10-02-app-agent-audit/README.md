# 2026-10-02 — native app, agent channel and dashboard: audit and repair

Objective: the operator reported that neither the agent nor the separate Mac app worked.
Audit every screen and every level — native app, assistant backend, MCP agent channel,
dashboard pages, HTTP server — fix what is found, and ship it. Synthetic names only in
this public record; operator-specific measurements stay in the private operations
repository.

## What was observed before any change

| Level | Symptom | Root cause (measured) |
|---|---|---|
| Mac app | banner `backend-invalid-response`, «Not connected» | the installed release 0.10.1 has no `full assistant` command (it lives on unmerged #112); the CLI prints `unknown step: assistant` on stderr, exit 2, and the bridge maps any non-JSON stdout to `invalid-response` |
| Agent (MCP) | `estate.survey` fails on every call | survey rows gained `organization`/`resources`; the published v0.2.0 item schema is closed (`additionalProperties: false`); the wire test used a fixture without an organization |
| Agent (MCP) | `observatory_status` unusable | 253,753 characters by default (178 projects); `observatory_machine` 168k, `observatory_recall` 90k, `observatory_findings` 78k — beyond what an agent host accepts as one result |
| Agent (MCP) | safety rules missing in the client | server instructions 2,476 characters on the branch, cut by the host at ~2,048 — exactly at the WRITE rule |
| Assistant | disk question answered «no disk data» | evidence trimmed from the END: the machine item went first, then findings, to keep 40 project descriptions |

## Requirements (frozen list; adding is free, removing needs the operator)

| REQ | Module | Requirement | Verified by |
|---|---|---|---|
| R1 | app | an incompatible or old engine is named as such with the next step, not `invalid-response`; a non-zero exit without JSON keeps the first stderr line | Swift bridge tests + native walkthrough against the 0.10.1 CLI |
| R2 | app | a completed answer renders without Refresh, and the transcript follows it | Model test + walkthrough |
| R3 | app | `busy`/`job` derive from the stored rows; polling retries; Stop→Send cannot leave a stuck or doubled poller | Model tests |
| R4 | app | a child that exits before reading stdin cannot kill the app (SIGPIPE) | Bridge test |
| R5 | app | one request id per draft until accepted; a timed-out bridge call kills its whole process group | Model + Bridge tests |
| R6 | app | answer Markdown rendered (inline, lists) without links opening by themselves; evidence ids shown in Sources; cost locale-independent | walkthrough |
| R7 | app | every backend code family has its own message and next step | Model test over the code list |
| R8 | app | Settings shows engine/protocol status, progress and success; typed paths resolved; language applies live without resetting the session | walkthrough |
| R9 | app | one main window; ⌘N is New conversation only | walkthrough |
| R10 | app | the length limit matches the engine (code points) and Send says why it is disabled | Model test |
| R11 | app | connection state is not colour-only; «no provider» points at the real configuration step | walkthrough (AX dump) |
| R12 | app | a project scope that left the list is cleared; initial load runs once | Model test |
| R13 | engine | evidence: per-source budgets, machine and findings before projects, every trimmed source named | assistant suite |
| R14 | engine | findings matched to a project by `subject`; `subject` and `action` reach the model | assistant suite |
| R15 | engine | measurement times read from the registry's real keys | assistant suite |
| R16 | engine | request index and `agent.ask` jobs pruned; conversations can be deleted explicitly; every limit has its own code | assistant suite |
| R17 | engine | `status` reports workspace availability; a missing workspace is `unknown-workspace` | assistant suite |
| R18 | engine | the job deadline cannot fire between the dialogue commit and the job commit | assistant suite |
| R19 | engine | `job`/`cancel` accept only assistant jobs; reconciled turn states are persisted | assistant suite |
| R20 | MCP | one `observatory_assistant_ask` implementation, typed errors on all three assistant tools, project list optional in status | wire suite |
| R21 | MCP | `estate.survey` meets its published schema on an estate with organizations | wire suite (red first) |
| R22 | MCP | an agent has a bounded entry point (`observatory_overview`) and a `detail: summary` page; `observatory_status` keeps the published «no limit means everything» and every default row meets the published item schema | wire + conformance_receipt suites |
| R23 | MCP | compact JSON in the text channel | wire suite |
| R24 | MCP | `observatory_machine` sections; `explainPid` answers only the explanation | wire suite |
| R25 | MCP | `observatory_findings` limit/cursor/subject; `observatory_recall` smaller default; `observatory_timeline` default 25 | wire suite |
| R26 | MCP | instructions under 1,800 characters with WRITE/spend first; stale counts and sizes corrected | wire suite length check |
| R27 | MCP | unknown project ids are typed errors in credentials/timeline | wire suite |
| R28 | server | a client that disconnects leaves no traceback (#107, carried here) | serverd suite |
| D1–D18 | dashboard | the 18 dashboard findings below | dashboard/render suites + browser walk |
| R40 | delivery | full offline suite, root tests, Swift tests, package and privacy checks green; hosted matrix green | receipts |
| R41 | delivery | native walkthrough of every screen in EN and RU at 900×620 and default size | screenshots reviewed + AX dumps |
| R42 | delivery | release, machine updated, app installed from the release build and connected to the installed engine | live check |
| R43 | delivery | docs/macos, CHANGELOG, HANDOFF and this record updated | doc checks |

Dashboard findings D1–D18 (from the browser walk, all eleven pages): finding text not
localised (D1); subject links missing for `heroku:`, `secret:`, `repository:` (D2); domain
tile counts a different set than the nav and page (D3); nav badges count other units than
their pages (D4); credential links to rows never rendered (D5); env «links» built from
folder names, not registry ids, and an unknown id opens a silent blank panel (D6); groups
fold only on env (D7); repeated conclusion lines (D8); raw English enum words in RU (D9);
a linked row lands under the sticky header (D10); a file-level env link highlights nothing
(D11); the copy toast cuts off the command's verb (D12); mixed RU/EN on mcp/creds/traffic
(D13); health queue lines cut mid-word, a value without unit, no route to the rest (D14);
filter options that select nothing, no «—» option (D15); machine tables become cards at
900 px and a raw ISO time (D16); plural agreement and the «+N свернуть» label (D17); a dead
sticky-offset measurement (D18).

Carried, not fixed here (a published contract revision is the operator's): `machine.mcp.refresh`
is declared `effect: none`, so hosts see `readOnlyHint` on a tool that probes and writes.

## Decision taken during the run: the survey contract stays

The first fix gave `observatory_status` a 25-project summary default. The full
offline matrix then failed `conformance_receipt`: the v0.2.0 manifest declares this
tool as what `estate.survey` runs on (`requiredFeatures: tool:observatory_status`),
its probes call it with no limit, and the published input schema requires no field,
so `{}` is a contract call meaning «every project». Changing that default is a
contract revision, which is not this change's to make. The default was restored —
with every row now cut to the published item schema, which the probe needs on any
estate that records an organization — and the bounded default an agent needs became
an additive tool, `observatory_overview` (about 5 KB on a 178-project estate against
165 KB for the unpaged survey). `detail: "summary" | "full"` stays additive.

## What shipped on this branch

Ten commits on `fix/macos-app-audit-20261002`, rebased from #112 onto 0.10.1, with #107
carried. The CLI's `full assistant` gained `delete`; MCP gained `observatory_overview`.

| Module | Outcome |
|---|---|
| Mac app (R1–R12) | done; the native walkthrough below covers every screen |
| Assistant (R13–R20) | done |
| MCP (R21–R27) | done; R22 changed shape during the run (decision above) |
| Server (R28) | done, carried from #107 |
| Dashboard D2–D18 | done; D1 partial: finding **types** are labelled in both languages and a test fails when a rule's type has none, but titles, details and actions are still English f-strings in the rule modules (about 116 producer sites); translating them needs message ids per rule and is carried |

## Checks actually run

- Engine, full offline matrix: `project-observatory full check` — **198 of 198 suites PASS,
  5,846 printed assertions, 581 unittest cases, 0 failures** (one run before the dashboard
  plural fix found 2 failures, `recall` and `conformance_receipt`; both are fixed above and
  the second full run is the one quoted). Live providers, external MCP host admission and
  secret rotation stay NOT_RUN, as the runner states.
- Root: `python -m unittest discover -s tests` — 85 tests OK. `compileall` on 3.14 and the
  touched modules on 3.11. `update_inventory.py --check` passed. Wheel + `check_package.py`:
  0 failures, 482 runtime files. `claude plugin validate --strict` on the three manifests:
  passed. Site checks (`docs/site/check.py --self-test`, `build_article.py --check`,
  `check_site_interactions.cjs`): passed.
- Privacy: `check_public_release.py --history --history-ref HEAD` — 0 findings (2,169 history
  blobs); the tree against a maintainer's private identifier list — 0 findings.
- Swift: `swift test --package-path macos` — 24 tests, 0 failures. The five new bridge tests
  were run against the previous bridge first: cancellation hung for 30 s and the test process
  died of SIGPIPE (signal 13).
- Native walkthrough (debug build, a copy of a real workspace, a real provider at a cost of
  well under one cent per answer): first launch; ask with answer rendered without Refresh
  while the window was inactive; Markdown and sources with ids; disk question answered from
  the machine snapshot; Settings with engine, protocol and readiness; Russian switched live
  with the session kept; 900×620 with Send reachable; File menu shows New Conversation, not
  New Window; delete with a named confirmation; Stop; agent disabled; dashboard for a
  workspace the server does not serve; and the app pointed at the installed 0.10.1 engine,
  which now reads «needs version 0.11 or newer … `project-observatory full update`».
  Screenshots carry real project names and stay in the operator's private repository.
- Dashboard: rebuilt from the same copy and walked in a browser — 0 console errors on 11
  pages in Russian, 0 `undefined`/`NaN`, no horizontal scroll at 1440 or 900 px, 0 of 428
  project links to an unknown project, 67 of 67 subject links landing on a rendered row.

## Open work and the exact next task

1. **Human step — the contributor's CLA box on the pull request.** Automation never ticks it.
2. Then: required checks green → merge → release PR 0.11.0 (version files, CHANGELOG,
   inventory) → tag → GitHub release with wheel and `SHA256SUMS` → `full update` on the
   machine → build the app from the tag and install it; check the app against the installed
   engine and `observatory_overview` from Claude Code.
3. Carried: D1 prose localization (message ids in the finding rules);
   `machine.mcp.refresh` is declared `effect: none` in the published manifest although it
   probes and writes (a contract revision); the 0.10.1 record's two items (`full update`
   re-running the server's `--install`; an authenticated update check).
