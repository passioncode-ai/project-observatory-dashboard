<sub>ssheleg skills — task-pipeline · evidence-docs · copywriting · agent-sync</sub>

# Confirmed non-increasing policy progress

A complete PATCH observation slightly above the requested share used to abort
before other safe decreases. This correction keeps the observed amount, moves
independent non-increasing peers and recalculates projected exposure before any
increase. The prior source is `e22b986e837125ce715a319bd7c47b5527081349`.

The fresh pre-PATCH row must match the recorded identity. A step classified as a
lowering must still request no more than that fresh supported ceiling. A complete
response above the requested amount is accepted only when it remains no higher
than the fresh pre-PATCH ceiling. Unknown fresh bounds do not authorize that
exception. Invalid, foreign, missing, wrong-reset or uncertain responses still
stop the run; no mutation is retried. Raises keep the strict planned bound.

Actual provider ceilings round upward for accounting. Each confirmed result
replaces its planned amount in both projected and resulting totals. Available
room is recalculated before increases. Two $8 keys can therefore both decrease
toward $2 and report $2.0004: a new $6 key is blocked under a $10 cap but fits
under $11. Neither original slot is falsely reported as satisfying its requested
share: `held-ceiling` exits 1, records the observed amount and names the remaining
provider precision question. Near-target read-only reruns retain that outcome.
CLI totals distinguish observations from remaining plans, and confirmed meter
hints use actual amounts. No provider label or credential field is added.

## Evidence

[Tests first](red.json): seven assertions failed on the prior implementation,
including a stale lowering that became a raise; four uncertainty controls passed.
[Focused checks](focused.json): 100 assertions passed, zero failed. The receipt
names all 13 functions that produced the prior 80-assertion result, plus the new
progression, CLI and partial-progress tests. Two expectation changes are explicit:
a valid non-increasing higher response now preserves actual evidence and blocks
unsafe issuance instead of aborting peers; three positive-fraction admission
cases label the existing unmet share as held rather than in-place. Their original
no-POST and strict-cap assertions remain unchanged.

[Priority replay](priority-replay.json) binds the unchanged model and helper
sources separately from the candidate production source. It replays 17 cases:
both original injected-save errors pass, all ten original strict admission seeds
still fail convergence without `(a)`, and external representation failures at
7001212 and 7004446 remain red. The first is reachable again after additional
safe progression. No seed, assertion, RNG operation, five-attempt bound or
provider behavior was changed. This is not a full 5200-case result for this source.
The two earlier full failure matrices remain immutable in the composed-source
measurement packet.

## Independent fresh-bound correction

Independent review rejected `0e53527`: missing, nonfinite or negative fresh
capacity could still conceal a decrease that had become an increase. The
[new regression](fresh-bound-red.json) reproduced five failing variants, adding
boolean and out-of-range metadata. A present, explicit provider `null` remains a
distinct unlimited ceiling and passes the finite-decrease control. Missing or
malformed fresh capacity now refuses before PATCH, even when the eventual
response could have fitted the request. This closes the unsafe admission
premise before an irreversible provider effect.

[A replay of all six corrective red assertions](fresh-correction-red.json) on
the exact prior blob also showed two $2.0004 observations plus $6 printed as $10.
The observed summary now counts each positive fraction upward and labels that
basis, displaying $10.02; exact per-key observed hints remain $2.0004. It no longer
hides the excess in a nearest-cent total. The initial 100-case receipt and
17-case priority replay above remain bound to the initial source, not this
correction. [The corrected focused receipt](fresh-bound-focused.json) records
107 PASS, 0 FAIL, including all previous 13 regression functions. Full 5200 was
not rerun on this correction. Independent re-review is required.

## Unresolved outcomes

| Boundary | Meaning and next step |
|---|---|
| Independent decreases stopped by a complete above-share response | This bounded source correction progresses them using actual accounting. Independent review is required. |
| Exact split plus a provider that always stores more than requested | Full convergence cannot be promised without a supported provider precision contract or an explicitly revised allocation. Do not round away the excess or keep issuing. |
| Active or unmeasured consumer | Retain refusal until authorized consumer remediation and complete current evidence; a test model cannot create real absence authority. |
| Unknown minted identity or rotation exposure | Preserve identity/reservation and block new admission until actual reconciliation; a saved number is not a current upper bound. |
| External mutation of an unobserved minted object | The original `(L)` assertion stays failed. The local door cannot prevent or immediately observe an external writer; later admission refusal is a narrower property. |

This packet uses offline synthetic providers only. There is no live provider,
installed-runtime, complete consumer-discovery or full-system acceptance claim.
Same-operation observations are not an atomic snapshot or a provider truthfulness
guarantee. The original strict cap remains unchanged.

## Next task

Review the exact candidate source and independently exercise actual-accounting,
fresh-identity and uncertain-effect boundaries. Integrate only the accepted
fix-only commit; do not substitute focused green checks for the preserved red
full acceptance matrices. Rerun a full measurement only when its evaluation has a
new decision to inform, not to search for a green seed range.

Skills: task-pipeline for the bounded red-to-green correction; evidence-docs for
source-bound receipts; copywriting for the explicit held outcome; agent-sync for
the shared proposed-string row. Humanization: on, own editorial pass; factual
boundary and next action retained. Brand lint exits 0 with pre-existing warning
noise; this does not claim the entire string register is complete.

---

**Made with [ssheleg skills](https://github.com/ssheleg/sshlg-skills)**

- [`task-pipeline`](https://github.com/ssheleg/task-pipeline) — bounded tests-first correction
- [`evidence-docs`](https://github.com/ssheleg/task-pipeline) — source-bound receipts
- [`copywriting`](https://github.com/ssheleg/super-ux) — explicit held outcome
- [`agent-sync`](https://github.com/ssheleg/agent-sync) — shared string lease

<sub>A star on [the bundle](https://github.com/ssheleg/sshlg-skills) helps.</sub>
