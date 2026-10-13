# Database rotation failure boundaries

## State now

Implemented on an isolated branch; independent review, rebase/integration and live
provider acceptance remain open. Next task: review the exact commit against the
failure matrix before updating the existing provider-door PR. No provider call,
operational vault access, installation or release was performed for this work.

## Contract and evidence

`tools/digitalocean.py` now distinguishes preflight refusal from an uncertain reset,
keeps an owner-only receipt before mutation, withholds untrusted error bodies and
continues independent consumers after a confirmed reset. Literal proof compares
current provider bytes and connection metadata; guessed environment names never
authorize replacement. Encrypted values and unknown pool endpoints are refused.
See [onboarding](../../ONBOARDING.md) and
[agent secret design](../../design/AGENT-SECRETS.md) for recovery and exit codes.

Test-first: the initial `test_digitalocean_safety.py` run reported 10 failures and
7 subcase errors across 10 tests. After implementation and receipt/peer-delivery
cases were added, all 14 tests pass. The existing fake-doctl suite passes 51 checks;
its positive literals now carry provable bytes, while opaque values remain negative
cases. Both suites use scratch state and fake provider seams, never live accounts.

Commands: `python observatory/engine/tests/test_digitalocean_safety.py` and
`python observatory/engine/tests/test_digitalocean_door.py`. Full engine/OS matrix,
independent adversarial review and live provider acceptance are NOT_RUN here.

A receipt is evidence, not a lock or distributed transaction. Concurrent external
rotation or app-spec edits can still race preflight. Unknown receipts require
read-only reconciliation; the command does not replay them. A failed temp-file
cleanup reports that an owner-only spec can remain. Source inventory and engine
document copies must be regenerated with this commit.

Final local checks: six engine-document-copy tests, source inventory `--check`,
public tree privacy (zero findings), syntax compilation and `git diff --check`
pass. The DigitalOcean source-contract function passes its three assertions when
extracted by AST; the unrelated provider suite was not executed. Privacy did not
scan history or use a private denylist.

## Independent review correction

App update/redeploy responses can also be lost after the effect. Three additional
tests failed five subcases before correction: committed-then-error for each app
mutation, the wrapper retry count, and malformed deployment phase output. Mutations
now make one attempt and retain per-consumer unknown outcomes, while local pre-call
failure is not_started and other apps continue. Recovery asks for read-only
reconciliation before retry. Deployment ids/phases are validated and ACTIVE is an
exact phase comparison; provider strings never supply a printable status. A duplicate --app flag also failed its new test by sending twice; consumers
are now deduplicated. All 18 safety tests pass after the fix. The transient-positive legacy fixture now places
both 503s on read operations; mutation 503s are intentionally not retried.
