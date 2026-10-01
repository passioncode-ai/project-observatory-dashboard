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
| SP-8 | Bounded resource history, comparisons only across measured identical roots | Pressure retention and cache history tests |
| SP-9 | Exclude Git history and duplicate aliases from generated-data estimate | Synthetic footprint regression |

## Design and safety decisions

Use existing PassionCode tokens and component classes. Space appears beside Machine
under System. Above the cache table: free bytes, alert, automatic mode and one
preview action. Below: measured cache sizes, protected reasons, cleanup history.
A size is occupied bytes, never a promise that every byte is reclaimable.
Falsifier: the page cannot distinguish a protected cache from an eligible operation,
or reports requested deletion as measured disk recovery. No new visual direction or
animation: free space first, actions second, then the cache table using existing
typography and spacing. No Figma file is changed. Accessibility assertions are limited
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

## Completed implementation and verification

Implementation: `89a0b05`, extended inventory in `3b89e74` ([source](https://github.com/passioncode-ai/project-observatory-dashboard/commit/3b89e74)); [PR #109](https://github.com/passioncode-ai/project-observatory-dashboard/pull/109).
The [verification receipt](verification.json) lists every suite and the distinction
between the initial full run and focused retries: 198 latest suite results pass,
including 24 Space safety/HTTP/CLI cases; 84 root tests pass. The first full run
had two page-count/allowlist failures, fixed here, and one timeout, resolved by an
isolated retry. This is combined local evidence, not a claim of one green hosted run.

Commands: `python observatory/engine/tests/run_portable.py --report-dir OUT`, then
focused `--suite` retries recorded by name in the receipt; `python -m unittest
discover -s tests -v`; `python tools/update_inventory.py --check`; `python -m pip
wheel --no-deps .`; `python tools/check_package.py WHEEL`; `claude plugin validate
--strict observatory/engine/skill/plugins/observatory-log`; `node --check
observatory/engine/dashboard/space.js`; Python 3.11 compilation. Current-tree
privacy was checked with a maintainer-local identifier list. No values or machine
paths from that list are published. BuildKit and cooldown are explicitly reviewed
public technical terms in `tools/public-identifiers.json`. Synthetic example
corrections also present in PR #107 are retained to satisfy the current-tree gate.

The Safari walk used fictional caches and replaced native commands with no-ops.
It verified preview, cancellation, execution/history, protected reasons, zero-byte
recovery, language switch, keyboard focus and enlarged layout. No real cache was
removed by these checks. Desktop delivery remains OS-dependent; the journal
records requested/unavailable, never human receipt.

Canonical contracts: [onboarding](../../ONBOARDING.md#disk-space-and-cache-maintenance),
[scenarios](../../../observatory/engine/docs/ux/portable-scenarios.md),
[manager](../../../observatory/engine/tools/space_manager.py),
[HTTP boundary](../../../observatory/engine/tools/serverd.py),
[safety tests](../../../observatory/engine/tests/test_space_manager.py).
The docs/brand lint diagnostic and unsupported live checks are named in the receipt.
No native tool output, user cache register, private state or real dashboard capture
belongs in this repository.

## Exact next task

Review this candidate PR and the combined receipt. A contributor personally
acknowledges the CLA under `CONTRIBUTING.md`; the agent has not agreed on their
behalf. Merge through the normal PR policy, publish a tagged release, then update
the installation through the documented release updater. Only then enable
`features.space_auto_cleanup` for the requesting installation and verify the live
Space endpoint/registry and scheduler. Keep `features.auto_cleanup` off if only
cache maintenance is wanted. Do not install an untagged worktree or treat this
source branch as a running feature. Hosted CI, merge, release and installation
remain distinct checks.

Used: task-pipeline bounded implementation; ux-scenarios recorded OSS-23..25;
sheleg-design reused tokens and guided browser review; copywriting supplied the
EN/RU interface; evidence-docs kept receipts and limitations; agent-sync held the
handoff lease and released it. No delegated agents or unrelated skill route.

## Follow-up: resource growth and estimate accuracy

The resource audit follow-up extends this candidate with SP-8 (bounded minute,
hourly and cache-inventory history) and SP-9 (Git history excluded from the
reinstallable estimate, checkout aliases deduplicated). No new deletion adapter,
UI strings or cleanup authorization are introduced. The scope remains cache-only;
active project data, session history and simulator data stay protected.

The new footprint regression was watched failing: a synthetic 50,000-byte cache
was reported as 450,000 bytes when Git objects and duplicate aliases were present.
After the correction it reports exactly 50,000. Three new space-history tests were
watched failing before implementation and passing after it. Pressure samples are
recorded even when automatic cleanup is disabled. History corruption degrades the
operation instead of erasing evidence. See `resource-followup.json` for checks.

Runtime history is local-only. A cache-root change or failed measurement produces
no growth comparison; gaps are not interpolated. History starts when the new
monitor is installed, not at any earlier audit date. Existing hosted results apply
to their recorded SHA, never automatically to this follow-up.
