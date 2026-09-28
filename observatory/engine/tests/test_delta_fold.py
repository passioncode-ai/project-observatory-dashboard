#!/usr/bin/env python3
"""The sample did not understate the work — it reversed it.

`agent/observe.py` sent the model `deltas[:MAX_DELTAS_PER_PROJECT]`, the OLDEST
twelve of however many a project had waiting, plus a line saying how many more
there were. Measured on this repository's own backlog on 2026-09-08, while the
agent was halted on a spent key and 41 deltas had queued for 13 hours:

    what the model was shown            what had actually happened
    commits-changed: 63 -> 64           commits-changed: 63 -> 111
    commits-changed: 64 -> 65             (over 20 changes)
    … five one-commit steps …
    dirty-changed:   177 -> 180         dirty-changed:   177 -> 4
    dirty-changed:   180 -> 182           (over 20 changes)
    … six steps, all upward …
    (+29 more of the same kinds)

Two different defects in one prompt. The commit count understates by an order of
magnitude — five commits shown, forty-eight made. And the dirty-file count
**points the wrong way**: the sample rises 177 → 189 across the window in which
it actually fell to 4, because the twelve oldest steps of a rise-then-commit
cycle are all rises.

**And nothing could catch it.** `project_facts()` carries the typed registry's
descriptive fields — name, ownership, lifecycle, folders — and no quantitative
state at all, so the movement lines are the model's only handle on how much
moved. Meanwhile the row it writes claims `provenance.deltas = 41` and lists all
41 ids as evidence: the conclusion was stamped as resting on evidence 29 pieces
of which were never shown to it.

**The fold, rather than a bigger cap or the newest twelve.** Deltas of one kind
over one window ARE a range: oldest `before`, newest `after`, and the count of
steps between. Folding loses nothing a cap was hiding, makes
`provenance.deltas` true, and shortens the prompt — which matters precisely
because the budget is the binding constraint. `MAX_DELTAS_PER_PROJECT` is gone,
not raised: the movement lines are now bounded by the number of KINDS, which is
bounded by the fingerprint's own fields, so there is no list left to cap.
"""
from __future__ import annotations
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "tests"))

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def d(kind: str, before, after, seq: int | None = None) -> dict:
    row = {"kind": kind, "before_json": str(before), "after_json": str(after)}
    if seq is not None:
        row["seq"] = seq
    return row


# ─────────── the direction, which the sample inverted ──────────────────

def test_the_fold_keeps_the_direction_the_window_actually_took() -> None:
    """THE MEASURED DEFECT, in its own shape: a value that rises for the first
    twelve steps and ends far below where it began."""
    import observe as O
    rows = [d("dirty-changed", 177 + i * 3, 180 + i * 3, i) for i in range(12)]
    rows.append(d("dirty-changed", 213, 4, 12))
    folded = O.fold(rows)
    check("one movement for one kind", len(folded) == 1, str(folded))
    if not folded:
        return
    m = folded[0]
    check("from where the window started", m["before_json"] == "177", str(m))
    check("to where it ended", m["after_json"] == "4", str(m))
    check("saying how many steps it took", m["changes"] == 13, str(m))
    p = O.build_prompt("project:x", rows, {}, [])
    check("and the prompt says so", "177 -> 4" in p, p[:400])
    check("rather than the direction the oldest twelve took",
          "-> 213" not in p and "213 ->" not in p,
          "the sample rose across a window in which the value fell")


def test_nothing_is_dropped_and_no_cap_is_left_to_hide_it() -> None:
    import observe as O
    src = (ROOT / "agent/observe.py").read_text(encoding="utf-8")
    check("the twelve-delta cap is gone rather than raised",
          "MAX_DELTAS_PER_PROJECT" not in src,
          "a bigger cap is the same defect with a later trigger")
    check("and the omission line it needed is gone with it",
          "more of the same kinds" not in src, "")
    rows = ([d("commits-changed", i, i + 1, i) for i in range(40)]
            + [d("dirty-changed", 1, 2, 100)])
    p = O.build_prompt("project:x", rows, {}, [])
    lines = [l for l in p.splitlines() if l.startswith("- ")]
    check("one line per kind, whatever the volume", len(lines) == 2, str(lines))
    check("every kind present reaches the prompt",
          "commits-changed" in p and "dirty-changed" in p, str(lines))
    check("and the 41 deltas are represented as 41",
          "over 40 changes" in p, str(lines))


