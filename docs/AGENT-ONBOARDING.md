# Set up my Project Observatory

Help me install and configure the full Project Observatory on this computer.
Read ONBOARDING.md and COMPATIBILITY.md first: under `docs/` in a checkout of the release, or under `$(project-observatory full-path)/docs/` in an installed release.

1. Verify Python and SQLite requirements. Explain the code directory and the separate private workspace. Run `project-observatory full version`, then `full init` and `full doctor` with an explicit OBSERVATORY_HOME.
2. Ask which project directory I want to observe. Configure only that directory. Explain which local metadata will be read. Start with filesystem scanning and the local dashboard.
3. Offer optional GitHub, Bitbucket, hosting, DNS, analytics, memory and model integrations separately. Missing integrations remain unconfigured. Do not enable paid reasoning, remote secret reads, memory remediation, notifications or the observation scheduler as part of a default setup. Offer `features.auto_cleanup` separately and say what it does: the maintenance job (every 15 minutes) then runs the disk guard (`full disk-guard`): it removes application and tool cache folders, npx's package cache, temporary files, macOS diagnostic reports, abandoned browser clones, Trash items older than 30 days, the cache tools' own caches and git-ignored build output of idle checkouts, then merged or pushed branches and clean worktrees whose branch stays — more of them as free space falls (below 25 GB: 3 days; below 10 GB: 1 day) — and refuses observed holders or unknown holder coverage, privacy-guarded folders and another account's files. Holder checks are point-in-time observations; exclusion of external writers starting afterward is unproved; preserve `.wrangler` local state even when an old artifact configuration lists it; manual dirty-worktree removal skips all ignored files because its archive excludes them (preserve or move those files before retrying `full cleanup --apply --include manual`); show me `full disk-guard` and `full cleanup --level low` (the plans, which remove nothing) before I turn it on. The maintenance job (every 15 minutes: updates and a daily encrypted backup) is separate: `full init` schedules it only when I run init at my own terminal; run by you, init schedules nothing and answers `not-scheduled-here` with the command, `full maintain ensure`, which also needs my terminal. Remote memory embeddings need my own consent per project (`full embedding-policy grant`, at my terminal); you cannot give it, and your searches stay local either way. Say this before I enable embeddings. Any caller other than you (the local stdio agent) reaches memory only through a binding I issue at my terminal (`full access-binding issue`); you cannot issue one, and I hand its bearer over as a file, never in chat.
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

An exit code of zero is separate from observed test evidence. Each suite declares
`test_protocol`, `minimum_executed`, `executed_checks` and `skipped_checks`.
`unittest` requires its final complete summary; compatibility `assertion-lines`
suites count explicit PASS/FAIL lines. Missing or insufficient executed checks
produce `ERROR` with `reason_code`, even on exit zero. A valid all-skipped suite
remains `SKIP`; reported failures are `FAIL` even if a script swallowed its exit.
Restore the suite's entry point or reporting contract, then rerun that suite.
These are reports from trusted repository tests, not authentication: forged,
well-formed counters can lie. A positive minimum does not prove full coverage.

### Disk cleanup revalidation

