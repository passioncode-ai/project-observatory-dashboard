# Next interface improvements for the full engine

The full engine already generates twelve local pages (`PAGES` in `dashboard/shell.py`), including project, metric and
finding views. The earlier portable overview remains a separate compatibility UI.
This plan improves the full instrument; it does not describe it as an empty shell.
The scenario contract is [full-engine scenarios](../../observatory/engine/docs/ux/portable-scenarios.md).

## Completed in this release

Copied commands resolve the selected workspace, Python executable and installed
engine. Paths and arguments are quoted, local environment actions avoid printing
values, scheduler actions select the workspace, and obsolete command flags were
removed. `test_dashboard_portability.py`, page and renderer suites cover these
boundaries. Public landing/article/mobile checks are in [DEPLOY.md](../site/DEPLOY.md).

## Ordered implementation packets

This table is the canonical status source for UX1–UX6 in
[the shared backlog manifest](../backlog-sources.json). Status is `not recorded`
until acceptance evidence establishes progress; the completed work described above
does not establish completion of these separate packets. Preserve IDs and done conditions.

| Packet | User problem and change | Status | Done conditions |
|---|---|---|---|
| UX1 | Coverage first. Missing, stale and unconfigured inputs compete with genuine findings. Put coverage and last observation beside status. | not recorded | New/empty/partial/stale fixtures have a clear next action; unobserved never means healthy; source timestamp and scope visible. |
| UX2 | Safe command handoff. Users need to understand copied commands before running them. Explain read vs mutation, workspace and external effects beside the action. | not recorded | Every action has scope, expected outcome and failure recovery; keyboard copy works; no value is placed in DOM or copied text except an explicitly authorized reveal surface. |
| UX3 | Cross-page continuity. A finding should preserve project/filter context when drilling into evidence and returning. | not recorded | Addressable state, working Back, consistent labels, empty result vs unobserved distinction; no lost keyboard focus. |
| UX4 | Dense data. Larger estates need readable tables, search and meaningful grouping. | not recorded | Synthetic 200-project fixture at 320/768/1440px; stable sort, accessible names, headers and table alternative for charts; no horizontal overflow for the main task. |
| UX5 | Onboarding in the instrument. Agent-led setup needs a clear in-product view of what is connected. | not recorded | Per-integration disabled/unconfigured/connected/error states; minimum required access explained; no keys in chat or default remote scans. |
| UX6 | Upgrade confidence. A user should see the active version, pending migration and snapshot location. | not recorded | Preview differs clearly from apply; stopped-writer requirement visible; restore opens a separate home; unsupported version explains the recovery path. |

Implement each packet through scenarios, copy and design before delivery. Use only
synthetic fixtures and explicitly separate read-only views from administrative
operations. No promise to remove the original engine's authenticated reveal route
is hidden inside a visual redesign. Compare desktop and narrow layouts, keyboard
focus and actual action results; source assertions alone do not establish usability.
