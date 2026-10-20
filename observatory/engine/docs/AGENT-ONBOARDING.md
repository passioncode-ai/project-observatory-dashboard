# Set up my Project Observatory

Help me install and configure the full Project Observatory on this computer.
Read ONBOARDING.md and COMPATIBILITY.md first: under `docs/` in a checkout of the release, or under `$(project-observatory full-path)/docs/` in an installed release.

1. Verify Python and SQLite requirements. Explain the code directory and the separate private workspace. Run `project-observatory full version`, then `full init` and `full doctor` with an explicit OBSERVATORY_HOME.
2. Ask which project directory I want to observe. Configure only that directory. Explain which local metadata will be read. Start with filesystem scanning and the local dashboard. Later, when a session opens an eligible Git repository or worktree outside that root, SessionStart writes its exact path to `config/tracked.json`; the next `full local` or tick materializes it through the ordinary scan → merge → emit pipeline. Do not broaden `sources.projects` to each parent and never write `registry/projects.json` directly. This grants no credential or memory binding and changes no organization ownership. `full untrack local-folder PATH` keeps a path removed across later sessions; an explicit `full track local-folder PATH` opts back in, and `full tracked` shows declarations and degradations.
3. Offer optional GitHub, Bitbucket, hosting, DNS, analytics, memory and model integrations separately. Missing integrations remain unconfigured. Do not enable paid reasoning, remote secret reads, memory remediation, notifications or the observation scheduler as part of a default setup. Offer `features.auto_cleanup` separately and say what it does: the maintenance job (every 15 minutes) then runs the disk guard (`full disk-guard`): it removes application and tool cache folders, npx's package cache, temporary files, macOS diagnostic reports, abandoned browser clones, Trash items older than 30 days, the cache tools' own caches and git-ignored build output of idle checkouts, then merged or pushed branches and clean worktrees whose branch stays — more of them as free space falls (below 25 GB: 3 days; below 10 GB: 1 day) — and refuses observed holders or unknown holder coverage, privacy-guarded folders and another account's files. Holder checks are point-in-time observations; exclusion of external writers starting afterward is unproved; preserve `.wrangler` local state even when an old artifact configuration lists it; manual dirty-worktree removal skips all ignored files because its archive excludes them (preserve or move those files before retrying `full cleanup --apply --include manual`); show me `full disk-guard` and `full cleanup --level low` (the plans, which remove nothing) before I turn it on. The maintenance job (every 15 minutes: updates and a daily encrypted backup) is separate: `full init` schedules it only when I run init at my own terminal; run by you, init schedules nothing and answers `not-scheduled-here` with the command, `full maintain ensure`, which also needs my terminal. Remote memory embeddings need my own consent per project (`full embedding-policy grant`, at my terminal); you cannot give it, and your searches stay local either way. Say this before I enable embeddings. Any caller other than you (the local stdio agent) reaches memory only through a binding I issue at my terminal (`full access-binding issue`); you cannot issue one, and I hand its bearer over as a file, never in chat.
4. Explain the minimum credential permissions for each selected integration. Have me enter secrets in a local hidden prompt, secure file or credential manager. Never ask me to paste a value into this conversation. Report only names and connection results.
5. Run a first observation and show its coverage, findings and unavailable inputs. A disconnected source is not a clean security finding.
6. Explain how to update, preview a migration and restore a backup. Say that the maintenance job also keeps a daily encrypted bundle of this machine's agent setup (`full machine-backup`, `full machine-backup status`) and that `full machine-restore --latest` previews moving it to a new machine; `--apply`, `--packages` and `--clone` are mine to run. If this is an existing installation, stop writers before migration and keep the original until the new copy is verified. Do not start a second scheduler over the same data.
7. Finish with the local dashboard address, commands for another scan, and the list of enabled integrations. Keep the public marketing site separate from the private dashboard.

## SSH keys

