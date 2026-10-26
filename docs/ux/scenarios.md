# Product scenarios by runtime

The [full dashboard redesign](DASHBOARD-REDESIGN.md) specified the ten original operator
screens and acceptance UI-01–09; the Machine page, the eleventh, and the Agents page, the twelfth, came later
(the twelve are `PAGES` in `observatory/engine/dashboard/shell.py`). Their behavior is recorded as
OSS-13–22 (Machine: OSS-21–22) and OSS-24–28 (Agents) in the full-engine scenario base linked below,
alongside existing safety contracts. The suppression contract (PB-032) is OSS-40 (numbered OSS-13 until
2026-10-06), and OSS-41–55 cover operating the engine: update, maintenance, backups, profile, the
agent plugin, MCP inventory, the agent's reads, credentials, embeddings, review, Health, models,
the dashboard server, provider keys and the leak register. OSS-62–67 cover the machine's SSH keys (PB-163): list, create,
copy the public part, send it to GitHub or a server, findings and rotation; the private key is
never shown or moved ([design](../design/SSH-KEYS.md)). OSS-79–93 plan the person's surfaces of 2026-10-26: sharing keys with another person, the personal cloud, a native prompt that gives an agent a token without a chat, the agents screen, access use, labels, the home page, infrastructure pages, leak watch, monitoring and the agents' log lifecycle ([design](../design/PERSON-SURFACES.md)). OSS-73 covers the five-minute update check on the release's static feed (operator rule, 2026-10-17); the apps' lifecycle defaults of 2026-10-16 (open at login, a launch-time permission check, closing hides, «Restart to finish updating») are [macOS](../macos/SCENARIOS.md) SCN-009, SCN-010, SCN-012 and SCN-013 and [Windows and Linux](../desktop/SCENARIOS.md) SCN-009, SCN-010, SCN-012 and SCN-013.

This file indexes the two distinct runtime paths. The table below describes the retained portable compatibility CLI only. Full-engine scenarios are in [portable-scenarios.md](../../observatory/engine/docs/ux/portable-scenarios.md); despite that historical filename, its OSS scenarios cover the full engine. The public site's scenarios live separately in [docs/site/](../site/). The implementation and tests are linked from [MIGRATION.md](../MIGRATION.md).

| ID | User and trigger | Path and successful outcome | Current state / evidence |
|---|---|---|---|
| O1 | New operator wants to understand the tool before granting access | Install → synthetic demo → local overview → explicit explanation of scope and limits | Shipped; `test_complete_cli_demo_is_synthetic` (`tests/test_observatory.py`). The scope-and-limits text: no automated test |
| O2 | Operator has many folders but has not chosen ownership | Discover inside a chosen root → preview candidates → add individual authorized directories → first scan | Shipped CLI; discovery never enrolls; `test_discovery_does_not_register_or_follow_symlinks`, `test_registered_root_replaced_by_symlink_is_refused` |
| O3 | Agent needs to know what work is not saved remotely | Scan → project Git metadata → ahead/dirty/no-upstream finding → remedy with local-ref freshness caveat | Shipped; no automatic push. `test_git_real_workflow_reports_ahead_without_network` asserts the ahead/behind/dirty metrics against a bare remote; the three findings and their remedies: no automated test |
| O4 | Operator wants to locate risky environment handling | Scan → variable names and equality fingerprints → permission finding → explicit local action | Shipped CLI; private metadata, never env values; same-installation equality only. `test_env_only_metadata_salted_per_installation` |
| O5 | Agent needs one credential for an authorized command | Operator enters through hidden local prompt/stdin → list label → inject into an explicit child → exit status | Shipped; child output suppressed; no reveal route. `test_secret_stdin_and_child_output_suppressed` |
| O6 | Operator suspects a copied value in a log or memory store | Select known slot(s) and explicit target → exact match → opaque target label and occurrence count → issuer/artifact review | Shipped file/SQLite checker; no automatic scrub or rotation. `test_known_value_file_scan_no_content_or_paths`, `test_sqlite_scan_is_readonly_and_quotes_identifiers` |
| O7 | A source is absent, unreadable, stale or bounded | Observe → visible partial status, skipped coverage and last scan time → repair scope/retry | Shipped degradation and timestamp; automatic age-based stale warning not yet shipped. `test_missing_root_is_partial_not_empty_success` |
| O8 | Operator shares progress with someone outside the machine | Aggregate export → explicit human review → share selected counts | Shipped; identifiers/fingerprints omitted; counts still may be sensitive. `test_env_only_metadata_salted_per_installation`, `test_export_refuses_untyped_snapshot_fields` |
| O9 | Operator returns later to inspect the estate | Open loopback overview → find project and next action → repeat scan in CLI | Shipped read-only single-page overview (`observatory/dashboard.py` serves `/` only); detail, history and filter screens exist only in the full engine (OSS-13–15), and none is planned for this CLI. `test_http_rejects_rebinding_origin_prefix_and_writes` |

Cross-scenario constraints: no hidden enrollment, unknown ownership remains unknown, no value in chat, no provider request without a separately implemented opt-in, no administrative mutation from the dashboard, and no deployment of generated private HTML.

Every test named in the table is in `tests/test_observatory.py`. [UI-PLAN.md](UI-PLAN.md) plans the full engine's dashboard, not this CLI's overview. Update this file in the same change as any new user path.

The public reading update adds SITE-10 (editorial figures, navigation, static text
alternatives) and SITE-11 (404 recovery) in [the site scenario base](../site/BRIEF.md).
The user explicitly authorized the visual direction and autonomous implementation;
reading outcomes remain unobserved until reader feedback exists.


