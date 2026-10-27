"""A credential shared with another person's estate, `fabric-peer-share/1` (contract DEC-0142, `src/peer-share.ts`,
`docs/specification/peer-share.md` PS-1 to PS-10): the protocol, as pure functions over the contract's vectors
(`fabric/node-vectors/peer/peer-share.json`, run by `tests/test_node_peer_share.py`).

Neither side needs an organization. The trust root is the link the sender gives the recipient
(`https://<mailbox>/s/<shareId>#c=<code>&k=<fpr>`) and a six-digit code the two people compare. Each side makes a
fresh P-256 key pair for the one share, `#sig` (ES256) and `#kem` (HPKE DHKEM(P-256)), never its device keys (PS-2),
so two shares, or a share and an organization enrollment, cannot be linked by a key.

- The claim's MAC (`claim_mac`, `fabric-peer-claim/1`) filters claims to those of a link's holder; it authenticates
  nothing against the mailbox, whose landing page can read the code (PS-5).
- The comparison code is DEC-0084's construction under the share's own types: both sides commit, then both reveal; the
  code is the first five bytes of a SHA-256 over an RFC 8785 transcript, modulo 10^6 (PS-6). One attempt per link, at
  both ends (`peer_exchange_verdict`).
- The value travels sealed with HPKE base mode, suite `[16, 1, 1]`, to the recipient's share `#kem`, info
  `fabric-peer-share-envelope/1`, the header as additional data, signed ES256 by the sender's share `#sig` (PS-7).
- Usage reports travel sealed the same way to the sender's share `#kem` (PS-8); revocation and deletion are signed
  records (PS-9).

Every checking function answers malformed input with a verdict and never raises on a JSON value; the seal and sign
functions raise `ValueError` on an input no receiver would open. A value never appears in an error, a verdict or a log.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Any, Optional

from . import envelope, jcs, p256, relay, webauthn

PEER_SHARE_PROTOCOL = 'fabric-peer-share/1'
PEER_FRAME_SCHEMA = 'peer-share-frame/1'
PEER_CLAIM_TYPE = 'fabric-peer-claim/1'
PEER_COMMIT_TYPE = 'fabric-peer-commit/1'
PEER_CODE_TYPE = 'fabric-peer-code/1'
PEER_ENVELOPE_TYPE = 'fabric-peer-share-envelope/1'
PEER_REPORT_TYPE = 'fabric-peer-share-report/1'
PEER_REVOCATION_TYPE = 'peer-share-revocation/1'
PEER_DELETION_TYPE = 'peer-share-deletion/1'
PEER_CODE_DIGITS = 6
#: A claim code answers at most this long after the share is made.
MAX_CLAIM_DAYS = 7
#: An envelope lives at most this long; the sender seals again to go on.
MAX_PEER_ENVELOPE_DAYS = 90
#: A lease the mailbox grants an honest recipient lasts at most this long.
MAX_LEASE_SECONDS = 86_400

DAY_MS = 86_400_000
ALG = 'ES256'
SUITE = (16, 1, 1)
SHARE_ID = re.compile(r'[0-9a-f]{32}')
#: 16 bytes as unpadded base64url: 22 characters, the last carrying two bits.
LINK_CODE = re.compile(r'[A-Za-z0-9_-]{21}[AQgw]')
#: 32 bytes as unpadded base64url: a thumbprint, a nonce, a MAC.
BYTES32 = re.compile(r'[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]')
_B64URL = re.compile(r'[A-Za-z0-9_-]+')
_SIG = re.compile(r'[A-Za-z0-9_-]{86}')
_COORD = BYTES32
SLOT = re.compile(r'[A-Z][A-Z0-9_]{0,63}')
PROVIDER = re.compile(r'[a-z][a-z0-9-]{1,62}')            # common.schema.json#/$defs/slug
_COMMIT = re.compile(r'sha256:[0-9a-f]{64}')
_DIGEST = _COMMIT
_DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
AGENT_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9 ._:@/-]{0,127}')
ROLES = ('sender', 'recipient')


def _full(pattern: 're.Pattern', value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _is_obj(value: Any) -> bool:
    return isinstance(value, dict)


def _exactly(value: dict, members) -> bool:
    return len(value) == len(members) and all(m in value for m in members)


def _safe_int(value: Any) -> bool:
    """JavaScript's `Number.isSafeInteger`: an integer (16.0 included), never a boolean."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not value.is_integer():
        return False
    return -(2 ** 53 - 1) <= value <= 2 ** 53 - 1


def _counter(value: Any) -> bool:
    return _safe_int(value) and value >= 1


def _sha256(text: str) -> bytes:
    return hashlib.sha256(text.encode('utf-8')).digest()


def _b64(raw: bytes) -> str:
    return p256.b64url_encode(raw)


def _node_bytes(text: str) -> bytes:
    """Base64url as Node's `Buffer.from(text, "base64url")` reads it."""
    return webauthn.node_b64url(text)


