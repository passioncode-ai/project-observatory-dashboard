# Refuse rotation when a recorded policy has no current headroom proof

<sub>ssheleg skills — task-pipeline · evidence-docs · handling-secrets · ux-scenarios · copywriting</sub>

## Problem and bounded correction

Policy issuance and standalone issuance previously produced indistinguishable
ledger rows. A project or folder cannot safely identify historical policy scope.
The conditional [capacity model](../2026-10-13-rotation-capacity-model/README.md)
shows why a reservation alone does not make overlapping ceilings safe.

`issue-for-policy` now records `policy_binding` on new rows before minting, and
re-attests existing rows selected by the policy's explicit credential sources.
It stores schema_version1, fixed origin `issue-for-policy`, canonical policy SHA256,
normalized project, mode/period/limit snapshot, recorded_at and headroom `not-verified`.
No arbitrary policy fields or source path are retained. Identical policy applications
retain the previous timestamp; changed policies explicitly update the snapshot.
Dry-run writes no binding. A failed binding write prevents provider mutation.

Rotation of any row containing the binding field refuses before provider calls or
ledger changes. Stale, malformed and unknown binding versions cannot bypass this.
No headroom-admission path exists yet, so a valid binding also refuses. Standalone
reissue of a formerly marked row preserves the marker; it cannot silently erase
policy history. The record is historical provenance, never live capacity authority.

Unmarked rows are unchanged. No installation-time migration, name/folder inference,
force flag or runtime policy registry is introduced. Unmarked legacy and standalone
rotation can still exceed an unknown aggregate limit. Policy scope for those rows
remains OPEN. Other standalone mutation commands still lack universal policy
admission; this change does not claim otherwise.

## Tests and scope preservation

Thirteen dedicated cases cover pre-mint provenance, pre-request rotation refusal,
unchanged standalone rotation, explicit re-attestation, idempotent snapshot writes,
policy changes, dry-run, sanitized origins, failed/unknown mint, write failure,
malformed bindings, preserved provenance on reissue and untouched unnamed peers.
The first nine-test run failed four assertions and three missing-field checks before
implementation. The final thirteen-case suite passes.

Compatibility checks pass:16 conditional capacity cases,19 rotation accounting cases,
eight model-binding cases and12 consumer-revoke cases. The low-level rotation/revoke
positive fixtures now issue standalone keys so they still exercise real rotation
mechanics, while the new suite pins policy admission. The full original adversary
script, its RNG choices, seed range and invariant assertions are unchanged. Blocking
policy rotation is a safety refusal, not successful rotation or proof of convergence.
The full credential-door suite passed486 assertions on initial candidate `3a006c1`;
the later alias repair is covered by the final focused suite. Full5200 acceptance,
native/live/provider checks and installed behavior remain NOT_RUN.

Run `tests/run_portable.py --suite policy_rotation_admission --suite rotation_capacity
--suite rotation_accounting --suite j3_adversary --suite revoke_consumers --jobs 1`
from the engine test context (or use the repository-relative runner path). No live
provider credential or operational vault is used.

## Independent alias correction

Review of `3a006c1` found an existing unmarked ledger alias with the same account/hash
could rotate the marked identity. The regression failed before repair. Admission now
checks policy binding on every ledger row sharing a primary, predecessor
or successor identity. Exact identity aliasing is not inferred project membership;
a distinct hash keeps the standalone positive path. Both direct and reserved-identity
alias cases are covered, and no label/folder comparison is introduced.
A second review of `f6161ac` showed that local account labels can alias the same
provider credential. The final guard matches the exact hash across account labels,
consistent with the existing unresolved-rotation identity guard. A tests-first
real-door probe pins this additional refusal before provider reads.

## Exact source receipts

The accepted source is `1536adddaadb427a6830fa2343774801f87a60c8`.
[Review bindings](raw/review.json) record source SHA256/Git blobs and the independent
14-probe scoped acceptance; [focused tests](raw/focused-tests.json) record13 passing
cases. Independent review retained both earlier alias counterexamples and checked
the corrected guard with synthetic fixtures. No operational credentials were read.

The [exact-source128-case replay](raw/baseline-128.json), seeds7000000–7000127,
completed128/128:58PASS,70FAIL,0ERROR, exit1. All70 failures are the unchanged
convergence assertion `(c')`; this remains a failing adversarial run. The preceding
consumer-safety baseline had28PASS/100FAIL and remains in its own receipt. Absence
of ceiling, untracked-key and consumer-deletion findings in this bounded slice is
not general acceptance. Seed7000104 now passes because policy rotation refuses
before minting and invalid policy input is rejected unchanged; it does not prove a
capacity-transfer implementation. The original0.034>0.03 counterexample is retained
in the earlier baseline. Full5200 acceptance remains NOT_RUN.

## Exact next task

Scoped independent review is complete. Investigate remaining convergence failures
against the current producer and remediation contract without weakening invariants.
Separately design a read-only legacy re-attestation proposal binding an operator-selected
policy to exact ledger revision, account/hash and named source, reporting unknown
peers separately. Do not infer membership from a display name or folder. Any future
apply path needs its own CAS/durable receipt and cannot manufacture current headroom.
The strict ceiling and full holder/reconciliation protocol remain separate open work.

---

**Made with [ssheleg skills](https://github.com/ssheleg/sshlg-skills)**

- [`task-pipeline`](https://github.com/ssheleg/task-pipeline) — explicit provenance and admission repair
- [`evidence-docs`](https://github.com/ssheleg/task-pipeline) — scope and regression evidence
- [`handling-secrets`](../../../observatory/engine/skill/plugins/observatory-log/skills/handling-secrets/SKILL.md) — synthetic provider isolation, outside the skills family
- [`ux-scenarios`](https://github.com/ssheleg/super-ux) — rotation refusal contract
- [`copywriting`](https://github.com/ssheleg/super-ux) — human and agent guidance
