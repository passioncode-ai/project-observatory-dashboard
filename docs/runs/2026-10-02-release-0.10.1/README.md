# 2026-10-02 — release 0.10.1 (standard server priority; one notification channel)

Handoff for the next agent. No private names or ids here. Procedure: `AGENTS.md → Releasing`.
What the release contains: [CHANGELOG 0.10.1](../../../CHANGELOG.md#0101--2026-10-01).

## What shipped

| PR | Merge commit | What |
|---|---|---|
| [#110](https://github.com/passioncode-ai/project-observatory-dashboard/pull/110) | rebased onto `main` | `tools/serverd.py` writes `ProcessType Standard`, no `Nice`, no `LowPriorityIO`; `tools/notify_findings.py` raises no banner of its own when the server's Fabric descriptor and Fabric Dashboards are present |
| [#111](https://github.com/passioncode-ai/project-observatory-dashboard/pull/111) | `d6ea9c8` | version files, `## 0.10.1 — 2026-10-01`, `SOURCE-INVENTORY.json` |

| Release | Tag commit | Wheel sha256 |
|---|---|---|
| [v0.10.1](https://github.com/passioncode-ai/project-observatory-dashboard/releases/tag/v0.10.1) | `d6ea9c8` | `4e2589751f11111790199a51112afba03a6b026cc9010201095d2169b828f8c6` |

## Checks actually run

- On the release branch: root `unittest` 84 OK; wheel build + `tools/check_package.py` 0 failures
  (478 runtime files, 485 archive entries); `tools/check_public_release.py --history --history-ref HEAD`
  exit 0; `tools/update_inventory.py --check` exit 0. `project-observatory full check` was **not run**
  for this patch.
- CI: #110 and #111 each passed the three required rows (ubuntu 3.11, ubuntu 3.14, macOS 3.14).
- From the tag commit: wheel rebuilt, `check_package.py` 0 failures, `SHA256SUMS` written and published
  with the wheel.

## One operator machine after the update

- `full update --check` → exit 3 for about 45 minutes: the anonymous GitHub API quota (60 requests an
  hour per address) was spent by other processes on the machine. Retried right after the quota reset:
  exit 10, then `full update --version 0.10.1 --apply` → exit 0, `degraded: []`,
  `services_not_restarted: []`.
- **`full update` does not rewrite the server's plist.** After the update the plist still said
  `ProcessType Background`, `Nice 5`. `tools/serverd.py --install --port 47311` from the installed
  package rewrote it: `ProcessType Standard`, neither key present; the server answered on its port as
  0.10.1 with build `sha256:4e258975…`.

## Next tasks

1. Make `full update` re-run the server's `--install` (or rewrite its plist) when the plist a release
   writes differs from the installed one; until then every plist change needs a manual `--install`.
2. Accept a GitHub token for the update check, or fall back to the release feed, so a spent anonymous
   quota does not block updates for an hour.