def second(ms: int) -> str:
    """An instant in whole seconds, `YYYY-MM-DDTHH:MM:SSZ`, the form every header carries."""
    return relay.rfc3339(ms)


# ---- Keys ---------------------------------------------------------------------------------------------------------

def peer_jwk(value: Any) -> bool:
    """Whether a value is a P-256 public JWK with exactly `kty`, `crv`, `x` and `y`, its point on the curve."""
    return (_is_obj(value) and _exactly(value, ('kty', 'crv', 'x', 'y')) and value.get('kty') == 'EC'
            and value.get('crv') == 'P-256' and _full(_COORD, value.get('x')) and _full(_COORD, value.get('y'))
            and p256.key_refusal(value) is None)


def peer_thumbprint(jwk: Any) -> Optional[str]:
    """The RFC 7638 SHA-256 thumbprint of a P-256 public JWK, unpadded base64url, or None for anything else."""
    if not peer_jwk(jwk):
        return None
    return _b64(_sha256(jcs.rfc8785({'crv': jwk['crv'], 'kty': jwk['kty'], 'x': jwk['x'], 'y': jwk['y']})))


def peer_keys(value: Any) -> bool:
    """A share's key pair: exactly `sig` and `kem`, each a P-256 public JWK, two different keys."""
    return (_is_obj(value) and _exactly(value, ('sig', 'kem')) and peer_jwk(value.get('sig'))
            and peer_jwk(value.get('kem')) and peer_thumbprint(value['sig']) != peer_thumbprint(value['kem']))


class SharePrivateKey:
    """One P-256 private key of a share (`#sig` or `#kem`), generated for that share only and never a device key."""

    def __init__(self, private_key):
        self._key = private_key

    @classmethod
    def generate(cls) -> 'SharePrivateKey':
        ec = _ec()
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def from_scalar(cls, raw: bytes) -> 'SharePrivateKey':
        ec = _ec()
        return cls(ec.derive_private_key(int.from_bytes(raw, 'big'), ec.SECP256R1()))

    def scalar(self) -> bytes:
        return self._key.private_numbers().private_value.to_bytes(32, 'big')

    def jwk(self) -> dict:
        numbers = self._key.public_key().public_numbers()
        return {'kty': 'EC', 'crv': 'P-256', 'x': _b64(numbers.x.to_bytes(32, 'big')),
                'y': _b64(numbers.y.to_bytes(32, 'big'))}

    def sign(self, data) -> str:
        return p256.sign_es256(self._key, data)

    def key(self):
        return self._key


def _ec():
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError as error:                                  # the engine's `full` extra
        raise RuntimeError('a shared credential needs the engine full extra (cryptography)') from error
    return ec


def new_share_id() -> str:
    """16 random bytes as 32 lower-case hex digits, never derived from a name, a path or an id (PS-4)."""
    return secrets.token_bytes(16).hex()


def new_link_code() -> str:
    """The link's claim code: 16 random bytes, unpadded base64url, single use (PS-4)."""
    return _b64(secrets.token_bytes(16))


def new_nonce() -> str:
    """A side's nonce for the comparison code: 32 random bytes, unpadded base64url (PS-6)."""
    return _b64(secrets.token_bytes(32))


# ---- The link -----------------------------------------------------------------------------------------------------

_SCHEME = re.compile(r'([A-Za-z][A-Za-z0-9+.\-]*):(.*)', re.S)
_HOST = re.compile(r'[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?)*\.?')
_PORT = re.compile(r'[0-9]*')
_DOT = re.compile(r'(?:\.|%2e)', re.I)
_DOTDOT = re.compile(r'(?:\.|%2e)(?:\.|%2e)', re.I)


def _path_segments(path: str) -> Optional[str]:
    """The WHATWG URL parser's path: `\\` read as `/`, `.` and `..` segments (and their percent-encoded spellings)
    resolved."""
    out: list = []
    segments = path.replace('\\', '/').split('/')[1:]
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if _DOTDOT.fullmatch(segment):
            if out:
                out.pop()
            if last:
                out.append('')
        elif _DOT.fullmatch(segment):
            if last:
                out.append('')
        else:
            out.append(segment)
    return '/' + '/'.join(out)


def _origin_of(authority: str) -> Optional[str]:
    """`https://host[:port]` as WHATWG's `URL.origin` writes it for this authority, or None when it does not parse or
    carries user information other than the empty one the parser drops."""
    userinfo, at, hostport = authority.rpartition('@')
    if at and userinfo not in ('', ':'):
        return None
    if hostport.startswith('['):
        close = hostport.find(']')
        if close < 0:
            return None
        host, rest = hostport[:close + 1].lower(), hostport[close + 1:]
        if not re.fullmatch(r'\[[0-9a-f:.]+\]', host):
            return None
    else:
        host, colon, port_text = hostport.partition(':')
        rest = colon + port_text
        host = host.lower()
        if not host or not _HOST.fullmatch(host) or host.replace('.', '').isdigit():
            return None
    if rest:
        if not rest.startswith(':') or not _PORT.fullmatch(rest[1:]):
            return None
        port = rest[1:]
        if port and int(port) > 65535:
            return None
        rest = '' if port in ('', '443') or int(port) == 443 else ':' + str(int(port))
    return 'https://' + host + rest


