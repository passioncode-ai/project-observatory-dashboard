"""A machine's device keys (contract DEC-0053, DEC-0056): one `<device.id>#sig` that signs (ES256) and one
`<device.id>#kem` that agrees keys (ECDH over P-256), never one key in both roles.

This store keeps them as owner-only PKCS#8 files through `private_io` (absolute paths, no symbolic links, atomic
writes, 0600 in a 0700 folder). It is the portable store; a hardware-held key (Secure Enclave, TPM) replaces the `#sig`
file behind the same `sign` once the native key helper exists (RM-21 NET-01). The private keys never leave this
object: callers get public keys, kids and signatures.

The device id is opaque — no person's or machine's name, so a record that carries it says nothing about who owns it.
"""
from __future__ import annotations

import base64
import json
import re
import secrets
import time
from pathlib import Path
from typing import Optional

import private_io

from . import p256

DEVICE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}$')
_FILES = ('device.json', 'sig.pem', 'kem.pem')


def new_device_id() -> str:
    """An opaque device id: `m-` and 32 random hex digits."""
    return 'm-' + secrets.token_hex(16)


def _crypto():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError as error:                           # the engine's `full` extra
        raise RuntimeError('device keys need the engine full extra (cryptography)') from error
    return serialization, ec


def _octets(private_key) -> bytes:
    serialization, _ = _crypto()
    return private_key.public_key().public_bytes(serialization.Encoding.X962,
                                                 serialization.PublicFormat.UncompressedPoint)


def _pem(private_key) -> str:
    serialization, _ = _crypto()
    return private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()).decode('ascii')


def _load(text: str):
    serialization, ec = _crypto()
    key = serialization.load_pem_private_key(text.encode('ascii'), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError('a device key is a P-256 private key')
    return key


class DeviceKeys:
    """The `#sig` and `#kem` keys of one device."""

    def __init__(self, device_id: str, sig_key, kem_key, created: str):
        if not DEVICE_ID.match(device_id):
            raise ValueError('a device id is opaque: [A-Za-z0-9][A-Za-z0-9._:~-]{0,127}')
        self.device_id = device_id
        self._sig = sig_key
        self._kem = kem_key
        self.created = created

    @property
    def sig_kid(self) -> str:
        return self.device_id + '#sig'

    @property
    def kem_kid(self) -> str:
        return self.device_id + '#kem'

    @property
    def sig_public(self) -> bytes:
        return _octets(self._sig)

    @property
    def kem_public(self) -> bytes:
        return _octets(self._kem)

    def sign(self, data) -> str:
        """ES256 by the `#sig` key: raw r ‖ s, low-S, base64url — what every relay frame carries."""
        return p256.sign_es256(self._sig, data)

    def kem_private(self):
        """The `#kem` private key, for the sealed-envelope opener (HPKE) only."""
        return self._kem

    def enrollment_keys(self) -> list:
        """The `deviceKeys` an enrollment and the estate record list: base64 SEC 1 public keys, by role."""
        encode = lambda raw: base64.b64encode(raw).decode('ascii')
        return [
            {'kid': self.sig_kid, 'use': 'sig', 'alg': 'es256', 'publicKey': encode(self.sig_public)},
            {'kid': self.kem_kid, 'use': 'kem', 'alg': 'ecdh-p256', 'publicKey': encode(self.kem_public)},
        ]

    @classmethod
    def generate(cls, device_id: Optional[str] = None) -> 'DeviceKeys':
        _, ec = _crypto()
        stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        return cls(device_id or new_device_id(), ec.generate_private_key(ec.SECP256R1()),
                   ec.generate_private_key(ec.SECP256R1()), stamp)

    # ---- The file store ----------------------------------------------------------------------------------------

    def save(self, folder: Path) -> None:
        """Writes the keys into `folder`; refuses to replace keys already there, because a replaced key is a lost
        device: the estate record still names the old one. Replacing a key is the record's ceremony (DEC-0084)."""
        folder = Path(folder)
        if any((folder / name).exists() for name in _FILES):
            raise FileExistsError('device keys already exist in this folder')
        private_io.write(folder / 'sig.pem', _pem(self._sig))
        private_io.write(folder / 'kem.pem', _pem(self._kem))
        # Written last: a folder with device.json holds a whole key set.
        private_io.write(folder / 'device.json', json.dumps({'schema': 'device-keys/1', 'id': self.device_id,
                                                             'created': self.created}, indent=1) + '\n')

    @classmethod
    def load(cls, folder: Path) -> 'DeviceKeys':
        folder = Path(folder)
        meta = json.loads(private_io.read(folder / 'device.json'))
        if not isinstance(meta, dict) or meta.get('schema') != 'device-keys/1':
            raise ValueError('not a device-keys/1 folder')
        keys = cls(meta.get('id', ''), _load(private_io.read(folder / 'sig.pem')),
                   _load(private_io.read(folder / 'kem.pem')), meta.get('created', ''))
        if keys.sig_public == keys.kem_public:
            raise ValueError('one key in both roles')
        return keys

    @classmethod
    def load_or_create(cls, folder: Path) -> 'DeviceKeys':
        folder = Path(folder)
        if (folder / 'device.json').exists():
            return cls.load(folder)
        if (folder / 'sig.pem').exists() or (folder / 'kem.pem').exists():
            # A half-written set (a crash between files) is never completed with a new key beside an old one.
            raise RuntimeError('incomplete device keys: device.json is missing beside a key file')
        keys = cls.generate()
        keys.save(folder)
        return keys

    def __repr__(self) -> str:                          # never the private keys
        return 'DeviceKeys(%s)' % self.device_id
