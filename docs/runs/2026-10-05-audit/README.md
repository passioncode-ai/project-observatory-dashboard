# 2026-10-05 — a full audit of 0.17.3, and the fix list

This audit followed the operator's instruction of 2026-10-05. It checked the code, the architecture and the interface, and every screen and every scenario. It then fixed the findings one by one, updated the docs, ran a final check and wrote a summary of what this release carries.

Baseline: `58bf0d9` (release 0.17.3). Three independent passes ran in parallel, all read-only and sandboxed:

- **Interface.** All 12 dashboard pages were opened in a browser against the live server, at 1440×900 and 390 px, in Russian. The page source was used for `file:line`.
- **Code and architecture.** The newest code (0.16.0–0.17.3) was read end to end, and claims were checked with sandboxed snippets.
- **Scenarios.** 57 scenarios: O1–O9, OSS-01…OSS-39 and SCN-001…009. Each was traced in code, and where cheap it was run in a sandbox. Every cited test was checked to exist and to assert what the scenario claims. `swift test` ran the app's model, bridge and navigation tests.

Screenshots of the live dashboard hold the operator's data, so they are kept out of this public record.

Live verification on the maintainer's machine added the rows marked "live".

Status words: **open**, **fixed** (with the change and its test), **deferred** (with the row it moved to, and why).

## P0 — data loss

| ID | Finding | Evidence | Fix |
|---|---|---|---|
| A01 | Losing `secrets/backup-passphrase` makes the next pass generate a NEW passphrase and overwrite the only outside copy (Keychain `-U`). Every existing encrypted backup then stops opening | `backup_vault.py` `ensure_passphrase`; reproduced with a dict store | When the file is missing and the store holds a value for this label, restore the file from the store. Never overwrite a stored value that differs |

## P1 — broken behaviour or security

| ID | Finding | Evidence | Fix |
|---|---|---|---|
| A02 | Auto-update never succeeds on Linux. `full update --apply` without `--writers-stopped` refuses when launchd is absent. The pass records `refused` and retries daily forever. The plugin bridge fails the same way | `maintenance.py` `step_update`; `engine_update.py` `stop_writers`; reproduced in a sandbox | Pass `--writers-stopped` where there are no launchd jobs: in the pass, and in the hook on Linux |
| A03 | A timeout, SIGTERM or `maintain uninstall` during an automatic update kills it halfway. The jobs it stopped stay stopped, and the update lock is released | `maintenance.py` `Commands.full` (`timeout=3600`), the SIGTERM handler, `unschedule` | Run the apply in its own session and never kill it. `uninstall` defers while a pass runs |
| A04 | Automatic updates trusted GitHub alone: two digests from the same release, with no signature check | `engine_update.fetch_verified` | Verify `SHA256SUMS.asc` against the organization's pinned release key before an automatic apply |
| A05 | The restore command `init` prints cannot work (the home is no longer empty). It also puts the passphrase on the command line | `maintenance.restore_latest`, `workspace._init` | Name `--home NEW_EMPTY_HOME` and a plain `full restore FILE`, which prompts |
| A06 | `restore --latest` takes the newest snapshot across all labels. An empty fresh workspace's daily backup can win over the real data | `backup_vault.candidates`, `maintenance.restore_latest` | When several workspaces' backups exist, do not choose: list them and name the command |
| A07 | Tests run from a terminal could load real launchd jobs and write Keychain items. The guard trusts a TTY when the home is not under the child's temporary directory | `maintenance.system_setup_allowed`; `run_portable.py` environment | The test environment sets `OBSERVATORY_SYSTEM_SETUP=0`. The engine refuses when `$HOME` is not the account's real home |
| A08 | Tests that leave `OBSERVATORY_HOME` unset resolve the operator's real workspace. `test_backup_store.py` run directly wrote three copies under the real label: 8522 bytes each, the size of its 8192-byte fixture encrypted at 0.17.0. This is OBS-39's caller | `tests/test_backup_store.py` `run()`; another session's 0.17.0 worktree | `tests/tmp.py` refuses a default home and sets `OBSERVATORY_SYSTEM_SETUP=0`. The suites set their own home |
| A09 | The Heroku scan failed (`heroku auth:token did not finish in 30s`), yet the board shows "0 apps", "$0/month" and "empty", and the failure never reaches the reader | UI audit; `build_dashboard.py` `nothingFound`; `shell.py` | Render the recorded `degraded` reason and the re-scan command. Tiles say "not read" |
| A10 | On a fresh workspace a workflow's declared keys read `unknown`, because no vault directory exists yet. That blocks the workflow, and "Needs you" says "Nothing is waiting for you" | scenario audit OSS-26/28; `survey.credentials`; `agents_view` `needsYou` | An absent default vault is empty (`missing`, with the `vault.py put` command). An `unknown` key appears in Needs you |