def parse_share_link(value: Any) -> dict:
    """The share link (PS-4), `https://<mailbox origin>/s/<shareId>#c=<code>&k=<fpr>`: an HTTPS origin without user
    information, the path `/s/` and a 32-hex share id, no query, and a fragment with exactly `c` (16 random bytes) and
    `k` (the thumbprint of the sender's share `#sig`), each once, in either order. Read after WHATWG URL normalisation
    for the cases the contract's vectors name (a port, the empty `:@`, dot segments, an upper-case host); a host
    outside ASCII is refused rather than converted."""
    no = {'ok': False, 'problem': 'malformed'}
    if not isinstance(value, str) or len(value) > 2048 or re.search(r'[\s\x00-\x1f\x7f]', value) or '?' in value:
        return no
    match = _SCHEME.fullmatch(value)
    if not match or match.group(1).lower() != 'https' or not value.isascii():
        return no
    rest = match.group(2).replace('\\', '/')
    if not rest.startswith('//'):
        return no
    rest = rest[2:]
    end = min([i for i in (rest.find('/'), rest.find('#')) if i >= 0] or [len(rest)])
    origin = _origin_of(rest[:end])
    if origin is None:
        return no
    rest = rest[end:]
    path, hash_mark, fragment = rest.partition('#')
    path = _path_segments(path or '/')
    found = re.fullmatch(r'/s/([0-9a-f]{32})', path or '')
    if found is None or not hash_mark:
        return no
    pairs = [part.split('=') for part in fragment.split('&')]
    if len(pairs) != 2 or any(len(pair) != 2 for pair in pairs):
        return no
    fields = dict((name, v) for name, v in pairs)
    code, fpr = fields.get('c'), fields.get('k')
    if len(fields) != 2 or code is None or fpr is None or not _full(LINK_CODE, code) or not _full(BYTES32, fpr):
        return no
    return {'ok': True, 'link': {'origin': origin, 'shareId': found.group(1), 'code': code, 'fpr': fpr}}


def share_link(link: dict) -> str:
    """The link's text for a share; for parts `parse_share_link` answered, a parse of it answers the same parts."""
    return f"{link['origin']}/s/{link['shareId']}#c={link['code']}&k={link['fpr']}"


# ---- The claim ----------------------------------------------------------------------------------------------------

def claim_mac(code: Any, share_id: Any, keys: Any, fpr: Any) -> Optional[str]:
    """HMAC-SHA-256 keyed with the link's 16 code bytes over the UTF-8 RFC 8785 form of
    `{type: "fabric-peer-claim/1", shareId, keys, fpr}`, unpadded base64url (PS-5)."""
    if not (_full(LINK_CODE, code) and _full(SHARE_ID, share_id) and peer_keys(keys) and _full(BYTES32, fpr)):
        return None
    data = jcs.rfc8785({'type': PEER_CLAIM_TYPE, 'shareId': share_id, 'keys': keys, 'fpr': fpr}).encode('utf-8')
    return _b64(hmac.new(_node_bytes(code), data, hashlib.sha256).digest())


def claim_holds(link: Any, frame: Any) -> bool:
    """Whether a frame is a claim for the link's share whose MAC the link's code gives, compared in constant time."""
    if not (_is_obj(link) and isinstance(link.get('code'), str) and isinstance(link.get('shareId'), str)
            and isinstance(link.get('fpr'), str)):
        return False
    claim = peer_frame(frame)
    if claim is None or claim['step'] != 'claim' or claim['shareId'] != link['shareId']:
        return False
    expected = claim_mac(link['code'], link['shareId'], claim['keys'], link['fpr'])
    return expected is not None and hmac.compare_digest(expected.encode('utf-8'), claim['mac'].encode('utf-8'))


def pin_holds(link: Any, frame: Any) -> bool:
    """Whether the sender's commit frame names the share `#sig` the link pins: its thumbprint is the link's `k`."""
    commit = peer_frame(frame)
    return (_is_obj(link) and isinstance(link.get('fpr'), str) and isinstance(link.get('shareId'), str)
            and commit is not None and commit['step'] == 'commit' and commit['shareId'] == link['shareId']
            and peer_thumbprint(commit['keys']['sig']) == link['fpr'])


# ---- Frames -------------------------------------------------------------------------------------------------------