The dashboard landing refresh adds SITE-12 (first-screen agent prompt, pending,
success, clipboard-denied and no-JavaScript paths), SITE-13 (repository rename
compatibility) and SITE-14 (explicitly fictional dashboard illustration) in
[the site scenario base](../site/BRIEF.md#dashboard-landing-refresh--2026-09-23).
This bounded update was explicitly authorized by the operator; it does not change
the private dashboard's O9 runtime capabilities.

The native macOS application adds [SCN-001–009](../macos/SCENARIOS.md). It opens on
the workspace's dashboard (SCN-007–009: live, saved pages with Start server, reopen from
the Dock); the conversational assistant is its own window, never a widget inside the
dashboard pages (SCN-001–006). The [specification](../macos/SPEC.md) and
[delivery plan](../macos/PLAN.md) own the surface and its verification scope.

Offline verification also serves agents and contributors: a failed suite setup or
report export must retain completed evidence and never publish a successful final
summary. See [offline verification](../../observatory/engine/docs/ux/portable-scenarios.md#offline-verification-receipts).

Lesson evidence recovery: an agent revises or repeats an observed lesson after its commit
is no longer available. The tool preserves the old observation as archived history and
returns a new proposal plus its `replaces` reference; a failure to create the proposal
leaves the old record and indexes intact. Valid wording-only revisions remain observed
when both known projects still resolve. Identity mutation is refused, and operator-owned
support remains protected. Executable scenarios: `tests/test_lesson.py` class
`EvidenceRevisionSafety` and `tests/test_corroboration.py`
`test_observation_is_revalidated_when_unchanged_commit_disappears`, inside the engine.

Lesson bridge migration concurrency: while a proposed note is migrating, another caller
records the same lesson identity. The contender cannot pass the migration's write lock;
a retry returns the migrated lesson instead of creating another active identity. A note
changed since candidate discovery is skipped. Generic ledger creation/conversion also
refuses a duplicate identity; erasure committed after its first read still prevents an
insert. `TheBridgeNotesMigrate` exercises these interleavings with two SQLite connections.

OBS-87 memory freshness: an operator or agent requests `full machine --memory-only`
while disk sizing is slow. RAM/swap and process RSS refresh independently, with separate
source timestamps and explicit unavailable values. The Machine page and MCP retain old
disk timestamps rather than presenting the whole machine as freshly measured. A failed
new memory attempt shows unknown swap, never zero or a newly dated old measurement.
Process RSS totals are labelled as sums that can count shared pages more than once.
The bounded packet is [memory freshness](../runs/2026-10-13-memory-freshness/README.md).

### OBS-87: inspect checkout candidates beneath explicit roots

The operator supplies each search root to the read-only checkout inventory command.
The receipt identifies its budgets, independent source times, omissions and cursor;
missing or failed observations remain unknown. A discovered checkout, including an
unregistered or old one, is never presented as abandoned or safe to remove. Unmeasured
allocated/reclaimable bytes stay null. The command grants no cleanup authority.

### OBS-87: understand cache coverage without cleaning

`full machine --cache-coverage` reports configured cache/runtime roots and command
availability without walking their contents or running those commands. Presence, prior
size evidence and source age are separate. Owner/use/regeneration and reclaimable bytes
stay unknown until separately proved. Protected containers and OS-managed caches remain
explicitly uncovered, and no inventory row authorizes removal.

### OBS-87: read saved coverage without rescanning

Persona: operator or agent inspecting the last explicit checkout/cache inventory.
Entry: `full machine --coverage` or `observatory_machine(section="coverage")`.
Preconditions: a workspace exists; either saved receipt may be absent or partial.
Steps: request coverage, inspect each source's own attempt age and snapshot counts,
then inspect the bounded rows and unknown ownership/reclaimable bytes.
Expected result: no collector runs; at most ten rows per source are returned. The
machine overview includes the same bounded summary. A saved count never becomes an
estate-wide total or permission to delete, and stale counts are labelled as snapshots.
Alternate paths and recovery: missing, malformed, oversized, future-dated or stale
receipts show unknown current counts and explicit source reasons; valid peers survive
malformed rows. Run the separate explicit inventory command to obtain new evidence.
UI elements: CLI flag, MCP section enum and JSON result; no template changes.
States covered: absent, partial, fresh saved snapshot, stale, invalid and bounded.
Status: draft. Coverage: `tests/test_coverage_view.py` (eight cases),
`tests/test_mcp_wire.py` (coverage schema and real stdio call). Product: unobserved.

Windows cleanup holder uncertainty: an operator or agent requests path cleanup
with no supported current holder inventory. Planning reports unknown holder
coverage; execution preserves the target, including when an older plan marked
it free. Rename success never changes that result. Native shared-delete handles
are covered by a Windows-only control; other hosts report that control skipped.
`WindowsHolderUnknown` in engine `tests/test_disk_guard.py` owns this contract.

POSIX cleanup argv coverage: a closed script with spaces, relative arguments or a
symlink alias can evade rendered process text. The system keeps positive holds but
refuses to turn absent matches into deletion or cache busy-tool admission. See
[the full-engine scenario](../../observatory/engine/docs/ux/portable-scenarios.md#posix-cleanup-rendered-argv-does-not-prove-absence).

Cleanup admission without collection: when current process observations cannot
prove non-use, preliminary path admission stays unknown without reading the
process table. Per-phase evidence marks `not-attempted` and no measurement time;
there is no claim of zero holders or freed RAM. Explicit positive-hint readers
and final refusal boundaries retain their behavior. `CheapHolderAdmission` in
engine `tests/test_disk_guard.py` verifies these outcomes.