## P2 — architecture, observability, interface, docs

| ID | Area | Finding | Fix |
|---|---|---|---|
| A11 | snapshots | "Writers stopped" is not true: session MCP servers and the Stop hook write to the store and logs during a snapshot. The inventory comparison then fails it, and copies opened `immutable` may tear | The store (copied through the backup API and verified) and append-only logs are left out of the equality check. A copy never opens `immutable` |
| A12 | maintenance | The daily snapshot and a person's `full update` both stop and start the tick and server without a shared lock | The snapshot skips while the update lock is held |
| A13 | maintenance | Stale state: `services_not_restarted` and `update.failures` are never cleared. `snapshot_failure` is read nowhere. `another-update-running` waits a day. `wait_idle`'s message is not transient. A stale `GH_TOKEN` gives 401 forever | Each is cleared on success or surfaced. A 401 retries anonymously |
| A14 | Health / doctor | "Scheduled" means installed, not loaded. The status warnings are not rendered. Where the passphrase is kept is not shown. The row names no versions (engine, app). "Full backup" hands over `maintain status`, not `backups status`. `maintain status` says check `update-available` and update `updated` with the same time | All shown. After an update the check reads as the version now installed |
| A15 | forget | The receipt misses plaintext snapshot directories and `.failed-update-*` copies. A restore brings erased text back, unsaid | Name them in the receipt; say it in FORGET.md |
| A16 | access bindings | A full restore restores `access-bindings.seen.json`, which re-opens revoked bindings | deferred to its own row: a design change to where the anti-rollback record lives |
| A17 | memory-http | The refusal limiter is keyed by the peer, which is always 127.0.0.1. One local process flooding without a bearer locks every client out | Keyed by peer and bearer fingerprint. No bearer has its own bucket |
| A18 | backups | `.failed-update-*` siblings (plaintext, with `secrets/`) pile up, unreported | Reported in `backups status` and doctor |
| A19 | logs | The maintenance and hook logs are rotated only by the tick, which is off by default | The pass applies the log policy to its own logs and the hook's |
| A20 | hook | `claim()` has a race in a stale window (`rmdir` then `mkdir`) | An exclusive `flock` in a one-line python call |
| A21 | Linux | The systemd unit uses `IOSchedulingClass=idle`, the throttling 0.17.2 removed on macOS | Removed |
| A22 | app | `pgrep -x` without `-u`: another user's running app blocks the swap | `pgrep -u <uid> -x` |
| A23 | docs | ONBOARDING says launchd jobs exist only after the scheduler is enabled. `memory-http` is missing from the lifecycle table. D8's wording. Bare tool names in a doctor warning. AGENTS' test path. `before-update` uncounted in `backups status`. A stale configuration comment | Corrected |
| A24 | tests | Vacuous or blind tests: a `-mmin -1440` check, the guard test, the `-shm`-only OBS-37 test, `handed_commands` leaving five command families out | Each asserts what it claims |
| A25 | system setup | `OBSERVATORY_SYSTEM_SETUP=0` still lets `doctor` and the dashboard probe launchctl, and `restore --latest` read the Keychain | `=0` skips both |
| A26 | ENV page | The "Prod" column is "—" everywhere and nothing says why: the Heroku read failed | The Heroku degradation is carried into the remote comparison and shown |
| A27 | Agents page | It says "live, refreshed" before any fetch. Raw `project:` ids. "Workflow" untranslated. `aria-live` on every refresh. Two time zones | Fetch at once. Names. Translated. Announce only on change. One zone |
| A28 | Findings | Detail and action texts are English in the Russian build, with raw ISO times and broken grammar ("the value was seen at echoed by") | Grammar and dates fixed. Translating finding texts deferred: OSS-19 says they stay English, and the doc is made exact |
| A29 | Findings | Copied commands start with `python`, which this Mac does not have | The engine's interpreter, as the Keys page does |
| A30 | Findings | Five rows share one title ("…no clone confirms…") with no repository named | Repository in the title |
| A31 | Findings | 390 px: the type filter overflows (413 > 390) | `max-width:100%` |
| A32 | Findings, Health | Copied ack/accept/reject commands end in `'--why' ''`, which the tool refuses | A visible `'REASON'` placeholder |
| A33 | Findings | Two near-duplicate type names ("Клон отстаёт…", "Клоны отстают…") | Merged |
| A34 | Health | Raw kind codes in the queue ("454 observation"). An ambiguous unit "кред." | Translated. `$` |
| A35 | Health, Keys | 390 px: long commands in `span.mono` overflow | Wrap |
| A36 | MCP | All 60 rows "not probed", with no word on how to probe | One line with `full scan-mcp` and the age of the last probe |
| A37 | MCP | A stdio command shown twice | Once |
| A38 | Projects | The sparkline total is unlabelled and wraps mid-number. A raw rule token in the panel | Labelled, `nowrap`. Words |
| A39 | Domains | The copied DNS command (`dig +short NAME A AAAA CNAME`) queries only CNAME. Raw Cloudflare states. Registrars split by spelling | A loop over types. Translated. Grouped by IANA id |
| A40 | Overview | ENV card counts two populations. No thousands separators in tiles. "REMOTE" untranslated | One population. Locale numbers. Translated |
| A41 | Keys | The issued-keys ledger is listed as a door | Listed as the door's ledger |
| A42 | Machine | English location labels, ISO times, mixed units, raw ids in the Russian build. `sysctl` called by bare name (memory missing under a minimal PATH, no degraded entry). The "Not measured" hint names the wrong command | Translated, formatted, `/usr/sbin/sysctl`, degraded entry, right command |
| A43 | Traffic | `owner-.github` shown for `owner/.github` | The repository name |
| A44 | scenarios | OSS-13 used twice. Counts "ten"/"eleven" pages (there are 12). O9 and OSS-38 stale. OSS-01 omits `init`'s scheduling. OSS-22/19 overstate. Links that break inside the wheel. Rows cite no test | Renumbered (OSS-40), corrected, tests cited |
| A45 | scenarios | 15 shipped user paths have no scenario: manual update, maintain, backups and passphrase, profile, agent install, MCP inventory, the agent's read path, credentials by name, vector namespaces, the review queue, Health rows, models and budget, open/serve, credential doors, the leak register | Written as OSS-41… |
| A46 | tests | No golden list of MCP tool names. A missing `node` reports PASS with nothing run. OSS-11/12 assert less than they claim. OSS-35 never calls `memory.*` over HTTP. SCN-003 untested | Added |
| A47 | app | With only the Assistant window open, a Dock click does not bring the dashboard back | The reopen handler shows the dashboard |
| A48 | update (live) | An update stopped the tick and server for 13.5 minutes (21:19:46–21:33:12 UTC); `workspace-upgraded` took 7.5 minutes under load | deferred with a measurement row: keeping the server up through an update is a design change |
| A49 | records | HANDOFF has no 0.17.x entry. No receipt for 0.17.1–0.17.3 | Written |
| A50 | site | The site states no fact about updates that install themselves | A facts row and one sentence on the Observatory page (website repository) |
| A51 | backlog | OBS-19/20 are more severe now: daily backups depend on a background Python's consent to `~/Documents` | Rows raised. The decision stays the operator's |