FRAME_MEMBERS = {
    'claim': ('schema', 'step', 'shareId', 'keys', 'mac', 'commit'),
    'commit': ('schema', 'step', 'shareId', 'keys', 'commit'),
    'reveal': ('schema', 'step', 'role', 'shareId', 'nonce'),
    'abort': ('schema', 'step', 'role', 'shareId'),
}


def peer_frame(value: Any) -> Optional[dict]:
    """A `peer-share-frame/1` of one step with the members `peer-share-frame.schema.json` gives it, or None."""
    if not _is_obj(value) or value.get('schema') != PEER_FRAME_SCHEMA or not isinstance(value.get('step'), str):
        return None
    members = FRAME_MEMBERS.get(value['step'])
    if members is None or not _exactly(value, members) or not _full(SHARE_ID, value.get('shareId')):
        return None
    step = value['step']
    if step in ('claim', 'commit') and (not peer_keys(value.get('keys')) or not _full(_COMMIT, value.get('commit'))):
        return None
    if step == 'claim' and not _full(BYTES32, value.get('mac')):
        return None
    if step in ('reveal', 'abort') and value.get('role') not in ROLES:
        return None
    if step == 'reveal' and not _full(BYTES32, value.get('nonce')):
        return None
    return value


# ---- Commit, reveal and the comparison code -------------------------------------------------------------------------

def _side_problem(side: Any) -> Optional[str]:
    if not _is_obj(side) or not _exactly(side, ('role', 'shareId', 'keys', 'nonce')):
        return 'malformed'
    if side.get('role') not in ROLES:
        return 'bad-role'
    if not _full(SHARE_ID, side.get('shareId')):
        return 'bad-share'
    if not peer_keys(side.get('keys')):
        return 'bad-keys'
    if not _full(BYTES32, side.get('nonce')):
        return 'bad-nonce'
    return None


def peer_commit(side: Any) -> dict:
    """A side's commitment: `sha256:` and the hex SHA-256 of the UTF-8 RFC 8785 form of
    `{type: "fabric-peer-commit/1", shareId, role, keys, nonce}`."""
    problem = _side_problem(side)
    if problem is not None:
        return {'ok': False, 'problem': problem}
    text = jcs.rfc8785({'type': PEER_COMMIT_TYPE, 'shareId': side['shareId'], 'role': side['role'],
                        'keys': side['keys'], 'nonce': side['nonce']})
    return {'ok': True, 'commit': 'sha256:' + _sha256(text).hex()}


def check_peer_reveal(commit: Any, side: Any, expected: str) -> dict:
    """Whether a side's reveal opens its commitment in the role expected (else `role-mismatch`, `commit-mismatch`)."""
    if not _full(_COMMIT, commit):
        return {'ok': False, 'problem': 'malformed'}
    made = peer_commit(side)
    if not made['ok']:
        return made
    if side['role'] != expected:
        return {'ok': False, 'problem': 'role-mismatch'}
    return {'ok': True} if made['commit'] == commit else {'ok': False, 'problem': 'commit-mismatch'}


def peer_comparison_code(sender: Any, recipient: Any) -> dict:
    """The comparison code over both reveals: the first five bytes of SHA-256 over the UTF-8 RFC 8785 form of
    `{type: "fabric-peer-code/1", shareId, sender: {keys, nonce}, recipient: {keys, nonce}}` as a big-endian integer,
    modulo 10^6, six digits with leading zeros."""
    for side in (sender, recipient):
        problem = _side_problem(side)
        if problem is not None:
            return {'ok': False, 'problem': problem}
    if sender['role'] != 'sender' or recipient['role'] != 'recipient':
        return {'ok': False, 'problem': 'role-mismatch'}
    if sender['shareId'] != recipient['shareId']:
        return {'ok': False, 'problem': 'share-mismatch'}
    mine = [peer_thumbprint(sender['keys']['sig']), peer_thumbprint(sender['keys']['kem'])]
    theirs = [peer_thumbprint(recipient['keys']['sig']), peer_thumbprint(recipient['keys']['kem'])]
    if any(key in theirs for key in mine):
        return {'ok': False, 'problem': 'same-keys'}
    transcript = jcs.rfc8785({'type': PEER_CODE_TYPE, 'shareId': sender['shareId'],
                              'sender': {'keys': sender['keys'], 'nonce': sender['nonce']},
                              'recipient': {'keys': recipient['keys'], 'nonce': recipient['nonce']}})
    number = int.from_bytes(_sha256(transcript)[:5], 'big') % 10 ** PEER_CODE_DIGITS
    return {'ok': True, 'code': str(number).zfill(PEER_CODE_DIGITS)}


def checked_peer_code(sender_commit: Any, sender: Any, recipient_commit: Any, recipient: Any) -> dict:
    """The code only after each reveal opened its own commitment: a port that codes reveals straight away loses the
    one-guess bound."""
    a = check_peer_reveal(sender_commit, sender, 'sender')
    if not a['ok']:
        return a
    b = check_peer_reveal(recipient_commit, recipient, 'recipient')
    if not b['ok']:
        return b
    return peer_comparison_code(sender, recipient)


