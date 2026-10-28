"""The share mailbox, `observatory-cloud/1`: the routes Project Observatory's nodes speak to the mailbox a sender names
in a share link (contract DEC-0142 PS-10; the routes and the name are the product's, not the contract's, CT-185).

The mailbox carries the frames, envelopes, reports and records of `fabric-peer-share/1` unchanged and holds
revocations. It signs nothing, opens nothing, and decides nothing about a value or about whom the sender trusts. Its
duties are mechanical: it runs `peer_exchange_verdict` over a share's frames and stops carrying a burnt exchange,
carries the frame that burns it to both nodes with its reason, stores no envelope and grants no lease for a burnt
share; after the first envelope is stored it runs no verdict and still carries a later claim or abort to the sender;
it grants an honest recipient a lease of at most 24 hours and counts each delivery; it refuses every renewal and
delivery after a revocation. It sees ciphertext and names, never a value.

Requests are signed with DEC-0056's construction (RFC 9421 subset: components `@method`, `@target-uri`,
`content-digest`; parameters `created`, `nonce`, `keyid`, `alg`) under the label `fabric-peer-share`, `keyid` the RFC 7638
thumbprint of the signing share `#sig`, `alg` `ecdsa-p256-sha256`; the mailbox keeps each share's nonces for the
300 s window. A share's creation is signed twice: by the share's own `#sig` and, under the label `observatory-owner`,
by the key registered for the mailbox's owner at deploy (`full cloud`, OBS-263), so nobody else files shares on a
person's mailbox.

The routes (every body JSON, every answer JSON; refusals `{"error": {"code", "message"}}`):

| Route | Who signs | What |
|---|---|---|
| `GET /.well-known/observatory-cloud` | nobody | `{protocol, peerShare}` |
| `PUT /v1/shares/{id}` | sender and owner | create: `{shareId, sender: {sig}, claimDeadline, leaseSeconds}` |
| `POST /v1/shares/{id}/frames` | the frame's side | carry one frame; a claim is signed by its own keys |
| `GET /v1/shares/{id}/frames` | either side | the frames, the verdict, the frames carried after sealing |
| `PUT /v1/shares/{id}/envelopes/{slot}` | sender | store one slot's envelope; only after a complete exchange |
| `POST /v1/shares/{id}/lease` | recipient | renew: a lease and the envelopes; counts a delivery; 410 after revocation |
| `POST /v1/shares/{id}/reports` | recipient | store a sealed usage report, a repeated sequence dropped |
| `GET /v1/shares/{id}/reports?after=N` | sender | the reports above sequence N |
| `PUT /v1/shares/{id}/revocation` | sender | hold the revocation; every later renewal is refused |
| `PUT /v1/shares/{id}/deletion` | recipient | hold the recipient's deletion receipt |
| `GET /v1/shares/{id}` | either side | status: verdict, sealed, deliveries, last renewal, revocation, deletion |

`FakeMailbox` is this module's in-process mailbox: the same rules, except that it is stricter about nonces (it keeps one
for every signed request, reads and refused ones included, and so refuses a replayed read the Worker answers), for tests
and for a loopback mailbox a person can run
to try a share between two workspaces on one machine. The deployed mailbox is the Worker under
`observatory/engine/cloud/` (OBS-263).
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from . import http_signature, p256, peer_share as PS, relay

PROTOCOL = 'observatory-cloud/1'
LABEL = 'fabric-peer-share'
OWNER_LABEL = 'observatory-owner'
ALG = 'ecdsa-p256-sha256'
WINDOW_SECONDS = 300
MAX_BODY_BYTES = 262_144
DEFAULT_LEASE_SECONDS = 3600
#: Late claims carried after sealing: one per claiming key, at most this many keys; the recipient's abort apart.
MAX_LATE_CLAIMS = 16
#: A loopback mailbox for a trial between two workspaces: `host=127.0.0.1:PORT`, read only for a loopback target.
RESOLVE_ENV = 'OBSERVATORY_CLOUD_RESOLVE'


class MailboxError(Exception):
    """A mailbox answered a refusal, or could not be reached. `code` is the mailbox's code or `unreachable`."""

    def __init__(self, code: str, message: str, status: int = 0, detail: Optional[dict] = None):
        super().__init__(message)
        self.code, self.status, self.detail = code, status, detail or {}


