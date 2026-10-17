"""Blobs through a relay (relay.md §Blobs, DEC-0047 item 8): content-addressed bytes a node puts for named recipients
and fetches by digest, over HTTPS, never in frames.

A blob's name is `sha256:` and the hex SHA-256 of its bytes. Each request carries `Fabric-Relay-Request`, an ES256
signature over `relay-blob/1` bound to the method, the digest and the recipients, accepted by the relay within 60 s; it
is not a bearer credential. A fetched blob is believed only when its bytes hash to the digest asked for: the relay
stores bytes, it does not vouch for them.

A relay's refusal is `{"error": {"code", "message"}}` with an HTTP status; it is raised as `BlobRefused` with both. A
GET that is not allowed — not held, expired, not a recipient, another recipient list — is one 403 `not-admitted`.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from typing import Callable, Optional, Sequence, Tuple

from . import relay

MAX_BLOB_BYTES = 33_554_432
TIMEOUT_SECONDS = 60.0
#: The whole exchange, however the bytes trickle in: a relay that sends one byte a minute never times out per read.
DEADLINE_SECONDS = 300.0
#: The most of an error body read: its code and message are small.
MAX_ERROR_BYTES = 65_536
_DIGEST = re.compile(r'^sha256:[0-9a-f]{64}$')


class BlobRefused(Exception):
    def __init__(self, status: int, code: str, message: str = ''):
        super().__init__(f'{status} {code}' + (': ' + message if message else ''))
        self.status = status
        self.code = code


def digest_of(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A relay answers in place; a redirect would carry the signed header to another origin."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
# A relay on this machine is reached directly, never through a proxy the environment names.
_LOCAL_OPENER = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))


def _request(origin: str, method: str, digest: str, header: str, data: Optional[bytes]) -> Tuple[int, bytes]:
    if not relay.is_relay_origin(origin):
        raise ValueError('not a relay origin')
    if not _DIGEST.match(digest):
        raise ValueError('a blob digest is sha256: and 64 lowercase hex digits')
    request = urllib.request.Request(origin + '/v1/blobs/' + digest, data=data, method=method)
    request.add_header(relay.REQUEST_HEADER, header)
    if data is not None:
        request.add_header('Content-Type', 'application/octet-stream')
    try:
        opener = _LOCAL_OPENER if origin.startswith('http://') else _OPENER
        deadline = time.monotonic() + DEADLINE_SECONDS
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, _read_bounded(response, MAX_BLOB_BYTES + 1, deadline)
    except urllib.error.HTTPError as error:
        try:
            document = json.loads(_read_bounded(error, MAX_ERROR_BYTES, time.monotonic() + TIMEOUT_SECONDS))
            refusal = document.get('error') if isinstance(document, dict) else None
        except (ValueError, UnicodeDecodeError, RecursionError):
            refusal = None
        finally:
            error.close()
        if not isinstance(refusal, dict):                  # the documented shape, or no code at all
            refusal = {}
        raise BlobRefused(error.code, str(refusal.get('code', 'unknown')), str(refusal.get('message', ''))) from None


def _read_bounded(response, limit: int, deadline: float) -> bytes:
    """At most `limit` bytes, and nothing after `deadline` (`time.monotonic()`): a refusal past it."""
    chunks, total = [], 0
    while total < limit:
        if time.monotonic() > deadline:
            raise BlobRefused(getattr(response, 'status', 0) or 0, 'timeout', 'the relay sent the body too slowly')
        read = getattr(response, 'read1', None) or response.read      # an HTTPError's body has no read1
        chunk = read(min(65_536, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b''.join(chunks)


def put_blob(origin: str, keys, data: bytes, recipients: Sequence[str], *,
             clock: Callable[[], float] = time.time) -> Tuple[str, bool]:
    """Uploads `data` for `recipients` (their `#sig` kids). Returns the digest and whether the relay already held the
    same blob for the same list (200 rather than 201). A blob held under another list is `BlobRefused` 409."""
    if len(data) > MAX_BLOB_BYTES:
        raise ValueError('a blob is at most 32 MiB')
    digest = digest_of(data)
    header = relay.relay_request_header(keys.sign, relay=origin, method='PUT', digest=digest, kid=keys.sig_kid,
                                        recipients=list(recipients), iat=relay.rfc3339(int(clock() * 1000)))
    status, _ = _request(origin, 'PUT', digest, header, data)
    if status not in (200, 201):
        raise BlobRefused(status, 'unexpected-status')
    return digest, status == 200


def get_blob(origin: str, keys, digest: str, recipients: Sequence[str], *,
             clock: Callable[[], float] = time.time) -> bytes:
    """Fetches a blob put for `recipients` (the list exactly as the sender signed it, this machine among them). The bytes
    are returned only when they hash to `digest`."""
    header = relay.relay_request_header(keys.sign, relay=origin, method='GET', digest=digest, kid=keys.sig_kid,
                                        recipients=list(recipients), iat=relay.rfc3339(int(clock() * 1000)))
    status, body = _request(origin, 'GET', digest, header, None)
    if status != 200 or len(body) > MAX_BLOB_BYTES:
        raise BlobRefused(status, 'unexpected-status' if status != 200 else 'too-large')
    if digest_of(body) != digest:
        raise BlobRefused(status, 'digest-mismatch', 'the relay returned bytes that are not the named blob')
    return body
