# Bounded local upstream graph observation

Status: scoped local implementation; independent source review pending.

## Objective and dependencies

Build on accepted checkout local-state and offline-profile source `284bc39`.
Discovering a checkout does not establish abandonment or remote durability. This
unit records only the relation between the current local HEAD and its locally
resolved configured upstream, with explicit incompleteness and source age.

Git's [rev-list documentation](https://git-scm.com/docs/git-rev-list) defines
left/right counts over the compared graph. The [fetch documentation](https://git-scm.com/docs/git-fetch#_configured_remote_tracking_branches)
explains remote-tracking refs being updated by fetch; their existence is not a
new remote observation. [rev-parse](https://git-scm.com/docs/git-rev-parse) provides
root/common-directory and shallow-repository queries used in the binding.

## Scope and implementation

`checkout_graph.py` uses the explicit offline safe_git profile. Its bounded
commands refuse diagnostics, timeout, oversized output or failed object reads.
No remote URL, ref name or commit subject enters aggregate coverage. Replacement
interpretation is disabled for inspection, while any replacement refs, grafts,
shallow/promisor markers or alternate object stores cause explicit unknown. This
policy does not reinterpret those graphs as complete ordinary history.

Supported graphs yield two bounded nonnegative integer counts. Before/after
bindings cover root identity, effective root, Git/common directories, HEAD,
upstream identity and effective configuration hash. Results remain point-in-time;
concurrent writers can change state after the final observation. The comparison
excludes other branches, tags, stash, reflogs and untracked content. Shared storage
is never an independent recovery copy. Detached/missing upstream is not abandonment.

Inventory publishes local-state before a separate graph phase. The total scan
budget reserves at most a quarter (capped five seconds) for graph work. A distinct
persisted cursor advances before each bounded child attempt; failure cannot erase
already published local-state rows. The coverage CLI/MCP retains the graph's own
age and source binding. Remote durability stays unknown, actionability false,
allocated/reclaimable bytes unchanged. No installed scan or cleanup is added.

## Validation and next task

[Checks](checks.json) record actual focused execution. Tests-first began with six
missing-module failures. Fixtures cover equal/ahead/behind/diverged local graphs,
stale remote tracking after a separate local writer pushes, missing/detached/unborn
state, history exclusions, drift, malformed counts, saved freshness and publication
under graph failure. All provider and filesystem inputs are synthetic.

Next: independent exact-source review, current/history privacy and draft stack on
PR269. Hosted/platform/installed acceptance remains separate. No merge or release
is authorized by this packet.


## First review and correction

Independent review rejected initial `885d1f6`: early discovery let the graph
phase consume unused discovery time beyond its declared five-second maximum,
and a saved binding missing directory/digest fields could still project counts.
The correction caps the graph phase against both total and phase-specific
absolute deadlines and validates every required directory/ref/config binding
field before displaying current counts. Source-hash read failure remains unknown.

A nine-root controlled-clock regression verifies exactly five seconds of graph
work per twenty-second pass and advancement past the last attempted root on the
next pass. Missing/null/boolean binding fields, changed candidate HEAD and failed
source hashing refuse counts; actual complete local graphs still project useful
results. The initial rejection is preserved; source review of the successor is
required before draft delivery.
