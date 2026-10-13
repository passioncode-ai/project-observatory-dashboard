# Rotation accounting: preserve uncertainty before external effects

## State now

Bounded source `f07ff79` passed independent counterexample review after the
corrections below. Nineteen focused rotation cases, 486 credential assertions and
49 key assertions pass. Earlier narrower and failed runs remain recorded.
No live provider or operational vault was accessed. Combined integration, release,
installed acceptance, explicit reconciliation and full rotation convergence remain
open; see [wave-four record](../2026-10-13-recovery-wave-4/README.md).

## Contract and change

Creation is a POST whose value appears only in the initial response according to
[OpenRouter's create reference](https://openrouter.ai/docs/api/api-reference/api-keys/create-a-new-api-key)
(read 2026-10-13). The reviewed schema provides no idempotency promise; treating an
unknown result as unresolved is our conservative decision. Hash-addressed updates
and ceiling/reset fields are documented in the [update reference](https://openrouter.ai/docs/api/api-reference/api-keys/update-keys).

A rotation writes intent before POST, identity before delivery, and a predecessor
exposure before deletion. Timeout/kill retains possible capacity and never deletes
a successor whose delivery may have happened. Repeated rotate refuses unresolved
state. Policy totals include an additional predecessor/successor or reserved unknown
exposure, and its command exits nonzero while the slot is unresolved. The fresh
provider ceiling/reset replaces stale local values; issue pending clears only when
completion is confirmed. All ledger mutator entrypoints share one local lock;
policy calls private locked implementations to avoid recursive acquisition.

`tests/test_rotation_accounting.py` reproduces failures first and checks retained
identities, actual mint attempts, totals, safe error text, lock refusal and pending
state. The writer-lock OS boundary carries the parent's portability correction.
`test_credential_doors.py` selected checks: rotation order, concurrent policy splits,
partial-policy recovery, ceiling journal/settlement hints.

## Remaining scope

This is not completion of the original rotation invariant. Unknown external effects
can leave overlapping live ceilings indefinitely; there is no reconciliation CLI
in this slice. Provider responses cannot restore a lost one-time key value.
No cross-machine lock or external dashboard serialization is claimed. Live-holder
revocation discovery, claim-by-slot/hash, show/usage and label-free presentation
are separate work. A lock does not enforce a project's policy on standalone limit
or issue commands; those existing semantics are unchanged.

Full acceptance must use the original rotation variant, not the no-rotate variant:
`python tests/adv_j1_property.py 7000000 5200 /owned/private/rotation-receipt.json`
from the engine directory, using the project's full interpreter and isolated home,
workspace and TMPDIR. The receipt records source hashes, attempted/completed seeds,
FAIL/ERROR and nonzero status. Before running it, extend its identity accounting to
recognize the explicit successor reservation and implement/test the reconciliation
contract; do not weaken overlap or convergence assertions merely to make it pass.

## Independent identity corrections

The reviewer reproduced a foreign predecessor hash returned by the provider and
an unbound successor value. Both regressions failed before correction. Rotation
now compares the predecessor to the locked ledger and checks SHA-256 of the
one-time successor before delivery or predecessor deletion. The hash contract is
also described by [OpenRouter key links](https://openrouter.ai/docs/guides/overview/auth/oauth#deep-link-to-the-users-key).
A mismatched successor leaves the reservation and returned identity unresolved;
it does not deliver, delete or repeat the mint.

A third probe claimed an unresolved successor under an alias and revoked it.
Claim now checks primary, predecessor and successor references; existing aliases
cannot mutate an identity reserved by any unresolved rotation. Both new claim
and pre-existing alias regressions failed first. Scope remains the local ledger;
external account actors are not serialized.

The broader compatibility run found a stale test injection into the public issue
wrapper after policy moved to its already-locked implementation, and a caller
contract regression wrapping a claimed-key ValueError. The fixture now injects
at the actual locked issue boundary; pre-mutation ValueError remains a caller
error. The success fixture now binds its synthetic plaintext to its real digest.
No production guard was relaxed to admit the older fake hash.

Correction checks: 16 focused rotation cases, the full credential-door suite (486 assertions) and OpenRouter key suite (49 assertions) pass through isolated portable workspaces. No live account was used.

## Accounting identity follow-up

A further independent probe made the predecessor exposure lookup return a foreign
hash with zero ceiling; the reader had accepted zero and dropped its reservation.
The exposure lookup now accepts a ceiling only for the queried identity. Foreign,
missing or malformed replies retain the known reservation, otherwise unbounded.
Primary hash lookup also treats malformed responses as unread rather than absent;
only the explicit provider 404 establishes absence. Regressions exercise both
readers and the real lookup path. An earlier fixture always returned the old hash
after rotation; it now follows the ledger identity as the actual lookup does.

Accounting correction acceptance: 19 rotation cases plus all 486 credential-door and 49 key assertions pass in the isolated portable runner. Independent exact-head rereview remains the publication gate.
