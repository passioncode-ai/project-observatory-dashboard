# Access bindings: who may do what with agent memory

Decision record for PB-137 N-007, 2026-10-04. It makes plan §7.2 (Fabric ADR-0026
bindings) machine-checkable. A binding is an immutable record of what one principal may do:
which projects, which scopes, the highest classification it may see, the highest effect it
may have, until when, and how its channel proves it is that principal. Enforcement at every
entry point is N-008. The HTTP transport that carries bearers is N-015/N-016.

- **Contract:** `access-bindings/1`, published as
  [`defaults/access-binding.schema.json`](../../observatory/engine/defaults/access-binding.schema.json).
- **Evaluator:** [`access_binding.py`](../../observatory/engine/access_binding.py). It
  decides and never serves.
- **Accepted cases:**
  [`tests/access_binding_cases.json`](../../observatory/engine/tests/access_binding_cases.json),
  proved by
  [`tests/test_access_binding.py`](../../observatory/engine/tests/test_access_binding.py).
  Eight planted defects were each caught.

## The rules

| # | Rule | Why |
|---|---|---|
| 1 | **Identity comes from the transport.** `resolve` reads only what the server observed: the channel, its own audience and the SHA-256 it computed of the presented bearer. An `owner`, `principal` or `caller` argument never selects a binding. | Every MCP client can type any string. The engine already refuses `owner: operator` over the wire (`mcp/server.py` `_owner_error`). This extends the same rule to every authority. |
| 2 | **stdio is explicit, built in, and not the operator.** `local:stdio` is the operator's local agent, started by the operator's own agent configuration. It reads every project and every class, and its effect stops at `propose`. It cannot be configured, stored or widened. | The plan names stdio `trust=operator`. The engine's long-standing boundary is that operator acts (promote, reject, consent) need a terminal. This keeps both: the local agent sees everything, as today, and decides nothing on the operator's behalf. |
| 3 | **Everything else is denied by default.** An HTTP request needs a bearer whose digest matches exactly one binding, issued for this server's audience (`observatory:<instance>`), unexpired and unrevoked. | No binding, no access. An audience stops a bearer issued for one instance from opening another. |
| 4 | **The registry holds digests, never tokens.** `credentialRef` is `{kind: bearer-sha256, digest}`. One credential maps to one binding. | A leaked registry must not be a leaked credential. A bearer that names two principals names none. |
| 5 | **An HTTP binding names its projects** and always expires. Only `local:stdio` covers every project. | A remote principal that reads across every project is the global scope D-3 and §7.2 exclude. |
| 6 | **Scopes are explicit:** `memory.read`, `memory.search`, `memory.record`, `memory.checkpoint`, `memory.handoff`. An unknown scope, `memory.forget` among them, is denied until it is defined. | A scope added by accident is a privilege added by accident. |
| 7 | **Effect has a ceiling: `read` < `propose`.** The operator's authority is never a binding effect, and a request for it is denied (`effect-above-ceiling`). | Writes through the wire are proposals. Only a person at a terminal promotes them. |
| 8 | **A session binding names its workflows.** A binding with `workflows` (a one-shot binding for a Fabric or Switchboard session) must name the workflow on every request and can touch no other. | A handed-over session continues one piece of work. It does not gain the project. |
| 9 | **Visibility follows the class ceiling.** A record is visible when its classification (plan aliases mapped) is at or below the ceiling. An unknown class is never visible. | Filtering by ceiling before ranking is N-009. This rule is the predicate it uses. |
| 10 | **A binding vouches for its queries at its ceiling.** `query_context` hands embedding-policy/1 an `authority: binding` context classified at the binding's ceiling. A binding that may see `confidential` text therefore never exports its queries, and that includes `local:stdio`. | The query text is the principal's. The most it may read is the most its words may carry. |
| 11 | **A broken registry is refused whole.** A caller then serves stdio alone and denies every HTTP request. | As embedding-policy/1: half-reading an authority file is the one way this could grant more than it says. |

## The refusal envelope

A denial is the wire's existing typed refusal. It carries no `isError` and no credential
material:

```json
{"error": "binding refused", "code": "project-not-bound",
 "detail": "bnd_partner01: project-not-bound",
 "hint": "this binding does not cover that project", "degraded": []}
```

Codes: `no-credential`, `no-binding`, `audience-mismatch`, `binding-revoked`,
`binding-expired`, `unknown-channel`, `project-not-bound`, `project-required`,
`scope-not-bound`, `workflow-not-bound`, `workflow-required`, `effect-above-ceiling` and
`invalid-request`. Each has a fixed hint saying what the caller can do about it.

## What this decision does not do

- It does not issue, store or revoke bindings, and it does not read an HTTP header. The
  operator command that writes `config/access-bindings.json` and the resolution from a
  request are N-008 and N-016.
- It does not filter results. N-009 applies `may_see` before ranking and limits.
- Session handoff across clients (N-018, N-022) builds on rule 8. It does not change it.
