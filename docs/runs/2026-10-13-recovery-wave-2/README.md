# Evidence, credentials and memory recovery

## State now

This wave combines independently reviewed corrections to key-policy verification,
database rotation outcomes, leak source coverage and lesson evidence/atomicity.
Source acceptance is scoped; live providers, installation and the complete program
remain unverified. Cleanup remains the separate prerequisite wave.

## Contracts and component records

- [Key-policy evidence](../2026-10-13-j1-evidence-recovery/README.md): errors and stale
  source receipts fail verification; erasure needs its recovery attestation first.
  Rotation/accounting follow-up remains open.
- [Database rotation](../2026-10-13-do-rotation-safety/README.md): mutations make one
  attempt; ambiguous outcomes remain unknown in owner-only receipts; consumers
  are matched by provider values and metadata before reset. ACTIVE is not app health.
- [Source coverage](../2026-10-13-leak-source-coverage/README.md): unreadable and
  unsupported sources stay unmeasured while readable siblings retain their findings.
- [Lesson evidence](../2026-10-13-m7-recovery/README.md): lost evidence returns
  a proposal, archive/successor writes are atomic, active identity is unique, and
  concurrent erasure prevents later revisions through every ledger write door.

## Verification

The combined implementation at `cbc7ee9` passed five independent adversarial probes
and retained all 87 board IDs. The reviewer caught incorrect completed statuses
for OBS-52 and OBS-58; both now explicitly remain partly open.

The selected 20 portable suites passed after correcting the retention fixture:
197 unittest cases, 1,208 legacy assertions, no platform skips. The initial run
failed retention because its old fixture omitted required lesson fields, reused
one identity and claimed corroboration without supporting repositories. The final
fixture supplies valid distinct identities and explicitly seeds a persisted
observed row to test retention; live evidence admission remains tested separately.
The production guard was not weakened. The root suite passes 173 cases.

Eleven reviewed public technical identifiers were added to the privacy allowlist:
provider environment-variable names, database schemes/parameters, a synthetic
default account and a generic callback path. No credential values are admitted.
Current-tree privacy passes. Historical intermediate candidate blobs still failed
the maintainer identifier gate. Delivery uses the reviewed final tree on a new
branch from the cleanup prerequisite; old candidate branches remain intact. The
new delivery history must pass independently before publication.

## Next task and limits

Complete required hosted checks and integrate this source under branch protection;
then perform release and installed acceptance. In parallel, finish findings
coverage and independently timestamped memory measurements. No live rotation,
cleanup, migration or provider spend was used as a test. A synthetic receipt is
not proof of provider execution, application health or complete erasure.

## Hosted matrix correction

The full Linux compatibility run at `5e523cd` found a direct `os.open` in the
policy lock, outside the portable privacy API. The lock now opens through
`osprivacy.open` with the same access/create flags and mode. POSIX behavior is
unchanged; Windows uses the shared binary-opening boundary. The lock contention
and synthetic policy tests are rerun alongside the OS privacy suite. The workflow
header now describes the already configured checks-based release gate; executable
workflow behavior did not change.
