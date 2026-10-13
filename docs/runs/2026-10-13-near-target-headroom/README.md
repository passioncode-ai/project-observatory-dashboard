<sub>ssheleg skills — task-pipeline · evidence-docs</sub>

# Reconcile the existing target before blocked admission

The close-value no-op could strand a policy slot even when the provider could
satisfy its declared share. An observed $3.3301 counted as $3.34 but remained
untouched, so a new $3.33 slot was blocked under $6.66. The source baseline is
`5acbb8eaa011ace4dec551048416fe1299de3b00`.

## Decision and limits

Reapply the caller's existing target once when known finite projected exposure
blocks a planned issue or increase. Run these decreases before increases, using
the existing fresh identity/capacity check and confirmed actual response. Recount
actual exposure before admission. No share, split, aggregate cap, precision
allowance or provider assumption changes. The tolerance remains a no-op if
admission already fits, no increase is planned or aggregate exposure is unknown.

An irreducible higher observation still blocks issuance. An uncertain effect
stops the partial run. A fresh lower or malformed ceiling refuses before PATCH.
There is no retry loop within an invocation and no claim that repeated operator
calls converge when a provider cannot satisfy the declared allocation.

## Evidence

- [Tests first](red.json): five failing assertions on the baseline and one
  passing headroom/no-op control.
- [Focused checks](focused.json): 115 PASS, 0 FAIL, including all 13 functions
  from the earlier 80-assertion strict-admission suite, progression, fresh-bound,
  CLI and partial-progress checks, plus eight new cases. The new cases cover
  exact, rounded and downward responses; an irreducible floor; uncertainty;
  already available room; a stale lowering; and unknown fresh capacity.
- The existing positive-fraction admission fixture now explicitly represents
  an irreducible provider floor. It allows one corrective PATCH instead of zero
  mutations, while retaining no-new-POST, original strict cap and actual-value
  assertions. Direct numerical rounding assertions are unchanged. Verified
  actual decreases have separate positive controls; they are not a cap waiver.
- [Before](before.json) and [after](after.json): all 13 selected exact, downward
  and rounded-provider residuals failed on the baseline and pass with this
  correction. Each baseline trace contains a skipped above-share near target.
  Seeds: 7000906, 7001367, 7001538, 7002134, 7002311, 7002432, 7002478, 7002514,
  7002543, 7002785, 7003074, 7003447 and 7003549.
- [Twelve preserved controls](controls.json): three pass (7000104, 7001556,
  7003454); nine retain their original failures. Positive echoes, consumers,
  pending-identity attestation and external representation failures are not
  waived. Both 7001212 and 7004446 retain `(L)` in this sample.

The advanced model source SHA256 remains
`059eeddcea7a08bbe4e92ffa496fc243d72915c1976bb39b3b923be6fb0b88a9`.
Original assertions, RNG operations, seed identities and five recovery attempts
are unchanged. Receipt file hashes bind the tested working source; their base
commit alone does not contain this correction. All providers and identities in
these receipts are synthetic; no live provider or vault was used.

## Remaining work and handoff

Independent scoped review passed ten controls on `5f6f2a9`. The subsequent
[full measurement](measurement.md) is FAIL and does not permit full acceptance. Review the finite trigger,
unchanged fresh/actual guard paths and all accounting deltas, including dry-run
behavior. The owning integration agent should cherry-pick only the accepted
fix-only commit and preserve the earlier lowering-progress dependencies.

The original full matrices remain failed: 4307 PASS / 891 FAIL / 2 ERROR, then
3501 PASS / 1699 FAIL / 0 ERROR on the strict composition. Neither is replaced by
this 13-case correction. The focused correction did not rerun full 5200; the subsequent
[composition measurement](measurement.md) did and remains FAIL. The remaining consumer and
unknown-identity cases need separate identity-bound remediation evidence; exact
provider behavior alone does not make them convergence successes. A future full
measurement needs a new evaluation decision, not a search for a green range.

Next task: independently review this bounded correction, then classify remaining
consumer/attestation residuals using their complete traces and current source.
No implicit consumer absence, new force bypass or provider mutation authority
is granted by this packet.

---

**Made with [ssheleg skills](https://github.com/ssheleg/sshlg-skills)**

- [`task-pipeline`](https://github.com/ssheleg/task-pipeline) — bounded tests-first correction
- [`evidence-docs`](https://github.com/ssheleg/task-pipeline) — source-bound receipts

<sub>A star on [the bundle](https://github.com/ssheleg/sshlg-skills) helps.</sub>
