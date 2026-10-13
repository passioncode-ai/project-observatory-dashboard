# Rotation and inventory follow-up

## State now

This wave preserves uncertain provider effects and extends bounded read-only
inventory. Its parent findings/measurement wave is separately accepted. Original
work is preserved; no useful files, worktrees, caches or live keys are test targets.
Release, installed and live-provider acceptance remain separate.

## Accepted component sources

- [Rotation accounting](../2026-10-13-rotation-accounting/README.md), `f07ff79`:
  intent before POST; successor identity before delivery; unresolved exposure
  retained; no retry on ambiguity; local ledger writers serialized. Review caught
  foreign predecessor/successor identities, alias bypass and foreign exposure
  replies understating the total. Exact source passed independent counterexamples.
  The complete 5200-case rotation/convergence suite and explicit reconciliation
  remain open; the issue-only property suite is not substituted for them.
- [Checkout candidates](../2026-10-13-checkout-candidates/README.md), `aafcfd5`:
  explicit roots, fair bounded cursor, independent Git metadata classification and
  unknown absence when registration is stale. Review corrected stale empty receipt
  coverage and direct Git metadata roots misclassified as checkouts. No candidate
  is actionable. Size, owner, use, local state and upstream are unmeasured.
- [Scoped provider survey](../2026-10-13-heroku-scoped-survey/README.md), `da10f4c`:
  preserve untargeted rows and global measurement age; provider-only refresh cannot
  clear unmeasured local state. Review caught malformed list responses, negative
  or nonfinite quantities and repeated refresh losing the unknown-local warning.
  Genuine empty lists and zero measurements remain valid.

Component acceptance is synthetic/source evidence, not live-provider execution.
The integration uses fix-only changes on the accepted prerequisite tree, retaining
all earlier safety corrections. Onboarding additions from both branches are kept;
source inventory is generated after the new files are tracked.

## Cache coverage candidate

[Cache lifecycle coverage](../2026-10-13-cache-coverage/README.md), `a68c81c`, adds
only metadata inspection and saved size provenance. Configured roots and available
executables are declarations, not proof of ownership, regeneration or reclaimable
bytes. OS-managed/protected categories stay explicitly uncovered. No cache command
is run and this inventory cannot authorize cleanup. Independent source review passed after correcting FIFO source blocking, empty
configuration coverage and malformed prior-row recovery. Eleven focused cases
pass, including the three reproduced failures.

## Verification

The combined ten-suite portable selection passes: 205 cases and 189
legacy assertions, 0 platform skips. The root suite passes 173 cases. Board
validation, document copies, generated inventory and current-tree privacy (951
files) pass. All prior 90 board IDs are preserved; none is newly declared complete. After remaining component integration, the final committed head
requires combined independent review, privacy including new history, document and
inventory checks and protected hosted checks. Failed counterexamples remain in
maintainer evidence; they are not silently replaced by later green results.

## Remaining work and exact next task

Complete exact combined independent acceptance and publish a protected PR. Release preparation carries version changes separately
until this exact source is accepted. After integration verify signed release assets,
installed code and scheduled receipts before considering automatic cleanup.

OBS-58 still needs explicit unknown-rotation reconciliation and full adversary
acceptance. OBS-87 still needs lifecycle/ownership proof and reader integration.
Provider identity evidence and fresh measurement do not grant deletion authority.

The parent hosted pipeline correction is included: its parser now sees both
integrity and ordinary collector phases, and declares prior-registry harness
attribution. The combined cache (11 cases) and pipeline (37 assertions) correction
run passes. The previous ten-suite result remains valid for unchanged components.

## Hosted provider-boundary fixture correction

The complete hosted suite exposed four old rotation tests that supplied an in-memory
ledger, unbound successor hashes and the former delete-on-delivery-error expectation.
The corrected fixtures persist the authoritative ledger and use a hash-bound synthetic
successor with a finite provider ceiling. Failure assertions retain both identities
and verify that replay makes no provider call. Finalization still requires recovery
state and never exposes the synthetic value. Production code is unchanged.

Evidence: `tests/test_provider_secret_boundaries.py` (12 isolated cases); full hosted
checks must rerun on the corrected commit before integration.
