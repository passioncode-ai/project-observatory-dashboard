# 2026-10-02 — 0.12.0: the app opens on the dashboard

The operator's report on the installed 0.11.0 app: no proper icon, the app does not start or
open properly, and opening Observatory should show the **dashboard**, not the assistant. Asked
for the whole app finished to production quality, every error found fixed, and a new release.
Synthetic names only in this public record.

## What was measured before any change

| Symptom | Cause (measured) |
|---|---|
| No icon | the bundle had no `CFBundleIconFile` and an empty `Resources/` |
| "Does not open" | after its window was closed, a Dock click or a second launch left **zero** windows — AppKit's reopen does not recreate a SwiftUI `WindowGroup` window; and Launch Services held three registrations of the bundle id, two of them QA builds in temporary folders that Spotlight could open |
| Opens on the chat | the main window was the assistant, by the first version's design |
| (found on the way) Start server, Stop did nothing | `posix_spawn` passes the calling thread's signal mask and a Swift worker thread blocks the asynchronous signals: every process the app started inherited a blocked SIGTERM — a server it started ignored `full open --stop` (still answering 20 s later; `sample` showed the main thread in `poll` and the stop event never set), an assistant runner kept waiting on the model after Stop |
| (found on the way) the bridge refused `serve`/`build` | its action allowlist did not name them; now a test compares it with the engine's own `choices` |
| (found on the way) the page's language never reached the app | a `localStorage.setItem` hook posted nothing; replaced by reading `document.documentElement.lang` after each load |
| (found on the way) "Измерено не измерено" | the header put "not measured" inside "Measured …" for an estate with no scan |
| Finding titles in English on Russian pages | carried from 0.11.0 (D1): titles were English f-strings in the rule modules |

## Decisions

- **The dashboard is the main window** (operator, 2026-10-02; recorded in `docs/macos/SPEC.md`,
  superseding the 2026-10-01 "dashboard stays in the browser"). Live when the workspace's own
  server is verified; otherwise the built pages from disk — they are self-contained — under a
  banner with Start server. A server starts only on that click.
- **The icon is the product mark** (`dashboard/brand/observatory-mark.svg`, pinned in its
  manifest), rasterized on the macOS grid — not a newly generated image, so the Dock, the
  dashboard's rail and its favicon show one glyph.
- **One release PR** (feature + version), as AGENTS.md allows for the release itself. Opening it
  is the CLA agreement; no box to tick (organisation decision of 2026-10-02).

## Checks actually run

- Engine: `project-observatory full check` — 198/198 suites, 5,852 assertions, 592 unittest
  cases, 0 failures (before the "measured" fix; the suites that fix touches —
  dashboard_render, i18n, dashboard_shell, pages — re-run PASS after it).
- Root: 85 tests OK. Wheel + `check_package.py`: 0 failures, 482 runtime files.
  `claude plugin validate --strict` ×3: passed. Site checks: passed. `compileall` on 3.11.
- Swift: 33 tests, 0 failures, including tests watched failing first: SIGTERM to a grandchild
  (5.3 s and exit 0 before the fix), the bridge/engine action contract (failed with `build`
  removed), navigation policy, dashboard modes, a pre-0.12 engine named incompatible.
- Privacy: `check_public_release.py --history --history-ref HEAD` — 0 findings (2,274 blobs). A
  maintainer's private identifier list over the tree: 0. Over the history it reports
  git-metadata matches inherited from earlier commits (the tip of `main` before this branch
  carries one); the four commits of this branch carry none.
- Native walkthrough (debug build): the dashboard live against a real workspace; saved pages
  with the banner, Start server → live, on a secret-free workspace copy served on a spare
  port; back/forward/overview; Russian chosen on the page → app in Russian, and the reverse
  from Settings; the assistant window from the toolbar; launch → 1 window, close → 0,
  reopen → 1; dark window chrome; Russian finding titles and dates. Screenshots carry real
  project names and stay in the operator's private repository.

## Open work and the exact next task

1. Required checks → merge → tag `v0.12.0` → GitHub release with wheel and `SHA256SUMS`
   → `full update` on the machine → `macos/scripts/build-app.sh` from the tag →
   `macos/scripts/install-app.sh --open` → check the installed app opens on the dashboard.
2. Carried: finding details and actions are still English (titles are done); the Mac app has
   no Developer ID signature or notarization (needs the maintainer's credentials); the system
   menus (File, Edit, View) follow the system language, as in every macOS app.
