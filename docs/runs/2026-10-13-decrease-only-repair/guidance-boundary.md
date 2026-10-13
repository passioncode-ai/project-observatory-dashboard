# Held-share guidance: contract required before an automatic remedy

Source `153a1605fbbaf2caf1674a3f70cf2da3557934f3`, production SHA256
`602d50bd7f594c74585c10eff3388bd68a16d90a0d5e26cb85c4c50e826e31ba`.
Status: PROPOSED; no production change or live provider access.

Current exact/rounded/downward cases 7000017, 7000032 and 7000039 retain a
slightly higher observed share even though their aggregate fits and there is no
planned increase. The held outcome is truthful; the policy call intentionally
preserves the close-value no-op and emits no exact correction command outside
the over-limit paragraph. This is separate from the adapter's stale action filter.

[Actual command probe](manual-semantics.json), using the real
`cmd_limit` against a synthetic provider, establishes the boundary:

| Case | Result |
|---|---|
| Observed 10.004, explicit set10 | One PATCH, observed10, ledger10, exit0 |
| Missing or unread identity | Zero PATCH, exit1, ledger retained |
| Fresh actual9, explicit set10 | One PATCH raises to10, exit0 |
| Requested10, provider returns10.004 | One PATCH, exit0, ledger records10 rather than actual10.004 |

Source `_set_limit_locked:1316–1339` applies fresh identity/decrease/actual
confirmation guards only when policy arguments are present. `cmd_limit:1373`
calls ordinary `set_limit` without those arguments. The general command accepts
an explicitly requested target, including a raise; it is not a decrease-only
policy reconciliation operation. A preceding model observation is not an external
writer transaction. A local lock cannot serialize provider/dashboard writers.

Therefore appending an executable-looking command and declaring the issue solved
would overstate safety. A truthful proposal can name the existing requested target,
observed amount/time and missing fresh-decrease/actual confirmation prerequisites,
without executing or describing the operation as policy-safe. The existing manual
operator remains responsible for deliberate target changes. This does not make
original spontaneous convergence pass.

If the product needs an executable safe reconciliation path, define it separately:
explicit exact-target/decrease-only context, fresh hash-bound finite capacity,
refusal when requested target became an increase, same-operation actual response
validation, retained uncertainty after unknown effect, and truthful readback.
Do not invent a lower allocation or use saved ledger capacity as current exposure.
The original strict aggregate bound must still hold; external concurrent mutation
remains an explicit unpreventable boundary without a provider transaction.

Next decision: whether to ship information-only held-share guidance or implement
a separately scoped decrease-only contract. Tests must demonstrate the actual CLI
semantics, not rely on hidden helper preconditions unavailable to an operator.
Keep the latest full5200 FAIL and broad convergence FAIL until actual corrections
and faithful independent measurement settle them.
