# 2026-10-09 — releases 0.20.0 and 0.20.1

The public record of two releases; the changes are in [CHANGELOG.md](../../../CHANGELOG.md). 0.19.4
is recorded in [the 0.19 run](../2026-10-08-releases-0.19/README.md).

## 0.20.0

Merged as #183 (`f59b0a3`), tag `v0.20.0`; CI green on Linux 3.11/3.14 and macOS 3.14, release run
signed, notarized and published after the `release` approval. Local gate in one run: 226 portable
suites, 132 top-level tests, privacy. Installed on a maintainer machine by `full update --apply`
(0.19.4 → 0.20.0, no rollback).

- The Mac app reads the maintenance record every 15 minutes and badges its Dock icon `↑` while an
  update waits (Swift `UpdatesTests`, red before).
- Health and doctor count the last 10 ticks' outcomes (`store/raw/tick-runs.jsonl`).
- OBS-26, OBS-17, OBS-42 — see the backlog.
- `mcp` 2.3.0 (Dependabot #179, closed as landed): the HTTP transport's SDK pin was re-measured with
  the experiment of 2026-10-04 — [re-pin record](../2026-10-08-http-transport-repin/README.md).

## 0.20.1 — from a final read-only audit of the live machine

The audit's worst finding: **no tick had finished in 13 hours.** With the collectors held back by
0.19.2's reserve, the tail's optional steps still ran before the findings and the dashboard and
used the last of the ceiling — `plugins` 142 s, then `agent` stopped at exactly the ceiling — on 14
ticks in a row. The tail is now split: optional steps (plugins, rollup, agent, index, retention,
sweep, corroborate, ledger, lost) stop two minutes before the ceiling, and the core (merge … findings,
dashboard, notify, registry) uses what is left. Tests in `test_lifecycle.py`, red before.

Also from that audit, each with a test that was red first: Health named a ceiling stop as "killed (a
restart, a sleep, a signal)" — it now reads the supervisor's record; the 3-of-10 alert reached only
the MCP survey — now the card and doctor too; a restored workspace dated every file "now", moving the
tick's daily refreshes — file times now travel with the copy; the maintenance record kept naming the
last automatic update after a person's newer one; the 0.19.3 heading had been overwritten by
0.19.4's version bump — restored, and `test_version_consistency` now refuses a gap.

Still open: OBS-52 — the step deadline is read only by the folder scan; the agent, the leak scan and
git hygiene can still be stopped with nothing written.
