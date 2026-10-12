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
| W2b | **File privacy — `osprivacy`** — one module for both profiles (the light `core` loads it by path): `open` (binary on Windows — without `O_BINARY` a descriptor is in text mode and LF becomes CRLF; `NOFOLLOW` refuses a link or junction by looking first), `private`/`others_may_write`/`owned_by_me` (mode bits and `st_uid` on POSIX; the file's ACL on Windows — every granting ACE names this account, LocalSystem or Administrators), `make_private` (0600/0700; a protected ACL inherited inside a folder), `private_folder`, `loose`, `describe`, and `real_home` without `pwd` (`SHGetKnownFolderPath`). `runtime_identity` holds its folder by path on Windows, where a folder has no descriptor. `test_osprivacy` refuses `os.open`, the POSIX-only open flags, mode-bit and `st_uid` checks anywhere else; `os.getuid` stays only in the five launchd/Mac-app modules W4 replaces | new | unchanged |
| W3 | **The tick in Python** — `tools/tick.py`: the same steps, order, watchdog (`tick_lease.py step`), log lines, age gates, early stops through `bail` and report; re-enters through the supervisor when started directly (`execv` on POSIX, a waited child on Windows); `tools/tick.sh` is a wrapper that picks the interpreter and hands over, so launchd jobs keep working. The tests that read the shell text read the tick's syntax tree now (`tests/tick_reader.tick_calls`) or drive `Tick` itself; `test_tick_py` runs a whole tick, and the `windows` job runs one on Windows | new | same code |
| W4 | **Scheduling** — Windows Task Scheduler for the tick, the server and the maintenance job (`schtasks` with an XML task, per-workspace names, no stored password); systemd user units for the tick and the server on Linux. **W4a** (this change): `osschedule` — one `Job` description and two supervisors, `TaskSchedulerJob` (a task under `\ProjectObservatory\<sha16>-<job>`, `InteractiveToken`, `LeastPrivilege`, started by `pythonw.exe` through `tools/scheduled.py`, which sets the environment, folder and logs a task cannot; a service gets a logon trigger, restart every minute and no time limit — fabric-service DEC-0032) and `SystemdJob` (oneshot + timer, or `Restart=always` service); `test_osschedule` runs a real task on the Windows job. **W4b**: the tick, the server and maintenance install through it; the server also needs fabric-agent-adapter 0.8.2 (DEC-0032 in the kit) vendored | new | completes |
| W5 | **Secret store** — the backup passphrase in Windows Credential Manager (`CredWriteW`/`CredReadW`); the vault's own files under the ACL of W2. **Done** (`backup_vault.WindowsCredentials`; `test_maintenance` covers it with a stand-in everywhere and the real store on the Windows job) | new | exists |
| W6 | **Updates** — `full update` into a Windows virtual environment (`Scripts\python.exe`), the job writers naming it; the app step drives the desktop app's updater. **Done for the engine:** `engine_update.OsscheduleServices` stops and restarts the tick and server tasks/units around an install (the tick is enabled, not run), the report's fix names `schtasks`/`systemctl`, and the running `project-observatory.exe` is moved aside before pip replaces it and put back if the install fails. The desktop app's updater is W8 | new | exists for the engine |
| W7 | **Paths and tools** — `%LOCALAPPDATA%` homes, `.exe`/`.cmd` lookup for `git`, `gh`, `heroku`, UTF-8 console output. **Done:** `osprivacy.default_home` (`%LOCALAPPDATA%\<name>`, an existing 0.21.0 `~\.local\share` workspace stays), `osproc.program` resolves a bare tool name through PATHEXT where the engine spawns tools by name (`slow_command`, `scan_mcp`), the CLI prints UTF-8 and tasks run with `PYTHONUTF8=1` | new | exists |
| W8 | **Desktop app for Windows and Linux** — a Tauri 2 shell (the organization's Switchboard stack): the dashboard window on `127.0.0.1:47311`, start/stop of the server, a tray icon, Restart to update; Windows installer signed with the organization's Authenticode certificate, Linux AppImage and `.deb`; updater signature key in the vault **W8a** (`desktop/`): the dashboard window (live server for this workspace, else the built pages through a confined `observatory:` protocol with a banner and «Start server», else «Build the dashboard»), navigation kept inside those pages, the banner's actions as intercepted `obs-action:` navigations so the dashboard's pages get no IPC, Settings with the system picker, first-run messages per engine error, the tray and close-to-tray, one instance, English/Russian; Rust tests in `cargo test`, the package built and installed on windows-latest and Ubuntu by `.github/workflows/desktop.yml`; scenarios in docs/desktop/SCENARIOS.md. **W8c** (this change): the app's updater — `tauri-plugin-updater` reads `releases/latest/download/latest.json`, verifies each package's minisign signature against the public key in `tauri.conf.json`, offers «Restart to update» in the menu and the tray (never a modal), installs on the person's click; a `.deb` copy never checks. The release signs each final package (after Authenticode) with `TAURI_SIGNING_PRIVATE_KEY` (vault slot `project-observatory-open-source/prod/TAURI_SIGNING_PRIVATE_KEY`, the `release` environment's secret; made 2026-10-11, no password) and the `feed` job writes `latest.json`, refusing one that misses a platform. **W8b**: the assistant window (Ctrl+Shift+A) — the engine's conversations over `full assistant list|get|ask|job|cancel|delete`, through one command that admits only those actions; model text rendered as text | new | new |
| W9 | **Release and documentation** — the release workflow builds and signs the app per OS; onboarding per OS. **Release jobs** (this change): `release.yml` `windows` (x64 on `windows-latest`, arm64 on `windows-11-arm`: the program signed, NSIS bundled around it and signed through `passioncode-ai/.github/actions/windows-signing@v1` with the signer pinned, installed silently) and `linux` (x64, arm64: AppImage and .deb, the .deb installed); `tools/desktop_release.py` names the files `ProjectObservatory-<version>-<platform>-<arch>…`, writes the PL-10 receipt and refuses an unsigned Windows release whose notes do not say so | new | new |

## Requirements on Windows

- **Python 3.13 or newer.** `os.fchmod` exists on Windows from 3.13; the engine calls it where a
  file it creates must not be read-only. Privacy itself comes from the folder's protected ACL,
  which every file created inside inherits.

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
| Top-level tests | 19 of 133 red: CRLF checkout (inventory, licence and allowlist digests — `.gitattributes`); `pwd` in `maintenance._real_home` (`osprivacy.real_home`); three test fixtures (an environment cleared of `USERPROFILE`, `?` in a file name, LF turned into CRLF on a text pipe to `git hash-object`); the macOS notarization script (skipped on Windows); `env.permissions` read mode bits, which Windows does not keep (W2b: the file's ACL) |

After W2b (PR #187, run 38010490756): every step green — 76/76 modules load, `test_oslocks` 6/6,
`test_osproc` 10/10, `test_osprivacy` 14/14, the CLI, and the top-level tests 133 OK (10 skipped:
the macOS notarization script). Two Windows facts the run taught: SDDL names the built-in
Administrator account `LA` rather than by its SID, and a folder created inside the profile
inherits an OWNER RIGHTS (`OW`) entry — both are compared as the SIDs they stand for.

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
