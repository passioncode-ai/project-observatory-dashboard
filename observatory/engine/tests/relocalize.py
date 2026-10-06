"""The page script's `localizeStatic`, in Python, for the checks.

A page is built in one language and read in the reader's (L10N-01): the script
re-translates every element the builder marked — `data-t` text with its
`data-t-args`, `data-t-<attribute>` attributes with their `-args`, `data-date`
dates and `data-num` numbers — and leaves the rest as built. This module does the
same to the HTML a check holds, so a check can prove that a page built in English
and read in Russian says what the page built in Russian says. A string the
builder rendered without a mark survives in the build's language, and the
comparison names it.

It mirrors the script, not the browser: scripts and styles are left alone, and
the `<title>` (which the script sets from `PAGE_TITLE`) and the root's `lang`
are normalised away by `comparable`.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
import i18n  # noqa: E402

_OPEN = re.compile(r"<([a-z][a-z0-9]*)\b([^<>]*)>")
_RAW = re.compile(r"(<script\b.*?</script>|<style\b.*?</style>)", re.S)
ATTRIBUTES = ("aria-label", "placeholder", "title", "data-label")


def _get(attrs: str, name: str) -> str | None:
    m = re.search(r"\s" + re.escape(name) + r'="([^"]*)"', attrs)
    return html.unescape(m.group(1)) if m else None


def _args(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _number(raw: str) -> float | int:
    value = float(raw)
    return int(value) if value.is_integer() else value


def _relocalize_part(part: str, locale: str) -> str:
    out, pos = [], 0
    for m in _OPEN.finditer(part):
        tag, attrs = m.group(1), m.group(2)
        new_attrs = attrs
        for name in ATTRIBUTES:
            msgid = _get(attrs, f"data-t-{name}")
            if msgid is None:
                continue
            text = i18n.translate(msgid, locale, **_args(_get(attrs, f"data-t-{name}-args")))
            new_attrs = re.sub(r"(\s" + re.escape(name) + r'=")[^"]*(")',
                               lambda a: a.group(1) + html.escape(text) + a.group(2), new_attrs, count=1)
        out.append(part[pos:m.start()])
        out.append(f"<{tag}{new_attrs}>")
        pos = m.end()
        msgid, date, num = _get(attrs, "data-t"), _get(attrs, "data-date"), _get(attrs, "data-num")
        if msgid is None and date is None and num is None:
            continue
        close = part.find("<", pos)
        if close < 0:
            continue
        if msgid is not None:
            text = i18n.translate(msgid, locale, **_args(_get(attrs, "data-t-args")))
        elif date is not None:
            text = i18n.format_date(date, locale)
        else:
            text = i18n.format_number(_number(num), locale)
        out.append(html.escape(text))
        pos = close
    out.append(part[pos:])
    return "".join(out)


def relocalize(markup: str, locale: str) -> str:
    """`markup` as the page script leaves it for a reader in `locale`."""
    return "".join(p if _RAW.fullmatch(p) else _relocalize_part(p, locale) for p in _RAW.split(markup))


def comparable(markup: str) -> str:
    """Without what the script handles apart — the root's language attributes
    and the document title — and without the inline scripts, whose data is
    each build's own (its paths and clocks), not text a reader sees."""
    markup = re.sub(r'<html lang="[^"]*"([^>]*) data-build-locale="[^"]*">', r"<html\1>", markup)
    markup = re.sub(r"<script>.*?</script>", "<script></script>", markup, flags=re.S)
    markup = re.sub(r"<title>.*?</title>", "<title></title>", markup, flags=re.S)
    # Two builds are seconds apart: their clocks are not their language.
    markup = re.sub(r"\d{2}:\d{2}:\d{2}", "hh:mm:ss", markup)
    return re.sub(r'data-signature="[^"]*"', 'data-signature=""', markup)


def first_difference(a: str, b: str, context: int = 80) -> str:
    """Where two pages part, with a little of each around the spot."""
    i = next((k for k, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    return f"…{a[max(0, i - context):i + context]!r}\n  vs\n…{b[max(0, i - context):i + context]!r}"
