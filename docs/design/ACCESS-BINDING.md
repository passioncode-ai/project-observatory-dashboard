# Access bindings: who may do what with agent memory

Decision record for PB-137 N-007, 2026-10-04. It makes plan §7.2 (Fabric ADR-0026
bindings) machine-checkable. A binding is an immutable record of what one principal may do:
which projects, which scopes, the highest classification it may see, the highest effect it
may have, until when, and how its channel proves it is that principal. Enforcement at every
entry point is N-008 (section "Enforcement" below). The HTTP transport that carries bearers
is N-015/N-016.

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
| 9 | **Visibility follows the class ceiling.** A record is visible when its classification (plan aliases mapped) is at or below the ceiling. An unknown class is never visible. | Search applies it inside its candidate query, before ranking and limits (N-009). |
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

## Enforcement (N-008)

[`memory_access.py`](../../observatory/engine/memory_access.py) is where the engine asks,
once per call, the same way at every door. It is proved by
[`tests/test_memory_access.py`](../../observatory/engine/tests/test_memory_access.py)
against the real MCP server module and a real store.

| Door | What it checks |
|---|---|
| `InteropServer.call_tool` (`mcp/capability_tools.py`) | Every tool outside the memory family, Fabric capabilities included, serves the local stdio agent only. A binding gets `local-only` before any handler runs. |
| `InteropServer.read_resource` | Resources are local-only too. |
| `observatory_search`, `observatory_recall` | Scope `memory.search` / `memory.read`, effect `read`, the named project. A binding reads only the classes at or below its ceiling: `recall` filters in SQL, so `total` agrees with the page. `search` filters project, class and validity inside its candidate query, before the window (N-009). |
| `observatory_explain` | Scope `memory.search`, effect `read`. The caller is identified first, and the receipt must be its own before its project is authorized ([RETRIEVAL-AUDIT.md](RETRIEVAL-AUDIT.md)). |
| `observatory_record` | Scope `memory.record`, effect `propose`, `owner` = the binding's principal. A correction (`memoryId`) must be a record of the authorized project. |
| `observatory_workflow_list` | Scope `memory.read`. A session binding lists only its own workflows, and `total` counts only those. |
| `observatory_checkpoint_write`, `_latest` | Scope `memory.checkpoint`. A new workflow is authorized on the project it names. A continued one is authorized on the project it already belongs to, read from the store. |
| `observatory_handoff_create`, `_accept`, `_get` | Scope `memory.handoff`, on the project of the handoff's workflow, read from the store. |

The rules the doors share:

- **The transport sets the channel.**
  - `mcp/server.py` is the stdio server, and it calls `memory_access.serve_stdio()` at import. A call with no channel of its own is therefore the local agent.
  - A process that serves HTTP calls `serve_http()`. It then runs each request inside `memory_access.channel(memory_access.http_channel(bearer=…, fabric_projects=…))`.
  - With no channel set, a request is refused `unknown-channel`. It is never served as stdio.
- **The target decides the project.** To a binding, a workflow or handoff outside it and one that does not exist get one answer, `target-not-bound`. It is given before owner, effect or scope are checked, so no other code can tell the two apart. "Outside it" means another project, or a workflow a session binding does not name. The local agent still gets the store's own `UnknownWorkflow`.
- **A record above the caller's class ceiling** is refused like a missing one (`project-not-bound`) when a binding tries to correct it, before the ledger could name its owner.
- **Search hides workflow memory without its scope.** Checkpoints need `memory.checkpoint`, and handoff packs need `memory.handoff`. Otherwise they are left out of the candidate query, as the workflow tools would refuse them.
- **An HTTP process stays one.** After `serve_http()`, a later `serve_stdio()`, for example from importing `mcp/server.py` again, changes nothing.
- **A binding writes as its principal.** Over HTTP, `owner` must equal the binding's principal (`owner-not-principal`). Idempotency records are kept per binding, so another binding that repeats a key and a request starts new work and never receives the first answer's `leaseId`.
- **Workflow memory is `project-internal`.** A binding whose ceiling is `public` is refused the workflow tools (`class-ceiling-below-workflow`). It is never handed a filtered pack.
- **Fabric narrows, never widens.** This is F-015, fabric-agent-contract DEC-0023.
  - With `X-Fabric-Projects`, the binding's projects are intersected with the header.
  - A header naming none of them is refused `project-not-bound`.
  - A malformed header is refused `fabric-projects-invalid`.
  - Without the header the call is workspace-level, and the binding applies as issued.
- **Revocation and expiry take effect on the next call.** The registry is read on every HTTP call.
  - The workspace records the highest revision it applied in `store/access-bindings.seen.json`.
  - A lower revision, such as a restored backup, is refused whole (`registry-unreadable`).
- **Every decision about a binding is journalled** in `store/logs/access.jsonl` (mode 0600).
  - Each line carries the binding, principal, tool, scope, effect, project, workflow, verdict and reason.
  - It never carries a bearer or its digest.
  - The local agent's allowed calls are not journalled; its refusals are.

Codes added by enforcement:
- `target-not-bound`
- `registry-unreadable`
- `local-only`
- `owner-not-principal`
- `fabric-projects-invalid`
- `class-ceiling-below-workflow`

They use the same envelope as the evaluator's codes.

### The acceptance race, observed and closed

`handoff_accept` reads the vault for the declared credentials before its write
transaction, because the vault is files and the store is not. The previous executor
keeps its lease until the acceptance commits, so it can write a checkpoint that declares
other keys inside that window.

Reproduced in `tests/test_workflow_memory.py` (`Credentials.test_the_answer_carries_the_declaration_it_evaluated`): a vault read
that writes such a checkpoint made the answer carry the old declaration's credentials
beside the new checkpoint. N-001 did not cover this, and the race was **observed**, not
hypothetical.

The fix:
- Inside the transaction, before any write, the declaration in force is compared with the one evaluated.
- On a difference the transaction rolls back and the vault is read again, up to `DECLARATION_ATTEMPTS = 3` times.
- If it is still moving, the acceptance is refused with `DeclarationMoved`, and nothing is accepted.

### The operator's command

```
project-observatory full access-binding show [--json]
project-observatory full access-binding issue PRINCIPAL --project PROJECT --token-file PATH
project-observatory full access-binding revoke BINDING_ID
```

`issue` also takes `--project` again, `--scope`, `--class`, `--effect read|propose`,
`--workflow`, `--days` and `--audience`.

- `issue` and `revoke` need a terminal, like `embedding-policy` and `review.py`. There is no `--yes`.
- `issue` writes the bearer only to a new owner-only file. The bearer is never printed or logged, and the registry keeps its SHA-256.
- Defaults for `issue`: scopes `memory.read` and `memory.search`, class `project-internal`, effect `read`, 30 days (365 at most), and this workspace's audience.
- `full doctor` names the state under `access_bindings`, read-only. The Health page shows the same as «доступ к памяти».

## What this decision does not do

- HTTP is served by `full memory-http` since N-016 ([HTTP-TRANSPORT.md](HTTP-TRANSPORT.md#the-service-n-016)):
  it calls `serve_http()` and runs each request inside `channel()` above.
- The vector arm's nearest-neighbour search is not filtered before the KNN: the legacy index
  has no metadata to filter on. It runs only under a consent, and its hits pass the same
  canonical recheck and say so in `degraded` (`vector-window`). Filtering before the KNN
  comes with an admitted local model's namespace (N-006, deferred under OBS-35).
- Session handoff across clients (N-018, N-022) builds on rule 8. It does not change it.