# ---- Request signatures (PS-10) -------------------------------------------------------------------------------------

def sign_request(signers: list, *, method: str, origin: str, target: str, body: bytes,
                 created: Optional[int] = None) -> Dict[str, str]:
    """The header fields of a request signed by each `(label, sig_jwk, private_key)` in `signers`, each with its own
    nonce, over the same components (RFC 9421 §4: one dictionary member per label)."""
    created = int(time.time()) if created is None else int(created)
    digest = http_signature.content_digest(body)
    inputs, signatures = [], []
    for label, jwk, key in signers:
        params = http_signature.signature_params(created, http_signature.new_nonce(), PS.peer_thumbprint(jwk), ALG)
        base = http_signature.signature_base(method, http_signature.target_uri(origin, target), digest, params)
        raw = p256.b64url_decode(key.sign(base.encode('utf-8')))
        inputs.append(f'{label}={params}')
        signatures.append(f'{label}=:{base64.b64encode(raw).decode("ascii")}:')
    return {'Content-Digest': digest, 'Signature-Input': ', '.join(inputs), 'Signature': ', '.join(signatures)}


def _lower(headers: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for name, value in (headers or {}).items():
        if isinstance(name, str):
            text = ', '.join(value) if isinstance(value, (list, tuple)) else value
            out[name.lower()] = text if name.lower() not in out else out[name.lower()] + ', ' + text
    return out


def read_signature(headers: Any, label: str, *, method: str, origin: str, target: str, body: bytes,
                   at_ms: int) -> dict:
    """The signature under `label`, checked for its shape, digest and freshness (not yet for its key): `{ok, keyid,
    nonce, base, signature}` or `{ok: False, code}` with `signature-missing`, `digest-mismatch` or `signature-stale`."""
    h = _lower(headers)
    try:
        inputs = [m for m in http_signature.parse_dictionary(h.get('signature-input', '')) if m[0] == label]
        sigs = [m for m in http_signature.parse_dictionary(h.get('signature', '')) if m[0] == label]
    except ValueError:
        return {'ok': False, 'code': 'signature-missing'}
    if len(inputs) != 1 or len(sigs) != 1:
        return {'ok': False, 'code': 'signature-missing'}
    _, value, params = inputs[0]
    if value[0] != 'inner' or [item for item, _ in value[1]] != [('string', c) for c in http_signature.COMPONENTS] \
            or any(p for _, p in value[1]) or [k for k, _ in params] != list(http_signature.PARAMETERS):
        return {'ok': False, 'code': 'signature-missing'}
    got = dict(params)
    created, nonce, keyid, alg = (got[k] for k in http_signature.PARAMETERS)
    if created[0] != 'integer' or nonce[0] != 'string' or keyid[0] != 'string' or alg != ('string', ALG) \
            or not http_signature.NONCE.fullmatch(nonce[1]) or not PS.BYTES32.fullmatch(keyid[1]):
        return {'ok': False, 'code': 'signature-missing'}
    _, sig_value, sig_params = sigs[0]
    if sig_value[0] != 'bytes' or sig_params or len(sig_value[1]) != 64:
        return {'ok': False, 'code': 'signature-missing'}
    digest = http_signature.content_digest(body)
    if h.get('content-digest', '').strip() != digest:
        return {'ok': False, 'code': 'digest-mismatch'}
    if abs(at_ms / 1000 - created[1]) > WINDOW_SECONDS:
        return {'ok': False, 'code': 'signature-stale'}
    params_text = http_signature.signature_params(created[1], nonce[1], keyid[1], ALG)
    base = http_signature.signature_base(method, http_signature.target_uri(origin, target), digest, params_text)
    return {'ok': True, 'keyid': keyid[1], 'nonce': nonce[1], 'created': created[1], 'base': base,
            'signature': p256.b64url_encode(sig_value[1])}


def signature_holds(read: dict, jwk: Any) -> bool:
    """Whether a read signature was made by the share key `jwk` (its thumbprint is the keyid, and it verifies)."""
    return (read.get('ok') is True and PS.peer_thumbprint(jwk) == read['keyid']
            and p256.verify_es256(jwk, read['base'], read['signature']) is True)


# ---- The mailbox's rules, in process ----------------------------------------------------------------------------------

def _answer(status: int, body: Any) -> Tuple[int, dict, bytes]:
    return status, {'content-type': 'application/json'}, json.dumps(body, sort_keys=True).encode('utf-8')


def _refuse(status: int, code: str, message: str, **detail) -> Tuple[int, dict, bytes]:
    return _answer(status, {'error': {'code': code, 'message': message, **detail}})


class FakeMailbox:
    """`observatory-cloud/1` in process: the rules the deployed mailbox keeps, for tests and for a loopback trial.

    State is in memory. `clock` answers milliseconds since the epoch; `owner` is the owner's public JWK whose
    signature a share's creation needs (None admits any creator, a loopback trial's choice). It never sees a value:
    `seen_bodies` records every body it was sent, so a test can prove only ciphertext and names reached it."""

    def __init__(self, origin: str, *, owner: Optional[dict] = None, clock: Callable[[], int] = None):
        self.origin = http_signature.public_origin(origin) or origin
        self.owner = owner
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.shares: Dict[str, dict] = {}
        self.seen_bodies: list = []
        self._lock = threading.Lock()

    # -- dispatch --
    def handle(self, method: str, target: str, headers: Any, body: bytes) -> Tuple[int, dict, bytes]:
        with self._lock:
            self.seen_bodies.append(bytes(body))
            try:
                return self._handle(method, target, headers, bytes(body))
            except (ValueError, KeyError, TypeError) as error:
                return _refuse(400, 'malformed', type(error).__name__)

    def _handle(self, method: str, target: str, headers: Any, body: bytes):
        if len(body) > MAX_BODY_BYTES:
            return _refuse(413, 'too-large', 'the body is larger than the mailbox carries')
        parts = urlsplit(target)
        path, query = parts.path, parse_qs(parts.query)
        if method == 'GET' and path == '/.well-known/observatory-cloud':
            return _answer(200, {'protocol': PROTOCOL, 'peerShare': PS.PEER_SHARE_PROTOCOL})
        m = re.fullmatch(r'/v1/shares/([0-9a-f]{32})(?:/(frames|envelopes/[A-Z][A-Z0-9_]{0,63}|lease|reports|'
                         r'revocation|deletion))?', path)
        if not m:
            return _refuse(404, 'not-found', 'no such route')
        share_id, sub = m.group(1), m.group(2)
        now = self.clock()
        doc = json.loads(body.decode('utf-8')) if body else None
        if sub is None and method == 'PUT':
            return self._create(share_id, headers, target, body, doc, now)
        share = self.shares.get(share_id)
        if share is None:
            return _refuse(404, 'unknown-share', 'the mailbox holds no such share')
        # Only a claim being carried (a POST) may name its own signer: a body sent with any other request is never a
        # key this share admits (the review of #386, M1).
        claim = doc if sub == 'frames' and method == 'POST' and isinstance(doc, dict) and doc.get('step') == 'claim' \
            else None
        signer = self._signer(share, headers, method, target, body, now, claim)
        if isinstance(signer, tuple):
            return signer
        if sub == 'frames' and method == 'POST':
            return self._frame(share, signer, doc, now)
        if sub == 'frames' and method == 'GET':
            self._expire(share, now)
            if signer not in ('sender', 'recipient'):
                return _refuse(403, 'forbidden', 'only the two sides read the frames')
            return _answer(200, {'frames': share['frames'], 'verdict': share['verdict'], 'late': self._late(share),
                                 'sealed': share['sealed']})
        if sub and sub.startswith('envelopes/') and method == 'PUT':
            return self._envelope(share, signer, sub.split('/', 1)[1], doc, now)
        if sub == 'lease' and method == 'POST':
            return self._lease(share, signer, now)
        if sub == 'reports' and method == 'POST':
            return self._report(share, signer, doc)
        if sub == 'reports' and method == 'GET':
            if signer != 'sender':
                return _refuse(403, 'forbidden', 'only the sender reads the reports')
            after = int((query.get('after') or ['0'])[0])
            return _answer(200, {'reports': [r for r in share['reports'] if r['header']['sequence'] > after]})
        if sub == 'revocation' and method == 'PUT':
            return self._revocation(share, signer, doc, now)
        if sub == 'deletion' and method == 'PUT':
            return self._deletion(share, signer, doc)
        if sub is None and method == 'GET':
            self._expire(share, now)
            return _answer(200, self._status(share))
        return _refuse(405, 'method-not-allowed', 'no such method on this route')

    # -- who signs --
    def _signer(self, share: dict, headers, method, target, body, now, frame):
        read = read_signature(headers, LABEL, method=method, origin=self.origin, target=target, body=body, at_ms=now)
        if not read['ok']:
            return _refuse(401, read['code'], 'the request is not signed by a share key')
        sides = {'sender': share['senderSig']}
        if share['recipientSig'] is not None:
            sides['recipient'] = share['recipientSig']
        # A claim is signed by the keys it carries: whoever holds the share id may claim, and every claim counts.
        if isinstance(frame, dict) and frame.get('step') == 'claim' and isinstance(frame.get('keys'), dict):
            sides = {'claimant': frame['keys'].get('sig')}
        who = next((name for name, jwk in sides.items() if signature_holds(read, jwk)), None)
        if who is None:
            if share['recipientSig'] is None and method == 'GET' and urlsplit(target).path.endswith('/frames'):
                # A claimant whose claim's answer was lost learns that no claim arrived, and only that.
                return _refuse(409, 'no-claim', 'no claim has reached the mailbox')
            return _refuse(401, 'signature-invalid', 'the signature is not by a key of this share')
        held = share['nonces']
        for key in [k for k, until in held.items() if until < now]:
            del held[key]
        if (read['keyid'], read['nonce']) in held:
            return _refuse(401, 'signature-replayed', 'this nonce was used')
        # Kept as long as a signature made at `created` is fresh: a clock ahead of ours included.
        held[(read['keyid'], read['nonce'])] = max(now, read['created'] * 1000) + WINDOW_SECONDS * 1000
        return who

    # -- routes --
    def _create(self, share_id: str, headers, target, body, doc, now):
        if not isinstance(doc, dict) or doc.get('shareId') != share_id or not isinstance(doc.get('sender'), dict) \
                or not PS.peer_jwk(doc['sender'].get('sig')):
            return _refuse(400, 'malformed', 'a share names its id and the sender share #sig')
        deadline = relay.instant(doc.get('claimDeadline'))
        lease = doc.get('leaseSeconds', DEFAULT_LEASE_SECONDS)
        if deadline is None or deadline <= now or deadline - now > PS.MAX_CLAIM_DAYS * PS.DAY_MS + 60_000:
            return _refuse(400, 'claim-deadline', 'a claim deadline is in the future and at most 7 days away')
        if not PS._safe_int(lease) or not 60 <= lease <= PS.MAX_LEASE_SECONDS:
            return _refuse(400, 'lease', 'a lease is 60 s to 24 h')
        read = read_signature(headers, LABEL, method='PUT', origin=self.origin, target=target, body=body, at_ms=now)
        if not signature_holds(read, doc['sender']['sig']):
            return _refuse(401, read.get('code', 'signature-invalid'), 'a share is created by its own #sig')
        if self.owner is not None:
            owner = read_signature(headers, OWNER_LABEL, method='PUT', origin=self.origin, target=target, body=body,
                                   at_ms=now)
            if not signature_holds(owner, self.owner):
                return _refuse(403, 'not-owner', "only the mailbox's owner files shares here")
        if share_id in self.shares:
            same = self.shares[share_id]['senderSig'] == doc['sender']['sig']
            return _answer(200, {'created': False}) if same else _refuse(409, 'exists', 'the share id is taken')
        self.shares[share_id] = {
            'shareId': share_id, 'senderSig': doc['sender']['sig'], 'claimDeadline': doc['claimDeadline'],
            'leaseSeconds': int(lease), 'createdAt': relay.rfc3339(now), 'frames': [], 'lateAbort': None, 'lateClaims': [],
            'verdict': {'state': 'open'}, 'recipientSig': None, 'sealed': False, 'envelopes': {}, 'revocation': None,
            'deletion': None, 'reports': [], 'deliveries': 0, 'lastRenewal': None,
            'nonces': {(read['keyid'], read['nonce']): max(now, read['created'] * 1000) + WINDOW_SECONDS * 1000}}
        return _answer(201, {'created': True})

    def _expire(self, share: dict, now: int) -> None:
        if share['verdict']['state'] == 'open' and not share['sealed']:
            share['verdict'] = PS.peer_exchange_verdict(share['shareId'], share['claimDeadline'], share['frames'],
                                                        relay.rfc3339(now))

    def _frame(self, share: dict, signer: str, frame: Any, now: int):
        if PS.peer_frame(frame) is None or frame['shareId'] != share['shareId']:
            return _refuse(400, 'malformed', 'not a frame of this share')
        role = frame.get('role')
        step = frame['step']
        if step == 'claim' and signer != 'claimant':
            return _refuse(403, 'forbidden', 'a claim is signed by its own keys')
        if step == 'commit' and signer != 'sender' or role is not None and role != signer:
            return _refuse(403, 'forbidden', 'a side posts only its own frames')
        if share['sealed']:
            # After the first envelope: no verdict; a later claim or abort is carried to the sender as a warning.
            # One abort (the recipient's own) and one claim per claiming key, at most MAX_LATE_CLAIMS keys: strangers
            # cannot crowd the recipient's abort out.
            # A repeat is answered without being accepted (the Worker then keeps no nonce for it).
            if step == 'abort':
                if share.get('lateAbort'):
                    return _refuse(409, 'repeated', "the recipient's abort was already carried")
                share['lateAbort'] = frame
                return _answer(202, {'carried': True, 'late': True})
            if step == 'claim':
                key = (frame['keys']['sig']['x'], frame['keys']['sig']['y'])
                claims = share.setdefault('lateClaims', [])
                if any(k == key for k, _ in claims):
                    return _refuse(409, 'repeated', "this key's claim was already carried")
                if len(claims) >= MAX_LATE_CLAIMS:
                    return _refuse(429, 'too-many', 'this share carries no more late claims')
                claims.append((key, frame))
                return _answer(202, {'carried': True, 'late': True})
            return _refuse(409, 'sealed', 'the share is sealed; only a claim or an abort is carried now')
        self._expire(share, now)
        if share['verdict']['state'] == 'burnt':
            return _refuse(409, 'burnt', 'this exchange is burnt', reason=share['verdict']['reason'])
        share['frames'].append(frame)
        if step == 'claim' and share['recipientSig'] is None:
            share['recipientSig'] = frame['keys']['sig']
        share['verdict'] = PS.peer_exchange_verdict(share['shareId'], share['claimDeadline'], share['frames'],
                                                    relay.rfc3339(now))
        return _answer(202, {'carried': True, 'index': len(share['frames']) - 1, 'verdict': share['verdict']})

    def _envelope(self, share: dict, signer: str, slot: str, env: Any, now: int):
        if signer != 'sender':
            return _refuse(403, 'forbidden', 'only the sender stores envelopes')
        if share['revocation'] is not None:
            return _refuse(410, 'revoked', 'the share was taken back')
        self._expire(share, now)
        if not share['sealed']:
            if share['verdict']['state'] == 'burnt':
                return _refuse(409, 'burnt', 'this exchange is burnt', reason=share['verdict']['reason'])
            if share['verdict']['state'] != 'complete':
                return _refuse(409, 'not-complete', 'the exchange has not completed')
        if not PS.peer_sealed_shape(env, PS.PEER_ENVELOPE_TYPE) or env['header']['shareId'] != share['shareId'] \
                or env['header']['slot'] != slot:
            return _refuse(400, 'malformed', 'not an envelope of this share and slot')
        share['envelopes'][slot] = env
        share.setdefault('storedAt', {})[slot] = now
        share['sealed'] = True
        return _answer(200, {'stored': True})

    def _lease(self, share: dict, signer: str, now: int):
        if signer != 'recipient':
            return _refuse(403, 'forbidden', 'only the recipient renews')
        if share['revocation'] is not None:
            return _refuse(410, 'revoked', 'the share was taken back', revocation=share['revocation'])
        self._expire(share, now)
        if not share['sealed']:
            if share['verdict']['state'] == 'burnt':
                return _refuse(409, 'burnt', 'this exchange is burnt', reason=share['verdict']['reason'])
            return _refuse(409, 'not-sealed', 'the sender has not sealed the share yet')
        # Each copy's end by the mailbox's own clock: no further than 90 days after it was stored, as the Worker keeps it.
        ends = [min(relay.instant(e['header']['exp']) or 0, share.get('storedAt', {}).get(slot, now) + 90 * 86_400_000)
                for slot, e in share['envelopes'].items()]
        if ends and now >= max(ends):
            return _refuse(409, 'ended', "every sealed copy of this share has ended; the sender's machine seals it again")
        share['deliveries'] += 1
        share['lastRenewal'] = relay.rfc3339(now)
        lease = {'grantedAt': relay.rfc3339(now), 'expiresAt': relay.rfc3339(now + share['leaseSeconds'] * 1000)}
        return _answer(200, {'lease': lease, 'envelopes': list(share['envelopes'].values())})

    def _report(self, share: dict, signer: str, report: Any):
        if signer != 'recipient':
            return _refuse(403, 'forbidden', 'only the recipient reports')
        if not PS.peer_sealed_shape(report, PS.PEER_REPORT_TYPE) or report['header']['shareId'] != share['shareId']:
            return _refuse(400, 'malformed', 'not a report of this share')
        if any(r['header']['sequence'] == report['header']['sequence'] for r in share['reports']):
            return _answer(200, {'stored': False})
        share['reports'].append(report)
        return _answer(200, {'stored': True})

    def _revocation(self, share: dict, signer: str, record: Any, now: int):
        if signer != 'sender':
            return _refuse(403, 'forbidden', 'only the sender takes a share back')
        if not PS.peer_record_stands(record, PS.PEER_REVOCATION_TYPE, share['shareId'], share['senderSig'])['ok']:
            return _refuse(400, 'malformed', "not a revocation the sender's share #sig signed")
        if share['revocation'] is None:
            share['revocation'] = record
            share['envelopes'] = {}                            # taken back: no sealed copy is served or kept
        return _answer(200, {'held': True, 'revocation': share['revocation']})

    def _deletion(self, share: dict, signer: str, record: Any):
        if signer != 'recipient':
            return _refuse(403, 'forbidden', 'only the recipient returns a deletion receipt')
        if not PS.peer_record_stands(record, PS.PEER_DELETION_TYPE, share['shareId'], share['recipientSig'])['ok']:
            return _refuse(400, 'malformed', "not a deletion receipt the recipient's share #sig signed")
        share['deletion'] = record
        return _answer(200, {'held': True})

    def _status(self, share: dict) -> dict:
        return {'protocol': PROTOCOL, 'shareId': share['shareId'], 'claimDeadline': share['claimDeadline'],
                'leaseSeconds': share['leaseSeconds'], 'verdict': share['verdict'], 'sealed': share['sealed'],
                'deliveries': share['deliveries'], 'lastRenewal': share['lastRenewal'],
                'revocation': share['revocation'], 'deletion': share['deletion'], 'reports': len(share['reports']),
                'late': len(self._late(share))}

    @staticmethod
    def _late(share: dict) -> list:
        return ([share['lateAbort']] if share.get('lateAbort') else []) + [f for _, f in share.get('lateClaims', [])]


# ---- Transports and the client ---------------------------------------------------------------------------------------

Transport = Callable[[str, str, Dict[str, str], bytes], Tuple[int, dict, bytes]]


def fake_transport(mailbox: FakeMailbox) -> Transport:
    """A transport that hands each request to an in-process mailbox."""
    def send(method: str, url: str, headers: Dict[str, str], body: bytes):
        parts = urlsplit(url)
        return mailbox.handle(method, parts.path + ('?' + parts.query if parts.query else ''), headers, body)
    return send


def _resolved(url: str) -> str:
    """The URL a request is sent to: the mailbox's own, or, under `OBSERVATORY_CLOUD_RESOLVE=host=127.0.0.1:PORT`, plain
    HTTP to a loopback port for a trial mailbox (`full share mailbox-trial`). Only a loopback address is accepted."""
    import os
    rule = os.environ.get(RESOLVE_ENV, '')
    parts = urlsplit(url)
    for entry in filter(None, (r.strip() for r in rule.split(','))):
        host, _, address = entry.partition('=')
        if host.lower() == (parts.hostname or '') and re.fullmatch(r'(127\.0\.0\.1|\[::1\]):[0-9]{1,5}', address):
            return 'http://' + address + parts.path + ('?' + parts.query if parts.query else '')
    return url


def http_transport(timeout: float = 20.0) -> Transport:
    """HTTPS to the mailbox (urllib, the system's trust store)."""
    def send(method: str, url: str, headers: Dict[str, str], body: bytes):
        request = urllib.request.Request(_resolved(url), data=body if method in ('POST', 'PUT') else None,
                                         method=method, headers={**headers, 'Content-Type': 'application/json',
                                                                 'User-Agent': 'project-observatory-share/1'})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as answer:
                return answer.status, dict(answer.headers), answer.read(MAX_BODY_BYTES * 8)
        except urllib.error.HTTPError as error:
            try:
                return error.code, dict(error.headers or {}), error.read(MAX_BODY_BYTES)
            finally:
                error.close()
        except (urllib.error.URLError, OSError) as error:
            raise MailboxError('unreachable', f'the mailbox did not answer ({type(error).__name__})') from None
    return send


class MailboxClient:
    """One side's door to a share's mailbox: every request signed by that side's share `#sig`."""

    def __init__(self, origin: str, sig_jwk: dict, sig_key: 'PS.SharePrivateKey', transport: Transport, *,
                 clock: Callable[[], int] = None, owner: Optional[tuple] = None):
        self.origin = origin.rstrip('/')
        self.sig_jwk, self.sig_key = sig_jwk, sig_key
        self.transport = transport
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.owner = owner                                         # (owner_jwk, owner_key) for a share's creation

    def _call(self, method: str, target: str, doc: Any = None, *, owner: bool = False) -> dict:
        body = b'' if doc is None else json.dumps(doc, separators=(',', ':')).encode('utf-8')
        signers = [(LABEL, self.sig_jwk, self.sig_key)]
        if owner and self.owner is not None:
            signers.append((OWNER_LABEL, self.owner[0], self.owner[1]))
        headers = sign_request(signers, method=method, origin=self.origin, target=target, body=body,
                               created=self.clock() // 1000)
        status, _, raw = self.transport(method, self.origin + target, headers, body)
        try:
            answer = json.loads(raw.decode('utf-8')) if raw else {}
        except ValueError:
            raise MailboxError('bad-answer', f'the mailbox answered {status} with no JSON', status) from None
        if status >= 400:
            error = answer.get('error') if isinstance(answer, dict) else None
            error = error if isinstance(error, dict) else {}
            raise MailboxError(str(error.get('code', 'refused')), str(error.get('message', f'status {status}')),
                               status, error)
        return answer if isinstance(answer, dict) else {}

    def create(self, share_id: str, claim_deadline: str, lease_seconds: int) -> dict:
        return self._call('PUT', f'/v1/shares/{share_id}', {'shareId': share_id, 'sender': {'sig': self.sig_jwk},
                                                            'claimDeadline': claim_deadline,
                                                            'leaseSeconds': lease_seconds}, owner=True)

    def post_frame(self, frame: dict) -> dict:
        return self._call('POST', f"/v1/shares/{frame['shareId']}/frames", frame)

    def frames(self, share_id: str) -> dict:
        return self._call('GET', f'/v1/shares/{share_id}/frames')

    def put_envelope(self, envelope: dict) -> dict:
        h = envelope['header']
        return self._call('PUT', f"/v1/shares/{h['shareId']}/envelopes/{h['slot']}", envelope)

    def lease(self, share_id: str) -> dict:
        return self._call('POST', f'/v1/shares/{share_id}/lease', {})

    def post_report(self, report: dict) -> dict:
        return self._call('POST', f"/v1/shares/{report['header']['shareId']}/reports", report)

    def reports(self, share_id: str, after: int) -> dict:
        return self._call('GET', f'/v1/shares/{share_id}/reports?after={int(after)}')

    def revoke(self, record: dict) -> dict:
        return self._call('PUT', f"/v1/shares/{record['shareId']}/revocation", record)

    def deletion(self, record: dict) -> dict:
        return self._call('PUT', f"/v1/shares/{record['shareId']}/deletion", record)

    def status(self, share_id: str) -> dict:
        return self._call('GET', f'/v1/shares/{share_id}')


def serve_trial(box: FakeMailbox, port: int = 0):
    """An HTTP server on 127.0.0.1 that hands every request to `box`: a trial mailbox between two workspaces on one
    machine (`full share mailbox-trial`), reached through `OBSERVATORY_CLOUD_RESOLVE`. It logs no path and no body."""
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def _serve(self):
            length = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(min(length, MAX_BODY_BYTES + 1)) if length else b''
            status, headers, raw = box.handle(self.command, self.path, dict(self.headers.items()), body)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_PUT = do_POST = _serve

        def log_message(self, *args):
            pass

    return http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler)