def test_a_single_change_is_not_dressed_as_a_range() -> None:
    """`(over 1 changes)` is noise and bad grammar, and it would appear on almost
    every prompt the agent sends when it is running normally."""
    import observe as O
    p = O.build_prompt("project:x", [d("lifecycle-changed", '"archived"', '"active"')],
                       {}, [])
    check("no step count on a single change", "over 1 change" not in p, p[:300])
    check("the movement itself is still there",
          '"archived" -> "active"' in p, p[:300])


def test_the_order_between_kinds_is_arrival_order() -> None:
    """A fold per kind must not become a set: "appeared, then disappeared" and
    the reverse are different histories, and the kinds carry that order."""
    import observe as O
    rows = [d("project-appeared", "null", "{}", 5),
            d("commits-changed", 1, 2, 6),
            d("project-disappeared", "{}", "null", 7)]
    folded = [m["kind"] for m in O.fold(rows)]
    check("the kinds keep the order they arrived in",
          folded == ["project-appeared", "commits-changed", "project-disappeared"],
          str(folded))
    # Unordered input must not silently reorder the history: the caller queries
    # `ORDER BY rowid`, and `seq` is what makes that guarantee travel.
    shuffled = [rows[2], rows[0], rows[1]]
    folded = [m["kind"] for m in O.fold(shuffled)]
    check("and `seq` is honoured rather than the list's accident",
          folded == ["project-appeared", "commits-changed", "project-disappeared"],
          str(folded))


def test_the_meaning_still_travels_with_the_movement() -> None:
    """`_meaning` was added because `commits-changed: 885 -> 886` and "commits
    were recorded" are different amounts of help. The fold must not drop it."""
    import observe as O
    p = O.build_prompt("project:x", [d("commits-changed", 1, 9, 0),
                                     d("commits-changed", 9, 20, 1)], {}, [])
    check("the folded line carries the field's meaning",
          "commits were recorded" in p, p[:300])


def test_the_prompt_is_shorter_than_the_sample_it_replaces() -> None:
    """Measured, not asserted: the budget is the binding constraint on this
    whole layer, so a change that shortens every prompt is worth a number."""
    import observe as O
    rows = [d("commits-changed", 63 + i, 64 + i, i * 2) for i in range(20)]
    rows += [d("dirty-changed", 177 - i, 176 - i, i * 2 + 1) for i in range(20)]
    p = O.build_prompt("project:x", rows, {}, [])
    movement = "\n".join(l for l in p.splitlines() if l.startswith("- "))
    check(f"forty deltas render in {len(movement)} characters",
          len(movement) < 400,
          f"{len(movement)} — the sampled form spent 922 on the live backlog")


# ─────────── the row's provenance is now true ──────────────────────────

def test_the_row_says_how_many_movements_it_read() -> None:
    """`provenance.deltas` claimed 41 while the model saw 12. After the fold the
    claim is true, and the movement count is recorded beside it so a reader can
    see WHICH form the prompt took — the two are different facts and a reader
    who cannot tell them apart is back where this started."""
    src = (ROOT / "agent/observe.py").read_text(encoding="utf-8")
    sys.path.insert(0, str(ROOT / "tests"))
    import source_reader
    code = source_reader.code_keeping_strings(src)
    check("the provenance records the movements as well as the deltas",
          '"movements"' in code and '"deltas"' in code,
          "one is what the model read, the other is what the row consumed")
    check("and the fold is what the prompt is built from",
          "fold(" in code, "")


if __name__ == "__main__":
    print("folded deltas — the sample pointed the wrong way\n")
    for fn in (test_the_fold_keeps_the_direction_the_window_actually_took,
               test_nothing_is_dropped_and_no_cap_is_left_to_hide_it,
               test_a_single_change_is_not_dressed_as_a_range,
               test_the_order_between_kinds_is_arrival_order,
               test_the_meaning_still_travels_with_the_movement,
               test_the_prompt_is_shorter_than_the_sample_it_replaces,
               test_the_row_says_how_many_movements_it_read):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma window's movement is its endpoints, not its first twelve steps\033[0m")
