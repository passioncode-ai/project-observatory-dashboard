#!/usr/bin/env python3
"""Turn a record_turn.py result into the Stop hook's structured output.

Standard library only, and silent on anything unexpected: a hook that reports
its own confusion as a failure teaches the operator to disable hooks.

It never blocks. The facts are already on the record without the agent's
cooperation, so the only thing left to ask for is the `why` — and asking is
worth more than a veto that costs a turn. An absent `why` stays visible in the
ledger as a proposed record with a null field, which is accountability without
obstruction.
"""
from __future__ import annotations
import json
import sys

OWNER = "agent:claude-code"


def main() -> int:
    try:
        r = json.load(sys.stdin)
    except Exception:
        return 0
    if not r.get("recorded"):
        # A FAULT IS NOT A QUIET TURN. This returned 0 for every `recorded:
        # false`, and the recorder's reasons are two different kinds: "nothing
        # changed" is an answer about the work, while `IllegalTransition:
        # observed -> proposed` is the recorder failing. Measured 2026-09-07:
        # once `tools/corroborate.py` promoted a session's record, every later
        # turn of that session raised that error and said nothing — roughly
        # seventy-two turns in one session, and the only visible sign was a
        # ledger row that had stopped moving.
        #
        # The recorder marks which kind it is; this only has to stop hiding it.
        reason = r.get("reason") or ""
        if not r.get("fault"):
            return 0                   # nothing changed, not watched, or explained
        print(json.dumps({"systemMessage":
                          "Observatory could NOT record this turn: " + reason +
                          "\n\nThe facts of this turn are not in the ledger. "
                          "`store/raw/record-turn.json` holds the same reason, and "
                          "`project-observatory full findings` raises it.",
                          "suppressOutput": True}))
        return 0
    if r.get("hasWhy"):
        return 0                                          # already explained

    mid = r.get("memoryId")
    rev = r.get("revision")
    project = r.get("project", "this project")
    files = r.get("files", 0)
    plural = "" if files == 1 else "s"
    unpushed = r.get("unpushed") or 0
    extra = f", {unpushed} unpushed commit(s)" if unpushed else ""

    message = (
        f"Observatory recorded the facts of this turn in {project}: "
        f"{files} file{plural} changed, +{r.get('insertions', 0)}/-{r.get('deletions', 0)}"
        f"{extra}. Stored as {mid}@{rev}, state proposed, with no `why`.\n\n"
        "The diff already says WHAT changed. Add WHY — one or two sentences a "
        "reader in six months could not reconstruct from the diff: the constraint "
        "that forced the shape, the alternative rejected, the trap avoided.\n\n"
        f"  observatory_record(owner=\"{OWNER}\", memory_id=\"{mid}\", "
        f"expected_revision={rev}, statement=<the same claim>, why=<the reason>)\n\n"
        "If the change genuinely needs no explanation — a typo, a rename — say so "
        "and skip it. An empty `why` on a trivial edit is honest; an invented one "
        "is worse than nothing, because it will be read as true."
    )
    # THE NUDGE AT THE MOMENT OF WORK. The agent is already inside this project;
    # rebuilding its graph or touching its note costs least right now. Thresholds
    # match the board's grace (7 days), and None means the artefact does not
    # exist — not adopting graphify is a choice this hook does not argue with.
    stale_bits = []
    g = r.get("graphAgeDays")
    if isinstance(g, int) and g > 7:
        stale_bits.append(f"its code graph is {g} days old — refresh it: "
                          f"/graphify . --update")
    w = r.get("wikiAgeDays")
    if isinstance(w, int) and w > 7:
        stale_bits.append(f"its wiki notes were last touched {w} days ago — if this "
                          f"work changed what the project IS (modules, scope, "
                          f"stack), update the overview note too")
    if stale_bits:
        message += ("\n\nWhile you are here — this project's recorded knowledge "
                    "is behind its code:\n  - " + "\n  - ".join(stale_bits))
    print(json.dumps({"systemMessage": message, "suppressOutput": True},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        raise SystemExit(0)
