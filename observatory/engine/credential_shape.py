r"""Does this text carry something shaped like a credential? One answer, everywhere.

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

THREE READINGS of the one rule (OBS-595), named by `reading=`:

  ENGINE    the shapes above: every door that judges text this engine keeps
  CONTRACT  fabric-agent-contract's `secretValue` (`src/secret-shape.ts`, FAC-SEM-167,
            DEC-0151), read exactly as JavaScript reads it: `\s` is `JS_SPACE`, `.`
            any code unit but a line terminator, `\d` is `[0-9]`, no case folding. A
            JWT anywhere, `Bearer ` at the start, a provider key in its canonical case
            after a boundary (an `sk-` key only with its strength), or a 40+ run mixing
            three classes. The operations digest and the folder-attribution port answer
            what the contract's own reader answers, over the contract's vectors.
  STRICT    the CONTRACT reading, plus any mention of a contract prefix or `eyJ` at a
            word start and any 40+ run of the base64 alphabet: for strings this engine
            writes itself and a host reads with the contract's detector, where a false
            refusal costs nothing and a hidden declaration costs the declaration.

THE ISSUERS. `PREFIXES` are the shapes; `ISSUERS` adds the longer forms of `sk-`
that name one provider (`OPENROUTER` is `sk-or-v1-`): the commit gate measures a
body without them, and a site that asks "is this an OpenRouter key" asks
`openrouter_key`, never its own copy of the prefix.
"""
from __future__ import annotations

import re

#: Vendor prefixes that mean "this is a credential" when a random body follows.
PREFIXES = ("sk-", "sk_", "rk_", "pk_live_", "pk_test_", "ghp_", "gho_", "ghu_", "ghs_",
            "ghr_", "github_pat_", "xoxa-", "xoxb-", "xoxp-", "xoxr-", "xoxs-", "xapp-",
            "glpat-", "lin_api_", "whsec_", "dop_v1_", "hf_", "npm_", "shpat_", "shpss_",
            "figd_", "sntrys_", "dckr_pat_", "AIza", "ntn_", "ol_api_", "secret_", "GOCSPX-",
            "tvly-")

#: A vendor whose body is shorter than the commit gate's default measurable
#: length still defines it exactly: a GitLab token is `glpat-` and twenty
#: characters. `tools/check_secrets.py` reads its minimum from here.
BODY_MIN = {"glpat-": 20, "AIza": 20}

#: Longer forms of `sk-`, each naming the provider that issued the key.
OPENROUTER = "sk-" + "or-v1-"
OPENAI_PROJECT = "sk-" + "proj-"
ANTHROPIC = "sk-" + "ant-api03-"
#: Any OpenRouter key's family, whatever its version: an `sk-` value under it is not OpenAI's.
OPENROUTER_FAMILY = "sk-" + "or-"
#: Every issuer prefix, the longest first: the first a text starts with is its issuer.
ISSUERS = tuple(sorted({OPENROUTER, OPENAI_PROJECT, ANTHROPIC, *PREFIXES}, key=len, reverse=True))


def issuer_of(text: object) -> str | None:
    """The longest issuer prefix `text` starts with, or None."""
    if not isinstance(text, str):
        return None
    return next((pre for pre in ISSUERS if text.startswith(pre)), None)


def openrouter_key(value: object) -> bool:
    """Whether `value` starts as an OpenRouter key does (its prefix only; the body is not judged)."""
    return isinstance(value, str) and value.startswith(OPENROUTER)

_PREFIXED = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(p) for p in PREFIXES)
                       + r")([A-Za-z0-9_\-]{16,})")
#: An AWS access key id, without the boundary before it (each reader states its own).
AWS_ID = r"(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])"
_AWS = re.compile(r"(?<![A-Za-z0-9])" + AWS_ID)
# A JWT starts a token. Without the boundary, a run of `eyJ` was scanned to its end
# from every occurrence looking for the dot — quadratic in the input.
_JWT = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_PEM = re.compile(r"-----BEGIN[A-Z ]*-----|-----BEGIN [A-Z ]*KEY")
#: A private key block, any algorithm word, ENCRYPTED (PKCS#8) and DSA included: the
#: commit gate's label for it. `_PEM` above reads more (any BEGIN line).
PRIVATE_KEY_BLOCK = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY")
# The scheme is bounded: unbounded, it ran from every position to the end of the
# input looking for `://` (a 2 MB statement took about an hour). Schemes are short;
# the match may start inside a longer run, which redacts more, never less.
_URL_PASSWORD = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{0,31}://[^/\s:@]+:[^/\s@]+@")
_UUID = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9A-Za-z])")
_HEX = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{32,}(?![0-9A-Za-z])")
HEX = _HEX
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
    "bearer": "a value that says it is a bearer credential",
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