# ---- One attempt per link -----------------------------------------------------------------------------------------

def peer_exchange_verdict(share_id: Any, claim_deadline: Any, frames: Any, at: Any) -> dict:
    """One share's exchange, as the sender's node, the recipient's node and the mailbox each read it from the frames
    in the order received (PS-6): claim, then commit, then the two reveals in either order; once complete, at most
    one `abort`. A node keeps the first burnt verdict it reaches and reads the share no further."""
    deadline, now = relay.instant(claim_deadline), relay.instant(at)
    if not _full(SHARE_ID, share_id) or deadline is None or now is None or not isinstance(frames, list):
        return {'state': 'burnt', 'reason': 'malformed', 'index': -1}
    claim = commit = None
    revealed: set = set()
    complete = aborted = False
    for index, value in enumerate(frames):
        frame = peer_frame(value)

        def burn(reason: str) -> dict:
            return {'state': 'burnt', 'reason': reason, 'index': index}
        if frame is None or frame['shareId'] != share_id:
            return burn('malformed')
        if aborted:
            return burn('closed')
        if complete:
            if frame['step'] == 'abort':
                aborted = True
                continue
            return burn('closed')
        step = frame['step']
        if step == 'claim':
            if claim is not None:
                return burn('repeated')
            claim = frame
        elif step == 'commit':
            if commit is not None:
                return burn('repeated')
            if claim is None:
                return burn('out-of-order')
            commit = frame
        elif step == 'reveal':
            if claim is None or commit is None:
                return burn('out-of-order')
            if frame['role'] in revealed:
                return burn('repeated')
            source = commit if frame['role'] == 'sender' else claim
            opened = check_peer_reveal(source['commit'], {'role': frame['role'], 'shareId': share_id,
                                                          'keys': source['keys'], 'nonce': frame['nonce']},
                                       frame['role'])
            if not opened['ok']:
                return burn('commit-mismatch')
            revealed.add(frame['role'])
            if len(revealed) == 2:
                complete = True
        else:
            return burn('aborted')
    if aborted:
        return {'state': 'burnt', 'reason': 'aborted', 'index': len(frames) - 1}
    if complete:
        return {'state': 'complete'}
    return {'state': 'burnt', 'reason': 'expired', 'index': len(frames)} if now > deadline else {'state': 'open'}


# ---- Sealed messages: the envelope and the usage report -----------------------------------------------------------

HEADERS = {
    PEER_ENVELOPE_TYPE: ('type', 'alg', 'suite', 'recipient', 'signer', 'shareId', 'slot', 'provider', 'sequence',
                         'iat', 'exp', 'enc'),
    PEER_REPORT_TYPE: ('type', 'alg', 'suite', 'recipient', 'signer', 'shareId', 'sequence', 'iat', 'enc'),
}


def _suite_ok(value: Any) -> bool:
    return (isinstance(value, list) and len(value) == 3
            and all(not isinstance(v, bool) and isinstance(v, (int, float)) for v in value)
            and tuple(value) == SUITE)


def peer_sealed_shape(value: Any, kind: str) -> bool:
    """Whether a value has exactly the shape the schema gives a sealed message of `kind`, its times real instants."""
    if not _is_obj(value) or not _exactly(value, ('header', 'ct', 'sig')) or not _is_obj(value.get('header')):
        return False
    h = value['header']
    if h.get('type') != kind or not _exactly(h, HEADERS[kind]):
        return False
    is_envelope = kind == PEER_ENVELOPE_TYPE
    return (isinstance(h.get('alg'), str) and len(h['alg']) <= 16 and _suite_ok(h.get('suite'))
            and _full(BYTES32, h.get('recipient')) and _full(BYTES32, h.get('signer'))
            and _full(SHARE_ID, h.get('shareId')) and _counter(h.get('sequence')) and relay.instant(h.get('iat')) is not None
            and (not is_envelope or (_full(SLOT, h.get('slot')) and _full(PROVIDER, h.get('provider'))
                                     and relay.instant(h.get('exp')) is not None))
            and _full(_B64URL, h.get('enc')) and _full(_B64URL, value.get('ct')) and _full(_SIG, value.get('sig')))


def _signing_input(sealed: dict) -> str:
    return jcs.rfc8785({'header': sealed['header'], 'ct': sealed['ct']})


def peer_digest(value: Any) -> str:
    """`sha256:` and the hex SHA-256 of a sealed message's or a record's RFC 8785 bytes."""
    return 'sha256:' + _sha256(jcs.rfc8785(value)).hex()


def _signer_holds(sig_jwk: Any, private: SharePrivateKey) -> None:
    if not isinstance(private, SharePrivateKey):
        raise ValueError("bad-key: the signer's key is not a share's P-256 private key")
    if not peer_jwk(sig_jwk):
        raise ValueError('bad-key: a share key is not a P-256 public JWK')
    public = private.jwk()
    if public['x'] != sig_jwk['x'] or public['y'] != sig_jwk['y']:
        raise ValueError("bad-key: the private key is not the signer's share #sig")


