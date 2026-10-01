# Native Observatory candidate — implementation and verification

Objective: a separate native Mac agent application, shared CLI/MCP core and local
conversation history, following the disk cleanup work. No conversational agent
was added to the dashboard. Start with [the product packet](../../macos/README.md).

## Implemented

- SwiftUI/AppKit app, native settings, English/Russian strings, conversation
  sidebar, scoped questions, answer/evidence/degraded-source view and Send/Stop.
- Shared bounded assistant in `observatory/engine/agent/assistant.py`; existing
  providers, configured budgets and Fabric jobs; private atomic dialogue storage.
- Input deduplication, busy refusal, cancellation races, journal-before-spawn,
  timeout, restart reconciliation, and saved history with explicit growth limits.
- Three additive MCP tools and existing Fabric job tools; no second MCP server,
  no rewritten immutable capability schema, no mutation tools exposed to the model.
- Reproducible app bundling, local ad-hoc signature check, and macOS steps in the
  existing CI matrix. CI trigger cadence is unchanged.

## Verification record

Machine-independent receipts are kept beside this file. Test results only apply
to this candidate's code; they do not establish that a tagged installation has it.

- Root Python suite: 85 tests passed, including the native source allowlist boundary.
- Focused core suites: assistant, interop, CLI compatibility and contract publication
  passed. Assistant has 13 tests; stdio interop now has 17, including new tool
  discovery, invalid input/trace propagation and readiness without job creation.
- Full offline run: 197 of 198 suites passed; the only failure was the old ten-tool count. After correcting the expectation and server instructions, wire_contract, assistant and interop passed. See [checks](checks.json).
- Swift: 10 tests passed (7 process bridge, 3 model state). The generation-race
  test proves that a late old-workspace response cannot replace the new state.
- Real Claude Code client: strict fixture MCP configuration, actual recorded call
  to `observatory_assistant_status`, protocol `observatory-assistant/1`, disabled
  fixture agent. See [client receipt](client.json). No live assistant-provider
  answer is implied. The host model itself incurred the small cost in that receipt.
- App bundle: debug and optimized release builds, Info.plist lint and strict ad-hoc signature verification
  passed. This is not Developer ID signing or notarization.

## Unverified / release gates

Native automation repeatedly returned `cgWindowNotFound` for both the candidate
and Finder, including after session recovery. Process launch and compilation were
observed; no screenshot, functional native walkthrough or VoiceOver pass is claimed.
The next visual check must cover SCN-001–005 in English/Russian at 900×620 and a
normal window, including stop/reopen and settings changes. A real provider answer
and hosted checks are separate from synthetic tests. The operational tagged engine
was not replaced with branch code. Developer ID/notarization and the human CLA
remain release gates under CONTRIBUTING.md.

## Decisions and boundaries

This is an advisory, bounded workflow, not an unrestricted autonomous shell agent.
Local conversation history is not canonical project truth. The published Fabric
capability set is unchanged; assistant tools are SDK-described additions with a
Fabric result envelope for jobs. This avoids pretending a future release tag has
already published new schemas. Native views use system controls, colors and font.
No private machine snapshots or account values belong in this public run. Two pre-existing example identifiers were replaced with synthetic values, matching the separately reviewed cleanup candidate; the denylist was not weakened. Native publication is restricted to seven explicitly reviewed Swift/source/test/build files.

## Resume / integration

The next task is the native scenario walkthrough once window access is available,
then resolve any actual UI defects before marking the PR ready. Review the exact
candidate checks and normal CLA gate; merge/release through the repository policy,
then install the tagged engine and app. Do not hotpatch the running installation.
Related org-index and Fabric Workspace documentation branches track candidate
ownership only. Their knowledge publication follows normal merge/scheduled sync;
no unmerged production submodule pin was moved.

Initial test-first attempt failed in fixture setup due to `/tmp` symlink rejection,
not the intended feature assertion. That attempt is not credited as a demonstrated
red feature test. Subsequent safety/race tests and integration checks ran against
resolved isolated workspaces; initial Swift compiler errors were fixed before the
successful build. This record does not rewrite those failures as passing runs.
