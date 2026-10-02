---
report:
  id: project-observatory/2026-10-03-observatory-audit-run-1
  title: "Observatory 0.12.0 audit, run 1 of 3: build, onboarding, keys, keychain, app design, scenarios, docs"
  kind: audit
  project: project-observatory
  domains: [audit, security, correctness, mcp]
  as_of: 2026-10-03
  status: active
  valid_until: 2026-11-02
  summary: >-
    First of three audit-and-fix passes over the public engine at v0.12.0. 77 defects
    were found across the build, a new user's first hour, key management, the macOS
    Keychain, the Mac app's look, the scenarios and the docs. 73 are fixed test-first on
    branch audit/run-1-2026-10-03, one of them only in part. Four stay open: one needs a
    design-system decision, one a published-contract change, one Apple credentials, and
    one is a version bump that belongs to the release. Most of the user-facing defects were found by running
    the product, not by its 198-suite gate.
  sources:
    - name: "project-observatory-dashboard at v0.12.0 (origin/main 3dbacdc)"
      url: "https://github.com/passioncode-ai/project-observatory-dashboard/tree/3dbacdc"
      read_at: 2026-10-03
    - name: "Walkthroughs in temporary HOMEs with synthetic workspaces (alpha-web, beta-api) and a local stub model provider"
      path: "docs/reports/2026-10-03-observatory-audit-run-1/README.md"
      read_at: 2026-10-03
  produced_by:
    agent: claude-code
    task: "Observatory audit run 1 of 3"
  supersedes: []
  consumers: [docs/macos/SCENARIOS.md]
---

# Observatory 0.12.0 — audit run 1 of 3

## Summary

This run audited eight areas:

- the build;
- a new user's onboarding in the terminal and in the Mac app;
- adding and managing keys;
- the macOS Keychain;
- the Mac app's design in PassionCode tones;
- every UX scenario;
- whether the documentation is true;
- any other bug found on the way.

The run found 77 defects and fixed 73 of them, one in part, on branch `audit/run-1-2026-10-03`. Each fix has a test, and each commit is listed in the findings table below. The four left open are the operator's or the release's to decide.

Most of the defects a user would hit were found by running the product, not by its gate. The 198-suite offline gate passed on v0.12.0 at the start of this run. Even so, a new user's first hour hit two dead ends:

- `full local` crashed with a traceback when no projects folder was configured;
- an assistant set up exactly as documented was refused with "budget reached".

Four other defects reached any user:

- an unlimited OpenRouter key was refused as "spent";
- a key stored with `vault.py put` never appeared on the Keys page;
- `git ls-remote` could raise a macOS Keychain dialog during an unattended tick;
- the Mac app's assistant and Settings were light grey with system blue, beside a dark gold dashboard.

## Scope and method

- **Source:** `origin/main` at `3dbacdc` (v0.12.0), checked out as a worktree with an editable install from `requirements-full.lock` on Python 3.14.
- **Isolation:** every walk used a temporary HOME and workspace with synthetic projects (`alpha-web`, `beta-api`). None touched the operator's workspace, launchd jobs, `/Applications`, `~/.claude*` or Keychain.
- **No spend:** the model was a local stub of an OpenAI-compatible provider. It served `/models`, `/key` and `/chat/completions`, and a question containing "slow" waited 40 s so that Stop could be tested. No paid call was made.
- **Native app:** debug builds in `/tmp`, launched directly with settings passed as launch arguments. They were driven through the Accessibility API and captured by their own window id. The app's user defaults were exported before and restored after.
- **Dashboard:** walked with chrome-devtools in EN and RU, at 1440×900 and 900×620, against a demo estate and a new-user workspace.

## Findings

Severity:

- **high:** a user cannot do the thing, or is told something false.
- **medium:** a wrong or missing path, with a workaround.
- **low:** copy, consistency, or a guard.