# ── the CONTRACT reading: fabric-agent-contract `src/secret-shape.ts` ─────────────────────────────────────────

#: ECMAScript's `\s`: WhiteSpace and LineTerminator. Python's `\s` differs (it reads
#: U+001C..U+001F and U+0085 as space and not U+FEFF), so a contract pattern spells this.
JS_SPACE = "\t\n\x0b\x0c\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
#: JavaScript's `.` without the `s` flag: any code unit but a line terminator.
_JS_DOT = r"[^\n\r\u2028\u2029]"
#: The provider prefixes the contract names, in their canonical case.
CONTRACT_PREFIXES = r"sk-|gh[pousr]_|xox[abposr]-|AKIA|glpat-"
#: A JWT or JWS anywhere: three base64url segments, the first a JSON object's `eyJ`. The
#: contract's `JWT_SHAPE`; `has_jwt` reads it in one pass.
JWT_SHAPE = re.compile(r"eyJ[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]*")
#: `Bearer` at the start in any case, then a space and anything. The contract's own spelling.
_CONTRACT_BEARER = r"\A[Bb][Ee][Aa][Rr][Ee][Rr][" + JS_SPACE + r"]" + _JS_DOT
#: A provider key's prefix in its canonical case only, after a boundary (the start, a character that is no ASCII
#: letter or digit, or an escape), and an `sk-` key only with its strength (DEC-0151, OBS-587).
_CONTRACT_PREFIXED = (
    r"(?:\A|[^A-Za-z0-9]|%[0-9A-Fa-f]{2}|\\[nrt\"']|\\u[0-9A-Fa-f]{4}|\\x[0-9A-Fa-f]{2}"
    r"|\\[bfv0ae]|\\[0-7]{1,3}|%25[0-9A-Fa-f]{2}|\x1b\[[0-9;:]*m|(?:\\u001[bB]|\\x1[bB]|\\e|\\033|\\33)\[[0-9;:]*m)"
    r"(?:sk-(?=[A-Za-z0-9_-]{0,255}?[0-9])(?:(?=[A-Za-z0-9_-]{0,255}?[A-Z])[A-Za-z0-9_-]{16,}|[A-Za-z0-9_-]{32,})"
    r"|gh[pousr]_[A-Za-z0-9]{20,}|xox[abposr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|glpat-[A-Za-z0-9_-]{20,})")
#: The contract's `SECRET_VALUE`, with no case flag (Python folds U+017F onto `s` and U+212A onto `k`).
CONTRACT_SECRET_VALUE = re.compile("(" + _CONTRACT_BEARER + "|" + _CONTRACT_PREFIXED + ")")
_CONTRACT_BEARER_RE = re.compile(_CONTRACT_BEARER)
#: A run of the base64 and base64url alphabets, 40 or more long.
LONG_RUN = re.compile(r"[A-Za-z0-9+/=_-]{40,}")
_BASE64URL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
#: The STRICT reading's extras: a contract prefix or `eyJ` mentioned at a word start.
_MENTION = re.compile(r"\b(?:" + CONTRACT_PREFIXES + r"|eyJ)")


def _run_end(text: str, start: int) -> int:
    end = start
    while end < len(text) and text[end] in _BASE64URL:
        end += 1
    return end


def has_jwt(text: str) -> bool:
    """`hasJwt`: whether `JWT_SHAPE` matches anywhere in `text`, read in one pass (the regex backtracks from every
    `eyJ` of a long run): every `eyJ` of one base64url run shares that run's end, so only its first is tried."""
    at = text.find("eyJ")
    while at >= 0:
        one = _run_end(text, at + 3)
        if one - at - 3 >= 2 and one < len(text) and text[one] == ".":
            two = _run_end(text, one + 1)
            if two - one - 1 >= 2 and two < len(text) and text[two] == ".":
                return True
        at = text.find("eyJ", one)
    return False


