# Observe open files after unlink without granting cleanup authority

## State now

- Objective: expose a blind spot of directory scans through a bounded metadata-only
  observation on macOS/Linux; preserve unknown whole-machine coverage.
- Implemented: explicit CLI collector, normalized receipt, shared saved coverage
  and doctor readers, MCP guidance and matching scenarios. No scheduler or cleanup
  action changes.
- Validation: tests-first missing-module failure, then eight parser/process tests
  pass; the first integration run used an interpreter missing full dependencies and
  failed three setups. With the existing full interpreter all eleven tests pass.
  The first portable run missed the new module in its explicit source allowlist;
  after adding it, open_unlinked11 and coverage_view8 pass in fresh sandboxes.
  Six documentation-copy checks and Python3.11 grammar pass; no3.11 runtime
  is claimed. Independent acceptance and hosted checks remain pending.
- Limits: source visibility is partial even with exit zero. A process may change
  during collection; device/inode/PID identities are valid only for that snapshot.
  Linux observation requires lsof; Windows is explicit unsupported. No lifecycle
  or ownership inference, release, installation or operational cleanup.
- Next task: independent adversarial review, privacy gate and protected integration
  after the current safety release; keep automatic cleanup unchanged.

Implementation: `observatory/engine/open_unlinked.py`; behavior tests:
`observatory/engine/tests/test_open_unlinked.py`.

## Independent correction

The independent reader found that an unknown file type was silently excluded,
producing a false complete zero. A new regression failed first, then passed after
unknown types became degraded. Twelve focused cases pass after this correction.
Known nonregular types are explicitly excluded; unrecognized dialect types remain
unknown. Field/type meanings were checked against the
[upstream lsof manual](https://lsof.readthedocs.io/en/stable/manpage/).
The initial candidate privacy gate passed990 files and13 introduced blobs with
4055 effective private identifiers; the corrected head needs its own gate.
