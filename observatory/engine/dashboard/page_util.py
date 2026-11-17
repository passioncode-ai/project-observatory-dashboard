"""The helpers every page shares (OBS-598): escaping, writing an instant, and the clock.

Before this module six page modules escaped text with their own copy of `_e`, and four wrote
a time four ways: one instant read `2026-10-26 17:20 UTC` on Home and `2026-10-26 17:20:05 UTC`
on Agents. Every page, and the two views that feed the Agents page (`agents_view`,
`sessions_view`), now call these; `tests/test_dashboard_shell.py` `OneTimeEveryPage` pins it.

An instant is written the way the page header writes "Measured" (the page script replaces `T`
with a space and `Z` with ` UTC`): `2026-10-26 17:20:05 UTC`. A date alone stays a date, and a
value in another shape is shown as it is, never guessed into a time.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone

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
