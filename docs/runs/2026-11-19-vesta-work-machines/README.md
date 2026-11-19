# Work machines on Vesta: backlog rows (2026-11-19)

A board run answering Fabric board request `019b8063` (thread `a7cda3ec`): for each item of the
organization's work-machines plan, one row of [the backlog](../../backlog.md) and a target date.

## What shipped

Docs only: rows OBS-647 to OBS-652 in `docs/backlog.md`, and OBS-200's target date moved from
2026-11-21 to 2026-11-28 (it was not started and depends on OBS-414).

| Item of the request | Row | Target |
|---|---|---|
| G7: enroll links the organization, `org status` counts | OBS-647 | 2026-11-24 |
| Context decided before reading a transcript | OBS-648 | 2026-11-26 |
| G1: agent response time | OBS-649 | 2026-11-28 locally; the batch field waits on fabric-agent-contract |
| G2: organization subscriptions with OE-02 | OBS-650 | 2026-12-01, after OBS-570 |
| G4: sessions outside the organization's folders | OBS-651 | 2026-11-28 |
| G5: the door between key stores | OBS-652 | 2026-12-05 |
| Joining for a non-engineer, key split by owner | OBS-200, OBS-414, OBS-415, OBS-417 | 2026-11-28 |

## Checks run

- `python3 tools/check_backlog.py`: 0 problems (shape patterns only; no local denylist here).
- `python3 -m unittest tests.test_backlog_board`: 84 tests, OK.
- The pre-push privacy gate with the private denylist: passed (it first refused a private
  repository name in a row's Source, now written as "the Vesta project").

## Next

The exact next task: OBS-647 (enroll writes `fabric_org`), then OBS-648.
