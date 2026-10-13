# Cleanup measurement coverage: bounded OBS-87 subset

Missing swap became a reported zero, and failed largest-file directory reads
could leave `complete: true`. This change preserves null plus reason/source
time, counts scan errors without error bodies, retains partial successes and
carries coverage through the dashboard, findings and bounded MCP overview.

`test_disk_guard.py CoverageTruth` reproduced the missing/zero, failed-root,
failed-entry, failed-directory and consumer defects before implementation.
After the correction, `CoverageTruth LargeFiles` passed nine tests (exit 0),
including measured-zero and complete-success positive controls. Final selected
`CoverageTruth LargeFiles Surfaces` passed 15 tests, exit 0; source inventory,
engine-doc sync and public-tree pattern checks passed (887 files, no findings;
no private denylist or history check). The existing
English/Russian incomplete/not-measured labels are reused; no visual redesign.

This is a source candidate. No live cleanup or machine survey was run. Full
worktree discovery and scheduler fairness remain open in OBS-87. The follow-up also preserves structural bare repositories and their descendants,
binds path plans to device/inode, and skips missing or replaced identities.
`SnapshotSafety` first failed five tests with nine failed assertions on the
previous behavior; a sixth test exposed the configured-subtree ancestry case.
All six pass with the shared guard. The final portable disk-guard suite passed
110 tests with two platform skips in 29.09 seconds on Python 3.14.7; see
[safety-verification.json](safety-verification.json). Source inventory generation,
engine-doc synchronization, public-tree pattern checks (887 files, no findings;
no private denylist/history check) and whitespace validation passed.

The shared tree inspection protects nested bare repositories and selected paths
inside them across its callers. The POSIX final identity check and Windows rename
probe remain point-in-time checks, not a filesystem lock against a concurrent
writer. A symlink-containing browser clone is now preserved conservatively.
Legacy build-action retirement and manual-archive repair are a separate concurrent
change; this candidate does not claim those are fixed. Next: integrate that change
and the portable-runner correction, then independently verify the combined commit.

Skills: task-pipeline (bounded test-first slice), evidence-docs (observed
coverage), copywriting (existing status labels and concise failure copy).