def _seal(kind: str, *, recipient_kem: dict, signer_sig: dict, signer: SharePrivateKey, share_id: str, sequence: int,
          iat_ms: int, plaintext: bytes, extra: dict, ephemeral: Optional[bytes] = None) -> dict:
    _signer_holds(signer_sig, signer)
    if not peer_jwk(recipient_kem):
        raise ValueError('bad-key: a share key is not a P-256 public JWK')
    ec = _ec()
    eph = (ec.derive_private_key(int.from_bytes(ephemeral, 'big'), ec.SECP256R1()) if ephemeral is not None
           else ec.generate_private_key(ec.SECP256R1()))
    numbers = eph.public_key().public_numbers()
    enc = b'\x04' + numbers.x.to_bytes(32, 'big') + numbers.y.to_bytes(32, 'big')
    header = {'type': kind, 'alg': ALG, 'suite': list(SUITE), 'recipient': peer_thumbprint(recipient_kem),
              'signer': peer_thumbprint(signer_sig), 'shareId': share_id, **extra, 'sequence': sequence,
              'iat': second(iat_ms), 'enc': _b64(enc)}
    if not peer_sealed_shape({'header': header, 'ct': 'AA', 'sig': 'A' * 86}, kind):
        raise ValueError("malformed: the message would not have the schema's shape")
    _, ct = envelope.hpke_seal_base(p256.point(recipient_kem), kind.encode('ascii'),
                                    jcs.rfc8785(header).encode('utf-8'), plaintext, eph)
    unsigned = {'header': header, 'ct': _b64(ct)}
    return {**unsigned, 'sig': signer.sign(_signing_input(unsigned))}


def seal_peer_share(*, recipient_kem: dict, signer_sig: dict, signer: SharePrivateKey, share_id: str, sequence: int,
                    iat_ms: int, exp_ms: int, slot: str, provider: str, plaintext: bytes,
                    ephemeral: Optional[bytes] = None) -> dict:
    """Seals a shared value to the recipient's share `#kem` (PS-7). Raises on an input no recipient would open."""
    life = relay.instant(second(exp_ms)) - relay.instant(second(iat_ms))
    if not 0 < life <= MAX_PEER_ENVELOPE_DAYS * DAY_MS:
        raise ValueError('lifetime-too-long: an envelope lives more than zero and at most 90 days')
    return _seal(PEER_ENVELOPE_TYPE, recipient_kem=recipient_kem, signer_sig=signer_sig, signer=signer,
                 share_id=share_id, sequence=sequence, iat_ms=iat_ms, plaintext=plaintext,
                 extra={'slot': slot, 'provider': provider, 'exp': second(exp_ms)}, ephemeral=ephemeral)


def seal_peer_report(*, recipient_kem: dict, signer_sig: dict, signer: SharePrivateKey, share_id: str, sequence: int,
                     iat_ms: int, body: Any) -> dict:
    """Seals a usage report to the sender's share `#kem` (PS-8); the body is checked first."""
    if not peer_report_body(body):
        raise ValueError("bad-body: the report's body is not a peer-share report body")
    return _seal(PEER_REPORT_TYPE, recipient_kem=recipient_kem, signer_sig=signer_sig, signer=signer,
                 share_id=share_id, sequence=sequence, iat_ms=iat_ms,
                 plaintext=jcs.rfc8785(body).encode('utf-8'), extra={})


PEER_ENVELOPE_REFUSALS = ('malformed', 'unsupported-alg', 'wrong-share', 'wrong-recipient', 'bad-key',
                          'bad-signature', 'revoked', 'replayed', 'sequence-reused', 'not-yet-valid',
                          'lifetime-too-long', 'expired', 'cannot-open')


_ABSENT = object()


def _marks_read(marks: Any) -> bool:
    if marks is _ABSENT:
        return True
    if not isinstance(marks, list):
        return False
    keys = set()
    for mark in marks:
        if not (_is_obj(mark) and _exactly(mark, ('shareId', 'slot', 'sequence', 'digest'))
                and _full(SHARE_ID, mark.get('shareId')) and _full(SLOT, mark.get('slot'))
                and _counter(mark.get('sequence')) and _full(_DIGEST, mark.get('digest'))):
            return False
        key = (mark['shareId'], mark['slot'])
        if key in keys:
            return False
        keys.add(key)
    return True


def _enc_bad(enc: str) -> bool:
    return p256.key_refusal(_node_bytes(enc)) is not None


