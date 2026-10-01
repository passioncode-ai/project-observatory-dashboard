# 2026-10-01 — release 0.10.0 (AGPL-3.0 or commercial; honest absence)

Handoff for the next agent. No private names or ids here. Procedure: `AGENTS.md → Releasing`.
What the release contains: [CHANGELOG 0.10.0](../../../CHANGELOG.md#0100--2026-10-01).

## What shipped

| PR | Merge commit | What |
|---|---|---|
| [#105](https://github.com/passioncode-ai/project-observatory-dashboard/pull/105) | `e1e0110` | release 0.10.0: version files, the `## 0.10.0 — 2026-10-01` section, companion plugin `observatory-log` 0.13.0 (its licence text changed after 0.12.3 was published without a bump; `handling-secrets` documents the new Cloudflare presets), `SOURCE-INVENTORY.json` |

| Release | Tag commit | Wheel sha256 (GitHub digest and `SHA256SUMS` agree, re-downloaded) |
|---|---|---|
| [v0.10.0](https://github.com/passioncode-ai/project-observatory-dashboard/releases/tag/v0.10.0) | `e1e0110f6618` | `ec89a46b0114dd588183db4358871366c3847359ff2cbab232ad144f9be3e336` |

`LICENSE` at `v0.10.0` is the GNU AGPL v3 (`gh api "repos/passioncode-ai/project-observatory-dashboard/contents/LICENSE?ref=v0.10.0"`).

## Checks actually run

- On the release branch, each exit code read directly: root `unittest` 0; `compileall` 0; wheel
  build + `tools/check_package.py` 0; `tools/update_inventory.py --check` 0;
  `tools/check_public_release.py --history --history-ref HEAD` 0; `claude plugin validate --strict`
  on `.`, the engine marketplace and the plugin 0/0/0; org-index `check_private.py` 0 findings;
  `project-observatory full check` 0 — 197 suites PASS, the three standing out-of-scope rows
  NOT_RUN (live provider acceptance, external MCP host admission, live secret rotation).
- CI on #105: the three required rows passed (ubuntu 3.11, ubuntu 3.14, macOS 3.14).
- From the merge commit: wheel rebuilt, `check_package.py` passed (478 runtime files, 485 archive
  entries), `SHA256SUMS` written; both assets re-downloaded from the release and
  `shasum -a 256 -c SHA256SUMS` OK; GitHub's asset digest equal to the inspected value.

## One operator machine after the update

- `full update --check` → exit 10 (0.9.1 → 0.10.0); `full update --version 0.10.0 --apply` → exit 0:
  verified against the GitHub digest and `SHA256SUMS`, tick and server stopped, encrypted snapshot,
  installed, workspace upgraded, version verified, tick and server started again,
  `services_not_restarted: []`.
- The always-on server answers on its port; Fabric Dashboards `list_services` reports version
  0.10.0 with build `sha256:ec89a46b…9be3e336`. `claude mcp list` → `observatory: ✔ Connected`.
- Before the update the service was `degraded` on six reasons. After the first 0.10.0 tick, three
  remained, all from receipts written by 0.9.1 that their collectors re-read only on their own
  cadence (RDAP domains, OpenRouter consumers, Google). Re-collected once each with 0.10.0 —
  `full domains`, `full openrouter`, and the dashboard's own Google refresh command
  (`collectors/scan_google.py … --force`, then `merge`, `emit`, `dashboard`) — they moved to
  `not_applicable` (7 RDAP TLDs without a registry service, 2 OpenRouter consumers not installed,
  3 Google surfaces read by another credential). The service then reported **`ready`, no
  reasons**. The other three (a switched-off integration's receipt, a one-branch remote, an
  uninstalled companion) cleared in the first tick.

## What this release does not do

- A collector's receipt written by an older release keeps its old `degraded` list until that
  collector next runs; `full update` does not re-collect. A machine that updates sees the service
  `degraded` for up to a day on reasons 0.10.0 would not raise. Next task 1 below.
- `./observatory.py google --force` is still refused by the step runner (open since 0.5.0); the
  dashboard's copy-command runs the collector directly instead.

## Next tasks

1. After `full update --apply`, re-collect (or reinterpret) receipts whose collector logic changed,
   so the service state reflects the installed release at once; a test plants a 0.9.1-shaped receipt
   and holds the post-update state to `ready`.
2. The open contributor tasks in [HANDOFF.md](../../HANDOFF.md#next-task-and-prerequisites).
