# J-1 evidence recovery and pending-row attestation

## Objective and provenance

Recover the uncommitted issue-policy fix-5 candidate without changing its original
checkout, then make synthetic acceptance evidence fail closed. The inherited
snapshot is commit `bb67f30`, based on `563c4976dd685001cd73b276825ef5cde0e2ee4c`;
PR #246 was at `6f30de4faa3f6b04d12c15d0da94375bdd972b7c`.
[inherited-snapshot.json](inherited-snapshot.json) preserves all ten file hashes.
That commit is an unvalidated transfer, not authored implementation or acceptance.
The next commit contains the authored corrections. No original file was changed,
no provider/vault operation ran, and no PR was pushed or updated.

## Authored corrections

- Named regression seeds retain ENVIRONMENT errors; the property gate requires
  every requested case to be judged before claiming success. Environmental failure
  remains different from an invariant counterexample, but both prevent acceptance.
- The four adversarial variants share `tests/adversarial_runner.py`. Each run emits
  JSON with variant, source commit and file hashes, requested/attempted/completed
  counts and per-seed PASS/FAIL/ERROR outcomes. Violations, crashes, incomplete
  evaluation, changed source and report-write failures exit nonzero. Both ranges
  and `one` use the same result contract. `adv_split.py` accepts only known variants
  and exits nonzero for reproduced failures. Its older JSON seed map remains readable.
- `adv_repro.py` has an import guard and measures R-F's first/retry ledger/provider
  counts; the former hard-coded zero is removed. It remains a diagnostic script,
  not the acceptance gate. Its redirected stderr is restored after each call.
- Seed 7000104 exposed an invalid generator input: $0.03 divided across four
  keys, each needing at least one cent. Invalid policies now form a separate
  rejection property: the door must raise ValueError without changing the
  provider state, write count or ledger. Valid policies still require convergence;
  no seed or environment error is suppressed. A planted mutation during refusal
  proves the new rejection property fails. All four variants retain the seed.
- `forget_pending_key` must persist `forget-pending-attested` before clearing the
  pending row. Journal failure refuses the clear. That record explicitly contains
  the operator's assertion, not provider-deletion verification. `forget-pending`
  remains the separate best-effort completion event after the ledger write. A
  later ledger failure can leave an attestation without a completed clear.

Shared ledger locks, cross-verb limits, provider reconciliation and rotation
accounting remain J-3. No claim here closes the whole money invariant. The
no-rotation+forget adversary is J-1 evidence only; the full variant and its 5200
historical seeds remain J-3 acceptance work.

## Verification

The eight new synthetic evidence tests first reported three failures and five
errors on the inherited state, including missing new runner functionality. Two
regressions demonstrated ENVIRONMENT cases printing PASS. The journal-ordering
regression demonstrated clearing the row before recording its reason. After the
correction all eight passed; four more tests cover seed 7000104, a planted
invalid-input mutation, a ledger failure after attestation and propagation of an
empty ERROR receipt through the split reporter. A root-suite wrapper runs them in an isolated child
so importing synthetic engine fixtures cannot replace the caller's environment.
They deny socket connections and use fake provider/journal data only.

Additional executed checks and exact scope are recorded in [checks.json](checks.json).
The initial 128-case no-rotation run correctly exited 1 for seed 7000104. Its
invalid-input domain was then separated and tested, rather than hiding the case.
The first broader credential check also caught an authored test assertion that
looked at a later issue event instead of the preceding forget completion; that
assertion was corrected before rerunning the same checks.
A bounded adversarial sample is never a claim that all 5200 J-3 cases passed.
Read the recorded exit codes and per-seed JSON before deciding acceptance.

## Exact next task

Independent review must inspect the authored diff separately from inherited work,
reproduce the planted false-PASS and journal-failure cases, and validate the exact
candidate head. Then the parent run routes integration with current main and
whether PR #246 is updated. No release, installation or live acceptance is claimed.

The final focused run passed the 2000-case issue-policy property, its named
regression seeds and round-4/fix-5 checks. The no-rotation+forget sample completed
all 128 requested seeds with zero violations/errors, including 7000104. Provider
deadline tests passed (18 tests). Evidence tests passed (12 tests) and their root
subprocess wrapper passed. Full rotation/J-3 acceptance was not run.

The full-tree private-denylist scanner still reports three existing identifier
matches in unchanged `collectors/scan_harness.py` and `tests/test_fabric_service.py`;
those are not fixed or whitelisted here. Check the scoped scan receipt for the
inherited/authored paths, and do not treat this branch as a green release gate.
