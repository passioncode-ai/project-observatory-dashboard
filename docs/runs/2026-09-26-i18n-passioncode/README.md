<sub>ssheleg skills — task-pipeline · ux-scenarios · copywriting · sheleg-design · evidence-docs</sub>

# 0.4.0 — English/Russian dashboard, PassionCode design system, passioncode-ai home

Objective (operator request, 2026-09-26): review the 0.3.12 release; make the dashboard
bilingual with English as the default and Russian as the operator's chosen workspace
language; bring the dashboard and the website into the PassionCode.ai brand; take a
screenshot that reveals none of the operator's projects; move Project Observatory into
the `passioncode-ai` organization; and present it on passioncode.ai as the second
available tool beside Switchboard.

## Where each part lives

| Owner | Remote / branch | Entry point | Status at writing |
|---|---|---|---|
| Engine, dashboard, site `site/` | `passioncode-ai/project-observatory-dashboard`, PR [#64](https://github.com/passioncode-ai/project-observatory-dashboard/pull/64) from `claude/i18n-passioncode` | this file; [CHANGELOG 0.4.0](../../../CHANGELOG.md) | pushed; merge, tag and release follow CI |
| passioncode.ai | `passioncode-ai/passioncode-ai.github.io`, branch `claude/observatory-product` | `docs/ux/scenarios.md` SCN-006, `observatory/index.html` | pushed; deploy after the 0.4.0 release exists |
| Private predecessor | `passioncode-ai/project-observatory` (private) | its own `docs/` | transferred; content unchanged by this run |
| Fabric contract | `passioncode-ai/project-observatory-contract` | its README | transferred; content unchanged |

All three repositories were transferred from `ssheleg` on 2026-09-26. GitHub redirects
the old addresses (checked: web, `git ls-remote`, and the pinned raw URL of a v0.2.0
Fabric schema under the older `project-observatory-open-source` name all resolve).
Fabric schema identifiers were deliberately not rewritten.

## What changed

- **Localization.** `observatory/engine/dashboard/i18n.py` and `dashboard/locales/{en,ru}.json`.
  English ids in code; Russian catalog with CLDR plural objects; `context@@text` ids where one
  English text needs two Russian forms. The builder renders in `interface.locale`
  (`configuration.INTERFACE_SETTINGS`, `full configure interface locale`), the page script
  re-renders in the reader's EN/RU choice (`observatory.locale` in localStorage). Static
  template text is marked `data-t`; attributes carry `data-t-<name>`. Stats keys became ids.
  The session-start hook line follows the workspace language. Finding texts stay English.
- **Brand.** `dashboard/brand/` vendors `passioncode-tokens.css` (SHA-256 `a2ff52c6…`, source
  commit `508e917` of the design-system repository) and the Observatory glyph; the template's
  role names alias `--pc-*`. One dark theme; the theme switch became the language switch.
  `site/tokens.css` does the same for the public website.
- **Screenshots.** `tools/demo_estate.py` builds the real dashboard over a fictional company
  (Northwind Labs). Files: `site/assets/dashboard-overview-en.png`,
  `docs/images/dashboard-overview-ru.png`, `docs/images/dashboard-projects-en.png`, each pinned
  by SHA-256 in `tools/check_public_release.py` (and the site copy in `docs/site/check.py`).
- **New home.** Package metadata, plugin manifests (0.11.2), agent installer
  (`PREVIOUS_REPOSITORIES` → reinstall moves the marketplace), website, README and onboarding.
- Scenarios OSS-19 (language) and OSS-20 (recognising the product) in
  `observatory/engine/docs/ux/portable-scenarios.md`.

## Checks actually run (macOS 25.6, Node 24)

| Check | Result |
|---|---|
| `project-observatory full check`, Python 3.14.7 and 3.11.16 | 57/59 PASS each. `workspace_scheduler` and `identity_map` fail only because `/usr/bin/git` exits 69 here (Xcode license not accepted; reproduced with a bare `git init`); both pass on CI's Linux runners |
| `python -m unittest discover -s tests` | 60 PASS |
| `tests/test_i18n.py` (new) | 23 PASS on 3.14 and 3.11 |
| `tools/check_public_release.py` with the maintainer's private list (3,848 tokens, built by the private repo's `tools/build_private_denylist.py`, kept outside every repository) | tree 0 findings; history 68 findings, identical to `main` before this change |
| `docs/site/check.py --self-test`, `tools/build_article.py --check`, `tools/check_site_interactions.cjs`, `tools/update_inventory.py --check` | PASS |
| `docs/brand/lint.py` | 0 errors, 700 warnings (429 on `main`): the new English strings have no rows in `docs/brand/strings.md`; warnings are advisory |
| Browser (managed Chrome) | dashboard EN↔RU switch; all 11 pages smoke-clean; 1440×900 and 390×844 without horizontal overflow; website desktop and mobile |
| CI on PR #64 | Linux 3.11/3.14 PASS on `b2128c1` after a real 3.11 f-string incompatibility was fixed; macOS recorded in the release receipt below |