| ID | Area | Severity | Evidence | Fix |
|---|---|---|---|---|
| KC-1 | Keychain | high | `git ls-remote` against a remote that answers 401 asked every configured credential helper (`osxkeychain` on macOS). In the test, a planted helper left its marker. | 7bc2a1e |
| KC-2 | Keychain / app | medium | The dashboard's web view let WebKit handle password and client-certificate challenges by default, which can consult the login keychain. | c5f82e4 (`DashboardOrigin.challenge`) |
| KC-3 | Keychain | low | Nothing stopped a future script from launching Chrome without `--use-mock-keychain`. None does today. | c5f82e4 (root test, with a planted offender) |
| PROV-1 | Keys / model | high | `limit_remaining: null` (a key with no limit) was read as 0. `check_budget` then refused every call: "the limit on this KEY is spent: 0.0000 of None". | 8a3d76c |
| MODEL-1 | Onboarding / assistant | high | A fresh workspace has `chain: []` and all ceilings at 0, and there was no command to set them. After `configure features agent true`, every question was refused as `budget-reached`. | e69247d (`configure model chain`, `configure budget`, `model_status`), 6ac67fe (app) |
| APP-1 | Build | medium | `build-app.sh` used `tomllib` through `python3`. On macOS's stock 3.9: `ModuleNotFoundError`. | c5f82e4 |
| APP-2 | Build | low | Two Swift warnings (a weak capture of an environment object), at Dashboard.swift:186-187. | c5f82e4 |
| APP-3 | Onboarding / app | high | The default engine path `~/.local/bin/project-observatory` is not where README → Install puts it. | c5f82e4 (search order, test) |
| APP-4 | Onboarding / app | medium | A missing engine read "choose the path in Settings", and a folder that was not a workspace gave no `init` command. | c5f82e4, 0ecfe01 ("not installed yet", installation-guide link) |
| APP-6 | Design | high | The assistant and Settings used system light and blue. | 0ecfe01 (Palette/Theme, `PaletteTests`: 31 system-colour sites refused, AA pairs) |
| APP-5 | App | low | The Window menu listed "Assistant" twice. | 0ecfe01 |
| APP-7 | App | low | `-russian YES` as a launch argument was ignored (it arrives as a string). | 0ecfe01 |
| APP-8 | App | low | Settings messages were cut off at the window edge. | 0ecfe01 |
| APP-9 | App | low | The banner button said "Retry" while the message said "Refresh". The toolbar help claimed ⌘R, which belongs to the dashboard. | 0ecfe01 |
| APP-14 | App copy | medium | The saved-pages banner said key buttons "need the server". `--serve` is read-only. | 0ecfe01 |
| APP-11 | App | low | Five engine error codes had no sentence in the app (`request-id-conflict`, four `invalid-*`). | 6ac67fe (contract test reads `assistant.py`) |
| APP-12 | App i18n | low | The Russian text read "Не удалось прочитать machine". | 6ac67fe |
| APP-13 | Tests | low | The process-group timeout test failed under load: the pid file was not yet written at 0.5 s. | 7a3ab93 |
| APP-15 | App a11y | low | Once the conversation list drew its own selection, it no longer took arrow keys. | 4d11c7c (⌥⌘↑/↓) |
| NU-1 | Onboarding | high | `full local` with no projects source ended in a traceback. | ed1dbf4 |
| NU-2 | Onboarding | high | `observatory demo` wrote 0.1-format state into a full workspace. | 7b154eb |
| NU-3 | Onboarding | medium | An unknown integration, feature or source name was accepted silently. | a4148e7 |
| NU-4 | Onboarding | medium | Messages named `./observatory.py` and `node dashboard/smoke.js`. | 78f26e8 |
| NU-5 | Onboarding | medium | `local` never ran `smoke`, so a fresh board always showed "dashboard unverified". | 7500145, 459df32 |
| NU-6 | Onboarding | medium | Maintainer and switched-off findings appeared on a new user's board. | eaebd6a |
| NU-8 | CLI | low | ANSI colour codes were written to logs and pipes. | ed1dbf4 |
| NU-9 | Machine | low | Local-only checkouts were left out of hygiene and cleanup. | 826303d |
| NU-10 | Backups | low | Error voices were mixed. `backups decrypt` refused the name that `backups status` prints. | b08b806 |
| NU-11 | Agent | low | `agent status` said "is None". | 0316207 |
| NU-12 | CLI / docs | low | `full --help` was incomplete. The update preview misstated where the snapshot goes. | ed1dbf4, 5cb068d |
| UX-1 | Keys | high | A vault slot never reached the Keys page without an OpenRouter scan. Reported again as KEY-1. | 1d4840e |
| UX-2 | Docs / app | high | README and ONBOARDING said `--serve` makes key actions live. It is GET-only; live actions need `keyserver.py`. | ce72f73, 0ecfe01 |
| UX-3 | Dashboard | high | The Machine page said "everything clean" when nothing had been measured. | c5d1f87 |
| UX-4 | Dashboard i18n | medium | The not-measured reason was in English on RU pages. | c5d1f87 |
| UX-5 | Findings | high | A remote on a host other than GitHub or Bitbucket was reported as "no remote at all". | 2618cf8 |
| UX-6 | Findings | medium | Maintainer findings and actions with relative paths appeared on a new user's board (overlaps NU-4 and NU-6). | 78f26e8, eaebd6a |
| UX-7 | Dashboard | medium | Six pages said "registry holds no row" instead of "not scanned" plus a command. | 1480613 |
| UX-8 to UX-12 | Dashboard | low–medium | A port formatted as a number; lowercase titles; a session badge below AA contrast; a blank Machine card; an unreadable empty-estate tile and a stray space. | 1480613 |
| INIT | Onboarding | low | A symlinked workspace was refused without naming the path to use. | e93452d |
| KEY-3 | Keys / MCP | high | `observatory_credentials` with a registry id returned nothing. | c44a177 |
| KEY-4 | Keys | medium | `.meta.json` and `.retired-…` files were counted as slots. | c44a177 |
| KEY-5 | Keys | medium | There was no way to remove a slot or a retired archive. | 8a2fcbb (`vault.py remove`, journalled) |
| KEY-6 | Keys | medium | `use_secret run` with a flag after the names, or a missing program, gave a generic error. | 9c3816f |
| KEY-7 | Keys | low | Installed users were told to run `./observatory.py` or a bare `tools/…`. | 015eddc |
| KEY-8 | Keys | low | `local` did not refresh the `.env` inventory. | 77a855c |
| KEY-9 | Keys | low | `vault.py backup` named a path that does not exist. | 8a2fcbb |
| KEY-10 | Keys | low | `install_key` on a terminal suggested `--for claude-mem`. | b2d73d0 |
| KEY-11 | Keys | low | The installer and the reader disagreed on the key prefix. | b2d73d0 |
| KEY-12 | Keys / secrets | low | `full key` echoed four characters of the key. | b2d73d0 |
| KEY-13 | MCP | low | An unknown project was not a typed error, and malformed arguments came back as raw SDK text. | c44a177 types the unknown project in `degraded`. The rest needs the operator: see "What remains". |
| PROF-1 | CLI | low | `profile export /tmp/x.json` was refused as "workspace paths must not contain symlinks". | 663141d |
| DEPS-1 | Build | low | The `deps` step pinned `mcp==2.1.1`; the package and the lock pin 2.2.0. | b63391f |
| DOC-1 to DOC-16 | Docs | medium–low | `--serve` claims; OBSERVATORY_PYTHON; the backups-root fallback; the tick verdicts; how to set the model and budget; the integration table; coverage rows; `init` with a symlink; exit codes; 0.1 refusals; translated finding titles; SDK 2.2.0; eleven pages; stale engine doc copies; the Mac app, Keychain and AGENTS sections. | ce72f73, b63391f (derived copies plus a drift test), d9c40b8, ea4089a |
| PORT-1 | Tests | low | `dashboard_portability` still asserted the old relative command after UX-12. | 88b2915 |
| CS-1 | Design system | low | `--pc-border-strong` on panel is 2.5:1, below WCAG 1.4.11's 3:1, on the dashboard's inputs and the app's alike. | operator (canonical token; fields keep AA labels and a 3:1 gold focus ring) |
| REL-1 | Release | low | The `observatory-log` hook text (78f26e8) and the handling-secrets skill (8a2fcbb) changed, so the plugin version must be bumped. | parent, at release |
| SIGN-1 | Distribution | low | The Mac app is signed ad hoc. There is no Developer ID or notarization. | operator (needs Apple credentials) |

