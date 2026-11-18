"""The helpers every page shares (OBS-598): escaping, writing an instant, and the clock.

Before this module six page modules escaped text with their own copy of `_e`, and four wrote
a time four ways: one instant read `2026-10-26 17:20 UTC` on Home and `2026-10-26 17:20:05 UTC`
on Agents. Every page, and the two views that feed the Agents page (`agents_view`,
`sessions_view`), now call these; `tests/test_dashboard_shell.py` `OneTimeEveryPage` pins it.

An instant is written the way the page header writes "Measured" (the page script replaces `T`
with a space and `Z` with ` UTC`): `2026-10-26 17:20:05 UTC`. A date alone stays a date, and a
value in another shape is shown as it is, never guessed into a time.

A source's reason (a `degraded` entry, a collector's note) reaches every page through `reason_html` (OBS-629):
a person is shown no command (CP-01), while the JSON and MCP answers keep it for agents.
`tests/test_docs_drift.py` `ControlPlaneTruth` keeps every page module on it.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from i18n import Translator

_INSTANT = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(:\d{2})?(?:\.\d+)?(Z|[+-]00:?00)?")


def escape(value: object) -> str:
    """Text for markup: None is empty, anything else its `str`, escaped (quotes included)."""
    return html.escape("" if value is None else str(value))


def stamp(value: object, missing: str = "") -> str:
    """An ISO instant as every page writes it: `2026-10-26 17:20:05 UTC`. A UTC offset reads
    `UTC`, a time without one has no zone, a missing value is `missing`."""
    text = "" if value is None else str(value)
    if not text:
        return missing
    m = _INSTANT.fullmatch(text)
    if not m:
        return text
    return f"{m.group(1)} {m.group(2)}{m.group(3) or ':00'}" + (" UTC" if m.group(4) else "")


def now() -> datetime:
    """The clock the views read, in UTC, to the second."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(at: datetime) -> str:
    """An instant as the views store it: `2026-10-26T17:20:05Z`."""
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


#: A reason that names a command: the CLI's name or a backtick-quoted span. Agents read it whole (MCP, JSON); a page
#: does not show it (CP-01, OBS-608, OBS-629).
COMMAND = re.compile(r"`|project-observatory")
#: Where a clause ends: a reason is cut at the last one ahead of its command.
_CLAUSE = ";,:("


def plain(reason: str) -> str:
    """REASON without the command it names: the clauses before the one that holds the command, or empty when nothing
    stands before it. A reason that names no command is returned whole."""
    m = COMMAND.search(reason)
    if not m:
        return reason
    head = reason[:m.start()]
    cut = max(head.rfind(c) for c in _CLAUSE)
    return head[:cut].rstrip(" .;,:(") if cut > 0 else ""


def reason_html(reason: dict | str | None, t: Translator, tile: bool = False) -> str:
    """A source's reason in the reader's language and with no command (CP-01). A coded entry by its code
    (`not-surveyed`, `unreadable`); a free text, or an entry's `reason`, that names a command cut to the clauses
    before it, or replaced by a sentence when nothing is left or on a Home tile (`tile`); any other as written,
    translated when the catalog holds it (`t.known`)."""
    if isinstance(reason, dict):
        if reason.get("code") == "not-surveyed":
            return t.mark("This machine has not been surveyed yet.") if tile else t.mark("not surveyed yet")
        if reason.get("code") == "unreadable":
            return t.mark("unreadable: {error}", error=plain(str(reason.get("error") or reason.get("reason") or "")))
        reason = reason.get("reason") or reason.get("text") or ""
    text = "" if reason is None else str(reason)
    if not COMMAND.search(text):
        return t.known(text)
    if tile:
        # A Home tile names no source and links to its page, which says the rest.
        return t.mark("Not read yet; its page says why.")
    kept = plain(text)
    return t.known(kept) if kept else t.mark("The reason names a command; an agent reads it through MCP.")
