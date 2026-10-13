# Findings and measurement recovery

## State now

This wave connects bounded configuration discovery, truthful scan receipts,
repeated guard findings and independent memory sampling. OBS-88–90 are reserved
execution IDs; OBS-87 remains partial. These changes do not authorize deletion.
Hosted, release and installed acceptance remain separate gates.

## Source and contracts

- [Findings recovery](../2026-10-13-findings-coverage-recovery/README.md): source `6021715`
  passed independent adversarial review. Harness coverage is bounded; omissions
  are explicit. Tick records integrity once, disabled scans without a fake start,
  and original measurement dates through merge. Guard history is streamed and
  repeated malformed records interrupt a failure streak. Stable finding IDs are
  deduplicated without hiding peer failures.
- [Memory freshness](../2026-10-13-memory-freshness/README.md): source `f3a29af`
  corrects four independent counterexamples to candidate `895a800`: a same-second
  failed attempt, future timestamps, child timeout and an older writer finishing
  after a newer attempt. RAM/swap/RSS use a cheap independent sampler, never a
  disk walk. RSS sums are not unique physical memory or reclaimable bytes.

Integration begins at `784355a`; component changes are applied without importing
intermediate historical blobs. The earlier accepted evidence/credential wave is
retained. Public source never contains operational receipts or private context.

## Verification and corrections

The first combined portable run found three integration fixture failures. Harness
and merge fixtures inherited the outer runner's registry/state paths; their nested
workspaces now explicitly own those paths. The tick fixture had assumed integrity
was omitted and every disabled scan ran. It now checks integrity exactly once and
requires disabled rows to retain null start/last-run with zero starvation streak.
The FIFO fixture has a platform-specific skip where mkfifo does not exist.
Production behavior was not weakened to make these fixtures pass.

Corrected portable suites pass: harness 19 cases, input-age 6, tick 38, memory
freshness 21, disk guard 123 (two platform skips), OS privacy 17. The root suite
passes 173 cases. Public current-tree privacy passes across 937 tracked files;
board validation preserves all prior 87 IDs and adds the three reserved IDs.

Evidence remains synthetic/offline. Failed runs are retained by the maintainer;
final combined acceptance must name the exact committed head.

## Remaining work

Unregistered checkout discovery and cache ownership/lifecycle proof remain
OBS-87 work. A directory's size or name does not prove it can be deleted. No
process was killed, system cache cleared or worktree removed by this wave.

OBS-60's lockfile includes Linux Tauri → GTK 3 → glib 0.18.5 paths. The
[RustSec advisory](https://rustsec.org/advisories/RUSTSEC-2024-0429.html) is fixed
from glib 0.20, but a compatible GTK/Tauri dependency set or maintained backport
still needs validation. No direct app reference proves only that narrow search;
transitive API reachability is unknown. The alert is not dismissed.

## Exact next task

Finish combined independent review and focused checks, publish through a protected
PR, then prepare a tagged fixed release and installed acceptance. Continue OBS-87
inventory and rotation accounting independently; keep auto cleanup disabled until
its fixed installation is verified. Human admission/provider gates stay explicit.

## Hosted dependency-graph correction

The full hosted suite found the pipeline contract test stopped reading at the first
rotation pass, which now contains integrity only. Its reader includes every planned
phase through the final pass; the existing deliberately dropped dependency and
planted-cycle controls still must fail. The harness now reads prior registry project
identities, so that read is declared with an explicit cross-tick attribution rule.
This changes the validation graph, not production scheduling or collector behavior.

The corrected portable pipeline, tick and harness suites pass; all deliberate inversion, dropped-read and cycle controls remain enforced.
