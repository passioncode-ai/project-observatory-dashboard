# Recover per-issued-key spend reporting

<sub>ssheleg skills — task-pipeline · evidence-docs · ux-scenarios · copywriting · handling-secrets</sub>

## Brief and scope

Restore PR #249 (`ba4c190b75a5169761c05ebb72e30ae9e49d8f08`) on the current
provider-door source without importing its obsolete ledger implementation. Its
`read_activity`, `cmd_spend`, parser and test are absent from this base. The original
branch remains unchanged. This is a named issued-key report, not project accounting,
rotation reconciliation or authority to spend. No provider call is used for development.

The [OpenRouter activity contract](https://openrouter.ai/docs/api/api-reference/analytics/get-user-activity-grouped-by-endpoint)
was read on 2026-10-13: a management-key GET returns endpoint-grouped activity for
30 completed UTC days and accepts one date and an issued-key SHA256 filter. The
report folds endpoint rows by day/model and identifies `usage` as the summed provider
field, without treating BYOK inference as an additional bill. No request attribution
or completeness beyond the provider response is inferred.

## Requirements and proof plan

- REQ-1: recover text/JSON/parser and hash-filtered GET; synthetic positive/empty cases.
- REQ-2: missing/malformed/nonfinite usage is unavailable, never zero; adversarial rows.
- REQ-3: validate calendar/window, model/date shapes and token counts; optional missing
  counters remain null. Preserve actual measured zero and sum endpoint rows correctly.
- REQ-4: no provider body, key value, hash or arbitrary extra field in output; exception
  and injected-field tests. Readonly operation never changes ledger/vault/provider keys.
- REQ-5: bind the selected ledger identity across the read, refuse unresolved rotation
  and changed identity; no network-held ledger lock. Test a synthetic concurrent change.
- REQ-6: bound new activity response and rows; synthetic oversized receipt refusal.

Scope: `tools/openrouter.py`, a focused synthetic suite/runner, docs/scenarios/inventory.
The existing request helper may gain an optional byte cap used only by activity reads;
other callers retain their current contract. Parent owns boards and releases.

Next: tests first, implement, scoped compatibility checks, independent exact-head review.
Installed/provider acceptance and retired-key/project aggregation remain open.

## Recovery and validation receipt

Only the original feature functions/parser were transplanted for the red run. Seven
adversarial tests against that implementation produced fourteen failed assertions
and one error, covering unsupported zero, aggregation, date/identity drift and error
privacy. The final focused suite has ten passing cases. A separate uppercase-handle
probe first failed and now passes. Compatibility checks: 19 rotation-accounting cases
and 486 credential-door assertions pass. All ran jobs 1 in isolated synthetic homes.
The shared request helper's existing behavior is unchanged unless its optional
`max_bytes` is supplied; only the new activity read supplies it.

Source inventory, generated onboarding copies, whitespace and public pattern privacy
checks pass. Full-history/private-denylist, live provider and installed acceptance
were not run in this unit. Original PR #249, its branch, five-file source and unrelated
worktrees were not modified. The new focused suite supersedes the old happy-path
spend test rather than replacing the existing credential-door tests. No changelog or
shared board was edited; the parent owns integration and original-PR disposition.

REQ-1–6 are covered by `tests/test_issued_spend.py`; its test names state the specific
contracts. `read_activity`, `_spend_identity`, `cmd_spend` and the spend parser implement
those boundaries. Optional counter absence is retained rather than making a partial
count look complete. JSON total basis is the provider's `usage` field; BYOK valuation,
retired keys, account/project aggregation, provider reconciliation and attribution of
an individual missing request are deliberately not implied.

Next: independent exact-head source/adversarial review. Only after accepted integration
may the old feature PR be marked superseded; this local candidate is not a release.

---

**Made with [ssheleg skills](https://github.com/ssheleg/sshlg-skills)**

- [`task-pipeline`](https://github.com/ssheleg/task-pipeline) — issued-key spend recovery
- [`evidence-docs`](https://github.com/ssheleg/task-pipeline) — source and test evidence
- [`ux-scenarios`](https://github.com/ssheleg/super-ux) — read-only spend behavior
- [`copywriting`](https://github.com/ssheleg/super-ux) — unknown and refusal wording
- [`handling-secrets`](../../../observatory/engine/skill/plugins/observatory-log/skills/handling-secrets/SKILL.md) — repository provider-door privacy boundary, outside the skills family