def long_secret(value: str) -> bool:
    """A run of 40 or more base64 or base64url characters that mixes upper case, lower case and a digit."""
    return any(re.search(r"[a-z]", run) and re.search(r"[A-Z]", run) and re.search(r"[0-9]", run)
               for run in LONG_RUN.findall(value))


def _contract_kind(value: str) -> str | None:
    if has_jwt(value):
        return "jwt"
    if _CONTRACT_BEARER_RE.search(value):
        return "bearer"
    if CONTRACT_SECRET_VALUE.search(value):
        return "prefixed"
    return "random" if long_secret(value) else None


def contract_secret(value: str) -> bool:
    """The contract's `secretValue`: a JWT anywhere, a provider key or `Bearer ...`, or a long secret-shaped run."""
    return has_jwt(value) or bool(CONTRACT_SECRET_VALUE.search(value)) or long_secret(value)


def _strict_kind(value: str) -> str | None:
    kind = _contract_kind(value)
    if kind:
        return kind
    mention = _MENTION.search(value)
    if mention:
        return "jwt" if mention.group(0) == "eyJ" else "prefixed"
    return "random" if LONG_RUN.search(value) else None


#: The readings of the rule (see the module's text).
ENGINE, CONTRACT, STRICT = "engine", "contract", "strict"
_READINGS = {CONTRACT: _contract_kind, STRICT: _strict_kind}


def find(text: object, *, uuids: bool = True, reading: str = ENGINE) -> str | None:
    """The kind of the first credential shape in `text`, or None.

    `uuids=False` is for a field whose documented content IS a UUID (an agent
    session id declared as a caller name); everywhere else a UUID is refused,
    because several providers issue keys in exactly that shape. `reading` names
    the reading (ENGINE, CONTRACT or STRICT); `uuids` applies to ENGINE only."""
    if not isinstance(text, str) or not text:
        return None
    if reading != ENGINE:
        try:
            return _READINGS[reading](text)
        except KeyError:
            raise ValueError(f"no such reading of the credential shape: {reading!r}") from None
    spans = _spans(text, uuids)
    return spans[0][2] if spans else None


def shaped(text: object, *, uuids: bool = True, reading: str = ENGINE) -> bool:
    return find(text, uuids=uuids, reading=reading) is not None


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


def shown(value: object, *, pattern: re.Pattern | None = None, own_id: bool = False) -> str:
    """`value` itself when a refusal may quote it (see `echo`), otherwise a
    marker carrying only its length — for a JSON field rather than a sentence.

    `own_id=True` says `pattern` is the exact shape of an id THIS program
    mints (`chat-` and 32 hex characters): matching it is then enough, since
    such an id is random by design and would otherwise read as a key."""
    if not isinstance(value, str):
        return f"<a {type(value).__name__}, not shown>"
    if own_id and pattern is not None and pattern.fullmatch(value):
        return value
    ok = (_ECHOABLE.fullmatch(value) is not None and not shaped(value)
          and (pattern is None or pattern.fullmatch(value) is not None))
    return value if ok else f"<{len(value)} characters, not shown>"


def echo(value: object, *, pattern: re.Pattern | None = None, own_id: bool = False) -> str:
    """How a refusal names the caller's input: quoted when it is a well-formed
    identifier (it helps the caller find its typo), otherwise only its length.

    `pattern`, when given, is the field's own shape; a value must pass it AND
    carry no credential shape to be quoted. A refusal is written to a transcript,
    and the input most worth refusing is a key pasted into the wrong field."""
    said = shown(value, pattern=pattern, own_id=own_id)
    return repr(value) if said == value else said


def refuse(field: str, text: object, *, uuids: bool = True) -> None:
    """Raise ValueError when `text` carries a credential shape; the message names
    the field and the shape, never the text."""
    kind = find(text, uuids=uuids)
    if kind:
        raise ValueError(f"{field} looks like it carries a credential ({describe(kind)}); "
                         f"a value never travels in a name or a note — it goes on stdin "
                         f"into a vault slot")
