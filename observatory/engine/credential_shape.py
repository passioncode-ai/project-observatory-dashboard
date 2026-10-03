"""Does this text carry something shaped like a credential? One answer, everywhere.

WHY ONE MODULE. Four doors decide this question about text a person or an agent
typed: the vault's slot names (`tools/vault.py`, and the keyserver routes that
call it), the signature fields of `tools/sign_credential.py`, the keyserver's
audit journal, and every refusal that would echo the caller's input back (the
MCP server, the keyserver). Each had its own pattern, and each pattern let a
different shape through: a 37-character token was accepted as a PROJECT name
and then printed in the registry, the findings, five dashboard pages and the
MCP answers; a 38-character token passed as a purpose; a UUID or a 32-character
hexadecimal key passed everything. Four copies of "is this a key" disagree the
day one is widened, so there is one.

WHAT COUNTS. Shapes a credential takes and an identifier does not:

  a provider prefix   `sk-`, `ghp_`, `github_pat_`, `xoxb-`, `glpat-`, `AIza`, …
                      followed by a random-looking body (16+ characters with a
                      digit or a capital, or 32+ of any kind — so
                      `npm_config_cache_dir` is a name)
  an AWS key id       `AKIA`/`ASIA` and sixteen upper-case alphanumerics
  a JSON web token    `eyJ…` `.` `…`
  a private key       `-----BEGIN …`
  a URL password      `scheme://user:password@host`
  a UUID              many providers issue keys in exactly this shape
  hexadecimal         a run of 32 or more: an API key, a hash of a secret, a
                      git object id. A commit is cited by its short form.
  a random run        an alphanumeric segment of 20+ characters mixing lower
                      case, upper case and digits, of 24+ in one case with
                      four or more digits and letters, or of 40+ of any kind;
                      or a base64-alphabet run
                      of 40+ with all three classes and few slashes (a path has
                      a slash every few characters, a key almost none)

WHAT DOES NOT, and the tests hold both directions: `alpha-web`,
`OPENROUTER_API_KEY`, `project:local-alpha-web`,
`credential:vault/alpha-web/prod/CF_API_TOKEN`, ISO timestamps, absolute paths,
URLs without a password, short commit ids, and a provider's truncated DISPLAY
form of a key (`sk-or-v1-abc...def`), which is a label rather than the key.

Deliberately generous on the refusing side: a false refusal costs a rewording,
and a credential that reaches a committed file, a journal or a transcript costs a
rotation and stays in the history.
"""
from __future__ import annotations

import re

#: Vendor prefixes that mean "this is a credential" when a random body follows.
PREFIXES = ("sk-", "sk_", "rk_", "pk_live_", "pk_test_", "ghp_", "gho_", "ghu_", "ghs_",
            "ghr_", "github_pat_", "xoxa-", "xoxb-", "xoxp-", "xoxr-", "xoxs-", "xapp-",
            "glpat-", "lin_api_", "whsec_", "dop_v1_", "hf_", "npm_", "shpat_", "shpss_",
            "figd_", "sntrys_", "dckr_pat_", "AIza")

_PREFIXED = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(p) for p in PREFIXES)
                       + r")([A-Za-z0-9_\-]{16,})")
_AWS = re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])")
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_PEM = re.compile(r"-----BEGIN[A-Z ]*-----|-----BEGIN [A-Z ]*KEY")
_URL_PASSWORD = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")
_UUID = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9A-Za-z])")
_HEX = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{32,}(?![0-9A-Za-z])")
_SEGMENT = re.compile(r"[A-Za-z0-9]{20,}")
_BASE64 = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")

#: What each kind is called in a refusal. Never the matched text.
KINDS = {
    "prefixed": "a provider key prefix with a random body",
    "aws": "an AWS access key id",
    "jwt": "a JSON web token",
    "pem": "a private key block",
    "url-password": "a URL carrying a password",
    "uuid": "a UUID",
    "hex": "a run of 32 or more hexadecimal characters",
    "random": "a long random-looking run of characters",
}


