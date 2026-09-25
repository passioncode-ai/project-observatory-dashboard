# Full-system installation and compatibility scenarios

These supplement the original product scenarios. Approved scope: operator requested full migration, per-user private configuration, future backwards compatibility and autonomous implementation. Product outcomes remain unobserved until a real installation walkthrough.

| ID | User goal | Observable result | Recovery and invariant |
|---|---|---|---|
| OSS-01 | Install on a new machine | init creates a private versioned home; doctor needs no credentials or network | Unsupported format fails before writes; re-running init preserves settings |
| OSS-02 | Add my project directory | Explicit absolute source path is recorded; filesystem scan reads it | No implicit home-directory traversal; missing source is reported |
| OSS-03 | Connect an external service | Agent explains permissions; local secret input; status shows no value | Integration remains disabled until chosen; credential errors remain local |
| OSS-04 | Bring my existing installation | Preview reports categories/counts; apply creates a separate copy, source remains usable | Changed source, symlink or unreadable data aborts; original is not overwritten |
| OSS-05 | Upgrade | Supported legacy schema upgrades atomically with a WAL-aware backup | Unknown newer format is refused before mutation; failed migration rolls back |
| OSS-06 | Return to the previous release | Restore a complete snapshot into a separate home and use its matching code | Never run an old writer against a newer schema; no silent database downgrade |
| OSS-07 | Keep old agent clients working | Existing MCP tool names and supported schemas stay available | Breaking tool changes require a new version; unknown required capability is refused |
| OSS-08 | Schedule observations | Explicitly enabled job carries the chosen workspace and no secret values | One writer per workspace; existing installation jobs are not replaced silently |
| OSS-09 | Open the credential UI locally | Correct loopback Host/Origin accepted; authenticated operation logged without secret values | Lookalike origins, DNS rebinding and malformed requests are refused before effects |
| OSS-10 | Copy a command from the local dashboard | Absolute program paths and Python select the displayed workspace; project and secret roots can contain spaces and quotes | Arguments remain literal, including shell metacharacters; static ENV actions locate a named slot without printing its value; importing a value requires an explicitly chosen private input file |
| OSS-11 | See what a project runs where | The projects table groups each project's apps by the environment they serve. Production comes first, and the account is shown on hover. An app no override or provider placed in an environment is shown as not specified | Never inferred from an app's name. `config/environments.json` places an app, and `tests/test_hosting_groups.py` covers the grouping |
| OSS-12 | Act on a key from the live dashboard | A refusal the server explains says "failed" ("не вышло") with its reason, and the action can be retried. A timeout, a dropped connection, an unreadable answer or a server fault says "outcome unknown · check" ("исход неизвестен · проверьте") and locks the button | A repeat after a lost answer could mint or revoke twice, so the button stays locked until a rescan shows what happened. Covered by `tests/test_action_outcomes.py` |

## Dashboard workspace scenarios (2026-09-26)

| ID | User goal | Observable result | Recovery and invariant |
|---|---|---|---|
| OSS-13 | Enter an inventory and compare records | Projects/ENV show records immediately; every list exposes selection and global sorting | Primary identities never hidden by default; search covers full metadata; empty source differs from no matches |
| OSS-14 | Understand a project and return | Compact row shows purpose/activity/resources; detail gives full linked repositories, sites, deployments and keys | Close preserves list and returns focus; deep links retained |
| OSS-15 | Triage and revisit findings | Overview counts all open severities; list shows matching rows and acknowledged history even with zero open findings | Preview names its boundary and links to full list; no invisible critical overflow |
| OSS-16 | Know what an action does | Capability stated before Keys/ENV rows; copy/manual/live verbs clearly differ | Copy never claims execution; uncertain outcomes and reveal protections remain |
| OSS-17 | Compare infrastructure/traffic | Cost and measurement context above lists; zero distinct from missing/error; one GA4 resource counted once across credentials | Contradictory chips cannot remain selected; evidence remains accessible |
| OSS-18 | Navigate and inspect observer | Eleven destinations (ten until the Machine page, OSS-21), visible main, page search, narrow-screen sort; Health begins with observer/recovery | Snapshot claims qualified by time; keyboard retained; actions never replay on navigation |
| OSS-19 | Read the dashboard in one's own language | A new workspace is English. `full configure interface locale ru` makes every page Russian after `full open --rebuild`; EN/RU in the rail switches one reader's pages at once, including pages opened as files, and the pressed button names the language shown. Counts use the language's plural forms and number grouping | An unsupported value is refused and nothing is written. A browser that keeps no storage says so and points to the workspace setting instead of pretending to switch. Choosing the workspace's language forgets the reader's override. Finding texts stay in English. Covered by `tests/test_i18n.py`, the ENV and action-outcome harnesses in both languages |
| OSS-20 | Recognise the product | Every page carries the Observatory glyph on the PassionCode tile, "by PassionCode.ai", one dark theme with gold for action, selection and focus, and a link to the PassionCode toolkit | State colour is always paired with words; the design tokens are the pinned PassionCode bytes (`dashboard/brand/manifest.json`) |
| OSS-21 | See what runs and where memory and disk go | `features.machine_watch` on: the Machine page and `full machine` show processes grouped by origin (agent session, launchd job, simulator by device name, app, detached), memory and swap, free disk, the largest cache/VM/history places with how each comes back, idle worktrees and branches holding the only copy; `full machine --explain PID` says why one process runs (witr detail when installed) | Read-only page; a process is shown by executable and script, never its environment or full command line; an unmeasured source is listed under Not measured, never shown as zero |
| OSS-22 | Get space back without losing work | `features.auto_cleanup` on: each tick removes merged or pushed branches, clean idle worktrees, stale worktree records and build output of idle projects, and lists them under Cleaned in the last 7 days; `full cleanup --apply --include manual` also removes unique branches and dirty worktrees after bundling/archiving them | Every target is re-checked when acted on (still merged, still clean, nobody's working directory); protected names are never touched; every action is a journal line with what brings it back |

OSS-10 regression evidence: `tests/test_dashboard_portability.py` executes generated
commands against inert synthetic tools from an unrelated working directory. Nine
tests cover workspace selection, quoted provider names and movement evidence,
configured secret/project paths, static ENV lookup, missing-project refusal, and
platform-neutral private-file input. This verifies command behavior, not visual
layout or a live credential operation.

For the observer status card, the portable command starts the server explicitly in
the foreground or checks its heartbeat. It does not address a historical launchd
label or silently install a background job. The onboarding flow separately owns
opt-in scheduler installation. Empty-state observation uses `local`; Google refresh
uses its collector's supported `--force` option and rebuilds the registry before
the dashboard. `tests/test_pages.py` checks the command bridges and actual tool
subcommands; `tests/test_dashboard_render.py` executes the renderer.

### OSS-13 — a suppression cannot hide a replacement value (PB-032)

Given an accepted sighting with a reason and expiry, the operator copies its secret name, exact location and opaque `version_id` into the suppression config. The accepted sighting remains visible as suppressed. A new value under that name, another location, or another workspace salt remains an active sighting. Expiry replays old evidence without requiring a new write to the source. Malformed and legacy unversioned rules raise a warning and suppress nothing. Proof: `tests/test_leak_coverage.py` (rotation, exact path, duplicate names in files/SQLite, salt change, malformed shapes, expiry and incremental value discovery). Detection still covers known values only; it does not discover every unknown secret shape or search all deleted Git history.
