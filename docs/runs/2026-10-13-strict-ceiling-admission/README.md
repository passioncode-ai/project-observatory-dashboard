# Strict admission counts every observed provider fraction

`tools/openrouter.py::_cents_up` previously subtracted one hundredth of a cent
before rounding an observed provider ceiling up. That allowance could hide
actual exposure. With a held ceiling of $3.3301 and a $6.66 policy, a new $3.33
key was admitted and the total became $6.6601. There is no verified precision
contract that permits discarding that fraction.

The correction rounds the Decimal representation upward without an allowance.
Unknown, nonfinite, negative and out-of-supported-range ceilings remain unbounded.
Supported integers retain their exact representation. Close-value mutation tolerance
still avoids automatic repeated PATCHes, but does not reduce counted exposure.
Exact whole-cent input retains the existing positive issuance path.

## Verification

- `test_credential_doors.py::test_or_policy_counts_every_positive_provider_fraction`
  was added first: 11 assertions passed and four failed on the parent source.
  It covers exact cents, positive fractions, unknown values, blocked issuance and
  exact-headroom successful issuance.
- Five focused policy tests passed 37 assertions after correction; [focused.json](focused.json)
  binds the production source hash and names the checks. Additional tests-first
  probes failed on two negative values and four precision/range boundaries before
  those observations were conservatively made unknown.
- The original 5200-seed measurement on model source `6ee4e6c` completed 5198 cases:
  4307 PASS, 891 FAIL and two ERROR. It is not acceptance. All ten strict `(a)`
  failures were engine POSTs after externally changed observed ceilings, not a
  provider spontaneously changing a just-requested limit.
- [original-strict-replays.json](original-strict-replays.json) preserves each
  violating before/after amount. [candidate-replays.json](candidate-replays.json)
  binds the unchanged model to this production correction: all ten now refuse
  unsafe issuance, retain their original `(c')` convergence failures and have no
  strict `(a)` violation. No assertions, event distribution or seed was changed.

## Boundaries and next task

This is observed-ceiling admission, not complete provider reconciliation.
External mutations after observation are not serialized. The unread-provider
fallback to a saved ledger ceiling does not prove a current upper bound and
requires its own counterexample and refusal design. Two original `(L)` failures
remain: external changes raised unknown minted identities beyond their pending
reservations. Two original ERRORs remain recorded separately; the direct test
revoke adapter does not yet reproduce the CLI's OSError boundary.

Next: independently review the exact correction, integrate by a fix-only commit,
and measure the unchanged original suite on the combined source. Retain every
new refusal as a convergence failure unless an authorized, verified remedy
actually succeeds. No live provider operation or deployment was performed.

The follow-up [unread-exposure correction](unread.md) closes the independently
reproduced saved-ceiling admission gap. Its source and 47-assertion receipt are
separate from the fractional correction above.
