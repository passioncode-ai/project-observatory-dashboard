---
report:
  id: project-observatory/2026-10-03-observatory-audit-run-3
  title: "Observatory audit, run 3 of 3: what runs 1 and 2 missed, and the hand-offs closed"
  kind: audit
  project: project-observatory
  domains: [audit, security, correctness, mcp]
  as_of: 2026-10-03
  status: active
  valid_until: 2026-11-02
  summary: >-
    Third and last audit-and-fix pass over the public engine after run 2 (main 21e4a0d). It closed
    run 2's hand-offs: the stale dashboard after a workspace change was real and is fixed (R2-APP-5),
    the PassionCode 1.1.0 control edge is vendored (CS-1), and a vault PROJECT is the folder name with
    the registry id normalised to it. With five independent read-only audits it found 67 defects in
    all (duplicates merged): 66 fixed test-first, one in part (KEY-R3-8); six more were noticed late
    and left open as low. The largest: every engine git call
    could run the user's credential helper, hooks, signer or filters unattended; `full cleanup` could
    delete uncommitted work; a second leak sighting stopped the whole board; a pasted token became a
    project name on every page; a folder on a non-GitHub host had no history at all.
  sources:
    - name: "Run 1 report"
      path: "docs/reports/2026-10-03-observatory-audit-run-1/README.md"
      read_at: 2026-10-03
    - name: "Run 2 report"
      path: "docs/reports/2026-10-03-observatory-audit-run-2/README.md"
      read_at: 2026-10-03
    - name: "project-observatory-dashboard main at 21e4a0d (run 2 merged)"
      url: "https://github.com/passioncode-ai/project-observatory-dashboard/tree/21e4a0d"
      read_at: 2026-10-03
    - name: "PassionCode design tokens at passioncode-ai.github.io@dfcfd8c (design-system/tokens.css)"
      url: "https://github.com/passioncode-ai/passioncode-ai.github.io/blob/dfcfd8c0bef370a5b0141266333e70273366dd73/design-system/tokens.css"
      read_at: 2026-10-03
  produced_by:
    agent: claude-code
    task: "Observatory audit run 3 of 3"
  supersedes: []
  consumers: [docs/macos/SCENARIOS.md, docs/HANDOFF.md, CHANGELOG.md]
---

# Observatory audit, run 3 of 3

## Summary

Run 2 merged at `21e4a0d` with 27 fixes and five hand-offs. This run closed the hand-offs first,
then ran five independent read-only audits, each in its own temporary HOME and workspace:

- documentation against the code;
- the key lifecycle and the MCP wire against the published schemas;
- every path that could raise a Keychain or passphrase prompt;
- a new user's install and first hour from a fresh wheel;
- the 11 dashboard pages in English and Russian, keyboard only.

Fixes were made test-first, in five fix branches and in this branch, and integrated here. In all,
the run found **67 defects** (duplicates merged): **66 fixed**, **1 fixed in part** (KEY-R3-8), and
6 more noticed late and left open as low (O-1 to O-6).

**The hand-offs:**

- **R2-APP-5 was real.** Close the dashboard window, change the workspace in Settings and
  reopen the window: the kept web view went on showing the old workspace's pages under the new
  subtitle. A window open during the switch was fine. A test now hosts the window over a real
  WKWebView, so the case needs no display.
- **CS-1:** PassionCode 1.1.0 is vendored byte for byte. The dark `--pc-border-strong` is now
  `#6f5e77`, at least 3:1 on every surface, in the dashboard, the website and the Mac app.
- **The vault project name:** `PROJECT` is the project's folder name. A registry id or name is
  accepted and normalised to that folder, and a string two projects claim is refused.
- **SCN-008 through Launch Services passed.** It also found that Dashboard → Start Server was
  enabled where it could not start anything.
- **The keyboard-only walk** found focus lost after a sort and after a reset. **The
  accessibility pass** is now a test of the tree VoiceOver is given.

**Seven defects reached any user:**

- every engine git call could run a program from the user's git config unattended: the credential
  helper (a Keychain dialog), hooks, signer, fsmonitor and filters (KC-R3-1/2/3);
- `full cleanup --apply --include manual` could remove a dirty worktree after saving an empty patch
  (KC-R3-4, **data loss**);
