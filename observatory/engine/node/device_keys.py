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
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Optional

import osprivacy
import private_io

from . import p256

DEVICE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}\Z')       # \Z, not $: no trailing newline
_FILES = ('device.json', 'sig.pem', 'kem.pem')
_LOCK = '.device-keys.lock'


def _write_exclusive(path: Path, text: str) -> None:
    """Writes an owner-only file that did not exist: the bytes go to a temporary file, which is then hard-linked to
    `path`. The link fails when `path` exists, so a key file is never replaced, even by a writer that lost a race."""
    private_io.check(path)
    private_io.parent(path.parent)
    fd, name = tempfile.mkstemp(prefix='.device-key-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _sweep_temporaries(folder: Path) -> None:
    """Removes `.device-key-*` files a killed writer left between its write and its link. Called under the folder's
    lock, when no writer is running: such a file is never a key the set uses, and it may hold a private key."""
    for stale in folder.glob('.device-key-*'):
        if stale.is_file() and not stale.is_symlink():
            stale.unlink(missing_ok=True)


def _private_folder(folder: Path) -> None:
    """Refuses a folder another account owns, or one a group or others may write: either could swap the keys."""
    if not osprivacy.owned_by_me(folder) or osprivacy.others_may_write(folder):
        raise RuntimeError('the device-key folder must be owned by this account and writable by it alone')


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
        if not isinstance(device_id, str) or not DEVICE_ID.fullmatch(device_id):
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
        with private_io.lock(folder / _LOCK):
            self._save(folder)

    def _save(self, folder: Path) -> None:
        """Under the folder's lock. Each file is created exclusively; device.json last, so a folder with it holds a
        whole key set."""
        if any((folder / name).exists() for name in _FILES):
            raise FileExistsError('device keys already exist in this folder')
        _write_exclusive(folder / 'sig.pem', _pem(self._sig))
        _write_exclusive(folder / 'kem.pem', _pem(self._kem))
        _write_exclusive(folder / 'device.json', json.dumps({'schema': 'device-keys/1', 'id': self.device_id,
                                                              'created': self.created}, indent=1) + '\n')

    @classmethod
    def load(cls, folder: Path) -> 'DeviceKeys':
        folder = Path(folder)
        _private_folder(folder)
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
        """The folder's keys, or new ones written there. The check, the generation and the writes happen under the
        folder's lock, so two processes starting together end with one key set, the one both return."""
        folder = Path(folder)
        # Checked before the lock: taking it makes the folder 0700, which would hide a folder others could write.
        if folder.exists():
            _private_folder(folder)
        with private_io.lock(folder / _LOCK):
            _sweep_temporaries(folder)
            if (folder / 'device.json').exists():
                return cls.load(folder)
            if (folder / 'sig.pem').exists() or (folder / 'kem.pem').exists():
                # A half-written set (a crash between files) is never completed with a new key beside an old one.
                raise RuntimeError('incomplete device keys: device.json is missing beside a key file')
            keys = cls.generate()
            keys._save(folder)
            return keys

    def __repr__(self) -> str:                          # never the private keys
        return 'DeviceKeys(%s)' % self.device_id
