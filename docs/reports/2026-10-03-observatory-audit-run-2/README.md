---
report:
  id: project-observatory/2026-10-03-observatory-audit-run-2
  title: "Observatory audit, run 2 of 3: what run 1 missed, re-verified independently"
  kind: audit
  project: project-observatory
  domains: [audit, security, correctness, mcp, concurrency]
  as_of: 2026-10-03
  status: active
  valid_until: 2026-11-02
  summary: >-
    Second of three audit-and-fix passes over the public engine after run 1 (main 1bdadff).
    It found 29 defects that run 1 missed or that its fixes did not close, and fixed 27 of them
    test-first. The largest were in the Mac app: each Dashboard command opened another blank
    window, and a /tmp workspace could never be saved. A new user's first board said its own
    page was unverified, and malformed MCP arguments echoed the caller's input (KEY-13). Two
    items stay open: one needs a native check on a dedicated display, the other a plugin
    version bump at release.
  sources:
    - name: "Run 1 report"
      path: "docs/reports/2026-10-03-observatory-audit-run-1/README.md"
      read_at: 2026-10-03
    - name: "project-observatory-dashboard main at 1bdadff (run 1 merged, PR #119)"
      url: "https://github.com/passioncode-ai/project-observatory-dashboard/tree/1bdadff"
      read_at: 2026-10-03
    - name: "Released v0.12.0 wheel, used for the upgrade walk"
      url: "https://github.com/passioncode-ai/project-observatory-dashboard/releases/tag/v0.12.0"
      read_at: 2026-10-03
  produced_by:
    agent: claude-code
    task: "Observatory audit run 2 of 3"
  supersedes: []
  consumers: [docs/macos/SCENARIOS.md]
---

# Observatory audit, run 2 of 3

## Summary

Run 1 merged as PR #119 with 73 fixes. This run read its report, then repeated its walks along
different paths. It took the Mac app through Launch Services, as a user opens it; run 1 had
started the binary from a shell. It drove a workspace switch while a question was still running,
and a new user's first board after a single `local`. It also covered the MCP wire with
malformed arguments, an upgrade from a released 0.12.0 workspace, the Russian copy, VoiceOver
labels, unattended Git commits, and every document run 1 had edited.

This run found 29 defects and fixed 27 of them, each with a test. Seven reached any user:

- every **Dashboard command** (⌘1, Window → Dashboard, Dashboard → Overview, the assistant's
  Dashboard button) opened **another window**, and all but one were blank;
- a **workspace under `/tmp`** could never be saved in Settings, because run 1's path fix
  resolved `/private/tmp` back to `/tmp`;
- the **first board** a new user opens still said "the dashboard has not been verified to render
  at all";
- a **malformed MCP argument** came back as the SDK's raw text, echoing the value sent (KEY-13);
- **six handed-over commands** named source-tree paths, among them the `sign_credential.py`
  remedy that shows on every new vault slot;
- an **unattended registry commit** ran the user's Git signing program;
- the Russian dashboard had **meaning errors**, for example "dormant" rendered as «мёртвым»
  ("dead").

Two stay open. One is unconfirmed (R2-APP-5). The other is a plugin version bump that belongs
to the release (REL-2).

Commits are cited by subject. The pull request merges by rebase, which rewrites every commit
id, and that is how run 1's report came to cite ids that do not exist on `main` (R2-DOC-1).

## Scope and method

- **Source:** `origin/main` at `1bdadff`, worktree branch `audit/run-2-2026-10-03`, editable
  install from `requirements-full.lock` on Python 3.14.
- **Isolation:** temporary HOMEs under `/private/tmp` with synthetic projects (`alpha-web`,
  `beta-api`). Two workspace copies were briefly made outside `/tmp` to rule out the `/tmp`
  symlink, then deleted. The operator's workspace, launchd jobs, `/Applications`, `~/.claude*`
  and Keychain were not touched.
- **No spend:** the assistant ran against a local stub of an OpenAI-compatible provider. It
  answered `/models`, `/key` (no limit) and `/chat/completions`, and a flag file made every
  answer wait 40 s. One request did leave the machine: run 1's `install_key.py`, which checks a
  key with the provider, sent a fake key to OpenRouter and got a 401. That check is free.
- **Native app:** debug builds under `/private/tmp`, launched with `open`. The QA bundle got its
  own bundle id and executable name, so neither its defaults nor its Accessibility identity
  could be confused with the installed app. It was driven through the Accessibility API, with
  screenshots taken by window id only. The bundle was unregistered from Launch Services
  afterwards, and its defaults and WebKit data were deleted.
- **Dashboard:** a sandbox workspace served on a spare port and walked with chrome-devtools:
  11 pages, EN and RU, at 900 px.