def check_peer_share_envelope(value: Any, ctx: Any) -> dict:
    """Every check of a peer-share envelope short of opening it, in the reference's order; the first that fails
    names the refusal (PS-7)."""
    def no(refusal: str) -> dict:
        return {'ok': False, 'refusal': refusal}
    if not peer_sealed_shape(value, PEER_ENVELOPE_TYPE) or not _is_obj(ctx):
        return no('malformed')
    at = relay.instant(ctx.get('at'))
    revoked = ctx.get('revoked', None)
    if (at is None or not _marks_read(ctx.get('marks', _ABSENT)) or not _full(SHARE_ID, ctx.get('shareId'))
            or not peer_jwk(ctx.get('ownKem')) or not peer_jwk(ctx.get('senderSig'))
            or ('revoked' in ctx and not isinstance(revoked, bool))):
        return no('malformed')
    h = value['header']
    if h['alg'] != ALG:
        return no('unsupported-alg')
    if h['shareId'] != ctx['shareId']:
        return no('wrong-share')
    if h['recipient'] != peer_thumbprint(ctx['ownKem']):
        return no('wrong-recipient')
    if _enc_bad(h['enc']) or h['signer'] != peer_thumbprint(ctx['senderSig']):
        return no('bad-key')
    if p256.verify_es256(ctx['senderSig'], _signing_input(value), value['sig']) is not True:
        return no('bad-signature')
    if revoked is True:
        return no('revoked')
    digest = peer_digest(value)
    mark = next((m for m in (ctx.get('marks') or []) if m['shareId'] == h['shareId'] and m['slot'] == h['slot']), None)
    if mark is not None:
        if h['sequence'] < mark['sequence']:
            return no('replayed')
        if h['sequence'] == mark['sequence'] and mark['digest'] != digest:
            return no('sequence-reused')
    iat, exp = relay.instant(h['iat']), relay.instant(h['exp'])
    if iat - at > relay.CLOCK_SKEW_SECONDS * 1000:
        return no('not-yet-valid')
    if exp <= iat or exp - iat > MAX_PEER_ENVELOPE_DAYS * DAY_MS:
        return no('lifetime-too-long')
    if at >= exp:
        return no('expired')
    return {'ok': True, 'mark': {'shareId': h['shareId'], 'slot': h['slot'], 'sequence': int(h['sequence']),
                                 'digest': digest}}


def open_peer_share(value: Any, ctx: Any, own_kem: Any) -> dict:
    """Checks, then opens with the recipient's share `#kem` private key (a `SharePrivateKey` or its 32-byte scalar).
    The node keeps the ciphertext sealed on disk and opens it in memory at each use."""
    check = check_peer_share_envelope(value, ctx)
    if not check['ok']:
        return check
    try:
        secret = envelope.hpke_open_base(_scalar(own_kem), _node_bytes(value['header']['enc']),
                                         PEER_ENVELOPE_TYPE.encode('ascii'),
                                         jcs.rfc8785(value['header']).encode('utf-8'), _node_bytes(value['ct']))
    except Exception:                                              # any failure to open is one refusal
        return {'ok': False, 'refusal': 'cannot-open'}
    return {'ok': True, 'secret': secret, 'mark': check['mark']}


def _scalar(key: Any) -> Any:
    return key.key() if isinstance(key, SharePrivateKey) else key


# ---- The usage report ---------------------------------------------------------------------------------------------

def peer_report_body(value: Any) -> bool:
    """A report's body (PS-8): per slot and UTC day the uses counted and the last use, the consumer labels and the
    exports made with the export button; never a value, a command, an argument or an output. At most 400 entries
    in each list, one day entry per date and slot."""
    if not _is_obj(value) or not _exactly(value, ('days', 'consumers', 'exports')):
        return False

    def entries(v: Any, ok) -> bool:
        return isinstance(v, list) and len(v) <= 400 and all(_is_obj(e) and ok(e) for e in v)

    def day(e: dict) -> bool:
        return (_exactly(e, ('date', 'slot', 'uses', 'lastUse')) and _full(_DATE, e.get('date'))
                and relay.instant(e['date'] + 'T00:00:00Z') is not None and _full(SLOT, e.get('slot'))
                and _safe_int(e.get('uses')) and e['uses'] >= 0
                and (e.get('lastUse') is None or relay.instant(e.get('lastUse')) is not None))

    def consumer(e: dict) -> bool:
        return _exactly(e, ('slot', 'agent')) and _full(SLOT, e.get('slot')) and _full(AGENT_NAME, e.get('agent'))

    def exported(e: dict) -> bool:
        return _exactly(e, ('slot', 'at')) and _full(SLOT, e.get('slot')) and relay.instant(e.get('at')) is not None
    if not (entries(value.get('days'), day) and entries(value.get('consumers'), consumer)
            and entries(value.get('exports'), exported)):
        return False
    keys = [(e['date'], e['slot']) for e in value['days']]
    return len(set(keys)) == len(keys)


