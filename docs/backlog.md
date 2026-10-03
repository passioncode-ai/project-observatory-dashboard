# Local backlog

This is the canonical status source for the tasks below. Keep stable IDs, close with a
receipt in Source, and retain closed rows. The workspace derives its common backlog
from [backlog-sources.json](backlog-sources.json). Dated handoffs remain historical evidence.

| ID | Item | Status | Source |
|---|---|---|---|
| OBS-01 | Re-collect or reinterpret older receipts immediately after an engine update | open | [0.10.0 release follow-up](runs/2026-10-01-release-0.10.0/README.md) |
| OBS-02 | Agent memory: a checkpoint per workflow step, one executor per workflow by lease token, and immutable handoff packs the engine assembles, over MCP | in review | [agent memory design](design/AGENT-MEMORY.md); [run record](runs/2026-10-03-agent-memory-workflows/README.md) |
| OBS-03 | Agent memory search: index only the latest revision of each record, a similarity floor with an explicit "nothing found", a field-level index of checkpoint bodies, Russian and English word forms, the lexical index written in the same transaction as the record | open | [agent memory design → Not built yet](design/AGENT-MEMORY.md#not-built-yet) |
| OBS-04 | Local multilingual embeddings chosen by measurement on the evaluation set, one index per model, the external provider per project only and never for private classes | open | [agent memory design → Not built yet](design/AGENT-MEMORY.md#not-built-yet) |
| OBS-05 | Per-caller access bindings for memory: read scopes by project and classification, redaction on output, checked on every call | open | [agent memory design → Not built yet](design/AGENT-MEMORY.md#not-built-yet) |
| OBS-06 | MCP Streamable HTTP on loopback as a `fabric-service`, with a binding per caller, so a local host other than a stdio child can reach agent memory | open | [agent memory design → Not built yet](design/AGENT-MEMORY.md#not-built-yet) |
| OBS-07 | Memory evaluation set: recall@5 and MRR in Russian and English, abstention, handoff continuation, injected instructions, forgetting, freshness | open | [agent memory design → Not built yet](design/AGENT-MEMORY.md#not-built-yet) |
