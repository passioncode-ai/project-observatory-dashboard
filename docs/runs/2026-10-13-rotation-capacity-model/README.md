# Conditional capacity transfer: safety is not guaranteed recovery

<sub>ssheleg skills — task-pipeline · evidence-docs · handling-secrets</sub>

## Follow-up status in the combined candidate

The record below describes the model's original source snapshot and its review
correction. Its proposed policy-provenance task is now implemented in the separate
[policy admission packet](../2026-10-13-policy-rotation-admission/README.md), which
supersedes that next-task status and the earlier bounded baseline for current diagnosis.
The model still adds no production reconciliation. The newer exact policy baseline
is 58 PASS / 70 FAIL; full 5,200-case acceptance remains open. Earlier receipts and
failed observations below are retained as historical evidence.

## What this unit establishes

The pure proposal and simulator in `tests/rotation_capacity_model.py` are test-only.
Every result is non-actionable. They import no provider, vault, file or network adapter.
They do not change rotation, add a remedy or close the original full adversary.
The source base is `e22f46fa877c72cf3d97c542d2ae2c3ad133896a`.

A [bounded receipt](raw/model.json) records exact model/test hashes, a successful
conditional trace, interruptions around every effect and explicit countermodels.
The [focused suite](raw/tests.json) passes 15 cases, including 275 interrupted traces
across 11 small numerical configurations and five effects with five boundaries each.
This is not the original 5200-seed gate. Its latest 128-case baseline remains FAIL:
36 strict-capacity, five representation and 99 convergence failures, overlapping case
counts, in the [prior receipt](../2026-10-13-consumer-safe-revoke/raw/baseline-128.json).

## Provider facts and assumptions

As checked 2026-10-13, the [create reference](https://openrouter.ai/docs/api/api-reference/api-keys/create-a-new-api-key)
says the plaintext appears only in its creation response. Its documented request
has no disabled field; a nullable numeric limit does not establish zero-limit
behavior. The [update reference](https://openrouter.ai/docs/api/api-reference/api-keys/update-an-api-key)
accepts limit and disabled by hash, as a separate operation. The
[single-key read](https://openrouter.ai/docs/api/api-reference/api-keys/get-a-single-api-key)
provides identity metadata by hash. These documents do not establish a transaction
across creation, delivery and predecessor deletion, or a recovery of lost plaintext.
No authenticated request or operational secret read was made.

The arithmetic model assumes bounded cap writes, authoritative current reads and
exclusion of outside writers. Boolean model inputs declare these assumptions only;
they are not observations, grants or production admission. Provider overshoot and an
outside writer each have an explicit failing countermodel. Production admission
cannot replace these missing contracts with `true` flags.

## Conditional safety proof

Let L be the policy ceiling, O other capacity, C predecessor capacity and S a positive
whole-cent successor cap. Reject already-over-policy input. Choose positive R no
greater than C such that O + R + S <= L, rounding R down to whole cents. A reduction
requires a separate availability contract; spare existing headroom can avoid one.

1. Record lowering intent while retaining C; lower the predecessor to R and confirm.
2. Reserve S before minting once. A lost response retains that reservation, not an
   invented identity. Possible capacity remains O + R + S <= L.
3. Deliver the bound value and confirm all old holders have switched. Unknown delivery
   never authorizes deleting the possibly consumed successor.
4. Delete the exact predecessor only with complete holder proof. An unknown deletion
   retains R until authoritative absence is recorded.
5. Restore the successor to C only after predecessor absence. Capacity returns to
   O + C, which was within L at admission.

Before/after intent, effect and confirmation interruptions keep the largest possible
capacity and stop the model. This models durable-record behavior abstractly; it does
not test filesystem durability, two-process locks or real provider read consistency.
A consumer already above reduced R may stop working; a test with usage35 and R20
shows that availability loss even though the arithmetic remains safe.

Lost creation can leave no usable value or known hash. Even after faults cease,
read-only reconciliation may be unable to recover either. Therefore this protocol
does not prove unconditional convergence. Restoring C after an unknown20 mint would
raise a40 total to60; the retained reservation prohibits that blind rollback.

A one-cent predecessor at a full ceiling cannot split into two positive whole-cent
caps. No unproved zero-cap creation is substituted. The original seed7000104's
invalid policy partition (0.03/four slots) remains separate from its valid-policy
strict rotation counterexample (actual0.034 >0.03); neither is rounded or suppressed.

## Strongest bounded next production task

`tools/openrouter.py` currently reads a caller-supplied policy only in
`cmd_issue_for_policy`; `issue_for_policy` stores no policy origin, version or digest
in its issued row. `CapacityModel.test_existing_ledger_has_no_policy_origin_binding`
compares real synthetic policy issuance with standalone issuance and gets identical
ledger rows. Project/folder names therefore cannot classify legacy policy-managed keys.

The next correction must record explicit policy-managed provenance under the existing
writer lock when this policy path creates or re-attests rows: schema/version, sanitized
origin kind, normalized project, exact policy digest and snapshot metadata. A snapshot
is not a fresh aggregate headroom proof. Rotation of a marked row should refuse before
POST until an independently designed proof path exists. Do not classify or disable
unmarked legacy/standalone rows by folder/name; their policy scope remains unknown.
No provider mutation, automatic migration or recovery replay belongs in that change.

Acceptance: tests first for new policy creation, existing-row re-attestation, dry-run
non-mutation, origin/hash preservation through failures, stale/malformed provenance,
refusal before any provider effect, unchanged standalone rotation, and all documented
legacy limits. Then independent exact-commit review. A later explicit offline legacy
re-attestation plan must bind the operator-supplied policy to exact account/key identities
and show proposed changes before any apply operation is designed.

## Independent model correction

Review of `9d709ac` found that a successor larger than the final restored cap (50
then20 under a100 policy) reduced its recorded upper bound before the lowering
effect. The model now retains max(current upper, requested cap) until confirmation.
The new regression first failed, then passed at restore intent, before effect and
after effect. The suite now passes16 cases. The [initial receipt](raw/model.json)
remains historical; [review correction evidence](raw/review-correction.json) binds
the updated source hashes. This fixes the conditional model only.

---

**Made with [ssheleg skills](https://github.com/ssheleg/sshlg-skills)**

- [`task-pipeline`](https://github.com/ssheleg/task-pipeline) — isolated conditional state model
- [`evidence-docs`](https://github.com/ssheleg/task-pipeline) — proof boundaries and next task
- [`handling-secrets`](../../../observatory/engine/skill/plugins/observatory-log/skills/handling-secrets/SKILL.md) — synthetic provider isolation, outside the skills family
