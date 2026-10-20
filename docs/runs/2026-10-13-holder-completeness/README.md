# Holder completeness and final worktree checks

## State now

Source correction prepared against `93223f5c095b9c688f24214aeb38f16bb9bc3e90`;
independent review and protected source integration remain pending. No live
cleanup, service change, provider access, tag or installation was performed.

## Defect and correction

`holders._run` accepted partial nonzero `lsof` and `ps` output. An omitted
worktree could be reported free; incomplete `ps` could admit a cache-tool command.
The disk tier already had a strict final target probe, so its initial planning
inventory defect is distinct from the git tier's removal gap. Clean worktrees
used a cached observation; dirty worktrees refreshed before potentially lengthy
archive revalidation.

The shared inventory now requires successful, diagnostic-free and decodable
command output, including at least one parsed absolute path in global `lsof`.
Linux enumeration/read errors return unknown instead of a partial
list. The existing bounded target probe moved to `holders.fresh_busy`, retaining
`disk_guard.fresh_busy` as a compatibility entry point. Both worktree classes call
it after content and ref verification, immediately before removal. Unknown/held
or exhausted budget preserves the target. External output is never copied into
failure receipts. See [the design contract](../../design/DISK-GUARD.md).

## Checks

`tests/test_disk_guard.py::HoldersCompleteness` reproduces partial `lsof`, failed
`ps`, failed `/proc` enumeration/links/fd/maps and undecodable output, with complete
positive controls. `WorktreeFinalHolders` uses disposable real Git worktrees to
check both clean and archived dirty preservation and valid free removal. The
first test attempt against the base failed (20 assertions, one error
across six test methods, including two fixture failures); the corrected source
run isolated a synthetic `/tmp`
versus resolved-path fixture mismatch, corrected to stable synthetic `/srv` paths.
The final check receipt is [checks.json](checks.json). All process observations
are synthetic; the former live Linux fallback test now supplies a controlled
inventory. Existing Git-hardening and machine-estate fixtures supply both
preliminary and final probe outcomes explicitly; an initial test-variable
shadowing error in the fixture correction was repaired before the final full
Git-hardening pass. An isolated HOME and resolved temporary root prevent host state use.

## Limits and next task

A process can still start after a successful final observation: this is not an
exclusive filesystem lock. `/proc` read churn or inaccessible visible processes
can conservatively make planning unknown. Missing `lsof` means no POSIX final
removal proof. The worktree idle-age threshold is not remeasured during execution.
The missing-worktree prune result/scope is a separate followup, not fixed here.

Next: independently test the exact committed source, then integrate through the
existing protected release candidate. Whole-system completion, hosted CI,
installed acceptance and the separate unresolved rotation reconciliation are not
claimed by these focused checks.
