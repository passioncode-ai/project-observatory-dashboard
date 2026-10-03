# Local backlog

This is the canonical status source for the tasks below. Keep stable IDs, close with a
receipt in Source, and retain closed rows. The workspace derives its common backlog
from [backlog-sources.json](backlog-sources.json). Dated handoffs remain historical evidence.

| ID | Item | Status | Source |
|---|---|---|---|
| OBS-01 | Re-collect or reinterpret older receipts immediately after an engine update | open | [0.10.0 release follow-up](runs/2026-10-01-release-0.10.0/README.md) |
| OBS-02 | Agent memory: a checkpoint per workflow step, one executor per workflow by lease token, and immutable handoff packs the engine assembles, over MCP | done | merged in [#127](https://github.com/passioncode-ai/project-observatory-dashboard/pull/127) as `d6f2b33`, required checks green on all three rows; [agent memory design](design/AGENT-MEMORY.md); [run record](runs/2026-10-03-agent-memory-workflows/README.md). Not released yet |
| OBS-03 | Agent memory search: index only the latest revision of each record, a similarity floor with an explicit "nothing found", a field-level index of checkpoint bodies, Russian and English word forms, the lexical index written in the same transaction as the record | open | [agent memory design → Not built yet](design/AGENT-MEMORY.md#not-built-yet) |
| OBS-04 | Local multilingual embeddings chosen by measurement on the evaluation set, one index per model, the external provider per project only and never for private classes | open | [agent memory design → Not built yet](design/AGENT-MEMORY.md#not-built-yet) |
| OBS-05 | Per-caller access bindings for memory: read scopes by project and classification, redaction on output, checked on every call | open | [agent memory design → Not built yet](design/AGENT-MEMORY.md#not-built-yet) |
| OBS-06 | MCP Streamable HTTP on loopback as a `fabric-service`, with a binding per caller, so a local host other than a stdio child can reach agent memory | open | [agent memory design → Not built yet](design/AGENT-MEMORY.md#not-built-yet) |
| OBS-07 | Memory evaluation set: recall@5 and MRR in Russian and English, abstention, handoff continuation, injected instructions, forgetting, freshness | open | [agent memory design → Not built yet](design/AGENT-MEMORY.md#not-built-yet) |
| OBS-08 | Agent memory, review findings H-01…H-12: lean listings, latest-revision search, project-scoped workflows, handoff entitlement, session-bound acceptance, identifiers without values, body-free export, no erasure into a dead end, rotation-aware redaction | in review | [plan → W1](design/AGENT-MEMORY-PLAN.md#w1--correctness-of-what-shipped-review-findings) |
| OBS-09 | Agents find and recover their work: list workflows, self-handoff after a lost token, the operator's `full workflow` commands, kept steps named in the review queue | in review | [plan → W2](design/AGENT-MEMORY-PLAN.md#w2--agents-can-find-and-recover-their-work) |
| OBS-10 | Credentials through Observatory: workflows declare credentials by name and the pack verifies them, slot-naming redaction, `--vault-only`, `use_secret serve` for agent services with rotation naming its consumers, the rule in the skill and the design | in review | [plan → W3](design/AGENT-MEMORY-PLAN.md#w3--credentials-through-observatory) |
| OBS-11 | Sessions linked to workflows: the Stop hook records the workflow, the session history reads its own records, stalls are derived | in review | [plan → W4](design/AGENT-MEMORY-PLAN.md#w4--sessions-linked-to-workflows) |
| OBS-12 | The Agents page: counters, needs-you, workflows by project, a lane view of executors and handoffs, sessions, live polling | open | [plan → W5](design/AGENT-MEMORY-PLAN.md#w5--the-agents-page) |
| OBS-13 | Credential checks that report: a finding when a known value reaches agent memory, the Observatory store in the leak scan, a finding for an agent project's secrets outside the vault, and for a run that fell back to a `.env` | open | [plan → W3 (S-04, S-05, S-08)](design/AGENT-MEMORY-PLAN.md#w3--credentials-through-observatory) |
