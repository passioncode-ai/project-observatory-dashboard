# The Agents page (W5)

**Objective.** OBS-12: show the operator, live, which agents are working, on what, and how
their work moves between sessions, accounts and models. Plan:
[AGENT-MEMORY-PLAN.md → W5](../../design/AGENT-MEMORY-PLAN.md#w5--the-agents-page). Behaviour:
[AGENT-MEMORY.md → What the operator sees](../../design/AGENT-MEMORY.md#what-the-operator-sees).
Scenarios OSS-24 to OSS-28 in
[portable-scenarios.md](../../../observatory/engine/docs/ux/portable-scenarios.md).

## What changed

| Piece | Where |
|---|---|
| the data, read-only, degrading by section | `observatory/engine/agents_view.py` |
| the renderer, shared by the build and the live refresh | `observatory/engine/dashboard/agents_page.py` |
| the page in the shell: Work group, question, overview card, data on its page only | `dashboard/shell.py` |
| payload, template slot, styles, live refresh script | `dashboard/build_dashboard.py` |
| `/agents?locale=…`: the section rendered now | `tools/serverd.py` |
| each checkpoint records the lease that wrote it, so lanes are exact | `store/workflow.py` |
| strings in English and Russian; words with another meaning elsewhere carry a context id (`key-state@@missing`) | `dashboard/locales/*.json` |
| a stacked table's cell labels follow the language switch, here and on the Machine page | `localizeStatic`, `t.attr("data-label", …)` |

## Looked at, not only tested

The page was opened in Chrome on a synthetic workspace with two workflows. One was handed from
one account to another after a limit, with a late write from the old session kept; the other was
silent for 90 minutes. It was checked at 1360 px in English and at 390 px in Russian. What
looking found, and the tests did not:

- a step's status, a duration's unit and a handoff reason passed as arguments stayed in English
  after the language switch. They are now marked elements of their own;
- the stacked table's cell labels stayed in English;
- the word "missing" meant a deleted worktree in the catalog. The page's words now carry a
  context id;
- checkpoints were assigned to an executor's lane by timestamp, and one written in the second a
  handoff was accepted landed in the wrong lane. Each checkpoint now records its lease;
- tile captions wrapped onto two rows; they are shorter now.

The live refresh kept two opened cards open across a refresh, and the console stayed empty.

## Checks run

Checked together on the stack's tip, `4a9600b` (branch `claude/agent-memory-eval`, which carries
W2–W5, OBS-13, OBS-07 and OBS-03), 2026-10-03. They were not checked per branch.

- `python -m unittest discover -s tests` (repository root): 124 tests, OK.
- `python tests/run_portable.py --jobs 4 --timeout 1200` (engine): 209 of 209 suites PASS,
  6206 assertions.
- Two earlier full runs on this stack found five defects, all fixed before this one:
  - the runtime check did not know the stemmer;
  - the indexer failed on an index without search keys;
  - an erasure test assumed the old index timing;
  - `observatory_record` redaction was quadratic: about an hour on a 2 MB statement, now 0.26 s;
  - a fixture used a key name that matched the maintainer's denylist.

  Load averages of 27–41 timed out `write_surface` (the quadratic redaction) and once failed
  `mcp_inventory`, which passes alone.
- Privacy (`tools/check_public_release.py --history --private-denylist`): the lines this stack
  adds name no private identifier once the fixture key and one private repository name were
  neutralised. The gate's remaining findings are in history already on `main`.
- Hosted CI: the pull request's three required rows.

## Next task

W3 second part (OBS-13): the credential findings and the leak scan over agent memory. Then W6.
