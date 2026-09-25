<sub>ssheleg skills — task-pipeline · evidence-docs · agent-sync</sub>

# PB-032 continuation: suppression boundaries

Objective: finish the scoped suppression behavior promised by PC-035 without letting an accepted old sighting hide a replacement value. Baseline: [0.3.11 source](https://github.com/ssheleg/project-observatory-dashboard/commit/327d656f5f98a85d3aef982d6ff245c5f5515386), PR #61.

Reproduced: a rule matched only secret name and a path substring; replacing the value or extending the path still suppressed it. Several valid JSON documents with the wrong structure crashed the scanner. The extended `test_leak_coverage.py` on baseline source exited 1 (8 failures, 4 errors).

Implemented: exact location and workspace-keyed version identity; validation of rule structure; distinct same-name versions in files and SQLite; replay of previous content when known values or effective rules change, including expiry. No-salt sightings remain active. Legacy rules are refused with a warning; the operator must review and pin a current sighting, with no automatic migration.

Contracts: [onboarding](../ONBOARDING.md), [compatibility](../COMPATIBILITY.md), scenario OSS-13 in [portable scenarios](../../observatory/engine/docs/ux/portable-scenarios.md). The new private scan-state fields cause a full initial scan when upgrading. This does not rotate credentials or write the incident register.

Validation: `test_leak_coverage.py` (12 tests) and `test_leak_scan_incremental.py` (6 tests), synthetic inputs only. Full local gate: 55 portable suites, 59 top-level tests, private-denylist privacy check; all passed on the final source (gate exit 0, final line GREEN). CI validates the pushed revision.

Open: merge/release/live-upgrade are separate from this branch delivery. PB-032 as a whole remains partial: known-value matching is not a generic secret-pattern detector, and deleted Git history is outside this scanner. No production secrets, provider calls, or live scans were used for this repair.

Next task: review this PR and its required CI results, then follow the maintainer release process. Do not close the broader PB-032 package based only on the narrow suppression fix; retain its remaining coverage requirements. PB-070 is the next independent privacy work package.

---

**Made with [ssheleg skills](https://github.com/ssheleg/sshlg-skills)**
