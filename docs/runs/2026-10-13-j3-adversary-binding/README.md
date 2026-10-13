# Bind the full rotation adversary to the current provider door

<sub>ssheleg skills — task-pipeline · evidence-docs · handling-secrets</sub>

## Scope and source

Adapt only the synthetic full-rotation model `tests/adv_j1_property.py`; production
code is unchanged. The source base is `a0452763918ed5d88a8d6d371f2efde48b9bf543`.
The original invariants remain: strict post-rotation actual ceilings, represented
live identities, acceptance of valid cent budgets, convergence/idempotence after
faults stop, and no revocation of a consumed key. The required full acceptance range
remains seeds 7000000 through 7005199 inclusive. This bounded unit is not that gate.

The old provider minted short synthetic handles unrelated to a constant plaintext,
so the current hash/value binding correctly refused every apparent successor. The
adapter now uses distinct synthetic values with their actual SHA256 and consumes no
additional random draws. Existing fault selection, seed domain and numeric echoes
remain. Foreign filler identities remain abstract, as they are never minted here.

The observer distinguishes primary/predecessor/successor identities from an unknown
mint reservation. A reservation can cover only one matching synthetic successor
within its recorded capacity; wrong names, excess capacity and extra siblings remain
untracked. This is a test observer's uncertainty accounting, not production authority
to identify/delete keys by a name. It cannot make an actual ceiling violation pass.
An unreadable ledger makes the invariant unjudged, never an empty untracked list.

The old convergence check treated any error-free returned plan without an old remedy
as convergence. The provider door can return held-rotation plans, so success now also
requires every requested slot to have issued/in-place/ceiling-moved evidence. Held
rotation remains a convergence counterexample: no reconciliation command exists to
apply. The original echo/idempotence conditions and negative assertions remain.

## Tests-first evidence

Eight fixed model/regression cases first failed seven assertions on the old fake.
The corrected cases cover unique value/hash binding, successful delivery, bad identity
refusal, delivery failure, bounded unknown-mint reservations, excess/foreign siblings,
ledger read failure, strict ceiling counterexamples and non-convergent held rotation.
Seed 7000104 retains its separate invalid-budget rejection with unchanged state and
a planted mutation detector. In the full rotation variant that same seed may also
fail its strict rotation ceiling property; it is not suppressed or reclassified PASS.

## Bounded baseline and next task

The [complete synthetic receipt](raw/baseline-128.json) binds the model and provider
source file hashes to commit `77866893bcef182df163113b933ec509c4ef00f1`.
All 128 requested seeds, 7000000–7000127 inclusive, completed: **62 PASS, 66 FAIL,
0 ERROR**. The runner returned exit 1, as required for an invariant failure.
Counts below overlap; they are affected cases, not independent incident counts.

| Original invariant | Failed cases | First counterexample |
|---|---:|---:|
| Strict post-rotation actual ceiling | 36 | 7000002 |
| Convergence after documented remedies | 64 | 7000002 |
| Represented live identities | 5 | 7000021 |
| No revocation of a consumed key | 1 | 7000099 |

Seed 7000104 is FAIL: a successful rotation leaves actual synthetic provider limits
at 0.034, above both the 0.03 ceiling and the pre-rotation 0.03. Its separate
invalid-budget rejection remains preserved. Representation counterexamples include
renamed unknown mints and a reservation exceeded by a later sub-cent mutation.
Seed 7000099 deletes a key read by a consumer label beyond the first destination
listing; preserving this failure is necessary for the next live-holder repair.

Reproduce from this repository checkout with Python 3 and the existing test dependencies:

```sh
python3 observatory/engine/tests/run_portable.py --suite j3_adversary --jobs 1
python3 -m unittest discover -s tests -p test_j1_evidence.py -q
python3 observatory/engine/tests/adv_j1_property.py 7000000 128 baseline-128.json
```

The eight fixed synthetic cases pass. The root evidence wrapper passes and executes
its existing twelve evidence checks in an isolated synthetic home. Independent
review of commit `7786689` accepted the model binding and observer semantics only;
the production invariants above remain red. No random draws or seed coverage were
removed, and the no-rotation variant was not substituted.

Next: implement a separately reviewed reconciliation and strict-ceiling design using
these fixed counterexamples and the provider's actual API contract, then repeat the
full unchanged acceptance range. A reservation records uncertainty and cannot prove
that an unknown provider effect is absent, identify a key for deletion, or constitute
convergence.
Full 5200-seed acceptance, reconciliation design, live-holder revoke, policy across
writers and live/native/install acceptance remain open. No production/provider/vault
operation is performed by this task; every provider operation uses a synthetic fake.

---

**Made with [ssheleg skills](https://github.com/ssheleg/sshlg-skills)**

- [`task-pipeline`](https://github.com/ssheleg/task-pipeline) — full-adversary model binding
- [`evidence-docs`](https://github.com/ssheleg/task-pipeline) — source-bound counterexamples
- [`handling-secrets`](../../../observatory/engine/skill/plugins/observatory-log/skills/handling-secrets/SKILL.md) — repository synthetic provider isolation, outside the skills family
