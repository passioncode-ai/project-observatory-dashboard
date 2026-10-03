#!/usr/bin/env python3
"""Search keys for Russian and English text: one key per word form a reader means alike.

The lexical index matched words exactly, so a question asking who *переключает*
an account did not find the record about *переключение* of an account: Russian
inflects every noun and verb, and a search that cannot see through that answers
half of the operator's questions from the wrong records. English loses less, but
`exports` and `exported` were two words too.

A key is the word's Snowball stem (`snowballstemmer`, the Snowball project's own
Python port), and for Cyrillic it is then cut to `RU_KEY` characters. The cut is
deliberate: Snowball's Russian stemmer is conservative — *переключение*,
*переключает* and *переключить* come out as three stems — and truncating stems
to a fixed length is the measured way Russian retrieval closes that gap. It also
joins some words that are not related; the coverage floor in `survey.search`
and the ranking keep that from reaching an answer.

Without the stemmer (an install without the full extras) the key is the word,
lower-cased and cut the same way, and `STEMMER` says so for the caller to report.
"""
from __future__ import annotations

import re

try:
    import snowballstemmer
    _RU = snowballstemmer.stemmer("russian")
    _EN = snowballstemmer.stemmer("english")
    STEMMER = True
except ImportError:                                                            # pragma: no cover
    _RU = _EN = None
    STEMMER = False

#: How many characters of a Russian stem make its key.
RU_KEY = 6
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_CYRILLIC = re.compile(r"[а-яё]")

#: Words that carry no subject. Removed from a QUERY only — the index keeps every
#: word, so a list that turns out too long costs a query term, not the index.
STOP = frozenset("""
a an the is are was were be been being do does did of to in on at for by with from and or
but not no our we us you your it its this that these those what which who whom whose when
where why how can could should would will shall may might must has have had there here
than then so as if into out up about any all each
и в во на с со по к ко о об от до за из у при для же ли не ни а но или что как кто где
когда почему зачем какой какая какое какие каков который которая которое которые это этот
эта эти тот та те то мы наш наша наше наши вы ваш он она они его её их ей им ему
сколько чей чья чьё чьи бы был была было были есть быть ещё уже так также
""".split())


def _key(word: str) -> str:
    w = word.lower().replace("ё", "е")
    if _CYRILLIC.search(w):
        stem = _RU.stemWord(w) if _RU else w
        return stem[:RU_KEY]
    return _EN.stemWord(w) if _EN else w


def keys(text: str | None) -> list[str]:
    """Every word's key, in order: what the index stores for a record."""
    return [_key(w) for w in _WORD.findall(text or "")]


def query_keys(text: str | None) -> list[str]:
    """The keys a question asks for: subject words only, each once, in order."""
    out: list[str] = []
    for w in _WORD.findall(text or ""):
        if w.lower().replace("ё", "е") in STOP or len(w) < 2:
            continue
        k = _key(w)
        if k and k not in out:
            out.append(k)
    return out


def stems_of(statement: str | None, why: str | None) -> str:
    """The lexical index's `stems` column for one record."""
    return " ".join(keys(f"{statement or ''}\n{why or ''}"))
