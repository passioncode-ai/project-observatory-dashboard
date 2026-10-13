# Findings coverage recovery

## State now

I-2, bounded K-2 and K-7 implemented locally; independent validation remains open.
Base: cleanup candidate `72e2e72` plus leak coverage candidate `d02a44b`, combined
at `811ed20`. No deployment, provider mutation or operational scan is in scope.
Next task: independently review the exact combined head and rerun focused gates after integration.

## Owned sequence and acceptance

1. **Harness configuration coverage (I-2).** `collectors/scan_harness.py`
   currently skips unreadable/malformed tool configs and emits project paths in
   `.claude.json` names (`tool_plaintext`). Add explicit `degraded` rows naming
   the source without exception bodies. A bounded line fallback may recover
   names from malformed JSON/TOML, always `parsed: false`; it never establishes
   complete coverage. Include user settings env and configured project `.mcp.json`.
   Project-scoped names use a registry id where measured, otherwise a basename,
   never an absolute path. Git-tracked configuration raises the plaintext finding
   to critical; unknown tracking must be visible rather than asserted clean.
   Own collector, harness block of `build_findings.py`, targeted tests, finding
   type/English/Russian entries, scenarios and onboarding. Tests: duplicate TOML
   table with header, project-scoped header, user env, tracked project config,
   unreadable file, malformed non-object input, absent values in every output.
2. **Tick and merge input truth (K-2).** Current `tools/tick.py` already writes
   schema-2 per-collector receipts. Do not replace this producer. Inspect its
   disabled semantics before adding `tools/tick_findings.py`: only planned
   collectors can starve; disabled is not a successful measurement; null outcome
   plus no finish is interrupted/unknown. Unknown receipt schema becomes explicit
   degradation. Retitle step failures by not-started/stopped/failed; merge, emit
   and validate remain warning or above. Merge inputs expose measurement ages
   (local 2h, GitHub 6h, daily 48h), never freshen carried evidence. Tests cover
   planned versus not-planned, disabled, unknown schema, old local input and
   severe tail steps. Producer compatibility/rollback needs verification before
   claiming this slice complete.
3. **Guard coverage findings (K-7).** Last three summary rows each with
   `git.stopped > 0` produce one warning naming count/level. Guard degradation
   stays visible as info; only a Trash permission/FDA cause receives the Full
   Disk Access remedy. Other causes receive their own bounded next step.
   Verify carried Git evidence remains visible in the compact agent view.
   Own machine findings, tests and corresponding type/locale entries.

## Shared contracts and validation

This branch is the single writer of new finding types and locale keys for these
slices. Published findings use additive types; raw `degraded` records retain
`source` and `reason`. Existing partial-scan sightings must remain visible.
No canonical board or guarded register is edited here. Each slice is committed
separately with focused offline synthetic tests, inventory regeneration, locale/
document checks and public tree privacy. Parent integration owns full gates,
independent review, board leases and release acceptance. A passing synthetic
case does not prove provider/native/runtime behavior.

## I-2 implementation evidence

Three first regression tests failed on missing config rows; two malformed-shape
subcases subsequently failed and were repaired. The harness suite passes 18 tests
under synthetic HOME/OBSERVATORY_HOME. New rows expose `parsed` and `tracked`;
malformed/unreadable/oversized configs and unknown tracking leave coded degraded
rows. English/Russian titles and type labels share one owner in this change.
Git file tracking uses `safe_git` with a five-second timeout. No config is executed.
The scanner's existing non-credential skill paths are outside the path-free field
contract; plaintext rows and their findings carry project labels instead.

Residual boundaries: line recovery is deliberately incomplete, Git tracking is
not proof of a pushed commit, discovery is one configured directory level, and
filesystem checks are not an atomic snapshot. Whole-product/live acceptance is
NOT_RUN. K-2/K-7 are packet-only at this commit.

Additional validation: six engine document-copy tests, source inventory and public
working-tree privacy pass. Brand lint exits zero with 1,963 unresolved
string-registry warnings across the broad dashboard sources; this is not a clean
brand audit. The changed error title uses the error surface's concrete observation
and remedy; editorial pass retained its literal source placeholder. K-2/K-7 locale
ownership remains reserved to this branch, not a claim that those nodes are done.

Final focused run: harness plus i18n `CatalogIsComplete` and `FindingTitles` passed
29 tests. An initial whole i18n invocation failed because built-page tests require
the portable runner's seeded workspace; that run also caught two invalid English
catalog entries. Those entries were removed: English singular titles live in
source by contract, with Russian in the catalog. Built-page integration remains
NOT_RUN for this slice. Humanization: own editorial pass of the short error title;
no decorative framing or unsupported promise was added.

## K-2 implementation evidence

