# Log rotation filesystem safety

## State now

- Objective: prevent log maintenance from changing files outside its own unlinked
  regular-file paths. This path runs independently of the auto-cleanup flag.
- Reproduced at ab3ddfc: broken generation links redirected copy/truncate; a linked
  log directory was traversed; a hardlinked live log lost the other alias's content.
  Three synthetic tests failed before correction. This is not evidence of a live incident.
- Correction: validate source, every ancestor and the entire generation chain before
  mutation; refuse symlinks, nonregular files and hardlinks. Apply the same restrictions
  before permission repair, including explicit extra log paths. Copy/truncate uses a
  verified source descriptor and an exclusively created destination; sync the copied
  bytes before truncating the verified descriptor.
- Evidence: `test_lifecycle.OneRotationPolicy` covers refusal and ordinary rotation,
  with the existing maintenance suite exercising the actual caller.
- Open: independent source acceptance, hosted checks, release and installed acceptance.
  This does not establish atomic exclusion against every external directory writer.
- Next task: independently replay links, hardlinks, malformed late generations and
  normal rotation, then integrate only the accepted source.

## Boundaries

The existing copy/truncate contract can lose a concurrent append between copying and
truncation; a lifecycle change to make the producer reopen its log is separate work.
Log sweep refusal still uses the existing boolean/list API; maintenance's swallowed
rotation exceptions need a separate observable result contract. No live log was
rotated or removed by this change. A missing source is not a successful rotation.

## Hosted portability correction

The full hosted OS boundary check rejected the platform flag lookup in931c1d6.
Both descriptor opens now use `osprivacy.NOFOLLOW`, preserving the existing POSIX
refusal while invoking the Windows adapter's own link check and binary mode.
`test_osprivacy.Boundary.test_every_open_goes_through_osprivacy` is required beside
the filesystem regressions; the rule was not weakened or excluded.