## Notably solid (so the list is not inflated)

- HTTP memory access fails closed.
- `OBSENC1` is sound.
- The `full update` transaction holds: double digest, lock-constrained install, rollback.
- Restore never overwrites.
- Secrets never reach argv.
- The maintenance pass is serialised.
- `forget` cannot over-claim.
- The ledger guards hold.
- Copies are verified before they count.
- All 123 commands the engine prints parse.

## Status (2026-10-06): every row is closed, and the fixes ship as 0.18.0

Every row above is **fixed**, with its test and a planted defect watched failing where that was cheap, except two:

- **A16 is deferred.** The anti-rollback record of revoked access bindings needs a home that a restore does not bring back. That is a design change, and it gets its own row.
- **A48 is deferred, with a measurement.** Keeping the server up through an update is a design change.

Two independent reviews then read the fixes themselves and found more defects. All of them are fixed, each with a test.

**Review of A01–A25, eight defects:**
- A Keychain read that failed looked like "no passphrase", and a guess replaced the stored one.
- Only one earlier passphrase was kept.
- The snapshot only checked the update lock instead of holding it.
- A rollback's outcome was written into the copy the rollback moves aside.
- The rollback wheel of a signed release was accepted unsigned.
- A malformed signature raised a traceback instead of a refusal.
- `maintain run` and `maintain hook` ignored `OBSERVATORY_SYSTEM_SETUP=0`.
- ONBOARDING was wrong about reboots and about a refused token, and systemd killed the detached update.