def _classes(s: str) -> tuple[bool, bool, bool]:
    return (any(c.islower() for c in s), any(c.isupper() for c in s),
            any(c.isdigit() for c in s))


def _random_segment(seg: str) -> bool:
    # Forty unbroken alphanumerics are no word and no identifier in any naming
    # convention used here, whatever their classes.
    if len(seg) >= 40:
        return True
    lower, upper, digit = _classes(seg)
    if lower and upper and digit:
        return True
    digits = sum(c.isdigit() for c in seg)
    return len(seg) >= 24 and digits >= 4 and len(seg) - digits >= 4


def _spans(text: str, uuids: bool) -> list[tuple[int, int, str]]:
    """(start, end, kind) of every credential-shaped run in `text`."""
    out: list[tuple[int, int, str]] = []
    for m in _PREFIXED.finditer(text):
        _, upper, digit = _classes(m.group(1))
        if upper or digit or len(m.group(1)) >= 32:
            out.append((m.start(), m.end(), "prefixed"))
    for pattern, kind in ((_AWS, "aws"), (_JWT, "jwt"), (_PEM, "pem"),
                          (_URL_PASSWORD, "url-password"), (_HEX, "hex")):
        out += [(m.start(), m.end(), kind) for m in pattern.finditer(text)]
    if uuids:
        out += [(m.start(), m.end(), "uuid") for m in _UUID.finditer(text)]
    out += [(m.start(), m.end(), "random") for m in _SEGMENT.finditer(text)
            if _random_segment(m.group(0))]
    for m in _BASE64.finditer(text):
        run = m.group(0)
        lower, upper, digit = _classes(run)
        if lower and upper and digit and run.count("/") * 12 <= len(run):
            out.append((m.start(), m.end(), "random"))
    return sorted(out)


def find(text: object, *, uuids: bool = True) -> str | None:
    """The kind of the first credential shape in `text`, or None.

    `uuids=False` is for a field whose documented content IS a UUID (an agent
    session id declared as a caller name); everywhere else a UUID is refused,
    because several providers issue keys in exactly that shape."""
    if not isinstance(text, str) or not text:
        return None
    spans = _spans(text, uuids)
    return spans[0][2] if spans else None


def shaped(text: object, *, uuids: bool = True) -> bool:
    return find(text, uuids=uuids) is not None


def describe(kind: str | None) -> str:
    return KINDS.get(kind or "", "a credential shape")


def redact(text: str, *, uuids: bool = True, marker: str = "[redacted]") -> str:
    """`text` with every credential-shaped run replaced by `marker`."""
    if not isinstance(text, str) or not text:
        return text
    spans = _spans(text, uuids)
    if not spans:
        return text
    merged: list[list[int]] = []
    for start, end, _ in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out, at = [], 0
    for start, end in merged:
        out += [text[at:start], marker]
        at = end
    out.append(text[at:])
    return "".join(out)


#: What a refusal may quote back: one short printable token of identifier
#: characters. Anything else is described by its length.
_ECHOABLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+=-]{0,119}")


def echo(value: object, *, pattern: re.Pattern | None = None) -> str:
    """How a refusal names the caller's input: quoted when it is a well-formed
    identifier (it helps the caller find its typo), otherwise only its length.

    `pattern`, when given, is the field's own shape; a value must pass it AND
    carry no credential shape to be quoted. A refusal is written to a transcript,
    and the input most worth refusing is a key pasted into the wrong field."""
    if not isinstance(value, str):
        return f"<a {type(value).__name__}, not shown>"
    ok = (_ECHOABLE.fullmatch(value) is not None and not shaped(value)
          and (pattern is None or pattern.fullmatch(value) is not None))
    return repr(value) if ok else f"<{len(value)} characters, not shown>"


def refuse(field: str, text: object, *, uuids: bool = True) -> None:
    """Raise ValueError when `text` carries a credential shape; the message names
    the field and the shape, never the text."""
    kind = find(text, uuids=uuids)
    if kind:
        raise ValueError(f"{field} looks like it carries a credential ({describe(kind)}); "
                         f"a value never travels in a name or a note — it goes on stdin "
                         f"into a vault slot")
