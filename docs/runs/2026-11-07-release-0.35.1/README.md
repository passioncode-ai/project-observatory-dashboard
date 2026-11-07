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

## Receipts

- Release PR #463 merged as `4575bdfc` (after two independent review rounds; the second checked the documentation fix
  `a6b6dd61`). Its `check.yml` run on main (`42375089919`) passed every job, Windows included. Tag `v0.35.1`
  (annotated, object `38912aff`) on `4575bdfc`.
- Release run `42378577237`: every build job passed on the first attempt (guard, wheel, manifest, Linux x64 and arm64,
  Windows x64 and arm64, macOS, feed); `publish` failed three times in `actions/attest-build-provenance` on a Sigstore
  outage: attempts 1 and 2 "error creating tlog entry - (500) unexpected result from transparency log" while
  `https://rekor.sigstore.dev/api/v1/log` itself answered 500; attempt 3, right after Rekor answered 200 again, "error
  fetching tlog entry - (404) Not Found"; attempt 4, five minutes later, passed. No release existed between attempts.
  Published 2026-11-07T14:56:50Z, 19 assets. The `check.yml` run the tag started (`42378577075`) passed.
- Verified after publishing: `shasum -a 256 -c SHA256SUMS --ignore-missing` 0 failures; `gpg --verify SHA256SUMS.asc
  SHA256SUMS`: good signature, key `63B30DC324BD697487AA31944FAFB8AEC803B6A7`; `gh attestation verify` on the wheel with
  `--signer-repo passioncode-ai/.github`: exit 0; the wheel carries `analytics-app-key`, its `machine_move.py` holds
  `NO_DOMAIN = frozenset({112, 125})`, and its `tools/space_daemons.py` is byte-identical to 0.35.0's (sha256
  `2940909a…`), so a root-owned copy installed from 0.35.0 stays valid.
- Installed on the operator's Mac with `project-observatory full update --version 0.35.1 --apply`; `full doctor` reports
  0.35.1 and the service `project-observatory.default` answers 0.35.1 on its well-known document. Doctor's attention
  after the update: `deferred` (the update holds its remaining step for a moment with no client), `tick-interrupted`
  (the update stopped the tick that was running) and `machine-backup-not-carried` (as before).
- The first real use of `tools/space_daemons.py` (1.1.0, unchanged here), reported on the board by the session that
  runs the computer: three services moved inside one machine from an administrator's agents into a headless space,
  `plan` 3/3, `apply` 3/3 loaded, `status` 3 as-written. After a `bootout` of the old agent the port was still held
  for a moment and the first daemon start failed, then KeepAlive restarted it: a note for the docs, filed after the
  pause.
- Told: the waiting agents (board thread `c1a45468`, message `fb47774d`), and the session that runs the Studio's move.

## State now

- Open: on the computer the move goes to, each space updates to 0.35.1, then the next headless cutovers and root's
  `apply` (run by that computer's session). `full maintain ensure` and the check job in a headless space still fail on
  exit 125: OBS-361 part 2.
- Waits: none in this repository.
- Next task: none for this release; OBS-361 part 2 after the shared source of truth.
