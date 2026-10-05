# Forget: withdrawal, erasure and the receipt

Decision record for PB-137 N-013, 2026-10-05.

- **Module:** [`store/forget.py`](../../observatory/engine/store/forget.py).
- **Operator command:** `project-observatory full forget MEMORY_ID --why TEXT`. Add `--plan` to read without changing anything.
- **Proof:** [`tests/test_forget_receipt.py`](../../observatory/engine/tests/test_forget_receipt.py), 8 tests. Six planted defects were each caught.

## Two acts

| Act | What it promises | How |
|---|---|---|
| **Withdrawal** | the record is unreadable: gone from every read, the lexical and vector indexes and every search | `ledger.tombstone` (unchanged), also run by retention |
| **Erasure** | on top of that, the record's text is unrecoverable everywhere this machine can reach, and the places it cannot reach are named | `store/forget.py`, the operator's act at a terminal |

Withdrawal stays the default, and `store/retention.py` still says why: the audit trail was
chosen over forgetting. Erasure is the exception for text that must not survive, such as a
secret pasted into a note, a person's data, or a client's terms.

Erasure keeps the trail without the text. Every revision of the record keeps its id,
revision, kind, project, owner, times and class. Its `statement` becomes `[forgotten]`, and
its `why`, body and evidence are cleared. So the ledger still says that something was
recorded, by whom and when, and that it was erased, by whom and why. It no longer says what.

## The receipt

Every backend the text can reach is named, with one status. The statuses are `erased`,
`absent` or `withdrawn`; or `retained` or `unverified`, each with its reason.

| Backend | What erasure does |
|---|---|
| `tombstones` | every revision tombstoned, which is the withdrawal |
| `ledger` | the text of every revision replaced; the audit columns kept |
| `handoff-packs` | a pack's copy of the record, as its checkpoint or a related record, replaced by the marker |
| `idempotency` | cached answers that quote the record dropped, so a retry of one runs again instead of replaying |
| `projection:<table>` | the record's rows deleted and attested by `retention.purge_projections`; the vector index is `unverified` where sqlite-vec cannot load |
| `file` | WAL checkpointed and the file vacuumed (`retention.scrub`), so no freed page holds the text; `unverified` when another writer holds the store |
| `export` | `registry/ledger.jsonl` rewritten from the erased canon at once and checked, so `export-ledger --check` stays consistent |
| `backups`, `encrypted-backups`, `migration-backups` | **retained** while any copy taken before the erasure exists: it keeps the text until it rotates out |
| `git-history` | **retained**: earlier commits of the export keep the text, and this engine does not rewrite git history |
| `residue` | listed only if any table of the store still contains the text, which is checked with `instr` and never assumed |

- `erasedInStore` is true when no table of the store holds the text.
- `complete` is true only when nothing is retained or unverified. A receipt never calls an erasure complete while a backup still has the text.
- Every receipt is appended to `store/logs/forget.jsonl` (mode 0600). It holds the record id, reason, approver and backends, never the text.

## Rules

- The checkpoint of an **open** workflow is refused, as withdrawal refuses it, because it is the workflow's state. Close the workflow first. Nothing is changed by a refusal.
- **Erasing again is safe.** Every backend is checked again, and the receipt says what changed (`0 revision(s) rewritten`).
- **A pending outbox row cannot bring the text back.** The indexer indexes only a non-tombstoned, latest revision (N-011), and after erasure the canon has no text left to index.
- **Erasing needs a terminal.** There is no `--yes`. `--plan` reads only and lists the packs and cached answers that would be rewritten.

## Not done here

- **Backups are not rewritten.** An encrypted backup is the recovery path, and editing it would make it no longer the state it recorded. They rotate out on their own schedule, and the receipt says so.
- **The wiki projection, transcripts and anything outside this workspace** are not reached. They hold no ledger text by this engine's hand. A narrative someone wrote by hand is theirs to edit.
