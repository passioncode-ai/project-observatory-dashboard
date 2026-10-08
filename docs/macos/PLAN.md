# Project Observatory for macOS — delivery plan

Operator request: a standalone Mac application, its own agent and an MCP surface;
keep the conversational agent outside the dashboard. Complete specification,
implementation, verification and linked ecosystem documentation autonomously.

The existing public engine owns the implementation. No new repository or duplicated
registry is needed. Native SwiftUI controls consume the same assistant domain module
as CLI and MCP; the existing observer, provider budget and private workspace remain
the backend. Conversations are workspace-local, not a third-party memory plugin.

## Requirements

| ID | Outcome | Check |
|---|---|---|
| MAC-01 | Native app, launch/reopen, sidebar, conversation, settings and diagnostics | Swift build/tests and native UI inspection |
| MAC-02 | Missing/incompatible backend is recoverable, no hidden install or service takeover | Process bridge refusal tests and first-run walkthrough |
| MAC-03 | Ask about measured project/machine facts; show evidence and missing coverage | Synthetic answer, invalid citation and empty-source regressions |
| MAC-04 | Agent uses existing provider configuration/budget; disabled/unconfigured states stay explicit | Stubbed provider failures and zero-call refusal tests |
| MAC-05 | Durable local conversations; restart resumes pending/finished jobs | Workspace persistence/recovery tests |
| MAC-06 | Stop, timeout and late completion have distinct outcomes | Real detached job cancellation and race regressions |
| MAC-07 | Same assistant available over MCP/Fabric job tools and CLI | Exact-schema, stdio, real-client probe |
| MAC-08 | Dashboard remains observational; app can open it through existing integration | No chat routes added to dashboard; native action check |
| MAC-09 | Buildable app bundle, packaging and release/install instructions | Bundle identity, signature and clean-checkout build |
| MAC-10 | Ownership/docs/org catalog/knowledge links updated together | Cross-repo link map, focused gates, remote SHA receipts |

## Work graph and boundaries

1. Contract and scenarios → schemas, state machine, privacy/provider boundary.
2. Assistant core → bounded retrieval, structured answer, local records, CLI.
3. Job/MCP seam → typed arguments, idempotency, cancellation, restart behavior.
4. Native app → process bridge, UI states, references, settings, local history.
5. Verification → Python, Swift, native UI, packaging, MCP client, privacy gate.
6. Ecosystem docs → org-index role/build/test row and knowledge base product entry,
   reference owner docs; no speculative runtime changes to unrelated products.

Edges carry the schemas (1→2,3,4), shared core (2→3,4), published CLI contract
(3→4), exact candidate artifacts (2,3,4→5) and verified behavior (5→6).
No parallel agents or global profile stages are implied. Scope/evidence/deps/resume
are retained at every step. Safety and interface contracts are checked with focused regressions; no automatic remote
model call in tests. Build caches use one bounded task directory on this low-space
machine. Do not stop other sessions or overwrite their worktrees.

## Current status / resume

Shipped in 0.12.0 (the app opens on the dashboard); native walkthroughs ran on 2026-10-02 and
in the 2026-10-03 audits ([run 1](../reports/2026-10-03-observatory-audit-run-1/README.md),
[run 2](../reports/2026-10-03-observatory-audit-run-2/README.md)). Developer ID signing and
notarization are done: `.github/workflows/release.yml` signs the app with the organization's CI
Developer ID, notarizes and staples it, and 0.13.0 carried the first notarized download. Open: the
items run 2 lists for run 3.