`full disk-guard --apply` preserves Git repositories and linked worktrees inside
temporary roots and refuses temporary targets beneath Git ancestry. Build
output is rechecked for checkout activity, ignore rules, tracked content and new
writes. Each path action probes current holders within a five-second shared
deadline; unknown evidence skips it. POSIX actions require `lsof` for this final
target-scoped probe; Windows retains rename-based protection. A skipped target
is not proof of zero reclaimable space. Replan after resolving the reported
reason. [Design and regression cases](design/DISK-GUARD.md#apply-time-preservation-2026-10-13).

Disk-guard measurement limits: unknown swap remains `null` with a reason and
source time; it is not zero. The largest-file report carries completeness,
error counts and budget exhaustion. An incomplete empty scan is shown as an
incomplete measurement. The MCP overview exposes `guard.largeFilesCoverage`
beside its bounded file list. See [measurement coverage](design/DISK-GUARD.md#measurement-coverage-obs-87-subset).

## Database rotation failure boundary

DigitalOcean rotation verifies every selected `--spec-env APP:KEY` literal before any reset.
Its name is not evidence: a plain value must equal the selected database user's current
provider password, or be a database URL with that password, user, engine and the provider's
connection host/port. Opaque encrypted values and unverified pool endpoints are refused;
migrate those consumers to App Platform database bindings before rotation. No command in
this flow prints the current password. Only the named consumers are moved, with downtime
between reset and ACTIVE; ACTIVE is not application health verification.

The door writes an owner-only receipt under `store/digitalocean-rotations/` before reset,
initially `reset: unknown`. A lost or malformed response, timeout or nonzero CLI result can
follow a committed reset: exit 5 means **unknown**, not “nothing changed”. Never repeat
`db rotate` to recover it. Use `db credential` to deliver the current value into the selected
vault slot, then reconcile each consumer without another reset. Exit 1 is a refusal before
reset; after an attempt, 2 means deployment unconfirmed, 3 vault delivery failed, 4 an app
update/redeploy or temporary spec cleanup failed, and 6 receipt update failed. The largest
code wins. Independent consumers continue after a confirmed reset despite another consumer's
failure. Provider/vault error bodies are withheld. A crash or failed final receipt write
leaves the earlier uncertain receipt; there is no automatic reconciliation, retry or claim
of a transaction across provider and vault. A failed temporary-file cleanup may leave its
owner-only spec file and is reported for local recovery.

Reset, app updates and redeploys each make one attempt; only read calls retry.
A lost/nonzero mutation response leaves that consumer `unknown`, even if the provider
already applied it. The receipt distinguishes this from `not_started` for a local
pre-call failure. Read current spec and deployment state before any retry; a blind
redeploy is not recovery for a literal. Independent peers still continue. Deployment
confirmation accepts a validated phase equal to `ACTIVE`, never a suffix or arbitrary
provider text; malformed metadata remains unconfirmed and its body is not printed.

### Leak discovery coverage

Vault slot discovery reports unreadable ancestors and excludes symbolic links or special files as unmeasured. Session directory links are likewise unmeasured and are never followed.

Leak discovery reports unreadable sessions, log directories, dashboard metadata and
tracked-tree listings as `source-unreadable`, preserving sightings from readable
siblings. Permission/stat/timeout errors never become an empty healthy source.
A package with no Git marker in its ancestry explicitly reports that it has no
tracked-tree source; a present marker whose Git listing fails is a coverage hole.
Missing optional paths remain absent. A source expected to be a file but resolving
to a directory, submodule entry or special file is explicitly unmeasured and is
never opened as a file. An interrupted or unreadable source is
unmeasured, not proof that it contains no credential.

### Tool configuration coverage

With the harness integration enabled, the scan names credential-shaped fields in
user tool configs and `.mcp.json` files one level below the configured projects
root. It never prints their values. Project-scoped names use a measured registry
id when available and otherwise the directory basename. A malformed config emits
`harness.scan_degraded`; line-based name recovery is marked `parsed: false` and
never establishes full coverage. Unreadable, unsupported or oversized files are
also explicitly unmeasured. Git-tracked plaintext configs raise a critical
finding; failed tracking checks remain unknown. Follow the reason-specific remedy
and rerun `project-observatory full harness`. A tracked key may remain in history:
review exposure and revoke or rotate through its owner before treating it as safe.

### Scheduled measurement freshness

Tick receipts distinguish `disabled`, `not_started`, `stopped`, `failed` and
`ran`. Disabled steps preserve the last successful measurement time; they do not
raise starvation. A collector absent from the last tick's `collector_order` is
not classified as starved. Planned collectors warn after three missed starts or
six hours without a completed measurement (`config/tick.json` `starve_hours`
overrides the age bound); leaks and integrity use critical severity. Interrupted
attempts retain unknown completion. Unsupported rotation schemas are reported as
`tick.rotation_degraded`, never interpreted as a healthy empty queue.

Merge provenance records `inputs` timestamps and degrades old inputs: local
filesystem data after two hours, GitHub/remotes after six, daily inputs after
48. Legacy files without a declared timestamp use file modification time;
invalid declared timestamps stay unknown. A new merge does not refresh them.
Rollback to an older engine can reset schema-2 starvation streaks; allow fresh
receipts before trusting scheduling coverage after rollback.

### Repeated cleanup stops and unmeasured sources

Three consecutive guard summaries reporting stopped Git cleanup produce
`machine.guard_stopping` (warning), with the latest count and pressure level.
The guard's own unavailable sources produce `machine.guard_degraded` (info).
Only a Trash Full Disk Access failure receives the macOS consent instructions:
a person grants the maintenance job's interpreter access in System Settings →
Privacy & Security → Full Disk Access, or leaves that source unmeasured. Other
access, journal and time-budget failures keep their own reason and recovery step.
The compact agent overview already carries Git `partial` and `carried` evidence;
carried measurements are not a new cleanup authorization.


For a single issued OpenRouter key, use `tools/openrouter.py spend NAME [--date
YYYY-MM-DD] [--json]` beneath `project-observatory full-path`. This is a management-key
activity read, not an inference call or a mutation. It reports the current ledger
identity only, excluding retired keys; do not call it a project spend total. Null
optional counters remain unknown. An unavailable/malformed report or changed identity
returns no figure and nonzero exit, never a measured zero. See the
[operator guide](ONBOARDING.md#spend-for-one-issued-openrouter-key) for bounds and recovery.

## OpenRouter revocation evidence

`openrouter.py revoke NAME` checks the provider identity by its recorded hash and
refuses known consumers, including a successor seen only in the scan's destination
labels. It requires a complete OpenRouter scan no older than 36 hours. Missing,
unreadable, malformed, degraded, future-dated or changed evidence refuses before
provider deletion and keeps the ledger row. Refresh the scan with the engine's
`collectors/scan_openrouter.py OUTPUT` command, using the workspace's
`store/raw/openrouter.json` as OUTPUT, then inspect the result before retrying.
Refreshing is a provider read and uses the configured provisioning credential.

The current provider label is used only in memory for matching. The guard does not
yet prove that every live vault slot or remote environment is unused, and a saved
scan is not a transaction with external consumer changes. There is no unread-evidence
bypass. `--forget-pending --reason` remains a separate journaled operator attestation
for a hashless pending row; it never deletes a provider key.

## Policy-managed rotation admission

`openrouter.py issue-for-policy --policy FILE` records explicit policy provenance
for new keys and existing rows selected by that policy's named sources. The record
contains a version, normalized project, policy digest, origin kind and observation
time. It stores no policy filename. `--dry-run` does not write this provenance.
A later policy application updates the snapshot when the policy changes.

`rotate NAME` and `rotate --leaked` refuse policy-marked rows before provider calls
because no current aggregate headroom proof is implemented. The refusal also applies
to an unmarked alias sharing an exact hash with a marked primary or reserved
predecessor/successor identity. A different ledger name cannot bypass it. The stored policy
snapshot does not authorize overlapping capacity; stale or malformed provenance
also refuses. There is no force option or automatic recovery command. Re-applying
a policy does not make its snapshot a live headroom proof.

Existing unmarked rows are not classified by project names or destination folders.
Unmarked legacy and standalone rotation retain their previous behavior and can leave
overlapping capacity after a failure. Their project-wide capacity remains unverified.
Explicit legacy re-attestation and a bound headroom/reconciliation protocol remain
open; no existing operational rows are migrated by installation.

### Missing worktree registration receipts

For a missing worktree, cleanup invokes Git's repository-wide registration prune.
A `pruned` journal row means that command exited zero; `registration_changes` stays
null because the row does not measure how many registrations changed. A nonzero
exit, timeout or launch error is `failed`; its effect remains unknown and it is not
counted as a successful prune. Reasons omit Git output and exception bodies.
Inspect `git worktree list --porcelain` in the owning repository before retrying.
Proof: `tests/test_git_hardening.py::MissingRegistrationReceipt` in the engine.
