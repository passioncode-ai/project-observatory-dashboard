# Desktop app for Windows and Linux — scenarios

The Windows and Linux app (`desktop/`, Tauri 2, docs/design/WINDOWS-LINUX.md W8) does what the macOS app
does: the same persona P-01, the same jobs, the same engine protocol (`observatory-assistant/1`). Its
scenarios are the macOS ones, [SCN-001–011](../macos/SCENARIOS.md); this page records only what differs on
these systems and which module delivers each. A scenario is "shipped" here only when a test or a recorded
walkthrough on that system covers it.

| ID | Difference on Windows and Linux | Module | Status |
|---|---|---|---|
| SCN-001 | The engine is looked for where README → Install puts it: Windows `%LOCALAPPDATA%\project-observatory-venv\Scripts\project-observatory.exe`, then `~\.local\bin\project-observatory.exe`; Linux `~/.local/share/project-observatory-venv/bin/project-observatory`, then `~/.local/bin/project-observatory`. The workspace defaults to `%LOCALAPPDATA%\project-observatory-full` (Windows) or `~/.local/share/project-observatory-full`. No engine: «Observatory is not installed yet» with the path, Settings and the installation guide | W8a | draft |
| SCN-002–004, 006 | The assistant window opens with Ctrl+Shift+A or the menu instead of ⇧⌘A | W8b | not built |
| SCN-005 | Settings (menu → Settings, Ctrl+,): program and workspace with the system's file picker, Save and check connection; a change reloads the dashboard for the new workspace and drops late answers of the old one | W8a | draft |
| SCN-007 | Opening the app shows the dashboard in its own window: the live server for THIS workspace, else the saved pages | W8a | draft |
| SCN-008 | Saved pages carry a banner with their build time and **Start server** (an installed always-on server restarts through its Task Scheduler task or systemd unit; otherwise the detached `full open --serve` start); no pages → **Build the dashboard**; a port held by another workspace is named and Start is disabled | W8a | draft |
| SCN-009 | Closing the window keeps the app in the notification area (tray); the tray's Open, a second launch, or the taskbar brings the window back; Quit is in the tray and the File menu | W8a | draft |
| SCN-010 | Updates come through the app's own updater (a minisign-signed package named by the release's `latest.json`, fabric-workspace platforms.md PL-04); **Restart to update** in the menu and the tray, never a modal | W8c | not built |
| SCN-011 | The interface follows the system language (Russian for `ru`/`ru-*`, else English); Settings → Language overrides it; the dashboard's own `observatory.locale` follows | W8a | draft |

Navigation stays inside this workspace's pages, as on macOS: other sites and `mailto:` open in the default
browser or mail app, and any other scheme is refused, so a page cannot launch a program.
