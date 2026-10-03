# Agent memory search and its evaluation (OBS-03, OBS-07)

**Objective.** OBS-03: the search over agent memory must answer in the reader's word form, find a
checkpoint by what its step decided, say "nothing found" instead of answering from the best of
the bad, and see a record the moment it is written. OBS-07 measures all of this, and gates it.
Behaviour: [AGENT-MEMORY.md → Search](../../design/AGENT-MEMORY.md#search). Numbers:
[memory evaluation baseline](../../reports/2026-10-03-memory-eval-baseline/README.md).

## What changed

| Piece | Where |
|---|---|
| search keys: Snowball stem, Russian cut to six characters, stopwords for questions; the checkpoint body's prose | `observatory/engine/textkeys.py` |
| the `stems` column; migration 0009 rebuilds the lexical index from canon with keys, bodies included | `store/schema.sql`, `store/migrate.py` |
| a revision enters the lexical index in its own transaction and leaves it with its tombstone | `store/ledger.py` |
| the indexer keys the body too | `store/indexer.py` |
| the coverage floor (0.4, measured), `abstain`, `abstainReason`, `floor` | `survey.py` |
| the stemmer is a `[full]` dependency the runtime check knows | `pyproject.toml`, both `requirements-full.lock`, `workspace.py` |
| checkpoint questions, a held-out hard split, the gate on the measured numbers | `tests/memory_eval/corpus.json`, `tools/memory_eval.py`, `tests/test_memory_eval.py` |
| keys, the floor, the body and abstention as unit cases | `tests/test_textkeys.py` |

## Measured

The before/after numbers are in the report, from raw files in the report's `raw/`:
- recall@5 went from 0.975 to 1.0 and MRR from 0.931 to 1.0;
- abstention went from 5 to 20 of 20;
- a checkpoint found by its body went from 0 to 8 of 8;
- freshness went from no to yes.

The floor was chosen from a sweep: 0.34 to 0.5 is a plateau, and 0.4 is its middle.

`test_a_checkpoint_is_found_by_its_body_and_nothing_else_abstains` was watched failing with
`textkeys.body_prose` replaced by an empty string. Both body questions failed.

## Moved, not dropped

The vector arm is top-k. With similarity on, a question nothing answers still returns its
nearest records, so the "nothing found" half of OBS-03 holds for the lexical arm only. The
distance floor depends on the embedding model, and it moved to OBS-04 in the backlog, where
the model is chosen.

## Checks run

CHECKS_PLACEHOLDER

## Next task

OBS-04: local multilingual embeddings chosen on this evaluation set, one index per model, and a
distance floor calibrated per model.
