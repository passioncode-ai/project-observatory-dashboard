# Dashboard workspace redesign — 2026-09-26

Status: implementation and browser review complete; final gates, integration,
release and installed verification pending. See the
[audit receipt](audits/2026-09-26-workspace-redesign.md) and
[delivery entry](../runs/2026-09-26-workspace-redesign/README.md).
Operator requests a complete review and update of
all ten dashboard screens, rejecting collapsed project groups that hide records.
Scope includes navigation, lists/detail, filters, action wording and empty states.
Public marketing and the compatibility CLI remain separate surfaces.

## Job, constraints and falsifier

One operator needs to find a project, understand its current state and follow the
evidence to an action. Preserve source facts, credential boundaries, routes/hashes,
page payload separation and the offline renderer. No new provider access, network
assets, telemetry or frontend dependencies. This is a private local surface.

Falsifier: entering Projects/ENV shows category names instead of records; sorting
does not compare all selected records; copy appears to execute; Overview claims
nothing is open while findings exist; navigation or sorting disappears on narrow
screens. These are acceptance failures, not aesthetic preferences.

Keep Workbench tokens and light/dark twins. Change composition to a persistent
navigation rail and compact workspace. Observable target: primary identity/state
visible before disclosure; search/selection adjacent to data; full detail without
losing list position. Calibration: variance 4, motion 1, density 7 (comparable
rows, not ten long inventories per row). No decorative motion.

Design artifact: working browser preview. No separate design-file delivery.
No external reference-library research was performed; no research claim follows.
Workbench owns colors/type; the layout and behavior below are project decisions.

## Alternatives before implementation

Criteria: visible records, cross-project comparison, direct ten-module access,
retained capabilities and responsive keyboard use. A: compact visible tables with
navigation rail and secondary detail, selected for comparison. B: organization
cards and wide top navigation, rejected for scanning distance and owner-before-
project hierarchy. This paragraph retains B; revisit for a small narrative-led
portfolio. Visual identity is already fixed; this is a composition decision.
Budget two browser critique passes on equal content/viewports, report unresolved
issues explicitly.

## Screen contract

| Screen | Primary answer | Secondary content / preserved actions |
|---|---|---|
| Overview | Open severity totals and visible attention preview | Activity, inventory links, explicit route to all findings |
| Projects | Flat visible rows: identity/purpose/owner, activity, resources, hosting, audience | Full paths/repos/sites/deployments/keys in linked project detail |
| Findings | Visible matching work, severity/type/search, honest count | Acknowledged history and copy-undo even when no open findings |
| Domains | Visible domains, reachability, expiry, project and account | Link evidence, registrar/Cloudflare destinations, copied diagnostics |
| Heroku | Visible apps; filtered cost estimate and measurement before table | State, project, deployment, resources, explicit command actions |
| Keys | Identity, owner/status, capability explanation before rows | Kinds, impact, purpose, movements, manual/copy/live distinction |
| ENV | Visible file metadata and risk; capability explanation before rows | Variable metadata, source/production comparison, protected reveal |
| MCP | Visible declarations, connectivity and agent | Transport, target/auth details; preserve distinct per-agent declarations |
| Traffic | Comparable properties, period/coverage/update-command first | Metrics, project evidence, report/admin links; missing is not zero |
| Health | Observer snapshot/freshness, recovery, degradation and queue | Retention/architecture details; remove unrelated inventory/movement tiles |

Navigation: Work (Overview, Projects, Findings), Infrastructure (Heroku, Domains,
Traffic), Access (Keys, ENV, MCP), System (Health). Narrow layout keeps all routes
reachable. Shared requirements: visible rows; global sort; explicit filter/reset;
mutually exclusive chips; visible main/skip link; current-page search shortcut;
detail focus restoration; explicit copy versus execution; snapshot-qualified
health. Long metadata may be disclosed; primary identities may not be concealed.

## Requirements and acceptance

| ID | Requirement | Check |
|---|---|---|
| UI-01 | Visible defaults on all seven lists | Built-page fixtures and browser Projects/ENV |
| UI-02 | Ten-screen structure/navigation (twelve since the Machine and Agents pages; `PAGES` in `observatory/engine/dashboard/shell.py`) | Browser walk plus screen contract above; `observatory/engine/tests/test_workspace.py` `test_complete_local_workflow_and_twelve_pages` |
| UI-03 | Compact project summary/full detail | Multi-resource fixture, links/focus in browser |
| UI-04 | Honest findings and acknowledged history | Warning-only, acknowledged-only, >8/type fixtures |
| UI-05 | Explicit safe action modes | Copy/live labels plus existing outcome/portability suites |
| UI-06 | Comparison and unknown states | Cross-account sort, zero/missing traffic fixtures |
| UI-07 | Health first, snapshot-qualified | Built structure and browser |
| UI-08 | Keyboard/narrow/theme paths | Browser and scoped semantic/contrast checks |
| UI-09 | Durable source and handoff | Local gates, pushed branch, private operational receipt |

UI-01–08 source/test/browser evidence and its limits are recorded in the
[audit](audits/2026-09-26-workspace-redesign.md#findings-and-disposition).
UI-09 final check, commit, remote, release and installed receipts remain
[pending](../runs/2026-09-26-workspace-redesign/README.md#delivery-status-and-next-task).

Sequence: shell/style; lists/detail; overview/findings/health; action/metric
corrections; regressions/browser critique; inventory/privacy/package gates;
protected-main integration and reviewed release before installed update.

Source ledger: dashboard shell/builder, page/render/ENV tests, portable scenarios,
brand pack and public handoff. Contradiction: old folding tests encode a product
choice explicitly rejected by the operator; replace with visible-record checks.
The actual skills applied are ux-flows/scenarios/audit, sheleg-design, copywriting,
accessibility-review, task-pipeline and evidence-docs. Model inherited; three
read-only audit batches per ux-audit's large-scope rule, converged into this brief.

Resume at the delivery entry's full-gate investigation and release sequence;
do not reopen the accepted composition without new evidence or operator steering.
Real registries, generated private pages, screenshots and local paths stay outside
the public repository. A pushed branch is not an installed update.
