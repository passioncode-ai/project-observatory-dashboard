# Windows path cleanup refuses unknown holders

## State now

The former Windows holder implementation returned known-empty and used rename
success as permission to remove paths. Microsoft documents that an open handle
can allow rename/delete through FILE_SHARE_DELETE. The current implementation
therefore reports unknown and refuses path removal; old rename-mode state also
refuses. Generic tool cleanup commands retain their separate policies.

This source is based on release candidate `12b7343`. It is not installed and does
not resolve that candidate's financial-model failures. Native Windows acceptance
remains pending; no new holder inventory or global exclusion lock is claimed.

## Implementation and verification

- `observatory/engine/holders.py`: Windows inventory unknown, fresh probe `None`,
  legacy rename-mode cannot grant deletion permission.
- `observatory/engine/tests/test_disk_guard.py`: three new synthetic controls
  failed before the fix. The first attempt used an interpreter missing full-engine
  dependencies and errored; the supported existing runtime reproduced all three
  failures before implementation. No new dependency environment was installed.
- Full disk-guard suite:140 tests,139 passed and one native Windows control skipped
  on macOS. Six root documentation-copy checks passed; inventory and docs sync pass.
- `.github/workflows/check.yml` selects `WindowsHolderUnknown` on Windows with an
  enforced step. The native test holds a FILE_SHARE_DELETE handle while renaming
  an isolated temporary file. Other historical informative Windows steps are not
  thereby converted to acceptance gates. The first actionlint run reported inherited SC2155 in the plugin validation
  step. Separating the existing temporary-directory assignment from export
  prevents masking its return code; the corrected full actionlint run passed.

[Microsoft CreateFile sharing contract](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)
is the primary API evidence. The local synthetic tests exercise the refusal
policy, not native Windows handle behavior.

Next task: independent review of the exact committed source, privacy gate, draft
publication and the explicit native Windows CI control. Then reconcile this
source with the combined recovery candidate before any release or installed test.
