# Final holder coverage and registration-prune composition

## State now

This supersedes source-preparation checkpoint
`93223f5c095b9c688f24214aeb38f16bb9bc3e90`, without rewriting its checks or the
historical failed hosted run for `8aff34f`. Versions remain app 0.25.0 and plugin
0.22.1. Protected CI, merge, release assets and installed acceptance are pending.
No live cleanup, registration prune, provider call or service mutation was used.

## Accepted inputs and composition

- Holder source `bef50e25752ae5ac31aaa555f9adf481897d8cad`, followed by
  `5458f228193f960f05b16bc162c0f5dde5eea7db`: strict whole-process evidence and
  final target checks for both worktree tiers. Independent synthetic review
  passed 29 probes at the latter source. [Packet](../2026-10-13-holder-completeness/README.md).
- Prune source `0dbda978eceedb305f3b2fab76f4a961a11287d5`, applied as
  `df74d29eb08311f737c8e820412f93c506891061`: nonzero/failed Git execution
  reports failure, unknown effects and repository-wide registration scope, with
  no raw child output. Independent five-case review passed.
  [Packet](../2026-10-13-prune-receipts/README.md).

The scenario append conflict retains both accepted sections. Source inventory
was regenerated from the combined tracked files; onboarding copies are generated.
Production holder and prune patches are not rewritten during composition.
Combined checks: 10 holder/final-probe cases, all 61 Git-hardening cases (including
five prune cases), and six document-copy cases passed. The holder source also
passed its full 136-case disk-guard suite, 10 machine-estate cases and three OS
privacy boundary checks. [Structured receipt](checks.json).

## Limits and next task

Partial Linux process inspection can conservatively refuse cleanup. Missing
`lsof` cannot grant POSIX worktree removal. Final observations are not locks
against a newly arriving external writer; worktree idle age remains a planning
condition. Prune still has repository-wide scope and has no exact removed-count
proof. Windows Git worktree deletion does not use the disk guard's explicit
rename probe; its native handle behavior remains unverified. No blanket cleanup
enablement follows from these source tests.

Original full rotation reconciliation remains open: the policy source's 128-case
baseline retains 58 PASS / 70 convergence FAIL, and full 5,200 acceptance is
NOT_RUN. Earlier scoped successes do not close that program.

Next: independently bind this exact composition, pass the privacy gate against
its introduced history, fast-forward the existing release branch and use the
protected source checks. Never reinterpret a stopped watcher or prior candidate's
checks as acceptance of this source.
