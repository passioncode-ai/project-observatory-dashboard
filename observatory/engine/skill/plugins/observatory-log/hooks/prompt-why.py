#!/usr/bin/env python3
"""Hand the model the request for a `why` that the Stop hook left for this session.

A UserPromptSubmit hook, so it runs before EVERY prompt of every session, and the
whole design is about that cost:

- **Silent unless an ask is pending.** One `stat` of a per-session file and exit 0;
  the file exists only after `ask-why.py` asked about a record, at most once per
  record (`why_asks.py` in the engine).
- **Once.** The file is taken by an atomic rename before it is read, so two prompts
  cannot both deliver it, and it is deleted after.
- **Never in the way of the prompt.** Every error — a payload that is not JSON, a
  session id that is not a plain name, a torn file — is exit 0 with nothing on
  stdout. It cannot block: it prints no `decision`, and it exits 0 on every path.
- **Delivery is recorded.** For each ask handed over it appends
  `{"event": "delivered"}` to the engine's journal (`store/raw/why-asks.jsonl`, the
  path the recorder named), because the answer rate is measured on delivered asks: a
  headless session that never sends a second prompt never receives its ask, and must
  not count as having ignored it.
- **Where the model reads it.** The ask goes out as
  `hookSpecificOutput.additionalContext`, which Claude Code renders into the
  model's input; the Stop `systemMessage` it replaces is a notice for the person
  (`tests/test_ask_why_reach.py`).

It holds no secret: the pending file carries the ask's text — a project id, a
record id and a diff's size — and is written 0600 in a 0700 directory.
Standard library only; it does not need the engine.
"""
from __future__ import annotations
import calendar
import json
import os
import re
import sys
import time

SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
#: An ask older than this is about a turn too far back to explain well; it is dropped.
STALE_SECONDS = 7 * 86400
#: The context a prompt can be handed, at most. One ask is about a thousand characters.
MAX_CHARS = 4000


def pending_dir() -> str:
    """The directory `ask-why.py` writes; the two must agree."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "project-observatory", "ask-why")


SEPARATOR = "\n\n---\n\n"


def _put_back(path: str, asks: list) -> None:
    """Return undelivered asks to the session's pending file, beside any the Stop hook
    wrote meanwhile. Owner-only, like the file `ask-why.py` writes."""
    try:
        try:
            with open(path, encoding="utf-8") as fh:
                current = [a for a in (json.load(fh).get("asks") or []) if isinstance(a, dict)]
        except (OSError, ValueError, AttributeError):
            current = []
        ids = {a.get("memoryId") for a in current}
        merged = [a for a in asks if a.get("memoryId") not in ids] + current
        tmp = f"{path}.{os.getpid()}.back"
        mask = os.umask(0o077)
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"asks": merged}, fh, ensure_ascii=False)
        finally:
            os.umask(mask)
        os.replace(tmp, path)
    except Exception:                                                  # noqa: BLE001
        pass                          # an ask that is not put back stays pending, unasked


def _record_delivery(ask: dict, session: str) -> None:
    """Append a `delivered` row to the journal the recorder named. Never raises."""
    journal = ask.get("journal")
    if not isinstance(journal, str) or os.path.basename(journal) != "why-asks.jsonl" \
            or os.path.islink(journal) or not os.path.isdir(os.path.dirname(journal) or "/nonexistent"):
        return
    row = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "memoryId": ask.get("memoryId"), "event": "delivered", "session": session}
    if not isinstance(row["memoryId"], str):
        return
    try:
        with open(journal, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass                    # an undelivered count is a floor, not a failed prompt


def _prune(directory: str) -> None:
    """Remove pending asks older than STALE_SECONDS: sessions that ended without a
    further prompt. Run only after a delivery, so a quiet prompt stays one `stat`."""
    try:
        now = time.time()
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            try:
                if now - os.stat(path).st_mtime > STALE_SECONDS:
                    os.unlink(path)
            except OSError:
                pass
    except OSError:
        pass


def _fresh(stamp) -> bool:
    try:
        when = calendar.timegm(time.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, OverflowError):
        return True                                  # an odd stamp still delivers
    return time.time() - when <= STALE_SECONDS


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    session = payload.get("session_id") if isinstance(payload, dict) else None
    if not isinstance(session, str) or not SESSION.fullmatch(session):
        return 0
    path = os.path.join(pending_dir(), session + ".json")
    if not os.path.isfile(path):
        return 0                                     # the common case: nothing pending
    taking = f"{path}.taking-{os.getpid()}"
    try:
        os.replace(path, taking)                     # one prompt takes it, not two
    except OSError:
        return 0
    try:
        with open(taking, encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception:
        doc = {}
    finally:
        try:
            os.unlink(taking)
        except OSError:
            pass
    _prune(os.path.dirname(path))
    asks = doc.get("asks") if isinstance(doc, dict) else None
    chosen = [a for a in (asks or [])
              if isinstance(a, dict) and isinstance(a.get("context"), str)
              and a["context"].strip() and _fresh(a.get("at"))]
    if not chosen:
        return 0
    # THE CAP, honestly counted. Only asks that fit inside MAX_CHARS whole are handed
    # over and marked delivered; the rest go back for the next prompt. The first ask
    # always goes (cut at the cap if it alone is longer: its record id and the call
    # to make come first).
    sent, rest, used = [], [], 0
    for a in chosen:
        cost = len(a["context"]) + (len(SEPARATOR) if sent else 0)
        if sent and used + cost > MAX_CHARS:
            rest.append(a)
            continue
        sent.append(a)
        used += cost
    if rest:
        _put_back(path, rest)
    context = SEPARATOR.join(a["context"] for a in sent)[:MAX_CHARS]
    for a in sent:
        _record_delivery(a, session)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                             "additionalContext": context}},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        raise SystemExit(0)
