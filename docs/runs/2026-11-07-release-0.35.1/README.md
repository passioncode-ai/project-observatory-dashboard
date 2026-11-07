# Run 2026-11-07: release 0.35.1

Objective: unblock the move of jobs into a macOS account with nobody signed in. 0.35.0's cutover recognised only
`launchctl print gui/<uid>` exit 112 as "no graphical session"; on the target computer such an account answers
exit 125 ("Domain does not support specified action"), so the cutover took the agent path, launchd refused the
bootstrap with the same 125, and the definition went back to the staging folder. Reported on the board by the session
that runs the move on that computer (measured twice, also under `script`).

Base: origin/main after release 0.35.0 (`fbf991d1`) on branch `fix/obs-361-gui-domain-125`.

## What it changes

- `observatory/engine/machine_move.py`: `NO_DOMAIN` is the set {112, 125}; `gui_session` answers no for either. Every
  other answer still keeps the agent path, which fails and puts back as before.
- Tests (`tests/test_machine_backup.py`, `HeadlessSpace`): both 112 and 125 place the job in the background folder and
  answer `waiting-for-root` with no `bootstrap` call; 1, 5 and 113 keep the agent path. The test that held 125 as an
  unknown answer is replaced: 125 was an unmeasured guess, now measured.
- `docs/COMPATIBILITY.md` (and its engine copy), `CLI-COMPATIBILITY.md` and `docs/ux/portable-scenarios.md` (OSS-101)
  name exit 125 since 0.35.1.

## Decisions

- **0.35.1, a patch release**: one recognised exit code; nothing else in the cutover changes.
- **Why 125 is safe to read as "no graphical session"**: in that state `launchctl bootstrap gui/<uid>` fails with the
  same 125, so the agent path could never succeed there; the background folder only waits for the administrator, and
  `leave` works from it as from the staging folder.
- **What 112 and 125 mean** (measured by the review on macOS 26.6 with read-only `launchctl print gui/<uid>`): 0 for
  an account signed in at the screen; 112 for an account nothing runs as (or no such account); 125 for an account
  something runs as (an SSH login, cfprefsd, its own daemons) with no graphical session, root included. So 125 is
  the usual answer of a space that already works, and every cutover after its first daemon starts sees it.
- **Not "any non-zero code"**, as some agents' own checks read it: a launchctl that fails for another reason must not
  send a signed-in person's job to wait for root; the agent path fails and puts the definition back.
- **The plugin stays 0.30.0.**

## Checks run

`tests.test_machine_backup.HeadlessSpace` with the dev interpreter: 14 tests OK after the fix (the new 125 case failed
with `KeyError: 'state'` before it). An independent review found nothing blocking: reverting
the fix to {112} makes the 125 case error; the whole `test_machine_backup` file is 140 OK; no other code compares
these exit codes. Its two low documentation findings (the precise meaning of 125, the OSS-101 scenario) are fixed in
this PR; its other low notes (a cutover run as root, a GUI-path cutover not reading the background folder) predate it
and go to the backlog after the pause.

## State now

- Open: the release PR, then the tag `v0.35.1`, its run, verification and the local install; then the move's session
  runs cutover and the administrator's `apply` on that computer.
- Waits: none beyond the release run (watched by this session).
- Next task: merge the release PR, push the tag, verify, install, record the receipts here.