## Checks actually run

**Engine:**

- `project-observatory full check --jobs 6` on v0.12.0 before any change: 198/198 PASS.
- Mid-run: 197/198. `dashboard_portability` failed after UX-12 and was fixed in 88b2915.
- After every engine fix (at `ea4089a`): 198/198 PASS, with 5,922 printed PASS checks. The later commits change only Swift, `docs/macos` and this report; `docs_current` and `handoff` were re-run on them.
- Per-fix suites were each watched failing first, apart from five dashboard fixes and one app copy fix, each of which got its test in the same commit as the fix.

**Repository:**

- `python -m unittest discover -s tests`: 94 tests OK.
- `compileall` on 3.14 and 3.11: clean.
- `update_inventory.py --check`: 0 differences.
- Wheel and `check_package.py`: 0 failures, 482 runtime files.
- `claude plugin validate --strict` on all three manifests: passed.
- `docs/site/check.py --self-test`, `build_article.py --check`, `check_site_interactions.cjs`: passed.
- `check_public_release.py --history --history-ref HEAD`: 0 findings, 2,471 history blobs.

**Swift:**

- `swift test`: 45 tests, 0 failures. The debug and release builds give 0 warnings.
- `build-app.sh` release produces Info.plist OK, an icon, version 0.12.0 and an ad hoc signature.
- `install-app.sh` was read through, not run.

