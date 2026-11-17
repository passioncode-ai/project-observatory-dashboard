# Built-in agent panel (M11, OBS-560, CP-19)

## What

One page of the dashboard, Assistant (`assistant.html`, Work group after Findings), carries the built-in agent: the
read-only assistant's chat, and beside it the notices of the two use-case agents, the daily doctor (M9) and the
security watch (M4, M5, M6, M8). No new capability: every control reaches a function an MCP tool already calls, through
the key server, the way the other pages' buttons do (rule R33 O8 of the agents database: a function a person reaches is
first an agent's capability, with the same data and effect).

| Part | Where |
|---|---|
| Page section | `observatory/engine/dashboard/agent_panel.py` (`agent_panel_html`), declared in `page_table.py` |
| Page script | `build_dashboard.py`, the `agentPanel` block |
| Key server routes | `observatory/engine/tools/keyserver.py`: `act_assistant`, `act_doctor` |
| Readiness and spend | `agent/assistant.py` `status()` gains `ready`, `blocked_by`, `spend`; `agent/providers.py` `local_spend()` |
| Tests | `observatory/engine/tests/test_agent_panel.py` (suite `agent_panel`); `tests/test_agent_parity.py` |

## Data

- Built into the page: the doctor's view (`daily_doctor.agent_view`, what `observatory_doctor` reads) and the open
  findings of the security group (`finding_types.group_of`). Neither rides on a page's data (`shell.slice_for`).
- Read live only: the assistant's status and its conversations. Conversations are private to the workspace
  (`store/assistant/`), so none is written into a built page.
- `observatory_assistant_status` gains three fields (added beside, R28): `ready`, `blocked_by` (the code `ask` or its
  runner would refuse with: `agent-disabled`, `provider-unconfigured`, `model-unconfigured`, `budget-unset`,
  `budget-reached`) and `spend` (`denomination`, `today`, `month`, `daily_ceiling`, `monthly_ceiling`, from this
  project's own journal and its model configuration, never the provider). An unreadable journal is `null` and named
  in `degraded`.

```
Assistant                                       Ask about the estate, and read what ...
+-- Ask about the estate ------------------------------------------------------------+
| The assistant is ready.                                                            |
| Conversation [ New conversation v ]  [New conversation]                            |
|  Q  Which projects have unpushed work?                                             |
|  A  ... next steps ... > Evidence: 3   Cost of this answer: 0.0021                 |
| Your question [______________________________________________]  [Ask] [Stop]      |
| Each question is paid from the engine's model budget: 0.02 of 1 credits today.     |
+------------------------------------------------------------------------------------+
+-- The daily doctor ----------------------------------------------------------------+
| Last run 2026-11-16 03:00 UTC.  <summary, only when it changed>                    |
| Fixes that can be undone:  Temporary files ...   2   2026-11-16   [Undo]           |
| What it only reports:      The workspace backup ...  reported  1  a finding of ... |
| Not read: harness                                                                  |
+------------------------------------------------------------------------------------+
+-- The security watch --------------------------------------------------------------+
| Open security findings: 5      critical: 1 . warning: 4                            |
| Keys and secrets 1 . Supply chain 2 . Dependencies 1 . Integrations 1              |
| Open the security findings  -> findings.html?group=security                        |
+------------------------------------------------------------------------------------+
```

## Contract

`POST /api/assistant` on the key server (token, same origin, as every action):

| `op` | Body | Answer | MCP twin |
|---|---|---|---|
| `status` | none | the status without `workspace`, `next`, `key_source`, `provider_status`, `projects` | `observatory_assistant_status` |
| `ask` | `question`, `request_id`, `conversation_id?` | `{job, conversation_id}` | `observatory_assistant_ask` |
| `job` | `id` | `{job: {id, status, updatedAt, error?}}` (no result: the answer is read from the conversation) | `fabric.job.get` |
| `cancel` | `id` | the same, after the cancel | `fabric.job.cancel` |
| `conversation` | `id` | the conversation | `observatory_assistant_conversation` |

`POST /api/doctor` with `{op: "undo", journalId}` answers `{undone, refused, degraded}`, the function
`observatory_doctor(action="undo")` calls. A refusal is the function's own code in `error`: 400, or 404 for
`unknown-job` and `unknown-journal-id`. `job` and `cancel` answer only for the assistant's jobs (`agent.ask`). Parity
entries `assistant` and `doctor` in `parity.json` name the tools.

## Failure behaviour

- Opened as a file (no key server, no token), every control is built disabled with a tooltip naming the agent's tool,
  and the page says the assistant answers when the local control server serves it.
- Ask is disabled while `ready` is false, with the reason as its sentence; a refused ask starts no job and leaves the
  question in the box. A repeated request id is the same job (the assistant's own de-duplication).
- The question never reaches the key server's journal: an ask is audited with the conversation id only, a cancel with
  the job id, an undo with the journal id. A malformed or key-shaped question is refused before the audit line.
- A full disk is `disk-full`; any other write failure is an unknown outcome (5xx), shown as such.
- A doctor that never ran says so without a command; its degraded reasons are shown by source alone, because a reason
  can carry a command line.
- Answers are written as text into the page, never as markup.

## Tests

`test_agent_panel.py`: the page built disabled with the cost note and without a command line, in English and Russian;
the doctor's fixes with their undo and what it only reports; a doctor that never ran; the security count by area and
severity with its link, and every security type in an area; the page in the Work group, built with its section, its
notices on no page's data, and its script reaching only the `assistant` and `doctor` actions; the readiness codes and
spend, an unreadable journal named; the key server's status without a path or a command, a refused ask that starts
nothing, ask then job then conversation with the question kept out of the journal, job and cancel for the assistant's
jobs only, malformed input and a key in the question refused, an undo and its refusals, and no token refused.

## Not done here

Streaming answers (the assistant answers whole, as a job); a doctor `run` button (an agent runs it with
`observatory_doctor`, the maintenance job daily); notifications pushed to the page between builds (the doctor's and the
security watch's notices are as fresh as the last build).
