# Missing registration prune receipts

## State now

- Objective: preserve failure and uncertainty when Git prunes missing registrations.
- Implemented: inspect the exit code; typed failed/incomplete reasons; explicit
  repository-wide scope and unknown registration change count; no raw error text.
- Validation: five focused regression cases, before 1 failure/3 errors/1 pass,
  after five pass. Existing prune selection and Git arguments remain unchanged.
- Limit: this is a receipt correction; no live prune or deletion was executed.
  Success records command completion, not a measured registration delta.
- Next task: independent exact-source review, then integration under branch protection.

Proof: `observatory/engine/tests/test_git_hardening.py::MissingRegistrationReceipt`.
The source change is bounded to `tools/cleanup.py`'s worktree-missing branch.