## Open work, in order

1. Merge #64 after all required checks pass; tag `v0.4.0` on the merge commit; publish the release
   with the checked wheel and `SHA256SUMS`.
2. Deploy `site/` to Cloudflare Pages (`docs/site/DEPLOY.md`) and passioncode.ai from
   `claude/observatory-product` after merging it (`npm run deploy`); verify served bytes.
3. Update the operator's installation (`~/.local/share/project-observatory-venv`) to 0.4.0, set
   `interface.locale` to `ru`, rebuild pages, restart the server job, run `full agent install`.
4. Brand registry: register the new interface strings in `docs/brand/strings.md` (advisory warnings).
5. Accept the Xcode license on this machine (`sudo xcodebuild -license accept`) so the two
   git-dependent suites run locally again — a human step.

The exact next task for another agent is item 1 if it is still open; each later item's result is
recorded in the release receipt appended below.

## Release and installation receipt (2026-09-26, appended after merge)

- PR #64 squash-merged as `6beae9e3872ef1b5221ef18072e1cbc25284dad0` after all six required jobs
  passed on `48c1801` (Linux 3.11, Linux 3.14, macOS 3.14; push and pull-request runs). Two CI
  failures on the way were real and fixed in the PR: a Python-3.12-only f-string, and the history
  gate rejecting the first render of each screenshot (both renders are now pinned as reviewed).
- `demo_estate.py` was found reading the invoking user's workspace (`OBSERVATORY_HOME` unset);
  fixed before merge with `tests/test_demo_estate.py`. The fictional ids matched nothing there,
  so no screenshot carried private data; all screenshots were re-rendered from the isolated demo.
- Tag `v0.4.0` → `6beae9e`; [release](https://github.com/passioncode-ai/project-observatory-dashboard/releases/tag/v0.4.0)
  with `project_observatory-0.4.0-py3-none-any.whl` (SHA-256
  `5fab00a2cb266ec9e38050b9a5d9111d005236c573d056a421a270144c7698a3`, built from a fresh anonymous
  clone of the merge commit; `check_package.py` 285 runtime / 291 archive files, 0 failures) and
  `SHA256SUMS`. The wheel fetched anonymously from the release matched `SHA256SUMS`; `releases/latest` answers `v0.4.0`.
- Operator installation: tick and server jobs booted out; `full workspace-backup --writers-stopped`
  (snapshot, 147 files); wheel installed with the lock file; `full upgrade --apply --writers-stopped`
  (its own pre-upgrade snapshot); `full configure interface locale ru`; `full open --rebuild`; jobs
  bootstrapped; `full agent install` moved the marketplace to `passioncode-ai/...`, plugin 0.11.2,
  no status problems. Observed: `/health` version 0.4.0, served pages `lang="ru"`, `full doctor`
  `interface.locale = ru` and tick `ok`, the session-start line in Russian.
- passioncode.ai: PR [#4](https://github.com/passioncode-ai/passioncode-ai.github.io/pull/4) merged as
  `c277b7b`; `npm run check` and `npm run build` PASS on merged `main`.

## Deployment receipt (2026-09-27)

- **observatory.sshlg.me:** `site/` from `main` at `9f63b6d` (fresh anonymous clone,
  `docs/site/check.py --self-test` PASS) deployed with Wrangler 4.135.0 to Cloudflare Pages project
  `project-observatory`, branch `main`; deployment `8199220c` (`https://8199220c.project-observatory.pages.dev`).
  Ten served files on the custom domain equal the source bytes (pages, CSS, JS, mark, `llms.txt`, both
  images, `404.html`); an unknown path returns HTTP 404; the header reads "by PassionCode.ai".
- **passioncode.ai:** already deployed from the site repository's `main` by its own session after
  PR #4 merged. Checked on 2026-09-27: `/`, `/observatory/` and `/switchboard/` served bytes equal a
  build of that `main`, and the Observatory assets match.

Nothing from this run remains open except the advisory items 4–5 above (brand-string registry rows;
accepting the Xcode license on this machine so two git-dependent suites run locally).
