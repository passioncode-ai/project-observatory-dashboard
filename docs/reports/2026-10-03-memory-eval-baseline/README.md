---
report:
  id: project-observatory/2026-10-03-memory-eval-baseline
  title: "Agent memory search: the evaluation baseline, before and after OBS-03"
  kind: benchmark
  project: project-observatory
  domains: [memory, rag, ai-agent, correctness]
  as_of: 2026-10-03
  status: active
  valid_until: 2026-11-02
  summary: >-
    The agent-memory evaluation set (OBS-07, tools/memory_eval.py) measured the lexical search
    before and after OBS-03 on a synthetic corpus: 40 records, 40 answerable questions (half
    Russian), 20 unanswerable, 8 questions answered only by a checkpoint's body, and a held-out
    hard split. After OBS-03, recall@5 and MRR are 1.0 in both languages (were 0.975 and 0.931),
    all 20 unanswerable questions abstain (were 5), a checkpoint is found by its body 8 of 8
    times (was 0), and a record is searchable before the index pass (was not). Paraphrases
    that share no word with their record are still missed (1 of 15); that, and a floor for the
    vector arm, is OBS-04's.
  sources:
    - name: "Evaluation before OBS-03 (tools/memory_eval.py at 35c7dd8)"
      path: "docs/reports/2026-10-03-memory-eval-baseline/raw/eval-before-35c7dd8.json"
      read_at: 2026-10-03
    - name: "Evaluation after OBS-03"
      path: "docs/reports/2026-10-03-memory-eval-baseline/raw/eval-after.json"
      read_at: 2026-10-03
    - name: "Coverage-floor sweep"
      path: "docs/reports/2026-10-03-memory-eval-baseline/raw/coverage-sweep.json"
      read_at: 2026-10-03
    - name: "Evaluation corpus"
      path: "observatory/engine/tests/memory_eval/corpus.json"
      read_at: 2026-10-03
  produced_by: {agent: claude-code, task: OBS-07}
  supersedes: []
  superseded_by:
  consumers: [docs/design/AGENT-MEMORY.md, docs/backlog.md]
---

# Agent memory search: the evaluation baseline

## Main points

| Measure | Before OBS-03 (`35c7dd8`) | After | Out of |
|---|---|---|---|
| recall@5 | 0.975 | **1.0** | 40 questions |
| recall@5, Russian | 0.95 | **1.0** | 20 |
| MRR | 0.931 | **1.0** | 40 |
| Unanswerable questions that come back empty | 5 | **20** | 20 |
| Answerable questions refused by the floor | — | **0** | 40 |
| Checkpoint found by its body (recall@5) | 0 (measured on this code with the body index switched off) | **8** (MRR 0.938) | 8 |
| Searchable before the index pass | no | **yes** | — |
| Handoff packs complete | 10 | 10 | 10 |
| Injected instruction stays data | yes | yes | — |
| Erased record served by search or listing | no | no | — |
| Held out: paraphrases found (recall@5) | — | 0.067 | 15 |
| Held out: near-miss questions that abstain | — | 9 | 10 |

The "before" column is `tools/memory_eval.py` run at `35c7dd8`, the commit before OBS-03, in a
detached worktree ([raw](raw/eval-before-35c7dd8.json)). The "after" column is the same tool on
this branch ([raw](raw/eval-after.json)). The checkpoint section did not exist at `35c7dd8`; its
"before" is this branch's code with `textkeys.body_prose` replaced by an empty string, which
missed all eight questions.

## What was measured

- **Corpus.** All data is synthetic (`observatory/engine/tests/memory_eval/corpus.json`). It
  has 40 records over two fixture projects, and each answerable question has one record that
  answers it. The Russian questions use different word forms from their records. 20 questions
  have no answer in the corpus.
- **Checkpoints.** 4 workflow checkpoints (2 English, 2 Russian). Each of the 8 questions about
  them is answered only by a decision, constraint, result or note — never by the goal or the
  next action.
- **Hard split.** It was written after the search was tuned, so tuning could not fit it. It has
  15 paraphrases with no shared subject word, and 10 near-miss questions that reuse corpus
  vocabulary but are unanswerable.
- **Lexical only.** The evaluation replaces the embedding provider with one that refuses, so it
  spends nothing. These are the numbers of the arm that still answers when a limit has run
  out.

## The coverage floor

A lexical hit must carry a share of the question's subject keys. The [sweep](raw/coverage-sweep.json):

| Floor | recall@5 | Unanswerable abstained (of 20) | Answerable refused | Checkpoint recall@5 | Near-miss abstained (of 10) |
|---|---|---|---|---|---|
| 0.2 | 1.0 | 16 | 0 | 1.0 | 2 |
| 0.3 | 1.0 | 18 | 0 | 1.0 | 6 |
| 0.34 | 1.0 | 20 | 0 | 1.0 | 9 |
| **0.4** | 1.0 | 20 | 0 | 1.0 | 9 |
| 0.5 | 1.0 | 20 | 0 | 1.0 | 9 |
| 0.55 | 0.95 | 20 | 2 | 0.875 | 10 |
| 0.7 | 0.75 | 20 | 10 | 0.625 | 10 |

The plateau runs from 0.34 to 0.5, and 0.4 is its middle. Above 0.5, answerable questions
start to be refused. One or two subject words need one of them, whatever the floor.

## What remains

- **Paraphrases** that share no word with their record: 14 of 15 missed. Only similarity can
  find them (OBS-04).
- **One near-miss question** still answers: *What font does the company letterhead use?*
  It matches a record about the landing page's display font on two of its four keys. A lexical
  floor cannot tell a font on a page from a font on paper.
- **The vector arm has no floor.** It is top-k, so with similarity switched on, a question
  nothing answers still returns its nearest records and `abstain` stays false. A distance
  floor depends on the model and is calibrated with it (OBS-04). This evaluation does not
  measure the vector arm.

## Reproduce

```bash
cd observatory/engine
python tools/memory_eval.py --json /tmp/eval.json      # the "after" column
python tests/test_memory_eval.py                       # the gate on these numbers
```

The gate (`tests/test_memory_eval.py`) holds the "after" column. A change that lowers a number
changes that test, with its measurement.
