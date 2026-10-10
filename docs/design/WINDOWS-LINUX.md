# Windows and Linux: the engine and the desktop app

Status: in progress (started 2026-10-10). Owner: this repository. Decision: the operator asked
for the engine **and** a desktop app on Windows and Linux, Windows first (2026-10-10).

## Where we start, measured

On 2026-10-10 (main `7365e75`, 0.20.1) the engine is macOS-first:

| Coupling | Non-test files |
|---|---|
| `fcntl` (file locks) imported at module top | 13, among them `workspace.py`, `store/compatibility.py`, `private_io.py`, `jobs.py` — on Windows almost nothing imports |
| `launchctl` (scheduling) | 19 |
| `/bin/bash`, `bash` (the tick is `tools/tick.sh`) | 23 |
| process groups (`os.killpg`, `start_new_session`, `os.setsid`) | 9 |
| `os.getuid` | 12 |
| `O_NOFOLLOW`, symlink refusals | 18 / 38 |
| `security` (the login Keychain) | 2 |
| `chmod`/`fchmod` 0600 as the privacy boundary | 67 |

Linux already has: the maintenance job as a systemd user timer (`maintenance.py`), the backup
passphrase in the Secret Service (`secret-tool`), and CI rows on Ubuntu 3.11 and 3.14. It lacks a
scheduled tick and server (`install_launchd.py`, `serverd.py --install` are launchd-only) and a
desktop app.

## Modules, in order

Each module lands as its own pull request with tests that were red first; the Windows CI row
turns from informative to required once W1–W3 are in.

| # | Module | Windows | Linux |
|---|---|---|---|
| W0 | **CI job `windows`** (`.github/workflows/check.yml`) and `.gitattributes` (LF on every checkout, so the inventory and licence digests match) — install the package, load every engine library module in its own interpreter (`tools/check_imports.py`), the lock tests, `--help` and the demo of the CLI, the top-level tests; each step runs regardless of the one before and the job summary lists every outcome; informative until W3 | new | exists |
| W1 | **`oslocks`** — `flock(fd, LOCK_SH/LOCK_EX/LOCK_NB/LOCK_UN)` over `fcntl.flock` on POSIX and `LockFileEx`/`UnlockFileEx` on Windows (shared, exclusive, fail-immediately; `BlockingIOError` on contention, as `fcntl`) | new | unchanged |
| W2a | **Process control — `osproc`** — a child in a group of its own (`CREATE_NEW_PROCESS_GROUP`), the tree stopped with `taskkill /T /F`, whether a process is alive asked through `OpenProcess` (on Windows `os.kill(pid, 0)` TERMINATES the process, so `jobs._alive` would have killed every job it checked), only the stop signals the OS has, no signal masks, the assistant's deadline as a timer instead of `SIGALRM`; `test_osproc` refuses `os.killpg`, `start_new_session=True`, `os.kill(pid, 0)`, `SIGHUP`, `pthread_sigmask` and `signal.alarm` anywhere else in the engine | new | unchanged |
| W2b | **File privacy — `osprivacy`** — the account's real home without `pwd` (`SHGetKnownFolderPath`, done: `maintenance._real_home`), the current user without `getuid`, owner-only files through the file's ACL instead of mode 0600, link refusals without `O_NOFOLLOW` | new | unchanged |
| W3 | **The tick in Python** — `tools/tick.sh` becomes `tools/tick.py` with the same steps, order, watchdog and log lines; `tick.sh` stays a two-line wrapper on POSIX for existing jobs | new | same code |
| W4 | **Scheduling** — Windows Task Scheduler for the tick, the server and the maintenance job (`schtasks` with an XML task, per-workspace names, no stored password); systemd user units for the tick and the server on Linux | new | completes |
| W5 | **Secret store** — the backup passphrase in Windows Credential Manager (`CredWriteW`/`CredReadW`); the vault's own files under the ACL of W2 | new | exists |
| W6 | **Updates** — `full update` into a Windows virtual environment (`Scripts\python.exe`), the job writers naming it; the app step drives the desktop app's updater | new | exists for the engine |
| W7 | **Paths and tools** — `%LOCALAPPDATA%` homes, `.exe`/`.cmd` lookup for `git`, `gh`, `heroku`, UTF-8 console output | new | exists |
| W8 | **Desktop app for Windows and Linux** — a Tauri 2 shell (the organization's Switchboard stack): the dashboard window on `127.0.0.1:47311`, start/stop of the server, a tray icon, Restart to update; Windows installer signed with the organization's Authenticode certificate, Linux AppImage and `.deb`; updater signature key in the vault | new | new |
| W9 | **Release and documentation** — the release workflow builds and signs the app per OS; onboarding per OS | new | new |

## What stays the same

- One engine, one wheel for every OS; no OS gets a fork of a module.
- The privacy boundary is the same claim on every OS — only the owner reads the workspace — and
  each OS proves it its own way (mode 0600, the file's ACL); a test asserts it per OS.
- Hooks of the Claude Code plugin stay shell scripts: Claude Code runs hooks through Git Bash on
  Windows. Checked again in W0 rather than assumed.

## Measured on Windows

First run of the `windows` job (PR #185, run 38006719799, Python 3.14.7, after W1):

| Step | Result |
|---|---|
| SQLite extension support, install, compile | pass |
| Every engine library module loads | pass — 76 of 76 |
| `--help`, `demo`, `doctor` of the CLI | pass |
| `test_oslocks` | the lock semantics held; every case failed in cleanup — the test deleted its folder before closing its descriptors, which Windows refuses (fixed: cleanups, last in first out) |
| Top-level tests | 19 of 133 red: CRLF checkout (inventory, licence and allowlist digests — `.gitattributes`); `pwd` in `maintenance._real_home` (`osprivacy.real_home`); three test fixtures (an environment cleared of `USERPROFILE`, `?` in a file name, LF turned into CRLF on a text pipe to `git hash-object`); the macOS notarization script (skipped on Windows); `env.permissions` reads mode bits, which Windows does not keep (W2b, still red) |

## Known differences on Windows

- **No graceful stop.** A process without a console receives no SIGTERM, so `osproc.stop_group`
  stops the tree at once there; `grace` applies on POSIX only.
- **The tree is followed by parent links.** `taskkill /T` reaches the descendants whose parent is
  still alive; a grandchild whose parent already exited is not reached. A Job Object with
  kill-on-close would close that gap and is the next step if a step's orphan is ever measured.
- **The deadline runs between bytecodes.** `osproc.deadline` interrupts the main thread from a
  timer; a call blocked inside C (a socket read without its own timeout) is not cut short as
  `SIGALRM` would cut it. The assistant's provider calls carry their own timeouts.

## Dependencies outside this repository

- `observatory/engine/fabric_service.py` and `tests/check_service.py` are vendored byte for byte
  from `passioncode-ai/fabric-agent-adapter` (`test_fabric_service` checks the digest). Both import
  `fcntl` inside one function — the service's instance lock (`fabric_service.py:151`) — so they
  load on Windows and only that lock fails there. Its Windows support is a change in that kit,
  released and vendored again — not an edit here (W4 needs it before the server is scheduled).

## What needs a person

- The Windows code-signing certificate (Authenticode, issued to the organization) — W8.
- A Windows machine for the first manual run of the installer and the app — W8.
