# Set up my Project Observatory

Help me install and configure the full Project Observatory on this computer.
Read ONBOARDING.md and COMPATIBILITY.md first: under `docs/` in a checkout of the release, or under `$(project-observatory full-path)/docs/` in an installed release.

1. Verify Python and SQLite requirements. Explain the code directory and the separate private workspace. Run `project-observatory full version`, then `full init` and `full doctor` with an explicit OBSERVATORY_HOME.
2. Ask which project directory I want to observe. Configure only that directory. Explain which local metadata will be read. Start with filesystem scanning and the local dashboard.
3. Offer optional GitHub, Bitbucket, hosting, DNS, analytics, memory and model integrations separately. Missing integrations remain unconfigured. Do not enable paid reasoning, remote secret reads, memory remediation, notifications or the observation scheduler as part of a default setup. Offer `features.auto_cleanup` separately and say what it does: the maintenance job (every 15 minutes) then runs the disk guard (`full disk-guard`): it removes application and tool cache folders, npx's package cache, temporary files, macOS diagnostic reports, abandoned browser clones, Trash items older than 30 days, the cache tools' own caches and git-ignored build output of idle checkouts, then merged or pushed branches and clean worktrees whose branch stays — more of them as free space falls (below 25 GB: 3 days; below 10 GB: 1 day) — and never anything a process holds, a privacy-guarded folder or another account's file; preserve `.wrangler` local state even when an old artifact configuration lists it; manual dirty-worktree removal skips all ignored files because its archive excludes them (preserve or move those files before retrying `full cleanup --apply --include manual`); show me `full disk-guard` and `full cleanup --level low` (the plans, which remove nothing) before I turn it on. The maintenance job (every 15 minutes: updates and a daily encrypted backup) is separate: `full init` schedules it only when I run init at my own terminal; run by you, init schedules nothing and answers `not-scheduled-here` with the command, `full maintain ensure`, which also needs my terminal. Remote memory embeddings need my own consent per project (`full embedding-policy grant`, at my terminal); you cannot give it, and your searches stay local either way. Say this before I enable embeddings. Any caller other than you (the local stdio agent) reaches memory only through a binding I issue at my terminal (`full access-binding issue`); you cannot issue one, and I hand its bearer over as a file, never in chat.
4. Explain the minimum credential permissions for each selected integration. Have me enter secrets in a local hidden prompt, secure file or credential manager. Never ask me to paste a value into this conversation. Report only names and connection results.
5. Run a first observation and show its coverage, findings and unavailable inputs. A disconnected source is not a clean security finding.
6. Explain how to update, preview a migration and restore a backup. If this is an existing installation, stop writers before migration and keep the original until the new copy is verified. Do not start a second scheduler over the same data.
7. Finish with the local dashboard address, commands for another scan, and the list of enabled integrations. Keep the public marketing site separate from the private dashboard.

## Reading offline verification receipts

`full check` emits JSON. A suite whose setup, execution or log capture raises is
`ERROR` with `error_type` and unknown completion; completed peers keep their rows.
An `ERROR` row is also listed under `not_run` to prevent treating missing evidence
as PASS. If saving `--report-dir` fails, the overall result is FAIL and stdout
keeps the suite rows with `report_error_type`; the destination may be partial.
Fix the named filesystem/runtime problem and rerun the affected verification.
### Disk cleanup revalidation

`full disk-guard --apply` preserves Git repositories and linked worktrees inside
temporary roots and refuses temporary targets beneath Git ancestry. Build
output is rechecked for checkout activity, ignore rules, tracked content and new
writes. Each path action probes current holders within a five-second shared
deadline; unknown evidence skips it. POSIX actions require `lsof` for this final
target-scoped probe; Windows retains rename-based protection. A skipped target
is not proof of zero reclaimable space. Replan after resolving the reported
reason. [Design and regression cases](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/DISK-GUARD.md#apply-time-preservation-2026-10-13).

Disk-guard measurement limits: unknown swap remains `null` with a reason and
source time; it is not zero. The largest-file report carries completeness,
error counts and budget exhaustion. An incomplete empty scan is shown as an
incomplete measurement. The MCP overview exposes `guard.largeFilesCoverage`
beside its bounded file list. See [measurement coverage](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/DISK-GUARD.md#measurement-coverage-obs-87-subset).
