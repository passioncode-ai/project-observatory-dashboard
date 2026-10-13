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

| harness.scan_degraded | the tool configuration scan is incomplete for {source} | observatory/engine/tools/build_findings.py | Harness configuration recovery (I-2) | proposed | copy |

| tick.rotation_degraded | collector scheduling coverage is unknown | observatory/engine/tools/tick_findings.py | Tick scheduling and input age (K-2) | proposed | copy |
| tick.collector_starved | the `{step}` collector has not run for {hours} hours | observatory/engine/tools/tick_findings.py | Tick scheduling and input age (K-2) | proposed | copy |

| machine.guard_stopping | Git cleanup stopped in three consecutive disk guard runs | observatory/engine/tools/machine_findings.py | Guard stop and coverage findings (K-7) | proposed | copy |
| machine.guard_degraded | the disk guard could not measure {source} | observatory/engine/tools/machine_findings.py | Guard stop and coverage findings (K-7) | proposed | copy |
