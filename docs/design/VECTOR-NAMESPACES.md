# Vector namespaces: one index per model

PB-137 N-005, 2026-10-04. Plan §6 asks for a separate vector index per embedding model, so
that vectors of two models never mix and a query is embedded by the model whose index it
asks. Before this change the engine had one index, `vec_notes`, filled by the configured
OpenAI model.

- **Code:** [`store/namespaces.py`](../../observatory/engine/store/namespaces.py), and
  migration `0010-vector-namespaces` in [`store/migrate.py`](../../observatory/engine/store/migrate.py).
- **Proof:** [`tests/test_vector_namespaces.py`](../../observatory/engine/tests/test_vector_namespaces.py).
  The suite was checked by planting defects: the width check, eligibility, the legacy
  quarantine, completion without activation, and the workflow-kind filter.

## What a namespace is

A namespace is a vector table plus the complete identity of the model that fills it:
`provider`, `model`, `revision`, `weights_sha256`, `tokenizer_sha256`, `dims`, `metric`,
`normalization`, `query_prefix`, `passage_prefix` and `chunker`. A difference in any one
field makes a different namespace. The identity's digest (`model_key`) is unique, and a model
without pinned weights is refused.

| State | Meaning |
|---|---|
| `legacy` | The pre-namespace `vec_notes` (OpenAI `text-embedding-3-small`, 1536 dimensions), registered by migration 0010. It answers only where embedding-policy/1 allows. It can be retired and is never activated again. |
| `inactive` | Registered; its table exists and is empty. |
| `backfilling` | Being filled. A cursor in the registry says how far. |
| `ready` | Every current record it may hold is embedded. It still answers nothing. |
| `active` | The namespace that answers. At most one at a time. |
| `retired` | Kept for the record. It is neither filled nor queried. |

## The rules

1. **Table names come from integers.** A namespace's table is `vec_ns_<id>`. The id is the
   registry's own row id, and no provider or model string becomes an SQL identifier.
2. **Vectors never mix.** A vector of a different width than the namespace's dimensions is
   refused, and nothing in the batch is written.
3. **A backfill is resumable.** It walks the current, untombstoned records in ledger order
   from the namespace's cursor. Each batch's vectors and the new cursor commit in one
   transaction, so a crash leaves the previous state usable and the next run continues.
4. **Eligibility is decided per record.** For a remote model that decision is the
   embedding-policy verdict, and a refused record is skipped, never sent. Checkpoints,
   handoff packs and refused steps are not held until they are chunked (N-011).
5. **A backfill never activates.** Full coverage moves a namespace to `ready`.
   `activate` needs a receipt naming the model admission (N-004), the scope-filter
   acceptance (N-009) and the abstention calibration (N-010). It refuses otherwise, and it
   always refuses `legacy`.

## What a person and an agent see

`full doctor` lists every namespace under `vector_namespaces`, with its provider, model,
revision, dimensions, state and how many records it holds. It reports names and counts,
never vectors. Search is unchanged by this decision: no namespace is active, and agents'
queries stay lexical (N-003).
