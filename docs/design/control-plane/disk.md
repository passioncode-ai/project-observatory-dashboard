# The Disk page (M13, OBS-562, CP-16)

## What

Disk and cleanup as their own page, `disk.html` ("Disk"), in the System group after Machine. Its question: how much
space is free, what the guard and auto cleanup removed and keep, and how they are set. The disk guard's last pass, the
largest files, the folders the guard keeps and the cleanup tables moved here from the Machine page; the Machine page
keeps one line (auto cleanup on or off, when the guard last ran) with a link, and the running simulators (they hold
memory, not disk space). Every setting the configuration door (M14, [configuration.md](configuration.md)) declares for
the guard and the cleanup is shown and can be changed from the page.

## Data

- `machine_view.summary()`, as the Machine page reads it: `disk.volume` (free space now), `guard` (the last pass: free
  before and after, pressure, planned classes, `kept`, `largeFiles`), `cleanup` (`autoEnabled`, `counts`, `journal`,
  `guardRuns` and `guardTotals`, the guard's run summaries of 7 days, freed by the disk's own count) and `degraded`
  (the page names the gaps of `machine.json`, `disk-guard.json` and `cleanup-plan.json`).
- `machine.diskSettings`: `configuration_door.listing()` cut by `disk_page.settings_of` to the keys in
  `configuration_door.DISK_SCOPE` (`features.auto_cleanup`, `guard.*`, `cleanup.auto.*`), each row with `value`,
  `default`, `set` (a file sets it) and `file`, plus the listing's `degraded`. Written by `build_dashboard.build()`.
- Free space has no history in the payload: the trend is the last pass (before and after) and the passes of 7 days.

## Wireframe

```
Disk
How much space is free, what the guard and auto cleanup removed and keep, and how they are set.
[Free disk 31.4 GB | Last guard pass 2026-11-10 | Auto cleanup on | Freed in 7 days 11.3 GB]
Auto cleanup is on: the guard and the auto tier run from the maintenance job.
Disk guard ran ...: free 20.1 -> 31.4 GB, 42 removed . pressure low
[Disk guard: what / planned]  [Kept by the guard]  [Largest files]
[Guard passes in 7 days: when / pressure / removed / freed]
[What auto cleanup removes and keeps: what / tier / now / kept for]
[Cleaned in the last 7 days]
Settings
  This page was opened as a file, so it only shows the settings. ...   (tooltip names observatory_configure)
  setting (key) | value | default | source | [number] [Preview] or [Turn on]
[Change preview: label: before -> after . Apply . Cancel]  then  [Applied . Undo]
[Not measured: source . reason]
```

## Contract

- Pages are static HTML. The live route is the key server's, the pattern of the Keys page's actions (`ACTIONS` in
  `tools/keyserver.py`, `call()` in the page script): the server writes its token into the page it serves; the page
  posts `/api/configure` with `X-Observatory-Token`, the same Host, Origin and token checks, and the audit line written
  before the act (key names only). The status server (`serverd.py`) stays GET-only and has no person's actions.
- `POST /api/configure` with `{op: "plan", changes}`, `{op: "apply", digest}` or `{op: "undo", journal}`. Every key
  (of the changes, of the plan the digest names, of the applied change the journal id names) must be in the
  catalogue and in `DISK_SCOPE`, else 400. It calls `configuration_door.plan`, `apply` or `undo` with the actor
  `person:page`, which passes the person's gate as `person:cli` does (`decision: {by: person, via: page}` in the
  journal). The answer never carries a command line. A delegation token is refused for this action: an agent uses
  `observatory_configure`, so `parity.json` records the action as `covered` by that tool.
- The page builds every control disabled with a tooltip naming `observatory_configure`. The page script enables them
  only when `LIVE` (served over http with the token): a control asks for a plan, the page shows each change's
  before and after and whether it is the person's decision, Apply applies that digest, Undo undoes by the journal id.

## Failure behaviour

- Opened as a file, or by a server without the token: the controls stay disabled and the explanation says so.
- A refusal (unknown key, a key outside the scope, an invalid value, a stale or spent plan, an undo after a later
  write) is a 400 with the door's sentence; the page shows "not changed" with it. A fault after the act may have begun
  is shown as unknown, never as failed.
- A settings file the door cannot read: its rows show "unreadable" (never the default) and the file is named under
  Not measured. A guard that never ran says so without a command; a survey not run is named under Not measured.
- No command line is shown on a row of this page.

## Tests

`observatory/engine/tests/test_disk_page.py` (suite `disk_page`): a guard that ran, one that never ran, auto cleanup
off, a degraded source and an unreadable settings file, values with their defaults and sources, disabled controls with
the tool's name, a label for every key the door declares, the navigation and the build, the Machine page's line and
link, and the route (plan, apply, undo as the person; an unknown key, a key outside the scope and a plan made for
another key refused; refused without the token). `tests/test_i18n.py` holds the catalogue complete;
`tests/test_agent_parity.py` holds the action to `observatory_configure`. Scenario: OSS-117.
