# One tooling inventory for the page and the tool (OBS-600, CP-05)

Status: built 2026-11-17, wave R2 of [refactor-plan.md](refactor-plan.md).

## What

The Installed tools page (`dashboard/tools_page.py`) and `observatory_machine(section="skills")` read one inventory,
built by one function: `tooling_view.summary`. Before this change the page drew `tooling_view.build` (agents with
versions, skills with their source tier, plugins, hooks, and the engine's `harness.*` findings beside each item),
while the tool answered `machine_view.skills_section` (the raw rows and the scan's own `plugin-behind` findings), so
an agent saw no tier, no finding beside an item, no hook and no agent version, and its counts meant other things.

## Data

- `machine_view.skills_read()` reads `store/raw/harness.json` once into a section (spaces, scan findings, counts,
  `open`, `degraded`); `machine_view.skills_page(section, limit, cursor)` pages it (one window: scan findings, then
  each space's plugins, then its skills; `offset@stamp` cursors). `skills_section` is now these two in a row.
- `tooling_view.build` also returns `spaces`: the section's spaces with each row replaced, one for one and in its
  order, by the row the page draws (`tier`, `findings`). A row naming no coding agent is kept and named in `degraded`
  (`inventory`, `no-agent`), never dropped silently.
- `tooling_view.summary(findings, limit, cursor)`: the page passes the open findings it carries; the tool passes none
  and the function reads `registry/findings.json` without the acknowledged rows (`open_findings`), the same set.
  With `limit` it adds `skills`, a page of the enriched rows, and a refused cursor joins the same `degraded`.

## Contract (the tool's answer, fields added, none removed or renamed)

```
{measuredAt, degraded,                       # the page's degraded, plus a refused cursor
 skills: {spaces[{space, home, plugins[], skills[]}], page, counts{skills, plugins, findings}, findings, open},
 tooling: {agents[{id, name, installed, version, findings}], hooks[{..., agent, findings?}],
           other[], counts{agents, plugins, behind, skills, hooks, findings}}}
```

Each plugin and skill row in `skills.spaces` now also carries `tier` (1 own code, 2 pinned marketplace, 3 third
party; left out when not recorded) and `findings` (left out when empty). `skills.counts` keeps its meaning (rows and
the scan's own findings); `tooling.counts` is the page's tiles, the same numbers.

## Failure behaviour

No scan, an unreadable one or one before the inventory: the section's own `degraded` entry, empty lists, an empty
page, as before. Findings not built: `degraded` names `findings` / `not-built` in both answers. A cursor of an
earlier scan, a malformed one or one past the end: `invalid-cursor`, as before.

## Tests

`tests/test_tools_page.py` class `OneInventory`: one fixture (a harness scan and a findings register with one
acknowledged row) builds the page's view and walks every page of the tool at three rows a page; agents, every row
with its tier and finding ids, hooks, other findings, counts and `degraded` sources match, the silenced finding is in
neither, the published fields stay, and unbuilt findings are named by the tool as on the page. The `machine` suite's
`Skills` class still pins the paging, the size budget and the unread cases.
