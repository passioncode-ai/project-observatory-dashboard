# Embedding policy: which memory text may leave the machine

Decision record for PB-137 N-002, 2026-10-04. It applies the operator's decision D-3 of
2026-10-03: embeddings come from a local model by default; a remote model is used for a
project only after an explicit consent; private text never leaves. This page fixes what
"explicit", "project" and "private" mean in code, so the enforcement (N-003) has a contract
to implement rather than a sentence to interpret.

- **Contract:** `embedding-policy/1`, published as
  [`defaults/embedding-policy.schema.json`](../../observatory/engine/defaults/embedding-policy.schema.json).
- **Evaluator:** [`embedding_policy.py`](../../observatory/engine/embedding_policy.py). It
  decides and never sends.
- **Accepted cases:**
  [`tests/embedding_policy_cases.json`](../../observatory/engine/tests/embedding_policy_cases.json),
  proved by
  [`tests/test_embedding_policy.py`](../../observatory/engine/tests/test_embedding_policy.py).
  N-003 must reproduce every case at the real call sites.

## The rules

| # | Rule | Why |
|---|---|---|
| 1 | **No policy, no export.** A workspace without the policy file consents to nothing. This includes a workspace that embedded through OpenAI before this contract existed. | A key in the configuration is a capability, not a consent. Treating it as consent would make the old default silently permanent. |
| 2 | **A consent names exactly one project** (`project:<slug>`), one provider and one model, and lists the classes it covers. There is no global or wildcard consent. | D-3 says "for a project". A consent that covers every project is the default it was meant to replace. |
| 3 | **`confidential` never leaves**, consent or not, and cannot be listed in a consent. The plan's `private` and `secret-adjacent` map to `confidential`, its `internal` to `project-internal`. | The store has one column, `classification` (plan §13). Two vocabularies would drift. |
| 4 | **A consent says where the texts go.** Its statement must name the provider, and it is given by a person at a terminal (`via: terminal`), never through a tool call. | "Texts of project X are sent to OpenAI" is what the operator agreed to. An agent cannot consent on the operator's behalf. |
| 5 | **Revocation stops new export at once** (`revokedAt` set). Texts already sent cannot be recalled; vectors made under the consent are not served after revocation (N-003, N-005). | A withdrawn consent that still answers queries from the provider's vectors is not withdrawn. |
| 6 | **A consent covers one model.** A different configured model or provider is `provider-mismatch` and stays local. A local model never needs a consent (`local-model`). | The plan keeps one index per model. A consent for one vendor's model is not a consent for the next one. |
| 7 | **No project, no export.** A record without a project, a `global` scope, an `agent-private` scope, or an unknown or missing classification stays local. | Each of these is a context the consent cannot be checked against. Deny by default. |
| 8 | **Work in progress stays local:** `checkpoint`, `handoff` and `step_result`. | A checkpoint is written after every step and carries the constraints of the next one. AGENT-MEMORY.md already keeps it lexical-only until a local model indexes it. |
| 9 | **A query is exported only under a trusted context.** The classification of a query counts when the server derived it: `binding` (the caller's authenticated binding, N-007) or `operator-cli` (a person at the operator's terminal). What a tool argument claims is `caller-argument`, and it authorizes nothing. | A query carries the asker's words. The asker's own claim about them is the thing the binding exists to replace. |
| 10 | **A broken policy is refused whole.** Any defect raises `PolicyError`, and the caller then treats the workspace as consenting to nothing and reports it in `degraded`. | Half-reading a consent file is the one way this design could export more than it says. |

`policyRevision` is raised on every change. N-003 refuses a policy whose revision is lower
than the last one it applied, so a restored old file cannot re-open a revoked consent.

## Reason codes

A local verdict always carries its reason. These codes are stable, and N-003 logs them and
counts them in `degraded`:

| Code | Meaning |
|---|---|
| `remote-consented` | the only remote verdict |
| `local-model` | the configured model is local |
| `no-configured-model` | the caller passed no configured model |
| `no-consent` | no consent for this project (including no policy at all) |
| `consent-revoked` | the consent was withdrawn |
| `provider-mismatch` | the consent is for another provider or model |
| `class-not-consented` | the class is not in the consent's list |
| `confidential` | `confidential`, `private` or `secret-adjacent` |
| `unknown-classification` | missing or unrecognized classification |
| `no-project` | no project, or not a `project:<slug>` id |
| `global-context` | `global` scope |
| `agent-private-scope` | `agent-private` scope |
| `workflow-kind` | `checkpoint`, `handoff` or `step_result` |
| `untrusted-authority` | the query context did not come from the server |

## What changes when N-003 enforces this

- **The indexer.** Today it embeds every non-workflow record through the configured OpenAI
  model whenever embeddings are enabled. After N-003 it asks `for_record` first, so in a
  workspace without consents nothing new is sent.
- **The query path.** Today `observatory_search` embeds the query remotely. After N-003 an
  MCP call carries `caller-argument` until bindings exist (N-007, N-008), so its query stays
  local and lexical. Semantic search comes back with the local model (N-004 to N-006) or with
  a binding. That is the intended price of rule 9, not a regression to work around.
- **Legacy vectors.** Vectors already made through OpenAI stay in their model's index. They
  are served only for a project whose consent covers that model, and they are never sent
  again.
- **The policy file** lives in the workspace (machine-local, not in git). It is written only
  by an operator command at a terminal, which N-003 adds. No MCP tool writes it.

## What this decision does not do

- It sends nothing, reads no key and spends no budget. That is N-003.
- It does not choose the local model. That is N-004.
- It does not define bindings. That is N-007. `binding` is the name of the trusted authority
  N-007 must produce.