**Review of A09 and A26–A43, six defects:**
- A missing stdio binary was no longer said.
- The pause message on the Agents page went unannounced.
- Health warnings appeared twice, the second time in English.
- The installed versions showed only on the healthy path.
- The queue digest read "454 наблюдение".
- A Cloudflare-only domain showed its raw state.

**Found on the way and fixed:**
- The scenario pass found five defects:
  - after `rotate --leaked` the doors named `vault.py rotate` instead of `settle`;
  - a ceiling move on an OpenRouter key was not journaled;
  - the MCP instructions called `observatory_project` and `observatory_timeline` paged, though neither takes a cursor;
  - `full machine` named the retired single page;
  - UI-PLAN miscounted the pages.
- `use_secret.py names` listed only one of a project's vault folders.
- Two dashboard suites, run directly instead of through `run_portable.py`, read the live workspace inherited from an agent's shell. `tests/tmp.py` now refuses a non-temporary `OBSERVATORY_HOME`, and the dashboard fixture refuses without the runner's synthetic workspace.
- A cold-open race in `store/compatibility.readonly_uri` failed 1 open in 360 under load. It was found by the release gate.
- The browser pass at 1440 and 390 px, in both locales, found a long finding subject and a long queue statement that scrolled the page sideways, and an MCP declaration's project scope shown raw.

**Checks:**
- `tools/release/gate.sh` (portable, 225 suites): PASS.
- Top-level tests: 127 OK.
- `swift test`: 63 OK.
- The browser pass: all 12 pages, both locales, 1440 and 390 px. No horizontal scroll, no `undefined`/`NaN`/`[object Object]`, no raw project id.

**What 0.18.0 carries** is in the CHANGELOG.

**Next:**
1. The release PR.
2. Tag `v0.18.0`. A person approves the `release` environment.
3. Verify the published set.
4. Install on the maintainer's machine.
