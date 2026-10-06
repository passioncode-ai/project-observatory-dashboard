# Product scenarios by runtime

The [full dashboard redesign](DASHBOARD-REDESIGN.md) specified the ten original operator
screens and acceptance UI-01–09; the Machine page, the eleventh, and the Agents page, the twelfth, came later
(the twelve are `PAGES` in `observatory/engine/dashboard/shell.py`). Their behavior is recorded as
OSS-13–22 (Machine: OSS-21–22) and OSS-24–28 (Agents) in the full-engine scenario base linked below,
alongside existing safety contracts. The suppression contract (PB-032) is OSS-40 (numbered OSS-13 until
2026-10-06), and OSS-41–55 cover operating the engine: update, maintenance, backups, profile, the
agent plugin, MCP inventory, the agent's reads, credentials, embeddings, review, Health, models,
the dashboard server, provider keys and the leak register.

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
