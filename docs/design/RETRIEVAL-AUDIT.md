# Retrieval receipts, scoped explain and redaction on the way out

Decision record for PB-137 N-012, 2026-10-05. Memory answers can now be audited after
the fact and explained without searching again. The fields listed under "Redaction on the
way out" carry no secret the workspace knows and no credential shape. A field not listed
there is an identifier, a number or a state.

- **Module:** [`retrieval_audit.py`](../../observatory/engine/retrieval_audit.py).
- **Tool:** `observatory_explain` (scope `memory.search`, effect `read`). It is the
  engine's candidate for the reserved `memory.explain` of `memory/0.1`
  (fabric-agent-contract DEC-0023). The contract names it when the consumer is
  materialized (N-025).
- **Proof:** [`tests/test_retrieval_audit.py`](../../observatory/engine/tests/test_retrieval_audit.py),
  9 tests over the real MCP server and store. Nine planted defects were each caught.

## Receipts

Every `observatory_search` answer carries `receipt: {receiptId, resultBytes}`. The receipt
is one line in `store/logs/retrieval.jsonl` (mode 0600, rotated with every other log). The line holds:

| Field | What |
|---|---|
| `receiptId` | `rcpt_` and 16 hex characters |
| `binding`, `principal` | who asked, from the transport (`local:stdio` for the local agent) |
| `projectId`, `classes` | the scope the search ran in (`classes` is null for the local agent: every class) |
| `policyRevision` | the embedding-policy revision in force |
| `arms`, `namespace` | the arms that produced the results; `lexical:search_notes` |
| `queryDigest` | `hmac-sha256:` of the query under the workspace's fingerprint salt; null when the salt is unreadable, and the answer says so |
| `total`, `abstain`, `floor`, `degraded` | the answer's own counts and the sources that degraded |
| `resultBytes` | the answer's result size in UTF-8 bytes |
| `results` | each `(memoryId, revision)` with how it matched: arms, score, coverage, rank, distance |

What a receipt never holds:
- the query text;
- a statement or a `why`;
- a bearer.

If the log cannot be written, the answer still stands. Its receipt then says
`receiptId: null` and why.

## Explain

`observatory_explain(receiptId)` re-reads the exact revisions the search returned. It does
not run a new query. For each result it says what the record is now:
- `current`;
- `superseded`, with `latestRevision`;
- `expired`;
- `erased`, without its text;
- `no-longer-visible`, without its text, when the caller's binding no longer covers its project or class.

The order of checks is the protection:
1. The caller is identified from the transport.
2. The receipt must be that caller's.
3. Only then is the receipt's project authorized.

A receipt of another caller and an id that does not exist get one answer, `unknown-receipt`,
so a receipt id says nothing about other projects. The local agent is a caller like any
other: it cannot explain a binding's receipt either.

## Redaction on the way out

Records are redacted on the way in (`memory_redact`), but two gaps remain:
- collectors and other writers store text without that pass;
- a workspace learns new secret values after a record was written.

The memory tools therefore run the same two filters on everything they return:
- credential shapes;
- the workspace's known values, replaced by the slot's name.

| Answer | What is filtered |
|---|---|
| `observatory_search`, `observatory_recall`, `observatory_explain` | `statement`, `why`, `evidence_json` and `provenance_json` of every row; the search's echoed `query`; every `degraded` reason |
| `observatory_checkpoint_latest`, `_handoff_get`, `_handoff_accept`, `_workflow_list` | the checkpoint body, the pack, `constraints`, each workflow's `goal` |
| every refusal of the ledger and workflow tools | `detail` |
| `observatory_record` on the way in | `statement`, `why` and `evidence`; the same filters as a checkpoint |

- Each answer carries `redacted`: counts and field paths, never the replaced text.
- Identifiers are never filtered: `wf_…`, `handoff:…`, `leaseId`, `leaseRef`, and the pack's `transcript` pointer, which is a session UUID the shape filter would otherwise eat. A redacted id would break the protocol.
- A full 40-character commit id inside free text is a credential shape and is redacted, as on the way in. Cite a commit by its short form.

## Caches and budget units

- There is no retrieval cache to guard. Search, recall and explain read the store on every call.
- The redactor's known-value cache is keyed by its sources' fingerprint and holds the same values for every caller.
- The size of an answer is reported in UTF-8 bytes (`resultBytes`). A token count would need the reader's tokenizer, which the engine does not have.