- a second sighting of one leaked slot stopped `full local` before the board was built (KEY-R3-1);
- a token pasted as a project name, owner or note reached the registry, five pages and MCP
  (KEY-R3-3/4);
- a folder whose remote is on a host other than GitHub or Bitbucket had no history, and its unpushed
  commits were reported nowhere (NU-R3-1, DASH-4);
- a 3-project estate gaining a fourth was refused by the registry guard as a "+33% swing" (DASH-5);
- seven commands the findings hand over were refused by the step runner (DOC-2).

**What stays open:** one finding is fixed only in part, KEY-R3-8, whose `isError` semantics are
pinned by a published schema. Six defects were noticed late and left low (O-1 to O-6). What was
not run, and why, is listed under NOT_RUN.

Commits are cited by subject: the pull request merges by rebase, which rewrites every id.

## Scope and method

- **Source:** `origin/main` at `21e4a0d`, worktree branch `audit/run-3-2026-10-03`, editable
  install from `requirements-full.lock` on Python 3.14.7.
- **Audits:** five read-only sub-audits, each under its own `/private/tmp/obs-r3/<name>/` HOME
  and workspace, with synthetic projects (`alpha-web`, `beta-api`, `gamma-docs`, `delta-app`) and
  fake values on stdin only. Each returned file:line and command evidence.
- **Fixes:** five fix branches, each in its own worktree and venv, each ending with the full engine
  gate; then integrated by cherry-pick, with conflicts resolved by hand and the inventory
  regenerated, and the whole gate re-run here.
- **Mac app:** the QA bundle had its own id from the start (`ai.passioncode.observatory.qa3`), its
  own executable name, and its own defaults domain. The operator's defaults were exported first and
  compared at the end: unchanged. The session was locked for the whole run (the front process was
  `loginwindow`), so AX lists no windows and nothing can be brought to the front. The native
  walk went through the menu bar, which AX still exposes, and WebKit's own log. Window states
  were then settled in-process: `DashboardWindowTests` over a real WKWebView, and
  `AccessibilityTests` over the tree SwiftUI gives VoiceOver. The QA bundle was quit, unregistered
  from Launch Services, and its defaults and WebKit data were deleted.
- **No spend:** no provider was called; `install_key` and `stash` ran against a dead proxy.

## Findings

Severity: **high**, a user cannot do the thing, loses work or is told something false; **medium**,
a wrong path with a workaround, or a value leaving where it should not; **low**, copy, consistency
or a guard.

### Hand-offs from run 2

