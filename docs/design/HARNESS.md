# The agent harness: what loads into every session, what it costs, and whether it is safe

Status: in progress (started 2026-10-11). Owner: this repository. Request (operator, 2026-10-10): track the
harness on the machine — which skills and MCP servers are installed, how much context they take, what budget
is safe so skills do not overflow the context (it had just happened), signal it; then audit skills for
security — what they run, call and load — without calling every script dangerous; monitoring and alerts on
Observatory's side, with token economy and security first and without hindering the agent's thinking.

The research behind every rule here (sources cited per claim) and the measurement of one real machine are kept
by the operator with the run record; this page carries the decisions and the rules.

## What is measured, and why first

A Claude Code session lists every model-invocable skill by name and description inside a budget of about 1 %
of the context window. Over the budget, descriptions are dropped (the least used first) and those skills can no
longer be matched to a request; the only trace is a debug log. One measured machine offered 638 skills whose
descriptions came to about 263,000 characters against a listing budget of about 30,000 — all but ~35 were
offered by name only. So the first alarm is **loss of function** (descriptions dropped, an instruction file cut,
hook output moved to a file), and only then **cost**.

| REQ | Requirement | Verified by |
|---|---|---|
| H1 | A collector reads, never runs, and only when the operator turns it on (`integrations.harness`, `sources.harness_root` — the home folder whose agent files it reads): skills (plain and plugin), plugins, instruction files, the settings that switch off deferred MCP tools — per harness (Claude Code; Codex where present). No value of any secret is read into the record. | `test_harness` fixtures; the scan starts no process |
| H2 | Each skill: name, source tier, description characters as the listing counts them (`min(len(description + when_to_use), 1536) + len(name)`), files, scripts, a content digest | `test_harness` |
| H3 | The listing budget is a setting (`OBSERVATORY_HARNESS_BUDGET_CHARS`, default 30,000; a settings-page control is later work) and the overflow is computed against it: how many descriptions drop and which ones would (largest first, since usage is unknown to the scan) | `test_harness`: an over-budget fixture names its dropped skills |
| H4 | Findings, loss of function first: `harness.listing_overflow` (warning), `harness.instruction_long` (a single CLAUDE.md/AGENTS.md over 200 lines, info; over 500, warning), `harness.mcp_deferral_off` (a non-Anthropic `ANTHROPIC_BASE_URL` or `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` set: deferred MCP tools are off and every tool schema loads upfront; warning) | `test_harness` rules; labels and Russian text in the gate |
| H5 | Security, deterministic and near-zero false positive (research C-1, C-2, C-4, C-5): hidden Unicode (tag characters, bidi controls) in model-visible text; download-and-execute; a credential source and an egress sink in one script; concealment or override directives in a description. Each hit names the skill, the file and the rule — never quotes a value | `test_harness`: one planted fixture per rule fails, a benign script does not |
| H6 | Provenance weighs every hit: tier 1 is this operator's own repositories or the family's marketplaces, tier 2 a pinned third party, tier 3 anything else. Hidden Unicode and concealment are critical in every tier; credential-and-egress is a warning in tiers 1–2 and critical in tier 3; download-and-execute is info in tiers 1–2 and a warning in tier 3 (most vendors install their own CLI that way); opaque content is info in tiers 1–2 and a warning in tier 3 | `test_harness` |
| H7 | Change, not presence: the first sight of a skill is recorded (its digest); a later change of the digest is `harness.skill_changed` until `full harness --accept KEY` records the new one; a change that adds a security hit is raised at that hit's severity | `test_harness` |
| H8 | Never truncate the scan: a text file over 1 MB, or a binary or archive inside a skill, is itself reported (`opaque-content`) rather than skipped silently | `test_harness` |
| H9 | Benign by default: scripts under a skill's own folder running local interpreters, git, installs into a local environment, credentials read through environment variables or the vault, network calls to the skill's own subject | the benign fixtures produce nothing |
| H10 | Operable: the tick runs the scan; `full harness` prints the summary (budget, top contributors, hits); suppressions go on the record through `ack.py` and come back when the digest changes | CLI test; the board shows the rows |

## Not decided here

- **Hooks and project skills.** Hook declarations (what runs at SessionStart and on every tool call) and a
  project's own `.claude/skills` are the next read; this scan covers the user-level harness.

- **Usage.** Which skills a session actually invokes lives in transcripts; ranking pruning by cost × (1 − usage)
  needs a usage reader and is the next step, not this one.
- **MCP schema cost.** Tool schemas are known only to a running server; this scan counts declared servers (from
  `scan_mcp`) and flags the setting that loads them all upfront, it does not start servers to measure them.
- **A language model as a judge** of description-versus-behaviour mismatch: only as a demote-only adjudicator on
  already-flagged skills, later, and never to clear a critical deterministic hit.
