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
worktree discovery and scheduler fairness remain open in OBS-87. Next: finish
the independently found bare-repository and planned-target identity safeguards,
then validate the combined cleanup candidate.

Skills: task-pipeline (bounded test-first slice), evidence-docs (observed
coverage), copywriting (existing status labels and concise failure copy).