**Native walkthrough**

| Scenario | Result | What was shown |
|---|---|---|
| SCN-001 | PASS | No engine: "Observatory is not installed yet", with the path and a guide. No workspace: the `init` command. Unbuilt: Build. |
| SCN-002 | PASS, against the stub | Answer, cost, limits, steps and sources, without pressing Refresh. |
| SCN-003 | PASS | Agent off, and no model or budget: typed banners with the commands. |
| SCN-004 | PASS | Stop leads to "Stopped". Quit while working, relaunch, and the turn resumes and completes. |
| SCN-005 | Model tests only | No native walk this run. |
| SCN-006 | PASS, over raw MCP stdio | Ask, the job, completed. A replay returns the same job; changed input is refused as `request-id-conflict`. |
| SCN-007 | PASS | EN and RU, at the default size and at 900×620. |
| SCN-008 | PASS | Port busy: Start is disabled, with the reason. Spare port: Start server, then Live. |
| SCN-009 | PASS | 1 window, close to 0, reopen to 1. |

**Dashboard:**

- 11 pages, EN and RU, at 1440 and 900 px.
- 0 console errors; 41 of 41 links resolve.
- The rest of the walk is in UX-1 to UX-12.

## What remains

- **NOT_RUN:**
  - real provider calls: OpenRouter mint, limit, revoke and rotate, and Cloudflare issue;
  - the README's `claude -p` proving call, which is paid;
  - `install_launchd`, `serverd --install` and `agent install` against a real `~/.claude`;
  - `install-app.sh` against `/Applications`;
  - a native SCN-005 walk.
- **Operator:**
  - CS-1, the canonical border token's contrast;
  - KEY-13: a typed error for a malformed MCP argument needs a wrapper around the SDK's schema check;
  - SIGN-1, Developer ID signing and notarization.
- **Release:** REL-1, the `observatory-log` plugin version bump.
- **Next run:** native SCN-005 (change workspace while a question is outstanding). Re-walk the assistant against the stub after the release build, and the onboarding from the published wheel.
