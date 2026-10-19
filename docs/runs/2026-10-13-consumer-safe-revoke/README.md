# Preserve known consumers when revoking a rotated key

<sub>ssheleg skills — task-pipeline · evidence-docs · handling-secrets · ux-scenarios · copywriting</sub>

## Counterexample and scope

Synthetic full-rotation seed 7000099 on source `ee2a56c3b51570e8c377b4fe857b322fe69c8514`
violates the no-consumed-key-deletion invariant. Rotation changes the recorded hash,
but a predecessor label remains in the legacy ledger. The old revoke guard compares
that stale label with a current successor's destination-only witness, then deletes
the successor. `RevokeConsumers.test_current_successor_label_only_holder_after_actual_rotation_is_refused`
reproduces the actual issue → rotate → destination-only witness → revoke sequence.
Four first-run regression tests fail before the correction, covering this sequence,
missing/stale evidence and evidence changing during provider lookup.

Under the existing writer lock, revoke now obtains the current provider row bound
to the recorded hash before matching holders. Its label stays in memory; legacy
aliases remain additional conservative refusal witnesses. No new label or value is
persisted. Known hash, name or label witnesses refuse without printing those witnesses.

The saved `collectors/scan_openrouter.py` output supplies schema_version 1, scanned_at,
keys, destinations, destination_hashes and degraded. The reader requires these
collections, a timezone-aware measurement within 36 hours, and no degradation.
It refuses symlinks, non-regular files, files above 4 MiB and more than 10,000 combined
rows. It checks opened-file identity and metadata, then compares the complete receipt
digest after provider lookup. Missing, unread, malformed, changed or expired evidence
is unknown and prevents deletion. No provider error body or source contents appear
in the refusal. No unread-evidence override is added.

## Source-bound bounded replay

The [after-correction receipt](raw/baseline-128.json) binds the same 128 seeds to
source `23b4249f23d5b4ebc469fcdcc95b022c7deac0f8`: all completed, 28 PASS,
100 FAIL, zero ERROR, exit 1. There are zero no-consumed-key-deletion violations.
Seed 7000099 now preserves the consumer's key, but remains FAIL for convergence
because its later remedy lacks current complete consumer evidence.

All 36 prior strict-ceiling failures and all five representation failures remain
on exactly the same seeds as the [before-correction baseline](../2026-10-13-j3-adversary-binding/raw/baseline-128.json).
Seed 7000104 still records 0.034 against 0.03. The number of cases with convergence
failures is now 99: the old model remedies do not provide the newly required survey,
so unknown evidence prevents deletion. No seed, invariant or failure was removed.
Full acceptance requires a genuinely evidenced recovery protocol, not assuming that
missing evidence means an unused key.

## Verification and limits

The [dedicated twelve-case synthetic suite](raw/focused-tests.json) and eight model-binding tests pass under
`tests/run_portable.py --suite revoke_consumers --suite j3_adversary --jobs 1`.
Sixteen existing round-four/fix-five compatibility assertions also pass.
Tests include the known-unused positive deletion path, current and legacy holder
witnesses, source shape/currentness, permission failure, FIFO, symlink, size limit,
provider identity drift and receipt drift during lookup. All providers are in-memory
fakes and live socket connections are refused by the test fixture.

This is an OpenRouter saved-scan guard repair, not full consumer discovery. Live vault
slots, remote fingerprints, external writer races, reconciliation of unknown rotations,
strict rotation ceilings and full 5200-seed acceptance remain open. A current empty
scan means no holder in that survey only. Pending-row attestation is unchanged and
does not delete at the provider. No live provider or installed acceptance was run.

Next: independent exact-commit regression review, then integrate this correction
separately from the unresolved full reconciliation and consumer-coverage work.

---

**Made with [ssheleg skills](https://github.com/ssheleg/sshlg-skills)**

- [`task-pipeline`](https://github.com/ssheleg/task-pipeline) — consumer-safe revoke repair
- [`evidence-docs`](https://github.com/ssheleg/task-pipeline) — source-bound counterexample
- [`handling-secrets`](../../../observatory/engine/skill/plugins/observatory-log/skills/handling-secrets/SKILL.md) — synthetic provider isolation, outside the skills family
- [`ux-scenarios`](https://github.com/ssheleg/super-ux) — refusal contract
- [`copywriting`](https://github.com/ssheleg/super-ux) — operator and agent guidance
