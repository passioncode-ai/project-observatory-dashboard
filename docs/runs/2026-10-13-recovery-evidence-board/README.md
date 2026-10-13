# Recovery board evidence refresh

## State now

The board keeps OBS-84 through OBS-90 open while distinguishing merged source,
draft follow-ups, hosted failures and release/installed acceptance. This is a
documentation-only refresh on main `97428c2`. Every original board ID and the next-ID
marker are retained. No source implementation, release or installation is included.

## Source-owned delivery map

| Scope | Exact source and review entry | Remaining boundary |
|---|---|---|
| Initial preservation and receipts | [PR255](https://github.com/passioncode-ai/project-observatory-dashboard/pull/255), main5c14e71 | Release and installed acceptance |
| Outcome/coverage recovery | [PR256](https://github.com/passioncode-ai/project-observatory-dashboard/pull/256), main79702ee | Release and original financial reconciliation |
| Measurement age and partial visibility | [PR257](https://github.com/passioncode-ai/project-observatory-dashboard/pull/257), main7bcf331 | Release and installed acceptance |
| Combined release candidate | [PR260](https://github.com/passioncode-ai/project-observatory-dashboard/pull/260),12b7343 | Draft; later original financial-model acceptance failed despite passing hosted checks at12b |
| Open-unlinked observation | [PR261](https://github.com/passioncode-ai/project-observatory-dashboard/pull/261),99f8570 | Draft; installed acceptance |
| Per-scope log retention evidence | [PR263](https://github.com/passioncode-ai/project-observatory-dashboard/pull/263),f342f30 | Draft; installed acceptance |
| Tick stage receipts | [PR264](https://github.com/passioncode-ai/project-observatory-dashboard/pull/264),a3b2481 | Scoped review passed; Ubuntu failed inherited credential tests; no installed/performance acceptance |
| Local checkout state | [PR265](https://github.com/passioncode-ai/project-observatory-dashboard/pull/265),2e2de84 | Draft; hosted/installed acceptance and separate owner/lifecycle proof |
| Independent pressure observation | [PR266](https://github.com/passioncode-ai/project-observatory-dashboard/pull/266),8b86dfa | Draft; hosted/installed acceptance; no lifecycle authority |
| Portable suite evidence | [PR267](https://github.com/passioncode-ai/project-observatory-dashboard/pull/267), `476d392` | Production `469d316` and fixture correction independently reviewed; hosted acceptance remains |

| Financial reconciliation evidence | [PR268](https://github.com/passioncode-ai/project-observatory-dashboard/pull/268), `f6e393b` | Draft; full original model at `153a160`: 2,147 pass, 3,053 fail, zero errors; not accepted |
| Offline checkout observation | [PR269](https://github.com/passioncode-ai/project-observatory-dashboard/pull/269), `284bc39` | Scoped source review passed; hosted/installed acceptance and upstream graph work remain |

These are bounded evidence statements, not a closure of all requirements. A
successful observation does not authorize deletion, prove reclaimed disk/RAM or
confirm another repository's owner. The execution board remains the status home;
product/package priorities remain with their owning organization board.

## Verification and next task

The ID-set/order and next-ID preservation check is in board-preservation.json.
Board lint passed with zero problems, all 18 root board tests passed and
`git diff --check` passed. The committed current/history privacy gate is required
before publication. No private machine evidence or
identifier list belongs in this public record.

Next task: finish independent and hosted checks for the bounded follow-ups, then
resolve the full financial-model refusals before protected release integration.
Do not close these rows based on source review or a successful observation alone.