Four scheduling tests first failed (missing reader and disabled misclassified as
ran); three input-age tests first failed (missing age reader). Current targeted
coverage includes an actual synthetic `merge.py` invocation with a ten-hour-old
local input. The producer recognizes the watchdog's exact disabled receipt,
preserves last successful/start times and clears only the skipped streak.
`tick_findings.py` joins the recorded collector plan, rejects unknown schemas,
retains interrupted outcomes and preserves severe tail findings. Merge provenance
names each inspected input timestamp; legacy no-timestamp files use mtime, an
explicit limitation rather than a newly asserted measurement time.

K-7 is still packet-only. Scheduling age alone does not prove a live collector
is enabled now; findings describe the last completed tick's plan. The initial bounded slice left integrity outside rotation history;
the follow-up below closes that producer gap while keeping its first-step order. Rollback to legacy engines can reset streaks (documented).

K-2 verification: 31 focused scheduling/merge/rotation/catalog tests passed;
legacy `tick_failures` passed under the portable runner (22 assertions). Its first
run caught loss of the ordinary-step explanation; that explanation was restored
while core tail failures still state that the tick stopped. Document copies (6),
public tree privacy and diff checks pass. Input-age integration used a synthetic
workspace only. New suites are registered in `run_portable.py`.

## I-2 independent correction

The verifier supplied a 2,000-level synthetic JSON object that raised
`RecursionError` while naming fields. The new regression failed first. Traversal
now has explicit depth (64), field (10,000) and name-length (4,096) bounds; parser
recursion failures also become incomplete parsing with safe fallback. No exception
body reaches the report, and a readable sibling config keeps its credential names.
`test_deep_config_is_incomplete_and_preserves_sibling_credentials` proves this.

## K-7 implementation evidence

Three guard signal tests failed first. `machine_findings.py` now streams guard
journal summaries and retains only the last three for repeated-stop evidence.
Both per-action stopped counts and a stopped Git subprocess qualify. An invalid
journal row breaks the streak and produces an info coverage finding; it cannot
establish three consecutive stops. Guard degradation is grouped by source, and
only the Trash/Full Disk Access cause receives that consent remedy.

The compact MCP view was already implemented on the cleanup base: no duplicate
change was made. Its existing regression
`test_machine.Tool.test_the_overview_says_when_the_git_survey_is_partial` passed.
The K-7 focused selection (new guard tests, existing machine findings, compact
carried evidence, i18n catalog/title checks) passed 18 tests. No cleanup was run.

## Integrity receipt follow-up

A no-execution synthetic `steps()` regression first failed because integrity was
absent from the plan and receipt. Integrity now runs alone through the receipt
writer before any ordinary collectors; its plan entry survives the second phase,
and it is never reordered behind them or executed twice. A never-started integrity
check increments its own streak and retains null last completion. The regression
uses callbacks only and deliberately stops before merge; no real collector runs.

Integrity follow-up verification: 25 focused receipt, order and bounded-rotation tests passed.

## K-2 independent correction

The verifier found the actual sessions producer uses `scanned_on`, a date-only
field. Copying an old sessions receipt refreshed its file time and hid its age.
New helper and actual-merge regressions failed first. Declared `scanned_on` and
`observed_on` now survive copying; date-only precision is explicitly unknown near
the freshness boundary, and an old day is only called old after its last possible
instant exceeds that boundary. Unsupported input file kinds are refused before
opening (synthetic FIFO regression), not read as ordinary JSON.

A first interrupted attempt with no last completion, and malformed numeric
receipt fields, also formerly yielded no finding. Their new regression failed
first. Both now emit unknown scheduling coverage with a cause-specific remedy;
neither invents a starvation duration. The corrected tick and merge suites pass
12 tests, including actual synthetic merge output with an old sessions receipt.

## K-7 independent correction

The verifier supplied `degraded: 7`; the new reader raised TypeError and could
hide peer findings. A root/list/member shape matrix failed first (including
scalar receipt roots). Guard receipt parsing now validates the root and each
source/reason pair and emits unknown coverage on malformed data. A simultaneous
low-disk finding survives. Unknown values are not copied into diagnosis text.

Combined correction verification: 30 focused guard, merge-age, tick-receipt, existing machine finding and i18n catalog/title tests passed.

## K-2 composed receipt correction

An independent combined fixture exposed a collision: an interrupted collector
and another collector with invalid numeric metadata each emitted the same
acknowledgement ID. The real `build_findings.main` refused that duplicate and
prevented all findings from being replaced. The new composed regression first
reproduced that refusal. Coverage causes and their remedies now share one stable
receipt finding; a simultaneous critical starvation finding survives the actual
writer. All seven tick-finding regressions pass in a sterile temporary workspace.
