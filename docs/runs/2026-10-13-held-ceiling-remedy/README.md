<sub>ssheleg skills — task-pipeline · evidence-docs</sub>

# Follow the emitted held-ceiling command in the test model

The door now labels an unmet share `held-ceiling`. Its exact manual decrease
command was still emitted in the over-limit plan, but the test-only adapter
accepted only the old `in-place` label. This correction admits either label while
retaining every command, target, ledger digest, plan digest, exact identity and
positive-decrease check. It does not manufacture a command when none was emitted.

[Tests first](red.json): two assertions fail on source
`5f6f2a98a8ad952deeb153857d22c442276f0f4b`. The fixture obtains the plan from the
actual door, proves it emitted `limit NAME --set 10`, then applies the real
`cmd_limit`. [All 23 model helper tests pass](green.json), including no-emission,
foreign/unknown identity, would-raise, changed plan/revision, uncertain-effect,
consumer preservation and pending-attestation controls. Proposal discovery has
no provider writes or ledger effect. The actual corrective command makes exactly
one provider write to the already declared target and the next read is in place.

[Binding](binding.json): `limit_remedies` is the only changed model function;
production source is byte-identical to 5f6. Original assertions, random operations,
events, seed identities and five-attempt recovery bound are unchanged. The source
precheck is not a production CAS or external-writer transaction; no nested lock,
new provider hook, force bypass or live operation is introduced.

[Twelve original replays](replay.json): 7000008, 7000018 and 7000029 now pass;
7000104 remains an invalid-input positive control. Eight remain failed:
7000017/7000032/7000039 still lack an emitted remedy; positive-echo 7000000/7000002,
consumer 7000072, pending 7002628 and external `(L)` 7003708 retain their failures.
A truthful unknown/held result is not substituted for convergence.

The [prior full 5200 matrix](../2026-10-13-near-target-headroom/measurement.md)
remains 2102 PASS / 3098 FAIL / 0 ERROR. Independent review passed 11 direct controls and 12 original replays. The
[subsequent full measurement](measurement.md) remains FAIL: 2147 PASS /
3053 FAIL / 0 ERROR. The original gate has not passed.
Only after that review may another bounded full measurement inform the next
source decision. The no-order1/no-guidance product gap is separate work.

---

**Made with [ssheleg skills](https://github.com/ssheleg/sshlg-skills)**

- [`task-pipeline`](https://github.com/ssheleg/task-pipeline) — bounded tests-first correction
- [`evidence-docs`](https://github.com/ssheleg/task-pipeline) — source-bound receipts
