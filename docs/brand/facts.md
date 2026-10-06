Contract: brand-contract v1

| Fact | Value | Source | Checked | Review by | Public |
|---|---|---|---|---|---|
| reviewed local replacement events | 202 value-replacement events across two local stores, not unique credentials | docs/site/case-study.json | 2026-09-21 | 2026-12-21 | yes |
| SQLite local history window in compatibility CLI | 100 snapshots | observatory/core.py | 2026-09-21 | 2026-12-21 | yes |
| minimum Python | 3.11 | pyproject.toml | 2026-09-21 | 2026-12-21 | yes |
| reviewed Chroma replacement events | 136 value-replacement events, not unique credentials | docs/site/case-study.json | 2026-09-21 | 2026-12-21 | yes |
| reviewed claude-mem replacement events | 66 value-replacement events, not unique credentials | docs/site/case-study.json | 2026-09-21 | 2026-12-21 | yes |
| website theme color | #072820 hexadecimal design token, not a product statistic | site/index.html | 2026-09-21 | 2026-12-21 | yes |
| cover dimensions | 1672 by 941 pixels | site/assets/observatory-cover.png | 2026-09-21 | 2026-12-21 | yes |
| dashboard screenshot dimensions | 1440 by 900 CSS pixels (2880 by 1800 at 2x) | site/assets/dashboard-overview-en.png | 2026-09-26 | 2026-12-26 | yes |
| Claude Code hook metadata | Hook inputs include a transcript path; PostToolUse includes tool input and response | https://code.claude.com/docs/en/hooks | 2026-09-21 | 2026-12-21 | yes |
| claude-mem capture mechanism | Separate optional integration captures tool observations through a PostToolUse hook | https://docs.claude-mem.ai/architecture/hooks | 2026-09-21 | 2026-12-21 | yes |
| reviewed remediation date | 14 September 2026 | docs/site/case-study.json | 2026-09-21 | 2026-12-21 | yes |
| author identity and experience (receipt for the 2026-09-21 field-notes article only; product surfaces name PassionCode.ai as author) | Sergey Sheleg, tech founder and product builder; 13 years building products; startup co-founder; formerly Head of Android for the Ultimate Guitar app | https://sshlg.me/ | 2026-09-21 | 2026-12-21 | yes |
| Observatory origin | Owner wanted to map folders to repositories, audit existing and missing work, identify projects worth reviving; repeated exposure warnings led to key management and monitoring | docs/runs/2026-09-21-observatory-origin-story.md#source-ledger | 2026-09-21 | 2026-12-21 | yes |
| external conversation cleanup example | Reddit author reports pasting keys in earlier conversations, improving storage later, and wanting to remove secrets while preserving history; self-report only | https://www.reddit.com/r/ClaudeCode/comments/1tjj77i/what_is_a_good_way_to_clean_up_keyssecrets_from/ | 2026-09-21 | 2026-12-21 | yes |
| external configuration attack research | Check Point reported Claude Code configuration vulnerabilities including API-key exfiltration; all reported issues patched before publication; separate from our local-retention case | https://research.checkpoint.com/2026/rce-and-api-token-exfiltration-through-claude-code-project-files-cve-2025-59536/ | 2026-09-21 | 2026-12-21 | yes |
| editorial reading estimate | 1432 Markdown source words; 7 minutes at 220 words/minute, rounded up | tools/build_article.py | 2026-10-06 | 2026-12-21 | yes |
| cleanup chart scale | 0 to 150 events, ticks every 50; plotted observations remain 66 and 136 | tools/build_article.py; docs/site/case-study.json | 2026-09-21 | 2026-12-21 | yes |
| original cartoon dimensions | 1536 by 1024 pixels; generated fictional illustration, no operational data | site/assets/credential-copies-cartoon.png | 2026-09-21 | 2026-12-21 | yes |
| automatic updates | An installation checks GitHub once a day and installs a newer stable release by itself (since 0.17.0); only a release whose SHA256SUMS the organization's release key signed (since 0.18.0); a workspace snapshot is taken first and a failed update is rolled back; `full auto-update off` stops it | docs/ONBOARDING.md (Staying in step); observatory/engine/maintenance.py; observatory/engine/release_signature.py | 2026-10-06 | 2027-01-06 | yes |
| missing-page HTTP response | 404 is the intended missing-page status, not a product statistic | site/404.html | 2026-09-21 | 2026-12-21 | yes |
