# OBS-87: explicit-root checkout candidate inventory

<sub>ssheleg skills — task-pipeline · evidence-docs · copywriting · skill-search</sub>

## State now

Source `aafcfd5` passed independent review after the two evidence corrections
below. Twelve focused cases pass; candidates remain non-actionable. Combined,
hosted and installed acceptance remain separate. The packet and initial checks
below are retained as historical development evidence.

## Bounded packet

`collectors/scan_git_hygiene.py:checkout_seeds` starts from repository/project registry
anchors. An unrelated checkout below an operator's worktree directory remains invisible.
This slice adds an explicit-root, read-only candidate collector. It never supplies
cleanup planners, labels abandonment or authorizes deletion.

The caller names every root. Persist the last attempted root before work, rotate roots
between passes, and retain a depth-first directory cursor with root/directory stat
identity. Bound total time, per-root time, entry count and depth; do not follow symlinks.
Stop traversal at a detected Git boundary. Resume offsets are hints rather than a stable
filesystem snapshot: changed directory identity resets that cursor and is disclosed.
Traversal is incomplete when a budget, permission, depth or identity check prevents it.

Each observed candidate carries its own attempted/measured times, stat identity, Git
classification or explicit unknown, registration evidence status, nullable allocated and
reclaimable bytes, and blockers. No disk sizing, content reads, remote calls, status,
process argv, ownership guesses or cleanup calls belong to this pass. A bounded child
uses hardened `safe_git` metadata reads; only that owned probe may be stopped on timeout.
An ordinary clone or linked/bare repository is still not an abandonment claim. Dirty,
ignored-only data, upstream containment, owner and active holder remain unmeasured.

## Synthetic acceptance

- Find a normal clone, linked worktree and bare repository under explicit roots.
- An unlisted checkout is a candidate; never a deletion recommendation.
- Empty/missing/unreadable roots and failed Git calls retain unknown and reasons.
- Symlink escapes are skipped and no target outside a root is traversed.
- A large first root cannot prevent a later small root across repeated bounded passes.
- Resume a bounded directory pass; replacing a cursor directory resets its identity.
- Preserve old candidate measurement times as stale when not observed in this pass.
- No allocated byte count or reclaimable estimate is invented; all actions are disabled.

Next after this slice: independent review, then cache ownership and richer candidate
readers. Full worktree safety/ownership and UI/MCP inventory are separate followups.

## Implemented entry point and checks

```sh
project-observatory full machine --checkouts --root /path/to/checkouts
```

`checkout_inventory.scan` writes `store/raw/checkout-candidates.json`. A single-writer
lock refuses competing scans instead of overwriting cursor progress. Git metadata is
queried in an owned subprocess through `safe_git`; its bounded group is the only process
this collector stops. Failed probes publish unknown classification, never clean state.
Fresh registration evidence names path membership without granting deletion authority.
Rows not observed this pass retain their measured time and are explicitly stale.

Defaults: 20 seconds total, 2 seconds and 250 entries per root, depth 8; 64 explicit roots
and 10,000 retained candidates maximum. Resuming a directory must replay its iterator
up to the saved offset; that replay is time-bounded, so exceptionally wide or constantly
changing directories can remain incomplete. Blocking kernel filesystem calls are not
preempted. These are disclosed limits, not an exhaustive machine scan claim.

Tests-first: six initial cases failed before the module existed. Final focused suite
passes ten synthetic cases, including real clone/linked/bare fixtures, cursor fairness,
symlink and tampered-cursor escape refusal, permission failures, preserved stale values,
CLI and partial registration evidence, competing writers and owned-probe timeout.
Compatibility checks: machine 59 and memory freshness 21 cases passed with jobs 1.
No live machine inventory or cleanup was used for these component checks. The
initial independent review was pending at this checkpoint; see State now and the
corrections below for the accepted source.

Remaining: dirty/untracked/ignored-only data, upstream containment, activity and owner
proof, allocated/shared blocks, typed cache lifecycle and dashboard/MCP readers. This
receipt is not fed into any cleanup planner. OBS-87 remains partially implemented.

Skills used: task-pipeline (bounded test-first slice), evidence-docs (receipt and limits),
copywriting (CLI wording), skill-search (route selection). Source inventory and public
pattern checks are run for this candidate; private/history checks belong to integration.

Source inventory and public pattern privacy checks passed (exit 0). History and private
denylist checks were not run in this candidate; installation and scheduling are unchanged.

**Made with [ssheleg skills](https://github.com/ssheleg/sshlg-skills)**

- [`task-pipeline`](https://github.com/ssheleg/task-pipeline): bounded inventory slice.
- [`evidence-docs`](https://github.com/ssheleg/task-pipeline): source and tested limits.
- [`copywriting`](https://github.com/ssheleg/super-ux): CLI wording.
- [`skill-search`](https://github.com/ssheleg/make-skill): route selection.

## Independent review corrections

Negative membership in a registration pass now requires a current top-level coverage
timestamp even when its repository list is empty. Independently current positive row
membership remains usable when other rows or the overall pass are incomplete. Unknown
registration coverage carries an explicit reason and keeps the inventory partial.

The Git probe binds its result to the candidate itself: a non-bare repository must
report inside-work-tree and an equal canonical top-level directory; a bare repository
must report an equal Git directory. Scanning a normal repository's `.git` directory or
a marker-like subdirectory can no longer classify inherited parent Git metadata as a
clone. Both review cases failed before correction. The focused suite now passes 12
cases, including real hardened Git child probes; no action authority was introduced.
