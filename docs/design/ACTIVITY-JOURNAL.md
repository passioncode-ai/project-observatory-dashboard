# The activity journal: units of agent work, their class and their spend (OBS-378)

**Status:** design, 2026-11-12. The event's shape is the contract's: fabric-agent-contract CT-259,
[the activity unit design](https://github.com/ssheleg/fabric-agent-contract/blob/main/docs/evidence/plans/2026-11-12-ct259-activity-unit.md)
(rules `AE-1` to `AE-5`). This document is Observatory's half: what the journal keeps, the capability a kit
writes through, how the North Star number is counted from it, and Observatory's answers to the four questions
the contract's design asks before its DEC. Code follows the DEC (CT-259) and the class map (G1-c).

**Asked by:** the organization's North Star gap table, gap G1 (fabric-workspace `knowledge/north-star.md` at
`adfade60`; board thread `eefa7959`, message `4a65469b`), and line 1.1 of the unified plan (fabric-workspace
`docs/plans/2026-11-11-unified/README.md` at `212cadcb`; board request `e9d30889`, thread `8da128cf`).

## What the metric needs

The North Star metric is verified agent work per person per month: units of work an agent delivered and someone or
something accepted, per task class, per organization, divided by the organization's people. Four sources meet in it:

| Gap | Who owns it | What it brings |
|---|---|---|
| G1-a | the contract (CT-259) | the `unit` event: one transition of one unit, with its agent, class, evidence, acceptance and spend |
| G1-b | the adapter kit | the writer every agent calls when a unit starts, ends, is handed over or is accepted |
| G1-c | the assessment agent | the task-class map and whether an accepted unit counts as verified |
| G1-d | **Observatory (this document)** | the journal that keeps the events and the join that turns them into the number |

## J1. Observatory's answers to the contract's questions

1. **`unit` inside `fabric-activity/0.1`, not a separate document type.** Agreed. One transport, one attribution and
   one person rule; the journal reads a `unit` event like any activity kind, and an older reader stores it unread
   (`AE-5`).
2. **No `org` in the event.** Agreed. The organization comes from where the event came in: a device collector's
   stream attribution, or the project of the capability's binding (J2), whose organization the registry holds
   (`organization` on the project, `registry/projects.json`). An organization named inside an event would be a
   claim the writer could make about anyone.
3. **The capability a kit calls: `activity.unit.record`**, bound exactly as `ops.digest.record` (J2).
4. **The class map's revision is needed.** `taskClass` alone is not enough: the assessment's map will rename, split
   and merge classes, and a month counted under one revision must still read the same after the next. Proposed for
   the DEC: an optional `taskClassMap: {id, revision}` beside `taskClass`, required whenever `taskClass` is not
   `unclassified`. A unit written without it is joined against the map revision in force at its `acceptedAt` and the
   month is marked `class-map-inferred`.

## J2. The capability `activity.unit.record`

A kit with no device collector records its units here (`AE-4`). The shape of the binding is the one
`ops.digest.record` already uses (`observatory/engine/ops_digest_store.py`, OBS-330), so a product that sends its
operations digest needs no second kind of credential:

- **Whose units they are comes from the transport, never from an argument.** Over stdio: the one project of the
  access binding the server runs under, issued with the scope `activity.unit.record` and the effect `propose`
  (`project-observatory full access-binding issue PRINCIPAL --project PROJECT --scope activity.unit.record --effect
  propose --token-file PATH`). Over the memory-http door: the `project` claim of a delegation token Fabric's hub
  minted for `project-observatory` with the scope `activity.unit.record`. Without either the answer is
  `binding-required` or `delegation-required`.
- **The agent must be the project's.** `data.agent` (`id.instance`) must be a service the registry attributes to
  that project (a curated `agent:<id>.<instance>` in `registry/identifiers.json`, or the agent row's source
  repository measured as the project's), as `ops.digest.record` checks `service`; otherwise `agent-not-of-project`.
- **Input:** 1 to 50 `telemetry-event` objects of kind `unit`, each checked against the contract's schema at the
  pinned revision and its `FAC-SEM` rule (`outcome` only on `ended`, `acceptedBy` and `acceptedAt` only on
  `accepted`). An event with a `user` or a `session` is refused (`AE-3`). One bad event refuses the whole call and
  names it by index; nothing is half written.
- **Idempotent by the event id** (`AE-2`): the same event again answers `repeated: true` and changes nothing. A
  second `accepted` for the same unit and stream without a `started` between them is refused (`accepted-twice`).
- **Answer:** `{recorded, repeated, refused: []}`, and on a refusal a typed `error` naming the event.

## J3. What the journal keeps

Owner-only compact JSON lines under `<state>/activity-units/<project slug>/<YYYY-MM>.jsonl`, one file per month of
the event's time, each line the event as received plus `received_at` and `channel` (`stdio`, `http`, `device`).
Kept: the agent, the unit's `{system, key}`, the transition, the class and its map revision, the outcome, the
evidence references (URIs and digests, never content), the acceptance and the cost. Never kept: free text, a token,
a person's name or address. A `person` acceptance keeps only its pairwise membership subject, and only in the
organization's own journal (`AE-3`): it never reaches a summary, a report or another organization.

Bounds per project, each refused with its own code before anything is written, as the operations digest does: at
most 10,000 events a day, 20 MB a month file, and a retention of 400 days (the North Star compares a month with the
same month a year earlier). A file that cannot be read is never overwritten (`store-unreadable`).

## J4. The number, and what it never shows

For one organization and one calendar month (UTC):

- **Accepted units:** the units whose last `accepted` event falls in the month, counted once each (`AE-2`: a unit
  reopened and accepted again counts in the month of its last acceptance).
- **Verified units:** the accepted units the assessment marks verified for their class (G1-c). Until that reading
  exists the number is published as accepted, and labelled so, never as verified.
- **Per class:** grouped by `taskClass` under the map revision of J1.4; `unclassified` is its own row.
- **Spend:** the sum of `cost.usd` over every event of the units counted. One unknown cost (`null`) makes the sum a
  lower bound, published with `partial: true`, never as if complete; an unknown cost is never read as 0.
- **Per person:** the organization's verified (or accepted) units divided by its member count in the month from the
  organization's membership roster. The division is the only place people enter: no unit is ever attributed to, or
  served by, a person, and no cell below the person rule's minimum (`FAC-SEM-067`, at least 3) is served.

Served per organization, project, agent and task class; never per person. The first monthly number comes from the
pilot organization once the kit (G1-b) writes and the map (G1-c) exists.

## J5. Fabric Dashboards

Fabric Dashboards writes its agents' spend and activity into the journal (its FD-165) through the same capability,
under each agent's project binding, so the journal has one door and one set of checks whoever writes.

## Not decided here

- The `verified` reading per class (the assessment agent's, G1-c).
- The kit's writer and when it calls (the adapter's, G1-b).
- The DEC itself, its schema and vectors (the contract's, CT-259).