When I ask for an SSH key, read `observatory_ssh_keys` first (my keys, fingerprints, passphrase
state, where each is used and registered, the findings). It never contains a private key, and you
never open one: no `cat ~/.ssh/id_*`, no `ssh-keygen -y`. A `--verify` that fails, or names another
key, means the new key is not proven: do not remove the old one. Never delete a GitHub key on the
strength of a `ssh.github_orphan` row: it is information, and Observatory cannot prove a key unused. Make a key with `project-observatory full
ssh-keys create NAME` (in my terminal, so I type the passphrase into ssh-keygen; `--no-passphrase`
only when I ask for an unattended key), copy its public line with `ssh-keys copy NAME`, and register
it with `ssh-keys send NAME --to github` or `--to server:ALIAS --verify`. A cloud has no door yet:
say so and give me the copied line. A missing `gh` scope is mine to grant (`gh auth refresh`).

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
target-scoped positive observation; unmatched rendered argv remains unknown, so
current POSIX production path removal is refused. Preliminary cleanup admission
uses an explicit unknown snapshot without process I/O. Disk-guard phase receipts
say `observation=not-attempted`, `measured_at=null`; these are no measurement
of zero holders. Explicit hint readers have a 2 MiB combined output cap, bounded
parsing and a shared deadline; limit failures remain unknown. The unbounded
Linux `/proc` fallback is retired. These are payload limits, not measured RAM
reclaimed; see [capture boundaries](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/DISK-GUARD.md#bounded-explicit-holder-observations). Windows also reports unknown
without attempting rename as a holder test. A skipped target
is not proof of zero reclaimable space. Do not advise an immediate retry as a fix
for unsupported holder completeness: preserve the target pending a supported
observation and owner lifecycle contract. Cache commands with nonempty `busy_if`
likewise refuse unmatched rendered argv; commands with no holder preconditions
keep their separately configured policy. [Design and regression cases](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/DISK-GUARD.md#apply-time-preservation-2026-10-13).

Disk-guard measurement limits: unknown swap remains `null` with a reason and
source time; it is not zero. The largest-file report carries completeness,
error counts and budget exhaustion. An incomplete empty scan is shown as an
incomplete measurement. The MCP overview exposes `guard.largeFilesCoverage`
beside its bounded file list. See [measurement coverage](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/DISK-GUARD.md#measurement-coverage-obs-87-subset).

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

### The agents on this machine

`registry/agents.json` (and its wiki copy `inventory/agents.json`) is generated each tick
with `features.machine_watch` on: one row per Fabric service descriptor, `id`
`agent:<service-id>.<instance>`. Read `supervisor.state` (`running`, `loaded`, `not-loaded`,
`not-measured`, `platform`, `unsupervised`), `manifest.state` (`pinned`, `absent`,
`unreadable`) and `mcp_declarations` (ids from `registry/mcp-servers.json`; `null` means not
measured). Check `stale` and `degraded` before saying an agent is absent or stopped: with `stale` true the
scan is older than one tick and `carried` rows are an earlier scan's. Never edit the file:
the next emit rewrites it. Fix a descriptor through its installer instead.

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

For a stopped tick, inspect `store/raw/tick-run.json` `stage_progress` and the
same field in `tick-runs.jsonl`. The run ID must match the producer's current
`tick.json` `progress`; the supervisor already rejects stale or malformed input.
Coverage is `observed`, `partial` or `unknown`, not a declaration that every
planned stage ran. Stages include captured-output tail commands. A start without
an end stays `unknown` with null exit/duration; never infer a stage timeout from
a whole-tick timeout. `not_started` needs the watchdog's explicit refusal.
Durations include watchdog overhead. While a run is active, top-level
`tick.json` `finished_at` still belongs to the last completed report. New stage
metadata excludes command/output/exception text, and its write failure does not
replace the command's exit. See `tests/test_tick_progress.py` for fault controls.

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

### Missing worktree registration receipts

For a missing worktree, cleanup first proves the directory absent: its parent folder
is listed and lacks the entry. An unplugged drive, a renamed parent folder or a
permission error is `unknown` instead; the survey marks that worktree `unreadable`
and nothing is pruned. When the directory is absent, the worktree's HEAD is kept
reachable first: by its branch, or for a detached HEAD by an
`archive/wt-<sha8>` branch (a symbolic ref there does not count). Then only that
registration's entry, `.git/worktrees/<name>`, found by the path its own `gitdir`
records, is moved to `.git/worktrees-retired/` with its index and reflog: never a
working directory, never a repository-wide `git worktree prune`. A directory that
comes back loses nothing; move the entry back and run `git worktree repair` to
register it again. An unknown, changed or unreadable HEAD, or a locked or ambiguous
entry, moves nothing. A `pruned` journal row names the kept ref (`retained_ref`) and
the moved entry (`retired_entry`); `registration_changes` stays null. A failed or
interrupted move is `failed` with an unknown effect. Reasons omit Git output and exception bodies.
Inspect `git worktree list --porcelain` in the owning repository before retrying.
Proof: `tests/test_git_hardening.py::MissingRegistrationReceipt` in the engine.

### Open files absent from directory scans

`project-observatory full machine --open-unlinked` records files that have been
unlinked but remain open on macOS or Linux when `lsof` is available. It never
stops a process, removes data or changes cleanup settings. Windows and a missing
source return unknown, not zero. There is no scheduled observation.

The bounded receipt `store/raw/open-unlinked.json` stores numeric process IDs and
snapshot-local device/inode identities, logical sizes, source start/end times and
the parser source hash. It collects no paths, process commands or environment.
The command has a five-second deadline, a combined two-MiB output ceiling,
100,000 fields and 10,000 distinct file records. Missing sizes, conflicting sizes
for a shared identity, diagnostics, malformed output and partial execution remain
degraded. Known-size peers survive as a separately labelled subtotal.

`full machine --coverage`, `full doctor` and
`observatory_machine(section="coverage")` read the saved observation only. The
reader exposes up to ten size/PID-count samples and marks snapshots older than
30 minutes stale. Shared files count once, so process totals must not be added.
A successful command does not establish visibility of every process or namespace.
Logical bytes are neither allocated blocks nor reclaimable memory or disk space:
allocated/reclaimable values stay null and every observation remains non-actionable.
Ownership and any supported supervisor action require separate evidence.

Contract tests: `tests/test_open_unlinked.py`, including a
disposable open-then-unlinked file owned by the test; no host process is stopped.


### Checkout observation does not fetch missing objects

Checkout inventory and local-state Git queries explicitly use the offline
observation profile. Partial clones can omit objects that an ordinary Git read
would download on demand; this observation refuses that download and keeps an
unreadable state unknown. It does not fetch upstream refs or establish remote
backup durability. Explicit fetch and remote-query operations outside this
profile retain their existing behavior. This is not a new cleanup permission.

Proof: `tests/test_checkout_state.py::OfflineGitObservation` in the engine uses
only disposable local file remotes, checks object-store contents before/after,
and includes a positive complete local observation and explicit remote controls.


### Read local upstream graph evidence

An explicit checkout scan saves local-state results before a separately budgeted
upstream graph phase. That phase reserves up to a quarter of the scan's time
budget, at most five seconds, with its own persistent round-robin cursor and
bounded child per checkout. Failures preserve the saved local-state observation.

For a supported complete local graph, the receipt records ahead/behind commit
counts and whether the upstream is another local branch or a remote-tracking
ref. These are local resolved commits only: a remote-tracking ref can be stale,
so `remote_durability` remains `unknown`, and `actionable` remains false. No
fetch, deletion, pruning or owner inference follows. Detached/unborn/missing
upstream, shallow or promisor history, replacement refs, grafts and alternate
object stores produce unknown counts. Those exclusions preserve uncertainty
rather than silently interpreting a different graph.

The saved coverage view uses the graph's own measurement time and source binding;
it never refreshes graph evidence from another phase's success. After 30 minutes,
a malformed receipt or a source/binding mismatch, counts are unknown. Git output,
remote URLs, commit subjects and ref names are absent from this aggregate view.
Proof: `tests/test_checkout_state.py::UpstreamGraph` and `GraphPhase` in the engine.