def check_peer_report(value: Any, ctx: Any) -> dict:
    """A usage report's checks short of opening it, in the reference's order (PS-8)."""
    def no(refusal: str) -> dict:
        return {'ok': False, 'refusal': refusal}
    if not peer_sealed_shape(value, PEER_REPORT_TYPE) or not _is_obj(ctx):
        return no('malformed')
    at = relay.instant(ctx.get('at'))
    last = ctx.get('lastSequence')
    if (at is None or not _full(SHARE_ID, ctx.get('shareId')) or not peer_jwk(ctx.get('ownKem'))
            or not peer_jwk(ctx.get('recipientSig'))
            or ('lastSequence' in ctx and not (_safe_int(last) and last >= 0))):
        return no('malformed')
    h = value['header']
    if h['alg'] != ALG:
        return no('unsupported-alg')
    if h['shareId'] != ctx['shareId']:
        return no('wrong-share')
    if h['recipient'] != peer_thumbprint(ctx['ownKem']):
        return no('wrong-recipient')
    if _enc_bad(h['enc']) or h['signer'] != peer_thumbprint(ctx['recipientSig']):
        return no('bad-key')
    if p256.verify_es256(ctx['recipientSig'], _signing_input(value), value['sig']) is not True:
        return no('bad-signature')
    if h['sequence'] <= (last if last is not None else 0):
        return no('replayed')
    if relay.instant(h['iat']) - at > relay.CLOCK_SKEW_SECONDS * 1000:
        return no('not-yet-valid')
    return {'ok': True, 'sequence': int(h['sequence'])}


def open_peer_report(value: Any, ctx: Any, own_kem: Any) -> dict:
    """Checks, then opens a report with the sender's share `#kem` private key, and reads its body."""
    import json
    check = check_peer_report(value, ctx)
    if not check['ok']:
        return check
    try:
        text = envelope.hpke_open_base(_scalar(own_kem), _node_bytes(value['header']['enc']),
                                       PEER_REPORT_TYPE.encode('ascii'), jcs.rfc8785(value['header']).encode('utf-8'),
                                       _node_bytes(value['ct'])).decode('utf-8', 'replace')
    except Exception:
        return {'ok': False, 'refusal': 'cannot-open'}
    try:
        body = json.loads(text)
    except ValueError:
        return {'ok': False, 'refusal': 'bad-body'}
    if not peer_report_body(body):
        return {'ok': False, 'refusal': 'bad-body'}
    return {'ok': True, 'body': body, 'sequence': check['sequence']}


# ---- Revocation and deletion ----------------------------------------------------------------------------------------

def peer_record_stands(record: Any, kind: str, share_id: Any, signer_sig: Any) -> dict:
    """Whether a signed record stands (PS-9): a `peer-share-revocation/1` the sender's share `#sig` signed, or a
    `peer-share-deletion/1` the recipient's share `#sig` signed, naming a revocation by `peer_digest` of it."""
    members = (('type', 'shareId', 'at', 'signer', 'sig') if kind == PEER_REVOCATION_TYPE
               else ('type', 'shareId', 'at', 'revocation', 'signer', 'sig'))
    if (not _is_obj(record) or record.get('type') != kind or not _exactly(record, members)
            or not _full(SHARE_ID, record.get('shareId')) or relay.instant(record.get('at')) is None
            or not _full(BYTES32, record.get('signer')) or not _full(_SIG, record.get('sig'))
            or (kind == PEER_DELETION_TYPE and not _full(_DIGEST, record.get('revocation')))
            or not _full(SHARE_ID, share_id) or not peer_jwk(signer_sig)):
        return {'ok': False, 'reason': 'malformed'}
    if record['shareId'] != share_id:
        return {'ok': False, 'reason': 'wrong-share'}
    if record['signer'] != peer_thumbprint(signer_sig):
        return {'ok': False, 'reason': 'bad-key'}
    unsigned = {k: v for k, v in record.items() if k != 'sig'}
    if p256.verify_es256(signer_sig, jcs.rfc8785(unsigned), record['sig']) is True:
        return {'ok': True}
    return {'ok': False, 'reason': 'bad-signature'}


def sign_peer_record(record: dict, signer_sig: dict, signer: SharePrivateKey) -> dict:
    """Signs a revocation (the sender's share `#sig`) or a deletion receipt (the recipient's). Raises on a key that is
    not the share `#sig` named, or a record `peer_record_stands` would refuse, so nothing unreadable is sent."""
    _signer_holds(signer_sig, signer)
    unsigned = {**record, 'signer': peer_thumbprint(signer_sig)}
    signed = {**unsigned, 'sig': signer.sign(jcs.rfc8785(unsigned))}
    kind = PEER_DELETION_TYPE if record.get('type') == PEER_DELETION_TYPE else PEER_REVOCATION_TYPE
    stands = peer_record_stands(signed, kind, record.get('shareId'), signer_sig)
    if not stands['ok']:
        raise ValueError(f"{stands['reason']}: the record would not stand")
    return signed
