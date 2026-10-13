# Leak source discovery coverage

## State now

Implemented on an isolated branch; independent review and integration remain open.
Next task: independently review the exact commit and rerun the focused suites on the
combined integration head. Original worktrees and operational state were untouched.

## Behavior and evidence

`tools/scan_leaks.py` now uses explicit stat/scandir errors for optional source
metadata and logs. A present Git marker whose listing fails is unreadable, while an
installed tree without a marker explicitly reports its absent tracked-tree source.
Tracked paths use NUL separation; missing tracked files become coverage holes.
Successful sibling results remain visible and degradation reaches Health.

Four tests were added first in `tests/test_leak_coverage.py`; the initial run failed
six cases including permission, timeout and nonzero Git results. After the fix,
`python -m unittest test_leak_coverage test_leak_scan_incremental` from the engine
`tests/` directory passed 38 tests with a temporary HOME/OBSERVATORY_HOME.
The first combined invocation used different canonical spellings for HOME and the
workspace on macOS and failed one existing path-label assertion; rerunning with
both temporary paths resolved passed. No product change was made for that harness
mistake. No live vault or operational transcript was read.

[Onboarding](../../ONBOARDING.md) and the portable scenarios describe the coverage
boundary. This does not add new transcript formats or prove absence of unknown or
encoded credentials. Full engine/OS matrix and independent review are NOT_RUN here.

Final local checks: source inventory `--check`, six engine-document-copy tests,
public tree privacy (zero findings), syntax compilation and `git diff --check` pass.
The privacy run did not scan history or use a private denylist.

## Independent review correction

The review found regular-file checks still silently excluded wrong-type targets.
Three new tests (dashboard directory, tracked directory and a mocked FIFO log)
failed first. `source_file_stat` now records these as explicitly unmeasured before
any open; session JSONL targets use the same check. Submodule directory entries
are not claimed as recursively scanned. Both focused suites now pass 41 tests.

## Discovery boundary correction

Independent synthetic probes found that vault globbing hid unreadable ancestors
and the session walk hid directory symlinks. Two regression tests failed first.
Vault discovery now walks exactly three levels with a 100,000-entry ceiling;
unreadable ancestors, unsupported entries and limit exhaustion leave explicit
unmeasured notes. Session directory links are not followed and leave the same
coverage signal. A readable sibling still contributes values or sightings.
`test_vault_discovery_boundaries_are_explicit_without_reading` covers symlink,
FIFO and zero-budget boundaries. The focused coverage/incremental suites pass
44 tests. No live vault was read. Concurrent path replacement remains outside
this discovery-time check; this is not an atomic filesystem snapshot.
