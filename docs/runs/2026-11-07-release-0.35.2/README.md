# Run 2026-11-07: release 0.35.2

Objective: release `peer_owner` 1.3.0 (OBS-384, #465), the loopback owner check that works on macOS 27, where a
process sees only its own sockets in the kernel's connection table; then tag, verify and install it.

## State now

- Open: this release PR, then the tag `v0.35.2`, its run, verification and the local install; then the announcement
  with the sha256 of `observatory/engine/peer_owner.py`, which several local services vendor byte for byte.
- Waits: none beyond the release run (watched by the releasing session's background job).
- Next task: merge the release PR, push the tag, verify, install, record the receipts here.

## What it releases

- #465 (`peer_owner` 1.3.0), written by another project's session to a specification from this repository's owner,
  then reviewed here. On macOS 27.2 `sysctl net.inet.tcp.pcblist_n` gave a process only its own rows (with the
  engine's Python 3.14) or none (with a Python 3.15 build), and `netstat -anv` lost its `process:pid` column, so 1.2.0
  answered `unknown` (`no-row`) for every other process: a service moved there refused its own account's clients
  (HTTP 403 `peer-unverified`), and its clients refused it. 1.3.0 treats a table with no row of another account as
  restricted and reads this account's own processes' sockets through libproc (`proc_listpids` for this uid,
  `PROC_PIDLISTFDS`, `PROC_PIDFDSOCKETINFO`, with a layout calibrated on a pair of its own). Found there means
  `this-account`; anything else is `unknown` (`table-restricted`), refused as before.

## Decisions

- **0.35.2, a patch release**: one module's fix; every answer on a full table (macOS 26 and earlier, Linux, Windows)
  is unchanged.
- **libproc, not `lsof`**: the same data `lsof` reads, without a subprocess or text parsing; one specification agreed
  on the board between this repository's owner and the session that wrote it, so two versions never shipped.
- **What the proof is**: a process of this uid holds the socket's descriptor, not the socket's credential (libproc
  carries none: `vst_uid` reads 0). A socket another account accepted and passed with `SCM_RIGHTS` to a process of this
  uid would read as this account's; `expect_pid` catches it. Written in the module docstring and ACCESS.md rule 10.
- **The plugin stays 0.30.0.**

## Checks run

- Independent security review at `56ce42ff`: approve, no blocking or high finding. `test_peer_owner` 78 OK,
  `test_peer_check` 24 OK, `test_docs_current` 46 PASS. Mutations: removing the libproc proof, answering not-found as
  this account, dropping the exact-bind listener rule, forcing the table to read as full, keeping a pid for several
  processes, answering `none`, keeping TIME_WAIT rows: each turns a test red. Live on macOS 26.6.2 (full table: 855
  rows, sysctl only, libproc never called) and on macOS 27.2 over SSH: own pair, a same-uid child listener and a late
  accept (342 ms) answer `this-account` through libproc; every listener of root (10) and of three other accounts (15)
  answers `table-restricted`, never `this-account`; `/usr/bin/python3` 3.9.6 imports and runs it on both.
- Its medium findings (M1 the weaker proof documented, M2 two tests that pin the uid selector and a failed calibration)
  and low findings L1 (refuse a setuid context), L2 (one deadline per question) and L3 (restricted means no row of
  another uid) are fixed at `cde87e39`. The confirming round there passed: 135 tests OK; eight mutations (the uid selector, the calibration ignored or skipped, the setuid refusal removed, the old restricted rule, either deadline dropped) each turn one test red; live again on both computers. Its two low points were wording, fixed on the release branch: the docstrings now say the libproc path also reads the effective uid (only to refuse a setuid context), and that `listener` and a first calibration each have a deadline of their own. L4 (several processes skip `expect_pid`), L5
  (root with `SO_REUSEPORT` and the exact-bind rule) and L6 (non-`TableUnreadable` ctypes errors; one transient
  calibration failure disables libproc for the process) are filed after the pause.
- The first CI run of the release PR failed on Linux in two tests the review round added: they mark the platform
  darwin, and on glibc `sysctlbyname` is no symbol, so ctypes raised `AttributeError` instead of `TableUnreadable`
  (the review's L6). Fixed in the module: a missing libc or libproc symbol now reads as an unreadable table, so the
  question answers `unknown`. `test_peer_owner` OK on Linux in containers with Python 3.11 and 3.13, and on macOS.
- On the release branch: the root release suites, `tools/sync_engine_docs.py --check`, `tools/check_backlog.py`,
  `observatory/engine/tools/check_docs.py`, `check_paths.py` and the inventory check (results in the release PR).
