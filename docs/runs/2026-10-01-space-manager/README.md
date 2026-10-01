# Disk space manager — implementation packet

## Scope and source ledger

Requested: a dedicated Space section, inventory of build/package caches, manual
cleanup, low-space alerts and automatic safe cleanup below 10 GB while sessions
continue working. Authorization covers this feature and its opt-in on the requesting
installation; other installations retain an explicit opt-in. This source branch is
not a release or an installation. User data and machine configuration stay local.

Sources: `tools/cleanup.py` (existing Git/build cleanup, excluded from pressure
cleanup), `collectors/scan_machine.py`, `dashboard/machine_page.py`,
`tools/serverd.py` (loopback origin boundary), `workspace.py`,
`docs/ux/scenarios.md` and engine `docs/ux/portable-scenarios.md`, brand pack.
Contradictions: the older dashboard was read-only. The requested Space action is a
bounded exception: same-origin JSON actions on a fixed adapter allowlist, no shell
commands or paths from a browser. Credential and Git actions remain unchanged.

## Requirements and evidence plan

| ID | Requirement | Verification |
| --- | --- | --- |
| SP-1 | Separate Space destination, English/Russian, empty/loading/error states | Shell/i18n tests and browser inspection |
| SP-2 | Cache register: owner, path, measured bytes/time, protection and supported action | Synthetic inventories, missing/error/symlink fixtures |
| SP-3 | Preview, explicit manual execution, result/history | HTTP/CLI tests; stale preview and duplicate requests |
| SP-4 | Below 10 decimal GB triggers enabled automatic cleanup, target 15 GB | Threshold, target, cooldown, concurrency tests |
| SP-5 | Protect active/unknown consumers; no directory removal or broad prune | Fake commands, busy/unknown/symlink/remote-daemon tests |
| SP-6 | Persistent low/recovered/action notifications and honest before/after | Event history and insufficient-space tests |
| SP-7 | No cross-origin cleanup and no arbitrary command/path injection | HTTP negative tests |

## Design and safety decisions

Use existing PassionCode tokens and component classes. Space appears beside Machine
under System. Above the cache table: free bytes, alert, automatic mode and one
preview action. Below: measured cache sizes, protected reasons, cleanup history.
A size is occupied bytes, never a promise that every byte is reclaimable.
Falsifier: the page cannot distinguish a protected cache from an eligible operation,
or reports requested deletion as measured disk recovery. No new visual direction or
animation: one composition, existing typography and dense tables (variance 4,
motion 1, density 7). No Figma file is changed. Accessibility assertions are limited
to checks actually run.

Fixed adapters call owners' cleanup APIs, not `rmtree`. Unknown/unsafe caches are
inventory-only. Automatic mode excludes all legacy Git/worktree/build-directory
cleanup. Native tool locks are retained; never bypass them. Busy or unavailable
process inspection blocks affected operations. Local Docker BuildKit GC keeps its
native in-use protection and never removes images, containers or volumes. The
scheduler checks pressure frequently; interprocess lock, cooldown and remeasurement
prevent overlapping work and loops. Errors remain in the journal. No credential or
command output values are copied into the journal.

Native cache contracts reviewed: [uv cache safety](https://docs.astral.sh/uv/concepts/cache/),
[npm cache](https://docs.npmjs.com/cli/v11/commands/npm-cache/),
[BuildKit reclaimability](https://docs.docker.com/reference/cli/docker/buildx/du/).
Direct uv cache mutation is prohibited by its owner; cleanup uses its native lock.

## Resume

Implement and test the bounded engine, then same-origin routes/CLI/scheduler, then
Space UI and scenario/catalog coverage. Run the full portable gate, source inventory,
privacy and package checks. Publish a PR and private installation handoff; merge,
tag and installation follow the owner's existing release/CLA policy.
