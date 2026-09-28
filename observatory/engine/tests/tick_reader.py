#!/usr/bin/env python3
"""Read `tools/tick.sh` as a sequence of INVOCATIONS, not as a string.

Four assertions in one sitting broke on the same shape: they located a step with
`src.find("some_script.py")` and compared offsets, so the day a COMMENT
mentioned that script the ordering assertion failed with nothing wrong.

* `tests/test_docs_current.py` — a retired claim quoted inside its own retirement
* `tools/corroborate.py`'s removed denylist — the words survived in the comment
  explaining the removal, and the test forbade the string outright
* `tests/test_tick.py` — "the lease is taken before build_findings.py", broken by
  a comment naming that file above the lease
* `tests/test_corroboration.py` — the same comparison, broken by the same comment

Three of those were fixed by hand. The fourth is why this module exists: a class
seen twice becomes a script rather than a third patch.

The distinction it encodes: a line RUNS something when it names the interpreter
(`"$PY"`) or the `step` helper, and a line that begins with `#` never runs
anything, whatever it mentions.
"""
from __future__ import annotations
import pathlib
import re


#: What makes a shell line an INVOCATION. The tick calls its steps through
#: `"$PY"` or the `step` helper; the companion hook uses a lower-case `"$py"`
#: it resolves itself. A caller passes its own markers rather than this module
#: guessing — the sixth assertion to break on "the string is in the file" was
#: written minutes after this module existed, because it compared
#: `index("tools/record_turn.py")` and found the EXISTENCE CHECK
#: `[ -f "$root/tools/record_turn.py" ]` rather than the call.
DEFAULT_MARKERS = ('"$PY"', "step ")

#: A `[ … ]` or `[[ … ]]` test that contains no command substitution: it names
#: paths and strings without executing any of them.
PURE_TEST = re.compile(r"\[\[?(?:(?!\$\().)*?\]\]?")


def run_lines(src: str, markers: tuple[str, ...] = DEFAULT_MARKERS) -> list[tuple[int, str]]:
    """Every (index, line) that actually executes something.

    A `[ -f … ]` test names a path without running it, and so does a comment.
    Only a line carrying one of `markers` runs anything.
    """
    out = []
    for i, line in enumerate(src.splitlines()):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        # A bracket test with no command substitution names things without
        # running them — `[ -f "$root/tools/record_turn.py" ] || exit 0` is why
        # this exists. One that DOES contain `$(` runs something inside itself,
        # like the re-entry guard `[ "$(read_field stop_hook_active)" = … ]`, so
        # it stays. Skipping every bracket line was the first attempt and it
        # lost the guard entirely.
        bare = PURE_TEST.sub(" ", s)
        if any((s.startswith(m) if m.endswith(" ") else m in bare) for m in markers):
            out.append((i, s))
    return out


def first_invocation(src: str, script: str,
                     markers: tuple[str, ...] = DEFAULT_MARKERS) -> int:
    """The line index where the tick first RUNS `script`, or -1.

    An index rather than a character offset: offsets invite `src.find`, which is
    the habit this module replaces.
    """
    for i, s in run_lines(src, markers):
        if script in s:
            return i
    return -1


def runs_before(src: str, first: str, second: str,
                markers: tuple[str, ...] = DEFAULT_MARKERS) -> tuple[bool, str]:
    """True when `first` is invoked before `second`, with the evidence."""
    a, b = first_invocation(src, first, markers), first_invocation(src, second, markers)
    if a == -1:
        return False, f"{first} is never invoked"
    if b == -1:
        return True, f"{first} at line {a + 1}; {second} is never invoked"
    return a < b, f"{first} at line {a + 1}, {second} at line {b + 1}"


def tick(root: pathlib.Path) -> str:
    return (root / "tools/tick.sh").read_text(encoding="utf-8")


def code_only(src: str) -> str:
    """A shell script with its `#` comments removed. Nothing else is touched.

    The seventh assertion to break on "the string is in the file" was in
    `tests/test_companion.py`, minutes after the fix it was checking: the hook's
    `${CLAUDE_PLUGIN_ROOT}/../../..` guess was replaced by a walk, and the
    COMMENT explaining the replacement contains the string the assertion
    forbade. Same shape as the five that produced `tests/source_reader.py`, in
    the other language.

    A shell heredoc can contain a `#` that is data rather than a comment, so
    this is deliberately line-based and shallow: it removes a line whose first
    non-space character is `#`, and a trailing ` #…` only when the `#` is
    preceded by whitespace and there is no quote after it on the line. Anything
    subtler needs a shell parser, and a checker that pretends to be one is worse
    than a blunt one that says what it does.
    """
    out = []
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        cut = re.split(r"\s#(?![^'\"]*['\"])", line, maxsplit=1)[0]
        out.append(cut)
    return "\n".join(out)
