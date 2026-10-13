# Recovery wave 8: policy admission and backup visibility

## State now

- Base: the previously accepted checkpoint `8aff34f801a4154882d536bba0472b6549b041e7`.
  This combined candidate supersedes it by fast-forward after source acceptance; its
  receipts and later CI failures remain historical. App 0.25.0 and plugin 0.22.1 remain
  pre-release.
- Source scopes: [conditional capacity model](../2026-10-13-rotation-capacity-model/README.md),
  [policy admission](../2026-10-13-policy-rotation-admission/README.md), and
  [backup inventory](../2026-10-13-backup-update-inventory/README.md).
- The model sources `9d709ac` / `f836932` are synthetic conditional proofs, not a
  deployed recovery protocol. Capacity reduction requires an explicit availability
  contract; undocumented zero-limit creation is not assumed.
- Policy sources `3a006c1`, `f6161ac`, `1536add` and receipt `5635602` are integrated.
  The final policy source was accepted through the integration owner's independent
  fourteen-probe review, with its source binding retained in that packet. An earlier
  unfinished review is not represented as a passing judgment.
- Backup source `4de46c3` was independently accepted with mixed corrupt, foreign and
  linked fixtures and a simulated export failure. No operational backup or key was read.
- Composition preserves the issued-key activity command and all its portable tests.
  Additive scenario conflicts retain both backup and policy behavior; the source
  inventory is regenerated. Handling-secrets guidance now distinguishes marked
  refusal from unmarked legacy/standalone overlap without claiming full reconciliation.
- Next: exact combined independent acceptance and protected PR integration. No tag,
  install, provider mutation or real backup migration belongs to this source wave.

## Full contract still open

The [source-bound 128-case replay](../2026-10-13-policy-rotation-admission/raw/baseline-128.json)
at `1536adddaadb427a6830fa2343774801f87a60c8` is **58 PASS / 70 FAIL / 0 ERROR**.
All seventy remaining failures concern convergence. The older 28 PASS / 100 FAIL
baseline is preserved in the earlier consumer-revoke packet. This is not the full
5,200-case gate and was not rerun against this combined tree. Safe refusal prevents
an unproved effect; it is not successful recovery or convergence.

Policy snapshots are not current headroom authorization. Unknown legacy rows are not
classified by names or folders, and other standalone writers do not acquire universal
policy enforcement from this change. Full holder proof, read-only legacy re-attestation,
reconciliation and strict aggregate capacity recovery remain separate work.

Backup status covers only known kinds. Unknown foreign kinds remain outside the count
and migration ownership. The existing passphrase and three-per-kind retention policy
are unchanged; the new code does not automatically migrate installed copies.

## Validation

The combined focused suites pass 126 unittest cases and 486 credential assertions,
with no skips. Fourteen version/manifest/document-copy cases, three strict plugin
validators and source inventory also pass. Receipts are in `checks.json`. These are synthetic
source checks, with live provider, native release and installed acceptance excluded.
Historical failing probes remain in each source packet rather than being overwritten.

## Hosted CI corrections retained

The original `8aff34f` source failed the
[hosted check run](https://github.com/passioncode-ai/project-observatory-dashboard/actions/runs/38349250996).
Raw no-follow flag access in log rotation violated the portable privacy boundary,
and an older claimed-key test supplied an incomplete consumer scan. Corrections
`f06c6a2` and `9892212` are included and independently accepted in their narrow scopes.
The first uses the established no-follow flag helper; the second updates only the
fixture and adds missing/stale evidence refusals before the valid consumer controls.

On this combined source, 12 log-policy and three privacy-boundary cases pass; the
claimed-key suite passes 51 assertions. These small checks use isolated resolved
HOME/TMPDIR without copying the engine. The first direct claimed-key attempt correctly
refused the platform's symbolic temporary-directory ancestor; the unchanged suite
passed after that harness path was resolved. `checks.json` retains that distinction.
The prior hosted failure is not relabelled green; the superseding source still needs
its own protected hosted checks.
