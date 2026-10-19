# OBS-87: bounded saved coverage summaries

<sub>ssheleg skills — task-pipeline · evidence-docs · ux-scenarios · copywriting</sub>

## Packet

Read saved checkout/cache receipts without triggering inventory, Git, process or disk
scans. The machine CLI adds `--coverage`; the existing `observatory_machine` tool adds
`section="coverage"` and includes the bounded summary in its overview. The default
machine CLI also includes this saved summary. No dashboard template changes.

Source map: `workspace.machine_command` owns CLI selection, `machine_view.summary`
combines machine evidence, and `mcp/server.py::observatory_machine` owns the real tool
input enum and output. `checkout_inventory.scan` and `cache_coverage.collect` own the
saved receipts and remain separate, explicitly invoked producers.

A new `coverage_view.summary` reads only two regular, non-symlink files with 4 MiB
and 10,000-row limits. It exposes snapshot counts, independent attempt ages, partial
status and at most ten rows per source. Missing, malformed, oversized or future-dated
sources remain unknown. Invalid rows degrade their source without erasing valid peers.
Counts of saved rows are not totals for the machine; partial or stale sources never
claim a current total. A 30-minute age limit is a reader convention, not proof of
current use. Allocated/reclaimable bytes remain null, and all rows are non-actionable.

## Acceptance and handoff

Tests first: missing versus explicit empty; stale/future timestamps; malformed peers;
row/byte caps and FIFO refusal; bounded samples without command arguments; CLI reads
without producer calls; real MCP input schema includes coverage and its output matches
the reader contract. The scenario is in `docs/ux/scenarios.md`.

Next: implement, run scoped synthetic tests and independently review the exact commit.
No live/provider/native acceptance is implied. OBS-87 remains partial.


**Made with [ssheleg skills](https://github.com/ssheleg/sshlg-skills)**

- [`task-pipeline`](https://github.com/ssheleg/task-pipeline) — bounded saved coverage reader
- [`evidence-docs`](https://github.com/ssheleg/task-pipeline) — source map and limits
- [`ux-scenarios`](https://github.com/ssheleg/super-ux) — CLI and MCP coverage behavior
- [`copywriting`](https://github.com/ssheleg/super-ux) — explicit uncertainty wording

## Candidate evidence

Eight reader cases and 59 existing machine cases pass. The real MCP wire suite
passes 131 assertions, including the advertised coverage input enum, published sample
cap and a coverage call returning unknown missing receipts. Checks ran jobs 1 over
synthetic workspaces. Seven reader cases first failed against the previous source.
`coverage_view.py`, `machine_view.summary`, `workspace.machine_command` and
`mcp/server.py::observatory_machine` implement the mapped seams. The bounded coverage
section reads directly and avoids the larger machine journals entirely.

The JSON output schema is attached to the actual MCP tool and validates unknown
counts/null bytes and non-actionability. The summary never copies producer error
bodies, command arguments, arbitrary Git branches or prior size estimates. A stale
registration sample is named `recorded_registration`, not a live membership claim.
Special files and symlinks are refused; bounded regular reads cannot preempt a kernel
filesystem stall. No whole-machine coverage, safe abandonment decision, cleanup
permission, installed acceptance or independently proven cache ownership is claimed.

Next: independent exact-commit review; integrate only after acceptance. The scenario
remains draft and product outcome unobserved. The existing prose scenario format is
preserved rather than replacing unrelated scenario bases.
