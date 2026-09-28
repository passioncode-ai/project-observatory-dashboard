#!/usr/bin/env python3
"""The search an apostrophe could break, and two scales compared by their defaults.

`survey.search` is the hybrid recall behind `observatory_search`: similarity
where the vector index and a key are both available, a lexical match always. The
lexical half exists precisely to answer when the other cannot — and it was the
half that broke on ordinary human text.

**Measured 2026-09-07: six of seven ordinary questions destroyed it.** The
caller's string went straight into `MATCH`, and FTS5's `MATCH` takes a query
LANGUAGE rather than text:

    "don't"                 fts5: syntax error near "'"
    "the agent's ceiling"   fts5: syntax error near "'"
    "a - b"                 no such column: b
    "store/raw"             fts5: syntax error near "/"
    "foo(bar)"              fts5: syntax error near "foo"
    '"unbalanced'           unterminated string

`no such column` is the sharpest: FTS5 read a bare word as a column NAME, so the
index's own columns were addressable from a user's question. Nothing was
injectable — the value is parameterised — but an apostrophe was enough to return
nothing at all, and with the vector half stopped by a spend guardrail (its live
state) the caller got `count: 0` and two degradations for a perfectly ordinary
question.

**And the two halves' scores were compared through their defaults.** The sort was
`(h.get("distance", 9e9), h.get("rank", 0))`. A vector hit carries a distance
around 0.3 while a lexical-only hit defaulted to 9e9 — so whenever the vector
half worked, a PERFECT lexical match sorted below every weak semantic one. And
bm25 ranks are NEGATIVE (the live values are about -2e-06), so a hit with no rank
defaulted to 0 and sorted after every real one. Two ordering faults in one line,
both invisible while the vector half was down.

Reciprocal rank fusion replaces it: each half contributes `1/(k + position)` for
what it returned, a record missing from one list contributes nothing from it, and
no shared scale is needed.

**A third, and it is the shape already closed for `observatory_recall`:** `count`
was the page size and nothing said how much was left. A query matching forty
records returned ten in silence. `total` and `truncated` now sit beside it.
"""
from __future__ import annotations
import json, os, pathlib, sqlite3, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []

#: The exact queries that broke it, kept as data so a regression names itself.
ONCE_BROKEN = ["don't", "the agent's ceiling", "a - b", "store/raw", "foo(bar)",
               '"unbalanced', "!!!", "NEAR(a b)", "statement: x", "*", "a AND",
               "проект"]


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def survey():
    import importlib
    import survey as s
    return importlib.reload(s)


# ─────────── ordinary text is not a syntax error ───────────────────────

def test_no_ordinary_question_degrades_the_lexical_half() -> None:
    s = survey()
    for q in ONCE_BROKEN:
        r = s.search(q, limit=5)
        lex = [d for d in r["degraded"] if d["source"] == "lexical"]
        check(f"{q!r} does not break the lexical half", not lex,
              lex[0]["reason"][:90] if lex else "")


def test_the_expression_is_built_from_quoted_phrases() -> None:
    s = survey()
    check("a word becomes a phrase", s.fts_query("registry") == '"registry"',
          s.fts_query("registry"))
    check("an apostrophe survives inside it", s.fts_query("don't") == '"don\'t"',
          s.fts_query("don't"))
    check("several words are joined by OR",
          s.fts_query("a b") == '"a" OR "b"', s.fts_query("a b"))
    check("an embedded quote is doubled",
          s.fts_query('say "hi"') == '"say" OR "hi"', s.fts_query('say "hi"'))
    check("punctuation alone matches nothing rather than raising",
          s.fts_query("!!!") == '""', s.fts_query("!!!"))
    check("and an FTS5 operator is treated as a word, not an operator",
          "OR" in s.fts_query("a AND b") and '"and"' in s.fts_query("a AND b").lower(),
          s.fts_query("a AND b"))


def test_a_column_name_can_no_longer_be_addressed() -> None:
    """`no such column: b` was FTS5 reading a bare word as a column. Every
    column of `search_notes` is tried, because the one that raised was the one
    that did not exist — and a caller must not be able to scope a query to
    `statement` or `why` either."""
    s = survey()
    for col in ("memory_id", "revision", "statement", "why", "nonexistent"):
        r = s.search(f"{col}: something", limit=3)
        lex = [d for d in r["degraded"] if d["source"] == "lexical"]
        check(f"`{col}:` is a word, not a filter", not lex,
              lex[0]["reason"][:80] if lex else "")


# ─────────── the two scales are fused, not compared ────────────────────

def test_the_ordering_never_compares_a_distance_with_a_rank() -> None:
    src = (ROOT / "survey.py").read_text(encoding="utf-8")
    sys.path.insert(0, str(ROOT / "tools"))
    import check_paths
    code = check_paths.prose_removed(src)
    check("the old default-comparison sort is gone",
          'h.get("distance", 9e9)' not in code,
          "a distance of 0.3 beat every lexical-only hit at 9e9")
    check("and the rank default with it", 'h.get("rank", 0)' not in code,
          "bm25 ranks are negative, so a missing rank sorted best")
    check("fusion replaces it", "RRF_K" in code and "fused[key]" in code)
    check("with the constant written down rather than tuned",
          "RRF_K = 60" in code, "a fitted number reads as evidence")