| ID | Area | Severity | Evidence | Status, fix (commit subject) |
|---|---|---|---|---|
| R2-APP-5 | App | high | Dashboard window closed, workspace A → B in Settings, window reopened. `onChange(of: dashboardMode)` never fired, because the mode was already B's. The view kept the controller's web view on A. Reproduced by `DashboardWindowTests.testAWindowReopenedAfterTheSwitchShowsTheNewWorkspace` ("the window shows …/ws-a/…, not …/ws-b/…"). | fixed: "fix(macos): a dashboard window reopened after a workspace change shows that workspace (R2-APP-5)" |
| CS-1 | Design | medium | The dark `--pc-border-strong` `#5c4e63` measured 2.60/2.51/2.33:1 on bg/panel/raised. Canonical `#6f5e77` at `dfcfd8c`. The 1.1.0 file adds a light block that `PaletteTests`' reader overwrote the dark values with (19 roles failed). | fixed: "fix(design): vendor PassionCode 1.1.0 — the dark control edge meets 3:1 (CS-1)"; dashboard, `site/tokens.css` and `Palette.swift`, with a 3:1 test in `PaletteTests` and `test_i18n` |
| DOC-1 / KEY-R3-5 | Keys / docs | high | `vault.py put local-alpha-web …` was accepted. The Keys page then called the slot "a credential of an organisation", and MCP's `use` command reached 1 of 4 slots. The docs never said which name `PROJECT` is. | fixed: "fix(vault): PROJECT is the folder name, and the registry id names the same folder (KEY-R3-5, DOC-1)" (`vault_project.py`; ONBOARDING and the skill say it) |
| SCN-008 | App | pass | Launched with `open`, workspace served once on spare port 47393, then Dashboard → Start Server: the port listened within 1 s. WebKit's log for the next Reload shows `didReceiveResponse: (httpStatusCode=200)`, an HTTP load (a file load has no status). | — |
| R3-APP-1 | App | low | Dashboard → Start Server was enabled with the port held by another workspace (the banner's button was disabled), on a live dashboard, and with no pages. | fixed: "fix(macos): Dashboard → Start Server is offered only where it can start" |

### Documentation (DOC)

| ID | Area | Severity | Evidence | Status, fix |
|---|---|---|---|---|
| DOC-2 | CLI | medium | `full google --force`, `full merge emit`, `full plugins --only ID --force`, `full emit findings` and `full scan merge emit` were handed over by findings and the Stop hook, but the step runner refused them ("step arguments are not accepted here", exit 2). This includes HANDOFF's contributor task 1. | fixed: "fix(cli): every handed-over `full` command is accepted; every --help is help". A `STEP_OPTIONS` allowlist was added, and `test_handed_commands` parses every `full …` command the engine emits |
| DOC-3 | HANDOFF | medium | Client-disconnect handling was called "proposed, unreleased" (it shipped in 0.11.0); a 0.6.3 block was headed "Current"; `full update` was listed as "being built" (0.7.0). | fixed: "docs: CHANGELOG Unreleased says what the three audit runs changed; HANDOFF is current" |
| DOC-4 to DOC-6 | Docs | low | A test path that does not resolve (COMPATIBILITY); SPEC's "WindowGroup" reopen (the dashboard is a `Window` scene); "all ten screens, OSS-13–20" (there are eleven pages, OSS-13–22). | fixed: "docs: scenario index counts eleven pages; a test path that resolves; the dashboard is a Window" |
| DOC-7 | Docs | low | The CLI-COMPATIBILITY receipt said 14 and 16 tests. Measured now: 17 and 17. | fixed: "docs(cli): the regression receipt's test counts are the measured ones, and dated" |
| DOC-8 | Hook | low | The SessionStart line printed a bare `use_secret.py names …`. | fixed: "fix(session-start): hand over use_secret.py by its installed path" |
| DOC-9 | CLI | low | `full machine --help` ran the survey and `full cleanup --help` printed the plan. `usage: observatory.py …`; `migrate-local` missing from `full --help`. | fixed in the DOC-2 commit (more than 200 subcommands answer `--help` and run nothing) |
| DOC-10 | Tools | low | `sync_engine_docs.py --help` ran in write mode. | fixed: "fix(tools): sync_engine_docs.py parses its arguments; --help writes nothing" |

### Keys and MCP (KEY-R3)

| ID | Area | Severity | Evidence | Status, fix |
|---|---|---|---|---|
| KEY-R3-1 | Leaks / board | high | Two `vault.py leak` rows for one slot → "duplicate finding id(s) ['secret.leaked_unrotated:secret:…']", `full local` failed at `findings`. | fixed: "fix(findings): one leak finding per vault slot, listing every sighting (KEY-R3-1)"; re-verified: one finding, board built |
| KEY-R3-2 | Keyserver | medium | `/api/leak` with `"project":"--help"` answered 200 `{"ok":true}` and recorded nothing (argv injection into `vault.py`). | fixed: "fix(keyserver): the leak route validates in process and needs the vault's receipt (KEY-R3-2)" |
| KEY-R3-3 | Vault / pages | medium | A 37-character fake token sent as a project name reached `credentials.json`, `findings.json`, five pages, the stdout of `full local` and MCP. | fixed: "fix(secrets): one credential-shape heuristic at every door that judges typed text (KEY-R3-3, KEY-R3-4)" (`credential_shape.py`) |
| KEY-R3-4 | Signing / audit | medium | `owner=sk-or-v1-…` was accepted by `/api/annotate`; a 38-character token passed as `purpose`; a 37-character id stayed unredacted in `keyserver.jsonl`. | fixed in the same commit |
| KEY-R3-6 | MCP / keyserver | medium | Refusals for an unknown project, a cursor, an owner or a tool name quoted the caller's value, a fake key included. | fixed: "fix(mcp,keyserver): refusals quote only well-formed identifiers; ignored input is named (KEY-R3-6, KEY-R3-7, KEY-R3-8)"; re-verified over stdio from the wheel |
| KEY-R3-7 | MCP | low | Unknown fields were ignored silently; `since:"garbage"` gave `degraded:[]`. | fixed in the same commit (named in `degraded`) |
| KEY-R3-8 | MCP | low | `degraded` is missing on writes and job handles, and `isError` is inconsistent. | **partly fixed**. The instructions and README now say what is true. `isError` was not widened, because `test_mcp_wire` and the published record schema ("a refusal is an answer") pin it. |
| KEY-R3-9 | Vault | low | `vault.py backup` looked only at `<gateway_root>/backup-secrets.sh`, while the docs said `bin/`. | fixed: "fix(vault): the backup script is found where the docs say, and the docs name it (KEY-R3-9)" |
| KEY-R3-10 / NU-R3-7 | use_secret | medium | `--env prod` silently fell back to a local `.env`; "not in projects/X/{local,stage,prod}" was printed when the slot was in another environment. | fixed in the KEY-R3-5 commit |
| KEY-R3-11 | OpenRouter door | low | When offline, `stash` said "this key cannot manage keys". | fixed: "fix(openrouter): an unreachable provider is its own stash refusal (KEY-R3-11)" |
| KEY-R3-12, KEY-R3-13 | Doors | low | `./tools/…` in `--help`; keyserver startup lines lost when redirected. | fixed: "fix(doors): --help names the installed path; keyserver startup lines are flushed (KEY-R3-12, KEY-R3-13)" |

### Keychain and unattended programs (KC-R3)

| ID | Area | Severity | Evidence | Status, fix |
|---|---|---|---|---|
| KC-R3-1 | Remotes probe | medium | A `gcrypt::` origin made `git ls-remote` run `git-remote-gcrypt` (planted marker); the real helper asks for a gpg passphrase. | fixed: "fix(engine): one hardened git door for every engine call (KC-R3-1/2/3/5)" (`safe_git.py`: transport allowlist) |
| KC-R3-2 | Wiki commit | medium | One tick ran the user's `pre-commit`, `commit-msg` and other hooks, fsmonitor ×8 and the clean filter ×22. | fixed in the same commit |
| KC-R3-3 | Collectors, Stop hook | medium | `scan_filesystem`, `scan_events`, `scan_git_hygiene`, `scan_env` and `record_turn` ran `gpg.program` (via `log.showSignature`), fsmonitor, `post-index-change` and filters (markers). | fixed in the same commit; an AST check refuses any git spawn outside `safe_git.py` |
| KC-R3-4 | Cleanup | high | With a `diff.external` set, `tracked.patch` was 0 bytes, yet the run reported "1 removed, 0 failed" and the uncommitted edit was gone. | fixed: "fix(cleanup): never remove a dirty worktree whose patch is not proven (KC-R3-4)" (`git apply --check` against a scratch index first) |
| KC-R3-5 | Cleanup | low | `branch -D` ran the user's `reference-transaction` hook. | fixed in the KC-R3-1 commit |
| KC-R3-6 | Light profile | low | A repo-local `log.showSignature` ran gpg from `core.git_metrics`. | fixed: "fix(core): the light profile's git never shows signatures (KC-R3-6)" |
| KC-R3-7 | Guard | low | The browser-launch guard missed pyppeteer, webdriver, ESM puppeteer and a second unflagged launch. | fixed: "test(keychain): check every browser launch, not every file (KC-R3-7)" |
| KC-R3-8 | Docs | low | It was not said that opt-in `gh`/`claude` and Developer ID signing can reach the login Keychain. | fixed: "docs(security): say which opt-in steps can reach the login Keychain (KC-R3-8)" |

### Dashboard (DASH)

| ID | Area | Severity | Evidence | Status, fix |
|---|---|---|---|---|
| DASH-1 | All pages | medium | Every control's edge measured 2.51:1. | fixed by CS-1 |
| DASH-2, DASH-3 | Keyboard | medium | Enter on a sort header, or on "reset", left `document.activeElement` on `body`. | fixed: "fix(dashboard): keyboard focus survives a sort and a reset (DASH-2, DASH-3)"; also checked in headless Chrome |
| DASH-4 | Project panel | medium | "No commits in the window" was shown for a project with 2 commits. | fixed: "fix(dashboard): the project panel says why history was not recorded (DASH-4)" |
| DASH-5 | Registry guard | high | Adding a 4th project gave "emit REFUSED: … +33%, limit ±25%", and 7 steps never ran. | fixed: "fix(emit): a small estate gaining a project is not a wholesale swing (DASH-5)"; re-verified: 3 → 4 projects, `local` exit 0 |
| DASH-6 | Demo | medium | Demo pages printed the builder's `$HOME` and workspace path. | fixed: "fix(demo): demo pages carry no path of the machine that built them (DASH-6)". This also found that every page carried its data payload twice. |
| DASH-7 to DASH-13 | Dashboard | low | Fake demo finding types; the keys page claimed what it had not measured; "active · active"; a permalink named "#" and three tab stops per row; untranslated "SWAP" and an echoing toast; empty pages without a command; HEAD answered 501. | fixed: one commit each, DASH-7 to DASH-13 |

### New user (NU-R3)

| ID | Area | Severity | Evidence | Status, fix |
|---|---|---|---|---|
| NU-R3-1 | Events / findings | high | A non-GitHub remote and 1 unpushed commit gave `events` = 0 rows for the project, and no finding mentioned it. | fixed: "fix(findings): unpushed work in a folder on an uninventoried host is reported (NU-R3-1)" plus the DASH-4 commit |
| NU-R3-2 | Remotes | medium | The recorded reason was "and the repository exists." rather than `ssh: Could not resolve hostname …`. | fixed: "fix(remotes): report git's cause, not its closing advice (NU-R3-2)" |
| NU-R3-3, NU-R3-4 | Findings copy | medium/low | "170-odd repositories" on a 2-project workspace; no `full remotes` remedy; "eighteen tools (credentials audit G14)"; "AGENTS.md rule 7". | fixed: one commit each |
| NU-R3-5, NU-R3-6 | Assistant | medium | No key, yet `model_status: ready`; an inherited `OPENROUTER_API_KEY` was silently taken as the assistant's key. | fixed: "fix(agent): no key is not ready, and the key's source is shown (NU-R3-5, NU-R3-6)" (`no-key`, `key_source`) |
| NU-R3-8 | Packaging | medium | README → Install drifted from the lock on 4 packages; `engine_update` looked for a lock the wheel did not carry. | fixed: "fix(packaging): the wheel carries the tested lock, and update and install use it"; re-verified: README lines installed `mcp 2.2.0`, `PyJWT 2.14.0`, `uvicorn 0.53.0`, `sse-starlette 3.4.11` |
| NU-R3-9 | Runtime | low | A missing `[full]` extra was blamed on the interpreter. | fixed: two "fix(runtime)" commits |
| NU-R3-10, NU-R3-11, NU-R3-14, NU-R3-15 | CLI | low | `usage: observatory.py`; the `--home` default; a bare `observatory` with no workspace exited 0; doctor listed no settings. | fixed: "fix(cli): doctor lists every setting; help names installed paths; bare exit 2" |
| NU-R3-12, NU-R3-18 | Server / overview | low | The skill version was served with its YAML quotes; the busiest-project tile showed the id. | fixed: one commit each |
| NU-R3-13 | Agent install | low | `install` claimed "installed" without checking; `uninstall` left empty objects. | fixed: "fix(agent): install proves the plugin is installed; …" |
| NU-R3-16 | Backups | low | The encrypted copies sat beside their passphrase, with no warning. | fixed: "fix(backups): warn when the encrypted backups root is inside the workspace" |
| NU-R3-17 | `full local` output | low | Python dicts, a dangling dash, and pre-settle counts. | fixed: "fix(local): the first `full local` reads as a person reads it" |

### Found while integrating (R3)

| ID | Area | Severity | Evidence | Status, fix |
|---|---|---|---|---|
| R3-TEST-1 | Mac tests | medium | Each `swift test` left about 12 empty `~/Library/Preferences/observatory-tests-<uuid>.plist`, because cfprefsd rewrites a removed suite. 480 had built up on the test machine. | fixed: "test(macos): the app's tests keep their defaults in memory, off the user's disk" (480 → 480 across a full run; the 480 empty files were removed) |
| R3-PRIV-1 | Comments | low | Eleven engine comments cited "AGENTS.md rule 2/3/7", "credentials audit G14" or DEC-02xx: records of the private predecessor. FABRIC-CONFORMANCE said "eighteen tools"; `tools/list` returns 22. | fixed: "docs(comments): name each rule in words, not by the predecessor's record ids" (guard in `tests/test_release_boundaries.py`) |
| R3-CLI-1 to R3-CLI-3 | CLI | low | `agent uninstall` without `claude` printed the install advice; the rollback hand-over had no lock; a bare `open --stop` looked on 47311. | fixed: "fix(cli): uninstall without claude, a rollback hand-over and a bare --stop each do what they say" |

### Noticed and left open

| ID | Severity | What | Owner |
|---|---|---|---|
| O-1 | low | `survey` raises `KeyError: 'ownership'` on a registry row without that field. The emitted registry always carries it, so only a hand-made row hits this. | next contributor |
| O-2 | low | The assistant MCP tools declare no `outputSchema`. Adding one is a contract addition. | next contributor |
| O-3 | low | `use_secret names` shows only the folder named, when a project also has an older registry-id folder. The Keys page and MCP list both. | next contributor |
| O-4 | low | `fabric_service.py` `build_info` calls bare git. The file is vendored with a digest; the fix is upstream in fabric-agent-adapter. The engine never calls it. | upstream |
| O-5 | low | The README dashboard screenshots predate DASH-6/7. They are reviewed images, pinned by hash. | release |
| O-6 | low | CI installs from the checkout's lock, so README's install-from-wheel line is not run in CI. It was run by hand here. | next contributor |
| SIGN-1 | low | The app is signed ad hoc. | operator (Apple credentials) |

Git hardening has known limits, and SECURITY.md lists them:

- `~/.ssh/config` `ProxyCommand` still runs.
- A submodule's own filter drivers still run.
- An LFS file shows up as its pointer.
- `includeIf` global values are not copied.
- Git older than 2.32 ignores `GIT_CONFIG_GLOBAL`.

## Run 1's and run 2's fixes, re-verified

| Claim | Re-verified by | Result |
|---|---|---|
| R2-NU-1 (the first board carries the smoke verdict) | one `full local` on a fresh workspace | **held**: no `dashboard.unverified` |
| KEY-13 (typed refusal for a malformed argument) | raw stdio, `limit:"x"` and a fake key as `projectId`, against the README-installed wheel | **held** for type errors. Meaning-level refusals still echoed the value: KEY-R3-6, fixed. |
| KEY-12 (no key characters in status) | `full key` | **held**: prefix and length only |
| R2-KC-1, KC-1 (no signer, no credential helper) | hostile `~/.gitconfig` and planted programs | **held** for those two paths. Every other git call did not hold: KC-R3-1 to KC-R3-3, fixed. |
| UX-5 (a remote on another host is a remote) | non-GitHub remote | **held** for that bucket, but the folder then fell out of the events scan: NU-R3-1, fixed |
| R2-APP-6 (one dashboard window) | the QA app's Window menu over AX | **held**: Dashboard ⌘1 and Assistant ⇧⌘A once each |
| MODEL-1 (`configure model chain` and `budget`) | CLI | **held**. "Ready" with no key: NU-R3-5, fixed. |
| R2-A11Y-1 ("Choose: …" labels) | `AccessibilityTests`, watched failing with the label removed | **held**, and now guarded |
| Run 2's docs | DOC sub-audit (187 relative links resolve; claims checked) | mostly **held**. DOC-2 to DOC-10 were found, all fixed. |

## Native walkthrough (QA bundle `ai.passioncode.observatory.qa3`)

| Scenario | Result | What was shown |
|---|---|---|
| SCN-005 (dashboard half) | FAIL → PASS | The reopen case failed. `DashboardWindowTests` over a real WKWebView covers both cases: window open during the switch, and window reopened after it. |
| SCN-007 | PASS | Opened with `open`; the window `Overview — Project Observatory` was on screen (CGWindowList). The default port, held by another workspace's server, was named. |
| SCN-008 | PASS | Dashboard → Start Server started the server; the next load answered HTTP 200 (WebKit log). |
| Accessibility | PASS | `AccessibilityTests` covers three windows, both languages, and three dashboard modes. Every control is named, and the only duplicate is the two "Settings" links, which open one window. |
| Theme | PASS | Offscreen renders of Settings, the assistant and the dashboard placeholders, in EN and RU, all in the PassionCode tones. |

## Checks actually run

**Engine:**

- `full check --jobs 6` on `21e4a0d` before any change: 198/198 PASS.
- On the integrated branch head: **202/202 PASS**, 6,103 printed PASS assertions, 683 unittest cases.
- Each fix branch ran its own full gate (199 or 200 suites, all PASS).
- Each fix's suite was watched failing first. The exceptions, all named in their commits:
  - `test_credential_shape`, written with its module;
  - two copy-equality tests;
  - the in-memory-defaults test, watched failing in its first file-removal form;
  - one regression `test_vault_project` found on the fresh venv.

**Repository:**

- Root tests (`unittest discover -s tests`): 107, OK.
- `compileall`, Python 3.11 and 3.14: clean.
- `update_inventory.py --check`: no differences.
- `sync_engine_docs.py --check`: nothing stale.
- Wheel and `check_package.py`: 0 failures, 492 runtime files.
- README → Install, run literally from that wheel into a fresh Python 3.14 venv under a temporary
  HOME. `pip freeze` matched the lock. `init`, `configure`, `local` and `doctor` all passed, and the
  MCP claims held over stdio.
- `claude plugin validate --strict` on all three manifests: passed.
- `docs/site/check.py --self-test`, `build_article.py --check` and `check_site_interactions.cjs`:
  passed.
- `check_public_release.py --history --history-ref HEAD` at the report commit: 0 findings, 2,865 history blobs.

**Swift:**

- `swift test`: 57 tests, 0 failures (50 before this run).
- Clean debug and release builds: 0 warnings.
- `build-app.sh` release: Info.plist OK, version 0.12.0, ad hoc signature. The bundle was built,
  not launched, and deleted.

**Dashboard:**

- 11 pages × EN/RU at 1440, 900 and 390 px: 0 console errors, no broken text, no overflow.
- 22 and 27 anchors resolve.
- A real-Tab walk of 36 stops on findings: no trap, and a focus ring on every stop.

**Report:** `reports.py check` on this file (the checker the earlier runs used).

## NOT_RUN

- **A real VoiceOver session, and an on-screen look at the windows.** The session was locked for
  the whole run, so AX exposed no windows and nothing could come to the front. The tree VoiceOver
  is given was read in-process instead (`AccessibilityTests`).
- **Real provider calls:** OpenRouter mint, limit, revoke and rotate; Cloudflare issue; the
  assistant against a model. The README's paid `claude -p` proving call.
- **Agents that ask before signing** (1Password, Secretive, FIDO): not tested, to keep away from
  the real agent.
- **`install_launchd`, `serverd --install` and `agent install`** against a real `~/.claude`.
- **`install-app.sh` into `/Applications`.**
- **`full update --apply`** against a published release.

## Items for the release

1. **REL-1, REL-2, REL-3:** bump `observatory-log`. The hook text (runs 1 and 3) and the
   `handling-secrets` SKILL.md (runs 1, 2 and 3) changed. Bump its three manifests and every
   `SKILL.md` (`tests/test_plugin_manifests.py`).
2. **Version and CHANGELOG:** bump the engine version everywhere `tests/test_version_consistency.py`
   checks, and turn `## Unreleased` into the release section. The behaviour changes to call out are:
   - a bare `observatory` with no workspace exits 2;
   - the registry guard has a floor;
   - `model_status: no-key`;
   - README's two-step install;
   - `PROJECT` normalisation;
   - collectors no longer run git filters, so LFS files are read as pointers.
3. **Tag, publish, verify:** tag the merge commit and publish the wheel with `SHA256SUMS`.
   Re-download and compare the digest. Then run `full update --apply` on each machine.
4. **Website:** `site/tokens.css` and the three pages' asset versions changed (PassionCode 1.1.0).
   Redeploy the retired site, or record that it stays on 1.0.0 until its next deploy.
5. **README screenshots (O-5):** regenerate them from the demo estate after the release, through
   the reviewed-image flow.
6. **Report index:** `reports.py index --commit --push` in the projects wiki. It was not run here,
   because it writes another repository.
7. **Operator:** SIGN-1 (Developer ID and notarization).
