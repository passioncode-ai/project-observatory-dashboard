"""RFC 8785, the JSON Canonicalization Scheme: the bytes every relay signature covers (contract DEC-0039, DEC-0043).

Members sorted by UTF-16 code units at every level, no whitespace, numbers in ECMAScript form (`1e+30`, `0.002`,
`-0` as `0`), strings escaped as `JSON.stringify` escapes them. A value with no canonical form — a non-finite number,
a lone surrogate, anything JSON cannot carry — raises `CanonicalError`, so two different values never share bytes.
The contract's `rfc8785` (`src/canonical.ts`) is the reference; `fixtures/vectors/canonical.json` holds both to it.
"""
from __future__ import annotations

import decimal
import math
from typing import Any


class CanonicalError(TypeError):
    """The value has no RFC 8785 form."""


_ESCAPES = {'"': '\\"', '\\': '\\\\', '\b': '\\b', '\f': '\\f', '\n': '\\n', '\r': '\\r', '\t': '\\t'}


def _string(text: str) -> str:
    out = ['"']
    for char in text:
        code = ord(char)
        if 0xD800 <= code <= 0xDFFF:
            raise CanonicalError('a lone surrogate has no canonical form')
        if char in _ESCAPES:
            out.append(_ESCAPES[char])
        elif code < 0x20:
            out.append('\\u%04x' % code)
        else:
            out.append(char)
    out.append('"')
    return ''.join(out)


def _number(value: float) -> str:
    """ECMAScript Number::toString(10) of a finite double (ECMA-262 §6.1.6.1.20)."""
    if not math.isfinite(value):
        raise CanonicalError('a non-finite number has no canonical form')
    if value == 0:
        return '0'
    if value < 0:
        return '-' + _number(-value)
    # repr is the shortest string that round-trips, the same digits ECMAScript chooses.
    sign, digits, exponent = decimal.Decimal(repr(value)).as_tuple()
    text = ''.join(map(str, digits)).rstrip('0')
    exponent += len(digits) - len(text)
    k, n = len(text), exponent + len(text)
    if k <= n <= 21:
        return text + '0' * (n - k)
    if 0 < n <= 21:
        return text[:n] + '.' + text[n:]
    if -6 < n <= 0:
        return '0.' + '0' * (-n) + text
    e = n - 1
    mantissa = text if k == 1 else text[0] + '.' + text[1:]
    return mantissa + 'e' + ('+' if e >= 0 else '-') + str(abs(e))


def _utf16(key: str) -> bytes:
    try:
        return key.encode('utf-16-be')
    except UnicodeEncodeError as error:
        raise CanonicalError('a lone surrogate has no canonical form') from error


#: How deeply a value may nest. RFC 8785 sets no bound; this one keeps recursion far from Python's limit, and every
#: document a node signs or checks nests a few levels. A deeper value has no canonical form here.
MAX_DEPTH = 64


def rfc8785(value: Any, _depth: int = 0) -> str:
    """The RFC 8785 text of a parsed JSON value; encode it as UTF-8 to sign or hash it."""
    if isinstance(value, (list, tuple, dict)) and _depth >= MAX_DEPTH:
        # The same bound as the node's parser (relay.MAX_JSON_DEPTH): containers nest at most 64 deep, so a node
        # never signs a document another node refuses to parse.
        raise CanonicalError('a value nested more than %d levels has no canonical form here' % MAX_DEPTH)
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, int):
        # JSON numbers are doubles: an integer is written as the double it parses to.
        try:
            return _number(float(value))
        except OverflowError as error:
            raise CanonicalError('a non-finite number has no canonical form') from error
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, (list, tuple)):
        return '[' + ','.join(rfc8785(item, _depth + 1) for item in value) + ']'
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise CanonicalError('an object key is a string')
        keys = sorted(value, key=_utf16)
        return '{' + ','.join(_string(key) + ':' + rfc8785(value[key], _depth + 1) for key in keys) + '}'
    raise CanonicalError(type(value).__name__ + ' is not a JSON value')


def canonical_bytes(value: Any) -> bytes:
    return rfc8785(value).encode('utf-8')


def is_canonical(value: Any, raw: bytes) -> bool:
    """Whether `raw` is exactly the RFC 8785 form of `value`; a value that has none is not."""
    try:
        return canonical_bytes(value) == raw
    except (CanonicalError, RecursionError, MemoryError):
        return False
