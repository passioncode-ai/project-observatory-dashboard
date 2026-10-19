# Offline Git checkout observation

Status: scoped independent source acceptance at `e23f5ae`; draft delivery pending.

## Reproduced boundary

On Git 2.55.0, disposable local `file://` partial clones made with
`--filter=tree:0 --no-checkout` show that the existing default `safe_git` profile
can download missing objects. `cat-file -p` succeeds and increases object-store
files from 4 to 8; `rev-list --objects HEAD` and `show HEAD:tracked` increase them
from 4 to 12. These are synthetic observations, not an established data-loss
incident or a reproduction in the cleanup execution path. The planned upstream
graph phase is not delivered here; ordinary commit-only graph queries may need
no omitted objects in this fixture.

Git documents demand loading in its [partial-clone design](https://git-scm.com/docs/partial-clone#_handling_missing_objects).
The [Git 2.55 manual](https://git-scm.com/docs/git/2.55.0) documents
`GIT_NO_LAZY_FETCH=1` as suppressing on-demand missing-object fetch, and
`GIT_ALLOW_PROTOCOL` as an explicit transport allowlist. This implementation uses
both: an empty transport allowlist also refuses the fallback if a Git version
ignores the newer lazy-fetch setting. The standalone reproduction and independent
21-control review used Git 2.55.0; isolated regression fixtures used Apple Git
2.54.0. A binary predating the lazy-fetch setting was not tested.

## Contract and scope

`safe_git.command` and `run` now accept explicit `offline=True`. The profile
applies both environment settings after caller extras, and rejects combining
`offline=True` with `write=True`. Checkout inventory and local-state queries use
it. The default profile remains unchanged for authorized fetch, ls-remote and
write callers. `PURE` identifies driver-enumeration behavior, not an offline or
immutable-object-store guarantee.

Missing required objects follow the existing typed unknown path; raw Git output
is not persisted by checkout observation. The profile is for trusted observation
commands, not a sandbox making arbitrary mutating arguments read-only. It does
not prove atomic exclusion of concurrent writers, visibility of all repositories,
remote durability or deletion eligibility. Existing authorized cleanup callers
are not migrated by this bounded change.

## Checks and next task

[Checks](checks.json) bind exact source hashes and tests. Initial regression run
failed on the real checkout query and lacked the new profile argument. Real local
promisor fixtures prove missing-object refusal with unchanged object-store
contents, unknown partial-checkout state, local positive behavior and unchanged
explicit remote operations. Removing only the lazy-fetch environment variable
still refuses transport and preserves objects on the tested Git binary.

Independent review at `e23f5ae` passed 21 real local Git controls, including
missing tree/blob/graph refusal, ordinary available data, actual checkout child,
transport fallback and configuration override attempts. This documentation-only
successor keeps the reviewed runtime and test blobs identical.

Next: current/history privacy for the delivery head, then a draft handoff
stacked on the checkout-local-state branch. No release, installed
observation, live remote or whole-system acceptance is claimed.
