#!/usr/bin/env python3
"""Turn a record_turn.py result into the request for a `why`, delivered with the next prompt.

Standard library only, and silent on anything unexpected: a hook that reports
its own confusion as a failure teaches the operator to disable hooks.

It never blocks. The facts are already on the record without the agent's
cooperation, so the only thing left to ask for is the `why` — and asking is
worth more than a veto that costs a turn. An absent `why` stays visible in the
ledger as a proposed record with a null field, which is accountability without
obstruction.

**Once, where the model reads it.** This printed the ask as a Stop
`systemMessage` after every changed turn: measured over fourteen days of one
machine's transcripts, 3,593 asks for 34 answers. A Stop `systemMessage` is a
notice for the person, never rendered into the model's input, and a session is
ONE record revised per turn, so every ask after the first repeated the question.
Now the recorder decides (`ask` is false once a record was asked about, and when
the turn wrote its own rationale), and this hook leaves the ask in a private
per-session file that `prompt-why.py` hands to the model as the next prompt's
`additionalContext`, once. If that file cannot be written the ask falls back to
the old Stop notice: an ask is never lost to a full disk.
"""
from __future__ import annotations
import json
import os
import re
import sys
import time

OWNER = "agent:claude-code"
#: A session id names a file, so it is held to a shape that cannot leave the directory.
SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
#: An ask nobody collected — the session ended — is removed after this long.
PRUNE_SECONDS = 7 * 86400
#: A session's pending asks, newest kept: a record is asked about once, so more
#: than a few undelivered ones means the session is gone, not busy.
KEEP = 3


def pending_dir() -> str:
    """Where a session's undelivered ask waits; `prompt-why.py` computes the same."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "project-observatory", "ask-why")


def message(r: dict, at_stop: bool = False) -> str:
    """The ask. `at_stop` words it for a Stop notice, which follows the turn it is
    about, rather than for the next prompt, which comes after it."""
    mid = r.get("memoryId")
    rev = r.get("revision")
    project = r.get("project", "this project")
    files = r.get("files", 0)
    plural = "" if files == 1 else "s"
    unpushed = r.get("unpushed") or 0
    extra = f", {unpushed} unpushed commit(s)" if unpushed else ""
    text = (
        f"Observatory recorded the facts of {'this' if at_stop else 'your previous'} turn in {project}: "
        f"{files} file{plural} changed, +{r.get('insertions', 0)}/-{r.get('deletions', 0)}"
        f"{extra}. Stored as {mid}@{rev}, state proposed, with no `why`."
        + (" This record is asked about once." if r.get("ask") is True else "") + "\n\n"
        "The diff already says WHAT changed. Add WHY — one or two sentences a "
        "reader in six months could not reconstruct from the diff: the constraint "
        "that forced the shape, the alternative rejected, the trap avoided.\n\n"
        f"  observatory_record(owner=\"{OWNER}\", memory_id=\"{mid}\", "
        f"expected_revision={rev}, statement=<the same claim>, why=<the reason>)\n\n"
        "If the change genuinely needs no explanation — a typo, a rename — say so "
        "and skip it. An empty `why` on a trivial edit is honest; an invented one "
        "is worse than nothing, because it will be read as true."
        + ("" if at_stop else " Then answer the user's new message.")
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
        text += ("\n\nWhile you are here — this project's recorded knowledge "
                 "is behind its code:\n  - " + "\n  - ".join(stale_bits))
    return text


def leave_for_next_prompt(session: str, r: dict, text: str) -> bool:
    """Write the ask where `prompt-why.py` finds it. False when it could not be kept."""
    if not isinstance(session, str) or not SESSION.fullmatch(session):
        return False
    try:
        d = pending_dir()
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
        now = time.time()
        for name in os.listdir(d):                  # sessions that never prompted again
            p = os.path.join(d, name)
            try:
                if now - os.stat(p).st_mtime > PRUNE_SECONDS:
                    os.unlink(p)
            except OSError:
                pass
        path = os.path.join(d, session + ".json")
        asks = []
        try:
            with open(path, encoding="utf-8") as fh:
                asks = [a for a in (json.load(fh).get("asks") or []) if isinstance(a, dict)]
        except (OSError, ValueError, AttributeError):
            asks = []
        asks = [a for a in asks if a.get("memoryId") != r.get("memoryId")]
        entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                 "memoryId": r.get("memoryId"), "revision": r.get("revision"),
                 "context": text}
        # Where `prompt-why.py` records the delivery: the engine's journal, named by
        # the recorder. Only a file of that exact name is ever written there.
        journal = r.get("journal")
        if isinstance(journal, str) and os.path.basename(journal) == "why-asks.jsonl":
            entry["journal"] = journal
        asks.append(entry)
        tmp = f"{path}.{os.getpid()}.tmp"
        # Created owner-only by the umask, not by a raw `os.open`: this script runs
        # from the plugin's own copy without the engine, so `osprivacy` (the one
        # module allowed to pass open flags) is out of reach, and the hook is one
        # single-threaded process, so the umask is its own.
        mask = os.umask(0o077)
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"asks": asks[-KEEP:]}, fh, ensure_ascii=False)
        finally:
            os.umask(mask)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except Exception:                                                  # noqa: BLE001
        return False


def main() -> int:
    try:
        r = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(r, dict):
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
        # A fault stays a Stop notice: it is for the person, and it is urgent.
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
    if r.get("rationale"):
        return 0          # the turn wrote its rationale; the record links that file
    if "ask" not in r:
        # AN OLDER ENGINE, which decides nothing about asking: it would send this
        # every changed turn, and an every-turn ask injected into the model's context
        # is worse than the notice it replaces. So it gets the old Stop notice.
        print(json.dumps({"systemMessage": message(r, at_stop=True), "suppressOutput": True},
                         ensure_ascii=False))
        return 0
    if r.get("ask") is not True:
        return 0          # this record was asked about already

    text = message(r)
    if leave_for_next_prompt(r.get("session"), r, text):
        return 0
    # THE OLD CHANNEL, as the fallback. A notice the model may not read is better
    # than an ask that silently never happens.
    print(json.dumps({"systemMessage": message(r, at_stop=True), "suppressOutput": True},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        raise SystemExit(0)
