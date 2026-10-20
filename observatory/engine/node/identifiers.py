"""Identifier equality (contract `src/identifiers.ts`, DEC-0076): two identifiers are the same when their normal forms
are equal.

A URI's normal form is RFC 3986 §6.2.2 syntax-based normalization: scheme and host lower-cased, percent-encoding hex
upper-cased, percent-encoded unreserved characters decoded, dot segments removed from a hierarchical path and only the
leading ones from a rootless path; nothing scheme-based. A `urn:` follows RFC 8141 §3.1: scheme and NID lower-cased,
hex upper-cased, nothing decoded, the NSS's case kept, r-, q- and f-components ignored. A string that is not a URI is
its own normal form. Nothing here raises.
"""
from __future__ import annotations

import re
from typing import Any, Optional

_UNRESERVED = r'A-Za-z0-9\-._~'
_SUB_DELIMS = r"!$&'()*+,;="
_PCT = r'%[0-9A-Fa-f]{2}'


def _run(chars: str) -> 're.Pattern':
    return re.compile(rf'^(?:[{chars}]|{_PCT})*$')


_USERINFO = _run(_UNRESERVED + _SUB_DELIMS + ':')
_REG_NAME = _run(_UNRESERVED + _SUB_DELIMS)
_PATH = _run(_UNRESERVED + _SUB_DELIMS + ':@/')
_QUERY = _run(_UNRESERVED + _SUB_DELIMS + ':@/?')
_PORT = re.compile(r'^[0-9]*$')
_SCHEME = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*$')
_H16 = '[0-9A-Fa-f]{1,4}'
_DEC_OCTET = '(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])'
_IPV4 = rf'{_DEC_OCTET}(?:\.{_DEC_OCTET}){{3}}'
_LS32 = f'(?:{_H16}:{_H16}|{_IPV4})'
_IPV6 = '|'.join([
    f'(?:{_H16}:){{6}}{_LS32}',
    f'::(?:{_H16}:){{5}}{_LS32}',
    f'(?:{_H16})?::(?:{_H16}:){{4}}{_LS32}',
    f'(?:(?:{_H16}:){{0,1}}{_H16})?::(?:{_H16}:){{3}}{_LS32}',
    f'(?:(?:{_H16}:){{0,2}}{_H16})?::(?:{_H16}:){{2}}{_LS32}',
    f'(?:(?:{_H16}:){{0,3}}{_H16})?::{_H16}:{_LS32}',
    f'(?:(?:{_H16}:){{0,4}}{_H16})?::{_LS32}',
    f'(?:(?:{_H16}:){{0,5}}{_H16})?::{_H16}',
    f'(?:(?:{_H16}:){{0,6}}{_H16})?::',
])
_IP_LITERAL = re.compile(rf'^\[(?:{_IPV6}|[Vv][0-9A-Fa-f]+\.[{_UNRESERVED}{_SUB_DELIMS}:]+)\]$')
_URN_NAME = re.compile(r'^([A-Za-z0-9][A-Za-z0-9-]{0,30}[A-Za-z0-9]):(.+)$', re.S)
_SPLIT = re.compile(r'^([^:/?#]+):(?://([^/?#]*))?([^?#]*)(?:\?([^#]*))?(?:#(.*))?$', re.S)
_UNRESERVED_CHAR = re.compile(r'^[A-Za-z0-9\-._~]$')


def _percent(text: str, keep_encoded: bool = False) -> str:
    def one(match):
        hexa = match.group(1)
        char = chr(int(hexa, 16))
        return char if not keep_encoded and _UNRESERVED_CHAR.fullmatch(char) else '%' + hexa.upper()
    return re.sub(r'%([0-9A-Fa-f]{2})', one, text)


def _strip_leading_dots(path: str) -> str:
    start = 0
    while path.startswith('./', start) or path.startswith('../', start):
        start += 2 if path.startswith('./', start) else 3
    rest = path[start:]
    return path if rest == '' or rest.startswith('/') else rest


def _remove_dot_segments(path: str) -> str:
    absolute = path.startswith('/')
    segments = (path[1:] if absolute else path).split('/')
    output = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment in ('.', '..'):
            if segment == '..' and output:
                output.pop()
            if last:
                output.append('')
            continue
        output.append(segment)
    return ('/' if absolute else '') + '/'.join(output)


def _authority_parts(authority: str):
    at = authority.rfind('@')
    userinfo = None if at == -1 else authority[:at]
    host_port = authority if at == -1 else authority[at + 1:]
    bracket = host_port.find(']') if host_port.startswith('[') else -1
    colon = host_port.find(':', 0 if bracket == -1 else bracket)
    host = host_port if colon == -1 else host_port[:colon]
    port = None if colon == -1 else host_port[colon + 1:]
    if userinfo is not None and not _USERINFO.fullmatch(userinfo):
        return None
    if port is not None and not _PORT.fullmatch(port):
        return None
    if not (_IP_LITERAL.fullmatch(host) if host.startswith('[') else _REG_NAME.fullmatch(host)):
        return None
    return userinfo, host, port


def _parse(text: str):
    match = _SPLIT.fullmatch(text)
    if not match:
        return None
    scheme, authority, path, query, fragment = match.groups()
    if not _SCHEME.fullmatch(scheme) or not _PATH.fullmatch(path):
        return None
    if query is not None and not _QUERY.fullmatch(query):
        return None
    if fragment is not None and not _QUERY.fullmatch(fragment):
        return None
    if authority is not None and _authority_parts(authority) is None:
        return None
    if authority is None and path == '':
        return None
    return scheme, authority, path, query, fragment


def _urn_form(parts) -> str:
    _, authority, path, query, _ = parts
    name = _URN_NAME.fullmatch(path)
    path = f'{name.group(1).lower()}:{_percent(name.group(2), True)}' if name else _percent(path, True)
    query = '?' + _percent(query, True) if query is not None and not re.match(r'^[+=]', query) else ''
    authority = '//' + _percent(authority, True) if authority is not None else ''
    return f'urn:{authority}{path}{query}'


def _generic_form(parts) -> str:
    scheme, authority, path, query, fragment = parts
    rendered = ''
    if authority is not None:
        userinfo, host, port = _authority_parts(authority)
        lowered = re.sub(r'%[0-9a-f]{2}', lambda m: m.group(0).upper(), _percent(host).lower())
        rendered = '//' + (_percent(userinfo) + '@' if userinfo is not None else '') + lowered \
            + (':' + port if port is not None else '')
    decoded = _percent(path)
    hierarchical = authority is not None or decoded.startswith('/')
    out = _remove_dot_segments(decoded) if hierarchical else _strip_leading_dots(decoded)
    if authority is None and out.startswith('//'):
        out = decoded
    query = '?' + _percent(query) if query is not None else ''
    fragment = '#' + _percent(fragment) if fragment is not None else ''
    return f'{scheme.lower()}:{rendered}{out}{query}{fragment}'


def normal_identifier(value: Any) -> Optional[str]:
    """The normal form of an identifier; any other string as it is; None for a value that is not a string."""
    if not isinstance(value, str):
        return None
    parts = _parse(value)
    if parts is None:
        return value
    return _urn_form(parts) if parts[0].lower() == 'urn' else _generic_form(parts)


def same_identifier(a: Any, b: Any) -> bool:
    """The same when the normal forms are equal; a value that is not a string is compared as it is."""
    if not isinstance(a, str) or not isinstance(b, str):
        return a == b and type(a) is type(b)
    return normal_identifier(a) == normal_identifier(b)
