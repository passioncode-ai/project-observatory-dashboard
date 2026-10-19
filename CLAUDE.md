@AGENTS.md
<!-- SSHLG:PROJECT:BEGIN managed by npx sshlg-skills enroll. To opt out, replace the whole block with one SSHLG:PROJECT:OPTOUT comment line. -->
## Family routes in this project

Surface: `public-web` (detected).
Signals:
- robots.txt: `site/robots.txt`
- sitemap: `site/sitemap.xml`
- string catalogue: `desktop/ui/i18n/ru.json`, `macos/Sources/ObservatoryCore/Resources/en.lproj/Localizable.stringsdict`, `macos/Sources/ObservatoryCore/Resources/ru.lproj/Localizable.strings` and 3 more

Routes that apply here (the global routing block says what each one owns):

| Router | Answers | Setup here |
|---|---|---|
| `super-ux` | what the interface must do | scenarios and their linter: partial (`/ux-init`) |
| `sheleg-design` | how it looks and moves | nothing to seed |
| `copywriting` | how it sounds | brand pack: present |
| `sheleg-dev` | what it runs on to charge, track and sign in | added by dependency `google-auth` |
| `seo-llmo` | whether a machine will find it | SEO brief or audit: present |
| `evidence-docs` | what proves it | nothing to seed |
| `task-pipeline` | how the change reaches the repository | DOCMAP: missing (`/task-pipeline setup`) |

Remaining setup:
- `/ux-init`: scenarios and their linter for `super-ux`, missing `docs/ux/lint.py`
- `/task-pipeline setup`: DOCMAP for `task-pipeline`, missing `docs/DOCMAP.md`
<!-- SSHLG:PROJECT:END -->