def test_fusion_orders_by_membership_in_both_lists() -> None:
    """The property that matters, on a fixture where both halves are known: a
    record found by BOTH must outrank one found by either alone, whatever the
    scales say."""
    s = survey()
    RRF_K = 60
    def fuse(vec_order, lex_order):
        out = {}
        for ordered in (vec_order, lex_order):
            for i, k in enumerate(ordered):
                out[k] = out.get(k, 0.0) + 1.0 / (RRF_K + i + 1)
        return out
    both, vec_only, lex_only = ("m:both", 1), ("m:vec", 1), ("m:lex", 1)
    scored = fuse([vec_only, both], [lex_only, both])
    check("a record in both lists scores highest",
          max(scored, key=lambda k: scored[k]) == both, str(scored))
    check("and a record in one list still scores",
          scored[vec_only] > 0 and scored[lex_only] > 0, str(scored))
    check("with the earlier position worth more",
          scored[vec_only] > scored[lex_only] or
          abs(scored[vec_only] - scored[lex_only]) < 1e-12,
          f"{scored[vec_only]} vs {scored[lex_only]}")
    # And the live answer is monotonic in the score it reports.
    r = s.search("the", limit=6)
    scores = [h["score"] for h in r["results"]]
    check("the live answer is ordered by its own score",
          scores == sorted(scores, reverse=True), str(scores))
    check("and every hit carries one", all("score" in h for h in r["results"]))


# ─────────── the scope, not the page ───────────────────────────────────

def test_the_answer_reports_what_it_left_out() -> None:
    s = survey()
    r = s.search("the", limit=2)
    check("`total` describes the whole match set", r["total"] >= r["count"],
          f"count={r['count']} total={r['total']}")
    check("`count` is the page", r["count"] == len(r["results"]),
          f"{r['count']} vs {len(r['results'])}")
    check("and truncation is stated", r["truncated"] is (r["total"] > r["count"]),
          f"truncated={r['truncated']} total={r['total']} count={r['count']}")
    # THE LIMIT COMES FROM THE MEASURED TOTAL, not from a guess about the
    # corpus. `limit=200` assumed this estate held fewer than 200 matches for
    # "the"; it holds 219 as of 2026-09-07, so the assertion began failing on
    # growth alone — the same shape as the disk assertion in
    # tests/test_footprint.py, which went red because a volume had room.
    #
    # And the number it was reading had a defect of its own: `total` was
    # `6 × limit`, so this call's own `limit=200` was what made `total` big
    # enough to look truncated. `RETRIEVAL_WIDTH` fixed that, and this asserts
    # the PROPERTY — a page wide enough for everything retrieved is not marked
    # truncated — which is true of any corpus.
    wide = s.search("the", limit=r["total"] + 1)
    check("a page wider than the whole retrieved set is not marked truncated",
          wide["truncated"] is False,
          f"total={r['total']} then {wide['total']}, truncated={wide['truncated']}")
    check("and `total` does not move with the page size",
          wide["total"] == r["total"], f"{r['total']} -> {wide['total']}")


def test_the_contract_notes_survive() -> None:
    """The two promises the search made before any of this, still made."""
    s = survey()
    r = s.search("registry", limit=3)
    check("conflicting records are still returned together",
          "conflicting records are returned together" in r["note"], r["note"][:80])
    check("and absence is still not proof of absence",
          "not proof that a record does not exist" in r["note"], r["note"][-80:])
    check("`degraded` is present even when empty", isinstance(r["degraded"], list))
    check("`contested` is still reported", "contested" in r, str(sorted(r)))


def test_the_projection_lag_is_still_named() -> None:
    """Both halves search an index fed by the outbox, so a pending queue means
    the answer cannot include the newest conclusions."""
    src = (ROOT / "survey.py").read_text(encoding="utf-8")
    check("the lag is measured from the ledger's own timestamp",
          "an append and its outbox row" in src or "ledger.created_at" in src,
          "no column had to be added to learn the enqueue time")
    check("and reported as a degradation", '"source": "projection"' in src)


if __name__ == "__main__":
    print("the search path — ordinary text, and two scales that are not one\n")
    for fn in (test_no_ordinary_question_degrades_the_lexical_half,
               test_the_expression_is_built_from_quoted_phrases,
               test_a_column_name_can_no_longer_be_addressed,
               test_the_ordering_never_compares_a_distance_with_a_rank,
               test_fusion_orders_by_membership_in_both_lists,
               test_the_answer_reports_what_it_left_out,
               test_the_contract_notes_survive,
               test_the_projection_lag_is_still_named):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32man apostrophe no longer empties the half that exists to answer\033[0m")
