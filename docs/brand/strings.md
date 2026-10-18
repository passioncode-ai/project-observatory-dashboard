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

| observatory.why_unanswered | {n} requests for a why went unanswered | observatory/engine/tools/build_findings.py | The request for a why: once, with the next prompt, measured (M-8) | proposed | copy |
| ssh.list_empty | No SSH keys yet. Create one: | observatory/engine/tools/ssh_keys.py | OSS-62 | proposed | copy |
| ssh.copy_done | Copied. Paste it where the key is asked for, or let Observatory send it | observatory/engine/tools/ssh_keys.py | OSS-64 | proposed | copy |
| ssh.copy_no_clipboard | the clipboard was not set: no clipboard tool (pbcopy, wl-copy, xclip); copy the line above | observatory/engine/tools/ssh_keys.py | OSS-64 | proposed | copy |
| ssh.create_needs_person | a passphrase is typed by a person, into ssh-keygen's own prompt | observatory/engine/tools/ssh_keys.py | OSS-63 | proposed | copy |
| ssh.never_overwritten | already exists; a key is never overwritten. Choose another name, | observatory/engine/tools/ssh_keys.py | OSS-63 | proposed | copy |
| ssh.cloud_gap | no Observatory door registers SSH keys at | observatory/engine/tools/ssh_keys.py | OSS-65 | proposed | copy |
| ssh.finding_no_passphrase | {key} has no passphrase | observatory/engine/tools/ssh_key_findings.py | OSS-66 | proposed | copy |
| ssh.finding_weak | {key} is a weak {kind} key | observatory/engine/tools/ssh_key_findings.py | OSS-66 | proposed | copy |
| ssh.finding_readable | {key} is readable by other accounts | observatory/engine/tools/ssh_key_findings.py | OSS-66 | proposed | copy |
| ssh.finding_folder | the SSH folder is open to other accounts | observatory/engine/tools/ssh_key_findings.py | OSS-66 | proposed | copy |
| ssh.finding_unused | {key} has not been used for a long time | observatory/engine/tools/ssh_key_findings.py | OSS-66 | proposed | copy |
| ssh.finding_orphan | GitHub accepts {title}, not found where Observatory looked | observatory/engine/tools/ssh_key_findings.py | OSS-66 | proposed | copy |
| ssh.finding_unregistered | {key} is registered nowhere this machine can see | observatory/engine/tools/ssh_key_findings.py | OSS-66 | proposed | copy |
| ssh.finding_private_in_pub | {path} holds a private key | observatory/engine/tools/ssh_key_findings.py | OSS-66 | proposed | copy |
| ssh.finding_public_missing | {key} has no public half | observatory/engine/tools/ssh_key_findings.py | OSS-66 | proposed | copy |
| ssh.finding_identity_missing | {host} names a missing key file | observatory/engine/tools/ssh_key_findings.py | OSS-66 | proposed | copy |