- **Docs:** a separate read-only pass checked every user-facing claim against the code or a
  sandbox run. A separate review read all 1,228 Russian strings.

## Findings

Severity:

- **high:** a user cannot do the thing, or is told something false.
- **medium:** a wrong path with a workaround, or a value leaving where it should not.
- **low:** copy, consistency, or a guard.

| ID | Area | Severity | Evidence | Fix (commit subject) or owner |
|---|---|---|---|---|
| R2-APP-6 | App | high | A `WindowGroup` dashboard: after ⌘1, Window → Dashboard and Dashboard → Overview, the window list held 3, then 5, "Overview" windows. All but one were blank, because they share one web view. Run 1 checked window counts at launch only. | "fix(macos): one dashboard window; /tmp workspaces save; segment labels". It is a single `Window` scene: Close All → 0, reopen → 1, again → 1. |
| R2-APP-4 | App / onboarding | medium | Settings saved `/private/tmp/…/ws` as `/tmp/…/ws` because `URL.resolvingSymlinksInPath` strips `/private`. The engine then refused it: "Configuration paths must not contain symbolic links". Run 1's "typed path is resolved" fix never worked, and the folder picker had the same flaw. | same commit (`realpath(3)`; a folder that does not exist yet resolves through its parent) |
| R2-NU-1 | Onboarding | medium | After one `full local` on a fresh workspace, the board carried `dashboard.unverified`, because `findings` runs before `smoke`. Run 1's NU-5 test ran `local` twice. | "fix(local): the first board a new user opens carries the smoke verdict" (a `settle` step) |
| KEY-13 | MCP | medium | `observatory_status {"limit": "<value>"}` answered "Error executing tool … input_value='<value>' … errors.pydantic.dev". The caller's value, a pasted key included, went back into the transcript. | "fix(mcp): a malformed argument or unknown tool is a typed refusal, never the SDK's raw text (KEY-13)": `invalid-input` with field and rule, or `unknown-tool` |
| R2-KEY-1 | Keys / messages | medium | Handed-over commands that named source-tree paths. On every new vault slot, `credential.unsigned` said `` `tools/sign_credential.py set …` ``. The others were `tools/scan_leaks.py --full`, `tools/record_turn.py`, `./tools/cloudflare.py issue` (twice) and `tools/revoke_key.py --list`. Run 1's guard covered four tools only. | "fix(messages): every handed-over tool command carries the installed path". The guard now covers any `tools/<x>.py <args>`. |
| R2-KC-1 | Keychain | medium | `commit_registry` and `commit_projection` commit unattended. With `commit.gpgsign=true`, a planted signer ran. | "fix(keychain): a scheduled commit never runs the user's signing program" (`commit.gpgsign=false`) |
| R2-APP-1 | App | medium | Changing workspace while Build (up to 300 s) or Start server ran left `dashboardWorking` true, because the stale action's reset was dropped. Build, Start and Rebuild stayed disabled. | "fix(macos): a workspace change mid-build leaves Build and Start usable; …" |
| R2-RU-1 | i18n | medium | Meaning errors in `ru.json`: «объявлен живым, измерен мёртвым» for declared active / measured dormant, «пусто» for an idle app, a feminine «привязана/ничья» for a property, «отказаны намеренно», «истекает ≤90 дней», «владелец» for an organisation. Anglicisms (property, workspace, эстейт, claim'ит, кред, стэши, GB) and counts that never inflect («1 коммитов»). The app said «дэшборд», «она» with no noun, and «токены … оплачены». | "fix(i18n): Russian copy says what the English says, in one vocabulary". The app strings are in the mid-build commit. A vocabulary guard is in `test_i18n`. |
| R2-DOC-1 | Docs | medium | Run 1's report cited 36 commit ids from its branch. None is on `main`, because the merge rewrote them. | "docs: every claim run 2 checked is true now; run 1's fix ids resolve on main" (each matched by subject) |
| R2-DOC-2 | Docs | medium | The ONBOARDING Mac-app section says the app "uses the existing MCP server". It runs `full assistant` and never MCP. | same docs commit (and the engine copy) |
| R2-DOC-3 | Docs / app | medium | `full update` without `--apply` only previews. AGENTS' release step, the macOS README and the app's too-old-engine message all handed it over as the fix. | same docs commit; the app message is tested |
| R2-DOC-4 | Docs / plugin | medium | The handling-secrets skill ran `python3 …/observatory.py doctor`. Stock macOS `python3` is 3.9, which the engine refuses. | same docs commit (`project-observatory full doctor`, `"$OBSERVATORY_PYTHON"`); the version bump is REL-2 |
| R2-DOC-5 | Docs | medium | SCN-005 and SCN-006 claimed native walks on 2026-10-02 that had not run. Run 1's own report lists SCN-005 as NOT_RUN. | same docs commit (now states what ran) |
| R2-DOC-6 | Docs | low | CLI-COMPATIBILITY lists the `local` steps without `env` or `smoke`, and does not mention `open`, `agent`, `onboard`, `version` or the `observatory-assistant/1` contract. | local-step and docs commits |
| R2-DOC-7–13 | Docs | low | SPEC's assistant toolbar; FLOWS put the disclosure in `App.swift`; SCN-001 ended at an "empty conversation"; PLAN said "unmerged candidate"; the macOS README said `install-app.sh` signs; AGENTS gave a wrong `run_portable.py` path; README Install lacked `brew install python@3.14`. | docs commit |
| R2-AST-1 | Assistant | low | On a workspace where `full machine` never ran, every unscoped answer said "The machine snapshot could not be read". | "fix(assistant): an unmeasured machine is "not measured yet", with its command" |
| R2-NU-2 | Onboarding | low | `full local` with no source said "the projects source <ws>/unconfigured/projects does not exist", a path nobody chose. | "fix(scan): an unset projects source says none is configured, not a placeholder path" |
| R2-APP-2 | App | low | The "no model provider" step named `tools/install_key.py`. | mid-build commit (installed path, key on stdin) |
| R2-A11Y-1 | App a11y | low | The two Settings "Choose…" buttons read the same to VoiceOver. | mid-build commit ("Choose: Program", "Choose: Workspace") |
| R2-A11Y-2 | App a11y | low | Both language segments were named "Language". Measured from the AX tree: the group's label renamed its children. | one-window commit |
| R2-COPY-1 | App copy | low | "has reached its 32 turns"; an unquoted "Start server" in a sentence. | mid-build commit |
| R2-APP-5 | App | unconfirmed | After Settings switched workspace A → B (files mode), the dashboard window still showed A's pages under B's subtitle. The QA windows were occluded on another Space, where WebKit does not paint (`visibilityState: hidden`) and AX lists no windows, so stale paint cannot be ruled out. A trial fix (a WebKit store per workspace) was withdrawn as unproven. | run 3: walk on a dedicated display |
| REL-2 | Release | low | The handling-secrets `SKILL.md` text changed, like run 1's REL-1. | release: bump `observatory-log` |

**Not defects, recorded so nobody re-files them:**

- A vault slot put under a registry slug (`local-alpha-web`) reads "belongs to no project", while
  the folder name (`alpha-web`) is claimed. The docs never say which name `vault.py put` takes;
  that is worth one sentence in run 3.
- `install_key.py` validates against OpenRouter whatever `models.json`'s `base_url` says. That
  is by design (`provider: openrouter`), not a defect.
- `full assistant build` works with the minimal PATH that Launch Services gives the app (no
  `node`). Checked with `env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin`.

**Incident.** The first QA launch used the installed app's bundle id. A ⇧⌘A sent through System
Events went to the operator's running app and opened its Assistant window. Nothing else
changed: the app's defaults differed from the pre-run export by one window frame, and the
export was imported back. Every later launch used a separate bundle id and executable name.

## Run 1's fixes, re-verified independently

| Run-1 item | Re-verified by | Result |
|---|---|---|
| NU-1 (no traceback without a source) | `full local` on a fresh workspace and an installed wheel | **held**; the message improved (R2-NU-2) |
| NU-5 (a fresh board is verified) | one `local`, as a new user runs it | **did not hold** on the first run (R2-NU-1); fixed |
| NU-6 (no maintainer findings) | branch wheel in a fresh HOME | **held**: no `gate.skips_uncovered`. A source checkout keeps it by design. |
| MODEL-1, PROV-1 (model chain, budget, no-limit key) | `configure model chain` and `budget`; stub `/key` with `limit: null`; ask → completed | **held** |
| UX-1, KEY-5, KEY-6 (vault slot on the Keys page; remove; redaction) | `vault.py put/rotate/leak/leaks/remove --retired/movements`, `use_secret run`, Keys page | **held**: slots shown, the value redacted as «EXAMPLE_TOKEN» |
| KEY-7 (installed tool paths) | engine-wide search | **partial**: 6 more paths (R2-KEY-1); fixed |
| KEY-13 | raw MCP stdio with malformed arguments | **was open**; fixed |
| KC-1, KC-2, KC-3 (Keychain) | code read and suites | **held**. A further path was found (R2-KC-1) and fixed. |
| APP-3 (engine search order) | `Model.defaultExecutable` against README | **held** |
| APP-6 (PassionCode tones) | screenshots of all three windows, EN and RU | **held** |
| APP-7 (`-russian YES`) | launch argument | **held** in the final build |
| APP-5, SCN-009 (one Window-menu entry, reopen) | Window menu and window counts after commands | **did not hold** past launch (R2-APP-6); fixed |
| Typed `/tmp` path resolved (run-1 docs) | Settings with `/private/tmp/…` | **did not hold** (R2-APP-4); fixed |
| SCN-004, SCN-002 | stub provider, the slow flag, Stop and a completed answer in the app | **held** |
| Run-1 doc edits | the claim-by-claim pass | mostly **held**; R2-DOC-2 to R2-DOC-13 were in files run 1 edited |
| Upgrade (`full update` semantics) | released 0.12.0 wheel → workspace with a vault slot and the agent on → branch wheel: `workspace-backup`, `upgrade --apply --writers-stopped`, `doctor`, `local`, `assistant status`; `full update` preview | **held**: `rolled_back` not needed, `settle` closed the board on the first branch run, `update_available: false` named |

## Native walkthrough

| Scenario | Result | What was shown |
|---|---|---|
| SCN-002 | PASS against the stub | Answer, model and cost, the limitation line, steps and sources (2) |
| SCN-005 | PASS for the assistant; dashboard half unconfirmed | A slow question running in A, then Settings → B. B's state appeared ("The agent is turned off"), with no A conversation or turn. A's job completed in A's own store, and B's store has no assistant directory. Dashboard: see R2-APP-5. |
| SCN-007 | PASS | Opened through Launch Services on saved pages; the port held by another workspace's server is named and Start is disabled |
| SCN-009 | FAIL, then PASS after the fix | 3 to 5 windows before; after, Close All → 0, reopen → 1, again → 1 |
| EN/RU | PASS | All three windows in RU after the fix: «Ассистент», «Сохранённые страницы · 3 окт. 2026 г., 3:13», «Запустить сервер» |
| VoiceOver labels | 2 defects fixed | Measured from AX attributes (`AXAttributedDescription`), not by running VoiceOver |

## Checks actually run

**Engine:**

- `full check --jobs 6` on `1bdadff` before any change: 198/198 PASS.
- At the end, on the branch head: see the final line of this section.
- Each fix's own suite was watched failing first, except the RU copy fix: the strings changed
  first, and the new vocabulary guard was then watched failing against the old catalog.

**Repository:**

- `unittest discover -s tests`: 94 tests OK.
- `compileall` on 3.11 and 3.14: clean.
- `update_inventory.py --check`: passed.
- Wheel and `check_package.py`: 0 failures, 483 runtime files.
- `claude plugin validate --strict` ×3: passed.
- `docs/site/check.py --self-test`, `build_article.py --check`, `check_site_interactions.cjs`: passed.
- `check_public_release.py --history --history-ref HEAD`: 0 findings, 2,538 history blobs at the report commit.

**Swift:**

- `swift test`: 50 tests, 0 failures.
- `swift build` debug and release: 0 warnings.

**Dashboard:**

- 11 pages × EN/RU at 900 px: 0 script errors, no `undefined`, `NaN` or `[object Object]`, no
  horizontal overflow.
- Every in-page anchor to a finding, key or project resolved.
- Russian pages keep finding details and actions in English, as the README states.

**Final engine gate** on the branch head before this report: `full check --jobs 6`, 198/198 PASS,
5,935 printed PASS assertions, 611 unittest cases, 0 failures. One earlier run of the gate failed
two suites, `signature` and `organizations`. Both asserted the old texts (a source-tree command
and «владелец»); their expectations now follow the fixes. The report commit changes only
`docs/reports/`; `docs_current` and `handoff` were re-run on it.

## What remains

- **Run 3:**
  - **R2-APP-5:** after a workspace switch in files mode, does the dashboard window paint B's
    pages? Walk it on a display where the app is frontmost and on the current Space, and read the
    web view's `document.URL` after the switch.
  - Repeat SCN-008 (Start server → Live) through Launch Services.
  - A keyboard-only walk of the dashboard pages.
  - A real VoiceOver pass.
  - One sentence in ONBOARDING on which project name `vault.py put` takes.
- **Release:** REL-1 and REL-2, the `observatory-log` plugin version bump.
- **Operator:** CS-1, the canonical border token's contrast; SIGN-1, Developer ID signing and
  notarization.
- **NOT_RUN:**
  - real provider calls (beyond `install_key`'s free 401 check);
  - the README's paid `claude -p` proving call;
  - `install_launchd`, `serverd --install` and `agent install` against a real `~/.claude`;
  - `install-app.sh` into `/Applications`;
  - typing into the app's text fields: synthetic key events were blocked on this machine, so
    fields were set through the Accessibility API.
