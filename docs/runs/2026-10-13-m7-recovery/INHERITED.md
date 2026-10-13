# Inherited M-7 work in progress

This branch starts at `a8726f553b12cbdb64aec4df8c97a18f8d4cbc44`, the local merge
following PR #251's `265f36d2ec5370e4351842b4536e561f07140278`. Six previously
uncommitted tracked files were copied into an isolated worktree without changing
the source worktree: engine inventory, MCP server, lessons store, lesson and
corroboration tests, and findings builder. The inherited binary diff SHA-256 is
`7d8aeb6c9986ce05d9e4796b39447308ada2a8ce4bc76db4658ca99fe943c64b`.

This checkpoint preserves unfinished work; it is not acceptance. Isolated checks
found lesson 1/15 failing (archive revision reference), corroboration 53 assertions
passing, and five source-inventory mismatches. Additional synthetic probes found
unsupported observation retained after a commit disappeared, typed lesson identity
mutation through a ledger revision, and non-atomic archive/successor writes.
Follow-up commits address these findings. No live memory write, release or install
is part of this branch.
