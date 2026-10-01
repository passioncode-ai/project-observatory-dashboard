Contract: brand-contract v1

| Key | Text (primary) | Location | Scenario | Status | Kind |
|---|---|---|---|---|---|
| dashboard.source_summary | A registry built from the connected sources: project folders, | observatory/engine/dashboard/build_dashboard.py:873 | OSS-10 | proposed | copy |
| dashboard.secret_files | files in the project's own secrets/ folder; | observatory/engine/dashboard/build_dashboard.py:2658 | OSS-10 | proposed | copy |

| site.copy_setup | Copy setup prompt | site/index.html | SITE-12 | proposed | copy |
| site.read_setup | Read the setup prompt | site/index.html | SITE-12 | proposed | copy |
| site.copy_ready | Paste it into your agent. It will guide the local setup. | site/index.html | SITE-12 | proposed | copy |
| site.copy_success | Copied. Paste this prompt into your coding agent. | site/app.js | SITE-12 | proposed | copy |
| site.copy_failure | Clipboard unavailable. The prompt is selected; copy it with your keyboard. | site/app.js | SITE-12 | proposed | copy |

| space.preview | Preview cleanup | observatory/engine/dashboard/space_page.py | OSS-24 | proposed | copy |
| space.auto | Automatically clean approved caches when disk space is low | observatory/engine/dashboard/space_page.py | OSS-25 | proposed | copy |
| space.protection | Sizes show occupied space, not guaranteed recovery. Protected caches stay in place. | observatory/engine/dashboard/space_page.py | OSS-23 | proposed | copy |
| space.confirm | Clean listed caches | observatory/engine/dashboard/space.js | OSS-24 | proposed | copy |
| space.failure | Space remains low. Protected data was left in place. | observatory/engine/dashboard/space.js | OSS-25 | proposed | copy |
