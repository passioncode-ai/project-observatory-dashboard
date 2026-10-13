# Cleanup command diagnostic privacy

## State now

- Objective: keep external tool output out of durable cleanup receipts.
- Implemented: failed commands retain an exit code and stable category; busy commands
  retain the busy classification without copying output. Successful behavior is unchanged.
- Evidence: `test_disk_guard.Acting.test_child_diagnostics_never_enter_receipts_or_journal`
  runs an owned fake child across stdout, stderr, busy and success outcomes, then reads
  the actual JSONL journal. No live cache command or operator file is used.
- Open: independent review, hosted checks, release and installed acceptance.
- Next task: verify the synthetic leakage counterexample against this candidate and
  integrate only after review and required checks.

The defect was reproduced with a synthetic marker in child output at source0673212.
It demonstrated a persistence path, not an observed live credential incident.
Automatic cleanup remains disabled on the affected installation; explicit apply
requires the fixed release and its acceptance before use.

## Error-envelope follow-up

The same privacy rule covers the guard's top-level exception envelope and invalid
Git-child output: only exception type or a stable failure category is returned.
An empty, malformed or incomplete child summary is failed even if the child exited0;
valid counts and carried-source evidence remain intact. Synthetic negative tests first
failed eight assertions, then passed with the structured error handling. No live
cleanup or installed settings changed. Full focused guard suite and independent review
must accept this exact follow-up before release.
