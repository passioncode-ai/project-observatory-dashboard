"""The node's shares: credentials this estate shares with another person's estate, and those shared with it
(contract DEC-0142 `fabric-peer-share/1`, OBS-264 and OBS-276; design `docs/design/PERSON-SURFACES.md` D2, D3).

A share is not a context and not a vault (PS-3). Both stores live under the node's own folder, apart from every
context's vault: `node/shares/sent/<id>/` on the sender's machine and `node/shares/received/<id>/` on the recipient's,
each owner-only. The sender's store holds the share's key pair, the claim code until the claim is read, its nonce,
the recipient's public keys and the usage the recipient reported; never a value: each value is read from the vault slot
at the moment it is sealed and never written anywhere here. The recipient's store holds the share's key pair, the
sender's public keys, the sealed envelopes as they arrived and the lease; it opens an envelope in memory, at each use,
for the one command `use_secret.py run --share` starts, and counts that use for the report.

The flows, each a step a person or the maintenance job runs (`full share ...`), each idempotent against the mailbox:

- sender `create` (keys, link, the share registered at the mailbox), `sync` (read the claim and check its MAC, commit,
  reveal, show the code, seal after the person's confirmation, read reports, reseal before an envelope ends or when the
  value changed), `confirm` / `differ` (the person's comparison, `gate: human`), `revoke` (step 1 of PS-9, observed
  against the mailbox; step 3 is the provider's and recorded when the person confirms it);
- recipient `accept` (keys, the claim with its commitment and MAC, the person's consent to the reports), `sync`
  (commit check and the pin, the reveal once and never again, the code, the lease and the envelopes, the usage report,
  the revocation answered with a signed deletion receipt), `differ`, and `open_value` (the use path).

Nothing here prints, logs or journals a value or a claim code: records carry names, keys, digests and times.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import functools
import threading
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Optional

import osprivacy
import private_io

from . import mailbox as MB, peer_share as PS, relay

SENT_SCHEMA = 'observatory-share-sent/1'
RECEIVED_SCHEMA = 'observatory-share-received/1'
DEFAULT_EXPIRES_DAYS = 30
DEFAULT_LEASE_SECONDS = 3600
#: The sender seals again this long before an envelope's `exp`, so a recipient never waits on an ended envelope.
RESEAL_BEFORE_MS = 7 * PS.DAY_MS
#: A received share reports at least this often when the maintenance job runs (PS-8: at each renewal and daily).
REPORT_EVERY_MS = 20 * 3600 * 1000
#: How long a step waits for another step of the same share (a sync talking to the mailbox) before it gives up.
LOCK_WAIT_SECONDS = 120
LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9 ._:@-]{0,63}')

#: What the recipient's "Accept" names (PS-8): accepting is the recipient's consent to it.
REPORT_CONSENT = ("The sender will see: the names of your agents that use these keys, how many times they are used "
                  "each day and when each day's last use was, any export made with the export button and when, and, "
                  "through the mailbox the sender runs, your machine's IP address and when it claims, renews and "
                  "reports.")


class ShareError(Exception):
    """A refused share step. `code` is short and stable; the message names the step and never a value."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(ms: int) -> str:
    return relay.rfc3339(ms)


def _day(ms: int) -> str:
    return _iso(ms)[:10]


# ---- Private files -------------------------------------------------------------------------------------------------

def _private_dir(path: Path) -> None:
    """An owner-only folder (POSIX modes, or this account's access list on Windows), no symbolic link on its path."""
    try:
        private_io.parent(path)
    except RuntimeError as error:
        raise ShareError('unsafe-path', f'{path.name}: {error}') from None


def _write(path: Path, text: str) -> None:
    """Owner-only, through a temporary file and a rename (`private_io.write`)."""
    try:
        private_io.write(path, text)
    except RuntimeError as error:
        raise ShareError('unsafe-path', f'{path.name}: {error}') from None


def _append(path: Path, line: str) -> None:
    """One line added owner-only with O_APPEND, so two commands using a share at once lose no use."""
    _private_dir(path.parent)
    fd = osprivacy.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | osprivacy.NOFOLLOW, 0o600)
    try:
        os.write(fd, line.encode('utf-8'))
    finally:
        os.close(fd)


def _read(path: Path) -> str:
    """An owner-only regular file, never through a symbolic link (`private_io.read`)."""
    try:
        return private_io.read(path)
    except RuntimeError as error:
        raise ShareError('unsafe-path', f'{path.name}: {error}') from None


def _key_pem(key: 'PS.SharePrivateKey') -> str:
    from cryptography.hazmat.primitives import serialization
    return key.key().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()).decode('ascii')


def _key_load(text: str) -> 'PS.SharePrivateKey':
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = serialization.load_pem_private_key(text.encode('ascii'), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ShareError('bad-key', 'a share key is a P-256 private key')
    return PS.SharePrivateKey(key)


class _Folder:
    """One share's owner-only folder: its record, its two private keys and its other files."""

    def __init__(self, path: Path):
        self.path = path

    def exists(self) -> bool:
        return (self.path / 'share.json').is_file()

    def record(self) -> dict:
        return json.loads(_read(self.path / 'share.json'))

    def save(self, record: dict) -> None:
        _write(self.path / 'share.json', json.dumps(record, indent=1, sort_keys=True) + '\n')

    def key(self, role: str) -> 'PS.SharePrivateKey':
        return _key_load(_read(self.path / f'{role}.pem'))

    def put_key(self, role: str, key: 'PS.SharePrivateKey') -> None:
        _write(self.path / f'{role}.pem', _key_pem(key))

    def text(self, name: str) -> Optional[str]:
        path = self.path / name
        return _read(path) if path.is_file() else None

    def put_text(self, name: str, text: str) -> None:
        _write(self.path / name, text)

    def drop(self, name: str) -> None:
        (self.path / name).unlink(missing_ok=True)


# ---- The store -------------------------------------------------------------------------------------------------------

def _one_share_at_a_time(method):
    """Runs a step of one share under that share's lock (`share.lock` in its folder), re-reading its record inside it:
    the hourly maintenance pass and a person's command (confirm, differ, revoke, a use) are two writers, and a stale
    copy written last could undo a seal, reuse a sequence or wipe a revocation. Re-entrant within one process, so a
    step that calls another (confirm, then sync) takes it once."""
    @functools.wraps(method)
    def locked(self, share_id, *args, **kwargs):
        folder = self._sent(share_id) if self._sent(share_id).exists() else self._received(share_id)
        with self._locked(folder):
            return method(self, share_id, *args, **kwargs)
    return locked


class Shares:
    """Both stores of one node. `transport` reaches the mailbox (`mailbox.http_transport()` by default); `clock`
    answers milliseconds; `read_value(project, env, slot)` answers a vault slot's value on the sender's side;
    `owner` is the mailbox owner's key pair `(jwk, private)` a share's creation is signed with."""

    def __init__(self, root: Path, *, transport: Optional[MB.Transport] = None,
                 clock: Optional[Callable[[], int]] = None,
                 read_value: Optional[Callable[[str, str, str], str]] = None,
                 on_wait: Optional[Callable[[str], None]] = None):
        self.root = Path(root)
        self.transport = transport or MB.http_transport()
        self.clock = clock or _now_ms
        self.read_value = read_value
        self._held = threading.local()
        self.on_wait = on_wait or (lambda message: print(message, file=__import__('sys').stderr, flush=True))

    @contextlib.contextmanager
    def _locked(self, folder: '_Folder'):
        held = self._held.__dict__.setdefault('paths', {})
        key = str(folder.path)
        if held.get(key):
            held[key] += 1
            try:
                yield
            finally:
                held[key] -= 1
            return
        with self._share_lock(folder):
            held[key] = 1
            try:
                yield
            finally:
                del held[key]

    @contextlib.contextmanager
    def _share_lock(self, folder: '_Folder'):
        """The share's lock file, taken without waiting first; when another step holds it (the maintenance pass talking
        to the mailbox), `on_wait` says so once and the lock is waited for up to LOCK_WAIT_SECONDS. Only taking the
        lock is translated into a lock refusal: an error inside the step stays that step's own (the review of #386)."""
        import oslocks
        path = folder.path / 'share.lock'
        try:
            private_io.check(path)
            private_io.parent(path.parent)
            fd = osprivacy.open(path, os.O_CREAT | os.O_RDWR | osprivacy.NOFOLLOW | osprivacy.NONBLOCK, 0o600)
        except (RuntimeError, OSError) as error:               # a link or a foreign file where the lock lives
            raise ShareError('unsafe-path', f'the share lock could not be taken: {error}') from None
        try:
            os.fchmod(fd, 0o600)
            deadline, told = time.monotonic() + LOCK_WAIT_SECONDS, False
            while True:
                try:
                    oslocks.flock(fd, oslocks.LOCK_EX | oslocks.LOCK_NB)
                    break
                except (BlockingIOError, PermissionError):
                    if not told:
                        self.on_wait(f'share {folder.path.name[:8]}: waiting for its other step (a sync with its '
                                     f'mailbox) to finish')
                        told = True
                    if time.monotonic() >= deadline:
                        raise ShareError('busy', f'share {folder.path.name[:8]} is held by another step for over '
                                                 f'{LOCK_WAIT_SECONDS} s; try again') from None
                    time.sleep(0.1)
            yield
        finally:
            os.close(fd)

    # -- folders --
    def _sent(self, share_id: str) -> _Folder:
        return _Folder(self.root / 'sent' / share_id)

    def _received(self, share_id: str) -> _Folder:
        return _Folder(self.root / 'received' / share_id)

    def _ids(self, side: str) -> list:
        folder = self.root / side
        if not folder.is_dir():
            return []
        return sorted(p.name for p in folder.iterdir() if PS.SHARE_ID.fullmatch(p.name) and (p / 'share.json').is_file())

    def find(self, ref: str) -> tuple:
        """`(side, share_id)` for a share id, a unique prefix of one (at least 6 hex digits) or a label."""
        matches = []
        for side in ('sent', 'received'):
            for share_id in self._ids(side):
                folder = self._sent(share_id) if side == 'sent' else self._received(share_id)
                label = folder.record().get('label')
                if share_id == ref or (len(ref) >= 6 and share_id.startswith(ref)) or label == ref:
                    matches.append((side, share_id))
        if not matches:
            raise ShareError('unknown-share', f'no share is called {ref!r}: `full share list` names them')
        if len(matches) > 1:
            raise ShareError('ambiguous', f'{ref!r} names {len(matches)} shares: use the share id')
        return matches[0]

    def owner(self) -> tuple:
        """The mailbox owner's key pair, made once under `node/shares/owner/`: the key `full cloud` registers with the
        mailbox at deploy, so only this node files shares there. Never a device key and never a share's key."""
        folder = _Folder(self.root / 'owner')
        if not (folder.path / 'sig.pem').is_file():
            folder.put_key('sig', PS.SharePrivateKey.generate())
        key = folder.key('sig')
        return key.jwk(), key

    def _client(self, origin: str, folder: _Folder, record: dict, *, owner: bool = False) -> MB.MailboxClient:
        key = folder.key('sig')
        return MB.MailboxClient(origin, record['keys']['sig'], key, self.transport, clock=self.clock,
                                owner=self.owner() if owner else None)

    # ================================================================================================================
    # The sender
    # ================================================================================================================

    def create(self, *, project: str, env: str, slots: list, label: str, origin: str,
               providers: Optional[dict] = None, expires_days: int = DEFAULT_EXPIRES_DAYS,
               claim_days: int = PS.MAX_CLAIM_DAYS, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> tuple:
        """Makes a share of `slots` (vault slots of `project`/`env`) for the recipient `label`, registers it at the
        mailbox and answers `(record, link)`. The link is returned once and kept nowhere but the claim code's own
        owner-only file, removed when the claim is read."""
        if not LABEL.fullmatch(label or ''):
            raise ShareError('bad-label', 'a recipient label is 1 to 64 letters, digits, spaces and ._:@-')
        if not slots or any(not PS.SLOT.fullmatch(s) for s in slots) or len(set(slots)) != len(slots):
            raise ShareError('bad-slot', 'a slot is an upper-case variable name such as OPENROUTER_API_KEY, each once')
        if not 1 <= claim_days <= PS.MAX_CLAIM_DAYS:
            raise ShareError('claim-days', 'a claim deadline is 1 to 7 days away (PS-4)')
        if not 60 <= lease_seconds <= PS.MAX_LEASE_SECONDS:
            raise ShareError('lease', 'a lease is 60 s to 24 h (PS-7)')
        if not 1 <= expires_days <= 3650:
            raise ShareError('expires', 'a share ends 1 to 3650 days from now')
        parsed = PS.parse_share_link(origin.rstrip('/') + '/s/' + '0' * 32 + '#c=' + 'A' * 22 + '&k=' + 'A' * 43)
        if not parsed['ok']:
            raise ShareError('bad-mailbox', 'a mailbox is an https origin with no path, user or query')
        origin = parsed['link']['origin']
        providers = providers or {}
        provider_of = {}
        for slot in slots:
            provider = providers.get(slot) or slot.split('_')[0].lower()
            if not PS.PROVIDER.fullmatch(provider):
                raise ShareError('bad-provider', f'{slot}: a provider is a lower-case slug such as openrouter')
            provider_of[slot] = provider
            self._value(project, env, slot)                  # present in the vault, or refused now, not at sealing
        now = self.clock()
        share_id, code = PS.new_share_id(), PS.new_link_code()
        sig, kem = PS.SharePrivateKey.generate(), PS.SharePrivateKey.generate()
        keys = {'sig': sig.jwk(), 'kem': kem.jwk()}
        record = {
            'schema': SENT_SCHEMA, 'shareId': share_id, 'origin': origin, 'label': label, 'project': project,
            'env': env, 'slots': [{'slot': s, 'provider': provider_of[s], 'sequence': 0, 'sealedAt': None, 'exp': None,
                                   'valueMac': None} for s in slots],
            'createdAt': _iso(now), 'claimDeadline': _iso(now + claim_days * PS.DAY_MS),
            'endsAt': _iso(now + expires_days * PS.DAY_MS), 'leaseSeconds': lease_seconds, 'keys': keys,
            'fpr': PS.peer_thumbprint(keys['sig']), 'state': 'waiting', 'burnt': None, 'claimChecked': False,
            'recipientKeys': None, 'recipientCommit': None, 'nonce': None, 'code': None, 'confirmed': None,
            'sealed': False, 'late': 0, 'revocation': None, 'revocationObserved': False, 'deletion': None,
            'providerEnded': False, 'deliveries': None, 'lastRenewal': None,
            'usage': {'days': {}, 'consumers': [], 'exports': 0, 'exportTimes': [], 'lastSequence': 0},
            'updatedAt': _iso(now)}
        folder = self._sent(share_id)
        _private_dir(folder.path)
        # Under the share's lock from its first file on: a maintenance pass that finds it half-made waits.
        refused = None
        with self._locked(folder):
            folder.put_key('sig', sig)
            folder.put_key('kem', kem)
            folder.put_text('claim-code', code)
            folder.put_text('mac-key', secrets.token_hex(32))
            folder.save(record)
            try:
                self._client(origin, folder, record, owner=True).create(share_id, record['claimDeadline'],
                                                                        lease_seconds)
            except MB.MailboxError as error:
                refused = error
                # Unlisted while the lock is held (a pass lists only folders with a record), so nothing reads it after.
                folder.drop('share.json')
        if refused is not None:
            # Removed after the lock is let go: Windows refuses to delete a folder that holds an open file.
            shutil.rmtree(folder.path, ignore_errors=True)
            raise ShareError('mailbox', f'the mailbox refused the new share: {refused.code}: {refused}')
        link = PS.share_link({'origin': origin, 'shareId': share_id, 'code': code, 'fpr': record['fpr']})
        return record, link

    def _value(self, project: str, env: str, slot: str) -> str:
        if self.read_value is None:
            raise ShareError('no-vault', 'this node reads no vault here')
        try:
            value = self.read_value(project, env, slot)
        except LookupError as error:
            raise ShareError('no-slot', str(error)) from None
        if not value:
            raise ShareError('no-slot', f'{project}/{env}/{slot} is empty in the vault')
        return value

    def _value_mac(self, folder: _Folder, value: str) -> str:
        key = bytes.fromhex(folder.text('mac-key') or '')
        return 'hmac-sha256:' + hmac.new(key, value.encode('utf-8'), hashlib.sha256).hexdigest()

    @_one_share_at_a_time
    def sync_sent(self, share_id: str) -> dict:
        """One pass of the sender's side against the mailbox; answers the record."""
        folder = self._sent(share_id)
        record = folder.record()
        if record['state'] in ('burnt', 'ended'):
            return record
        client = self._client(record['origin'], folder, record)
        now = self.clock()
        try:
            if not record['sealed']:
                self._exchange_sent(folder, record, client, now)
            if record['state'] not in ('burnt',):
                self._after_seal_sent(folder, record, client, now)
        except MB.MailboxError as error:
            record['mailbox'] = {'error': error.code, 'at': _iso(now)}
        except ShareError as error:
            # A slot removed from the vault, or one that no longer reads: the share waits, and says why.
            record['problem'] = {'code': error.code, 'message': str(error), 'at': _iso(now)}
        except ValueError as error:
            # A seal the contract's checks refuse (a value no recipient would open): never sent, and said.
            record['problem'] = {'code': 'seal-refused', 'message': str(error).split(':')[0], 'at': _iso(now)}
        else:
            record.pop('mailbox', None)
            record.pop('problem', None)
        record['updatedAt'] = _iso(now)
        folder.save(record)
        return record

    def _burn(self, record: dict, reason: str, why: str) -> None:
        if record['burnt'] is None:
            record['burnt'] = {'reason': reason, 'why': why}
        record['state'] = 'burnt'

    def _exchange_sent(self, folder: _Folder, record: dict, client: MB.MailboxClient, now: int) -> None:
        share_id = record['shareId']
        answer = client.frames(share_id)
        frames = answer.get('frames') or []
        verdict = PS.peer_exchange_verdict(share_id, record['claimDeadline'], frames, _iso(now))
        if verdict['state'] == 'burnt':
            folder.drop('claim-code')
            self._burn(record, verdict['reason'], 'the exchange burnt: a new attempt is a new share')
            return
        if not frames:
            return
        claim = frames[0]
        if not record['claimChecked']:
            code = folder.text('claim-code')
            link = {'shareId': share_id, 'code': code, 'fpr': record['fpr']}
            folder.drop('claim-code')
            if code is None or not PS.claim_holds(link, claim):
                # The link's one claim, whatever its MAC (PS-5): the link is spent.
                self._burn(record, 'claim-mac', "the first claim's MAC did not hold: the link is spent")
                return
            record.update(claimChecked=True, recipientKeys=claim['keys'], recipientCommit=claim['commit'],
                          state='exchanging')
            folder.save(record)
        mine = [f for f in frames if f.get('step') == 'commit']
        if not mine:
            if record['nonce'] is None:
                record['nonce'] = PS.new_nonce()
                folder.save(record)                            # the nonce is kept before the commit is sent
            side = {'role': 'sender', 'shareId': share_id, 'keys': record['keys'], 'nonce': record['nonce']}
            commit = PS.peer_commit(side)['commit']
            client.post_frame({'schema': PS.PEER_FRAME_SCHEMA, 'step': 'commit', 'shareId': share_id,
                               'keys': record['keys'], 'commit': commit})
            frames = client.frames(share_id).get('frames') or []
        if not any(f.get('step') == 'reveal' and f.get('role') == 'sender' for f in frames):
            # The recipient's commitment is held (its claim): the sender may reveal now (PS-6, step 2).
            client.post_frame({'schema': PS.PEER_FRAME_SCHEMA, 'step': 'reveal', 'role': 'sender',
                               'shareId': share_id, 'nonce': record['nonce']})
            frames = client.frames(share_id).get('frames') or []
        verdict = PS.peer_exchange_verdict(share_id, record['claimDeadline'], frames, _iso(now))
        if verdict['state'] == 'burnt':
            self._burn(record, verdict['reason'], 'the exchange burnt: a new attempt is a new share')
            return
        if verdict['state'] == 'complete' and record['code'] is None:
            reveal = next(f for f in frames if f.get('step') == 'reveal' and f.get('role') == 'recipient')
            commit = next(f for f in frames if f.get('step') == 'commit')
            code = PS.checked_peer_code(commit['commit'], {'role': 'sender', 'shareId': share_id,
                                                           'keys': record['keys'], 'nonce': record['nonce']},
                                        record['recipientCommit'],
                                        {'role': 'recipient', 'shareId': share_id, 'keys': record['recipientKeys'],
                                         'nonce': reveal['nonce']})
            if not code['ok']:
                # A commit reflecting the claim's own keys stops at the code (`same-keys`): nothing is sealed.
                self._burn(record, code['problem'], 'the comparison code could not be made: nothing is sealed')
                return
            record.update(code=code['code'], state='comparing')

    @_one_share_at_a_time
    def confirm(self, share_id: str, *, code: Optional[str] = None, without_comparing: bool = False) -> dict:
        """The sender's comparison (PS-6 step 4, `gate: human`): the code the person read from the recipient matches
        (sealed, `confirmed: true`), or "Share without comparing" (sealed, `confirmed: false`, trust on first use). A
        code that differs is "They differ": the exchange is aborted and nothing is sealed."""
        folder = self._sent(share_id)
        record = folder.record()
        if record['state'] != 'comparing' or record['code'] is None:
            raise ShareError('not-comparing', f"share {share_id[:8]} has no code to compare (state {record['state']})")
        if not without_comparing:
            typed = re.sub(r'\D', '', code or '')
            if len(typed) != PS.PEER_CODE_DIGITS:
                raise ShareError('bad-code', 'a comparison code is six digits')
            if not hmac.compare_digest(typed, record['code']):
                folder.save(record)
                return self.differ(share_id)
        record.update(confirmed=not without_comparing, state='sealing')
        folder.save(record)
        return self.sync_sent(share_id)

    @_one_share_at_a_time
    def differ(self, share_id: str) -> dict:
        """"They differ" on either side (PS-6). Before the sender sealed, the side posts its one `abort` and nothing is
        sealed. After it sealed, an abort no longer stops the recipient's machine (the mailbox carries it to the sender
        only as a warning), so the sender's "They differ" takes the share back: the signed revocation makes the
        mailbox refuse every renewal, and the recipient's honest machine stops at its next renewal, within one lease.
        The recipient's own "They differ" deletes its copies at once, opens nothing more, and warns the sender."""
        side = 'sent' if self._sent(share_id).exists() else 'received'
        folder = self._sent(share_id) if side == 'sent' else self._received(share_id)
        record = folder.record()
        now = self.clock()
        # The sender's record only: a recipient's slots carry no sequence of the sender's and it seals nothing.
        sealed = side == 'sent' and (record['sealed'] or record['state'] == 'sealing'
                                     or any(e['sequence'] > 0 for e in record['slots']))
        if side == 'sent' and sealed:
            record['differedAt'] = _iso(now)
            folder.save(record)
            record = self.revoke(share_id)
            refuses = ("The mailbox refuses it now; the recipient's machine stops at its next renewal, within one lease"
                       if record['revocationObserved'] else
                       "The mailbox did not answer: every later pass (the maintenance job's comes within an hour) hands "
                       "it the revocation again, and the recipient's machine stops at its next renewal after that")
            record['warning'] = (f"you said the codes differ after the key was sealed: the share is being taken back. "
                                 f"{refuses}. Whoever received it may have copied it, so revoke or rotate the key at "
                                 f"its provider")
            folder.save(record)
            return record
        role = 'sender' if side == 'sent' else 'recipient'
        client = self._client(record['origin'], folder, record)
        try:
            client.post_frame({'schema': PS.PEER_FRAME_SCHEMA, 'step': 'abort', 'role': role,
                               'shareId': share_id})
        except MB.MailboxError as error:
            record['mailbox'] = {'error': error.code, 'at': _iso(now)}
        if side == 'received':
            shutil.rmtree(folder.path / 'envelopes', ignore_errors=True)
            record['lease'] = None
            record['marks'] = []
            why = 'you said the codes differ: this machine deleted its copies and opens nothing of this share'
        else:
            why = 'you said the codes differ: nothing was sealed, and this link is spent'
        self._burn(record, 'aborted', why)
        folder.save(record)
        return record

    def _after_seal_sent(self, folder: _Folder, record: dict, client: MB.MailboxClient, now: int) -> None:
        share_id = record['shareId']
        if record['state'] == 'sealing' or (record['sealed'] and record['revocation'] is None
                                            and record['state'] == 'active'):
            self._seal_due(folder, record, client, now)
        if not record['claimChecked']:
            return
        status = client.status(share_id)
        record['deliveries'], record['lastRenewal'] = status.get('deliveries'), status.get('lastRenewal')
        late = status.get('late', 0)
        if record['sealed'] and late and late != record.get('late') and record['revocation'] is None:
            # After the first envelope the mailbox carries a late claim or abort to the sender only, as a warning to
            # take the share back (PS-6): the recipient's own "they differ", or a claim by whoever read the link.
            steps = {f.get('step') for f in client.frames(share_id).get('late') or [] if isinstance(f, dict)}
            if 'abort' in steps:
                record['warning'] = (f"the recipient said the codes differ after the key was sealed: take the share "
                                     f"back with `full share revoke {share_id[:8]}`, and revoke or rotate the key at "
                                     f"its provider")
            else:
                record['warning'] = (f"another machine claimed this link after the key was sealed: whoever claimed "
                                     f"may have read the link; take the share back with `full share revoke "
                                     f"{share_id[:8]}` if you did not expect it")
        record['late'] = late
        deletion = status.get('deletion')
        if record['revocation'] is not None:
            def stands(held) -> bool:
                return PS.peer_record_stands(held, PS.PEER_REVOCATION_TYPE, share_id, record['keys']['sig'])['ok']
            held = status.get('revocation')
            if not stands(held):
                # A revocation the mailbox does not show yet (it was down when it was taken back): handed again, as
                # often as it takes.
                held = client.revoke(record['revocation']).get('revocation')
            # Any revocation this share's #sig signed is the share taken back; the mailbox keeps the first it held,
            # and the recipient's receipt names that one, so the record follows it.
            record['revocationObserved'] = stands(held)
            if record['revocationObserved']:
                record['revocation'] = held
                if record.get('differedAt'):
                    record['warning'] = ("you said the codes differ after the key was sealed, and the share is taken "
                                         "back at the mailbox: the recipient's machine stops at its next renewal, "
                                         "within one lease. Whoever received it may have copied it, so revoke or "
                                         "rotate the key at its provider")
            if isinstance(deletion, dict) and record['recipientKeys'] is not None \
                    and PS.peer_record_stands(deletion, PS.PEER_DELETION_TYPE, share_id,
                                              record['recipientKeys']['sig'])['ok'] \
                    and deletion.get('revocation') == PS.peer_digest(record['revocation']):
                record['deletion'] = deletion
        if record['recipientKeys'] is not None and record['sealed']:
            self._read_reports(folder, record, client, now)
        if record['revocation'] is None and record['sealed'] and now >= relay.instant(record['endsAt']):
            record['state'] = 'expired'

    def _seal_due(self, folder: _Folder, record: dict, client: MB.MailboxClient, now: int) -> None:
        """Seals each slot whose envelope is missing, ends within a week, or whose vault value changed (rotation,
        PS-7), under the next sequence; never past the share's end date."""
        ends = relay.instant(record['endsAt'])
        if now >= ends:
            return
        sig = folder.key('sig')
        target = min(ends, now + PS.MAX_PEER_ENVELOPE_DAYS * PS.DAY_MS)
        for entry in record['slots']:
            value = self._value(record['project'], record['env'], entry['slot'])
            mac = self._value_mac(folder, value)
            exp = relay.instant(entry['exp']) if entry['exp'] else None
            changed = not entry['sealedAt'] or mac != entry['valueMac'] or exp is None
            # Near its end an envelope is sealed again only when the new one lasts longer (the share's own end caps
            # both in its last week, and a pass must not reseal it every time).
            ending = exp is not None and exp - now <= RESEAL_BEFORE_MS and target > exp
            if not (changed or ending):
                continue
            sequence = entry['sequence'] + 1
            entry['sequence'] = sequence
            folder.save(record)                                # a sequence is never used for two envelopes
            sealed = PS.seal_peer_share(recipient_kem=record['recipientKeys']['kem'], signer_sig=record['keys']['sig'],
                                        signer=sig, share_id=record['shareId'], sequence=sequence,
                                        iat_ms=now, exp_ms=target, slot=entry['slot'], provider=entry['provider'],
                                        plaintext=value.encode('utf-8'))
            del value
            client.put_envelope(sealed)
            entry.update(sealedAt=_iso(now), exp=sealed['header']['exp'], valueMac=mac)
            record['sealed'] = True
            folder.save(record)
        record['state'] = 'active'

    def _read_reports(self, folder: _Folder, record: dict, client: MB.MailboxClient, now: int) -> None:
        usage = record['usage']
        answer = client.reports(record['shareId'], usage['lastSequence'])
        kem = folder.key('kem')
        for report in sorted(answer.get('reports') or [], key=lambda r: r.get('header', {}).get('sequence', 0)):
            ctx = {'at': _iso(now), 'shareId': record['shareId'], 'ownKem': record['keys']['kem'],
                   'recipientSig': record['recipientKeys']['sig'], 'lastSequence': usage['lastSequence']}
            opened = PS.open_peer_report(report, ctx, kem)
            if not opened['ok']:
                usage['refused'] = (usage.get('refused', []) + [{'sequence': report.get('header', {}).get('sequence'),
                                                                 'refusal': opened['refusal']}])[-50:]
                continue
            body = opened['body']
            for day in body['days']:
                usage['days'].setdefault(day['date'], {})[day['slot']] = {'uses': day['uses'], 'lastUse': day['lastUse']}
            for consumer in body['consumers']:
                if consumer not in usage['consumers']:
                    usage['consumers'].append(consumer)
            usage['exports'] += len(body['exports'])
            usage['exportTimes'] = (usage['exportTimes'] + body['exports'])[-400:]
            usage['lastSequence'] = opened['sequence']
            usage['lastReportAt'] = _iso(now)

    @_one_share_at_a_time
    def revoke(self, share_id: str) -> dict:
        """Takes a share back (PS-9): signs `peer-share-revocation/1` with the share `#sig` and hands it to the mailbox,
        which refuses every renewal from then on (step 1, observed against the mailbox). Step 2 is the recipient's
        deletion receipt at its next contact; step 3 is the provider's: `provider_ended` records it."""
        folder = self._sent(share_id)
        record = folder.record()
        now = self.clock()
        if record['revocation'] is None:
            record['revocation'] = PS.sign_peer_record({'type': PS.PEER_REVOCATION_TYPE, 'shareId': share_id,
                                                        'at': _iso(now)}, record['keys']['sig'], folder.key('sig'))
            folder.drop('claim-code')
            folder.save(record)
        client = self._client(record['origin'], folder, record)
        try:
            answer = client.revoke(record['revocation'])
            record['revocationObserved'] = answer.get('revocation') == record['revocation']
            record.pop('mailbox', None)
        except MB.MailboxError as error:
            record['mailbox'] = {'error': error.code, 'at': _iso(now)}
        if record['state'] not in ('burnt',):
            record['state'] = 'taking-back'
        folder.save(record)
        return record

    @_one_share_at_a_time
    def provider_ended(self, share_id: str) -> dict:
        """Step 3 of PS-9, the person's statement that the provider revoked a key minted for the share or the sender
        rotated their own: only then, with step 1 observed, does a screen say "taken back"."""
        folder = self._sent(share_id)
        record = folder.record()
        if record['revocation'] is None:
            raise ShareError('not-revoked', 'take the share back first: `full share revoke ID`')
        record['providerEnded'] = True
        if record['revocationObserved']:
            record['state'] = 'ended'
        folder.save(record)
        return record

    # ================================================================================================================
    # The recipient
    # ================================================================================================================

    def accept(self, link_text: str, *, label: Optional[str] = None, pinned: bool = True) -> dict:
        """The recipient's "Accept" (PS-5, `gate: human`): consent to the reports (`REPORT_CONSENT`), a key pair for the
        share, and the claim with its commitment and MAC. `pinned` is true when the link reached the app as the sender
        sent it (pasted): the sender's commit must then name the share `#sig` the link's `k` names (PS-4)."""
        parsed = PS.parse_share_link(link_text)
        if not parsed['ok']:
            raise ShareError('bad-link', 'this is not a share link: https://MAILBOX/s/ID#c=CODE&k=FINGERPRINT')
        link = parsed['link']
        share_id = link['shareId']
        if label is not None and not LABEL.fullmatch(label):
            raise ShareError('bad-label', 'a label is 1 to 64 letters, digits, spaces and ._:@-')
        folder = self._received(share_id)
        if folder.path.exists():
            # The app reveals a nonce for a share id once and never again (PS-6): a link is opened once.
            raise ShareError('already-opened', 'this link was already opened on this machine; a new attempt needs a '
                                               'new link from the sender')
        if self._sent(share_id).exists():
            raise ShareError('own-share', 'this is a share this machine sent; it is opened on the recipient\'s machine')
        now = self.clock()
        sig, kem = PS.SharePrivateKey.generate(), PS.SharePrivateKey.generate()
        keys = {'sig': sig.jwk(), 'kem': kem.jwk()}
        nonce = PS.new_nonce()
        commit = PS.peer_commit({'role': 'recipient', 'shareId': share_id, 'keys': keys, 'nonce': nonce})['commit']
        mac = PS.claim_mac(link['code'], share_id, keys, link['fpr'])
        record = {'schema': RECEIVED_SCHEMA, 'shareId': share_id, 'origin': link['origin'], 'mac': mac,
                  'label': label or ('share-' + share_id[:8]), 'fpr': link['fpr'], 'pinned': bool(pinned),
                  'keys': keys, 'nonce': nonce, 'commit': commit, 'revealed': False, 'senderKeys': None,
                  'senderCommit': None, 'code': None, 'state': 'claiming', 'burnt': None, 'lease': None, 'marks': [],
                  'slots': [], 'revocation': None, 'deletion': None, 'reportSequence': 0, 'lastReportDay': None,
                  'lastReportAt': None, 'consent': REPORT_CONSENT, 'acceptedAt': _iso(now), 'updatedAt': _iso(now)}
        _private_dir(folder.path)
        # Under the share's lock from its first file on: a maintenance pass cannot post this claim a second time.
        with self._locked(folder):
            folder.put_key('sig', sig)
            folder.put_key('kem', kem)
            folder.save(record)                                    # kept before the claim is sent: never a second claim
            client = self._client(link['origin'], folder, record)
            try:
                client.post_frame(self._claim_frame(record))
                record['state'] = 'exchanging'
            except MB.MailboxError as error:
                if error.code in ('burnt', 'sealed', 'unknown-share'):
                    self._burn(record, 'link-used' if error.code != 'unknown-share' else 'unknown-share',
                               'the link was used elsewhere, expired or is unknown to its mailbox: ask the sender for a '
                               'new one')
                else:
                    record['mailbox'] = {'error': error.code, 'at': _iso(now)}
            folder.save(record)
            return self.sync_received(share_id) if record['state'] == 'exchanging' else record

    @_one_share_at_a_time
    def sync_received(self, share_id: str, *, due_only: bool = False) -> dict:
        """One pass of the recipient's side against the mailbox; answers the record. `due_only` (the maintenance job)
        renews an active share only when its lease ended or its last report is older than REPORT_EVERY_MS, so the
        sender's delivery count stays a count of renewals the recipient needed, and a report still goes daily."""
        folder = self._received(share_id)
        record = folder.record()
        if record['state'] in ('burnt', 'removed', 'declined'):
            return record
        client = self._client(record['origin'], folder, record)
        now = self.clock()
        reported = relay.instant(record.get('lastReportAt')) or 0
        if due_only and record['state'] == 'active' and self._lease_live(record, now) \
                and now - reported < REPORT_EVERY_MS:
            return record
        try:
            if record['state'] in ('claiming', 'exchanging'):
                self._exchange_received(folder, record, client, now)
            if record['state'] in ('comparing', 'active', 'expired'):
                self._renew(folder, record, client, now)
            record.pop('mailbox', None)
        except MB.MailboxError as error:
            record['mailbox'] = {'error': error.code, 'at': _iso(now)}
        except (ShareError, ValueError) as error:
            record['problem'] = {'code': getattr(error, 'code', 'refused'), 'message': str(error).split(':')[0],
                                 'at': _iso(now)}
        record['updatedAt'] = _iso(now)
        folder.save(record)
        return record

    def _exchange_received(self, folder: _Folder, record: dict, client: MB.MailboxClient, now: int) -> None:
        share_id = record['shareId']
        try:
            answer = client.frames(share_id)
        except MB.MailboxError as error:
            if error.code == 'no-claim' and record['state'] == 'claiming':
                # The claim never reached the mailbox (its answer was lost before it arrived): send the same claim.
                client.post_frame(self._claim_frame(record))
                answer = client.frames(share_id)
            elif error.code == 'signature-invalid':
                # The mailbox holds another first claim: this machine's claim is not the link's one claim.
                self._burn(record, 'link-used', 'another claim reached the mailbox first: ask the sender for a new link')
                return
            else:
                raise
        frames = answer.get('frames') or []
        if not frames or frames[0].get('keys') != record['keys']:
            self._burn(record, 'link-used', 'another claim reached the mailbox first: ask the sender for a new link')
            return
        record['state'] = 'exchanging'
        verdict = PS.peer_exchange_verdict(share_id, '9999-12-31T00:00:00Z', frames, _iso(now))
        if verdict['state'] == 'burnt':
            self._burn(record, verdict['reason'], 'the exchange burnt: ask the sender for a new link')
            return
        commit = next((f for f in frames if f.get('step') == 'commit'), None)
        if commit is None:
            return
        if record['pinned'] and not PS.pin_holds({'shareId': share_id, 'fpr': record['fpr']}, commit):
            client.post_frame({'schema': PS.PEER_FRAME_SCHEMA, 'step': 'abort', 'role': 'recipient',
                               'shareId': share_id})
            self._burn(record, 'pin-mismatch', "the sender's commit does not name the key the link names: aborted")
            return
        record.update(senderKeys=commit['keys'], senderCommit=commit['commit'])
        if not record['revealed']:
            record['revealed'] = True
            folder.save(record)                                # one nonce for this share id, never a second
        if not any(f.get('step') == 'reveal' and f.get('role') == 'recipient' for f in frames):
            # The same committed nonce again when its first post was lost: never a fresh one, so a mailbox posing as
            # the sender cannot rerun this side for another code.
            client.post_frame({'schema': PS.PEER_FRAME_SCHEMA, 'step': 'reveal', 'role': 'recipient',
                               'shareId': share_id, 'nonce': record['nonce']})
            frames = client.frames(share_id).get('frames') or []
        reveal = next((f for f in frames if f.get('step') == 'reveal' and f.get('role') == 'sender'), None)
        if reveal is None:
            return
        code = PS.checked_peer_code(record['senderCommit'], {'role': 'sender', 'shareId': share_id,
                                                             'keys': record['senderKeys'], 'nonce': reveal['nonce']},
                                    record['commit'], {'role': 'recipient', 'shareId': share_id,
                                                       'keys': record['keys'], 'nonce': record['nonce']})
        if not code['ok']:
            self._burn(record, code['problem'], 'the comparison code could not be made: nothing is opened')
            return
        record.update(code=code['code'], state='comparing')

    @staticmethod
    def _claim_frame(record: dict) -> dict:
        return {'schema': PS.PEER_FRAME_SCHEMA, 'step': 'claim', 'shareId': record['shareId'], 'keys': record['keys'],
                'mac': record['mac'], 'commit': record['commit']}

    def _renew(self, folder: _Folder, record: dict, client: MB.MailboxClient, now: int) -> None:
        share_id = record['shareId']
        try:
            answer = client.lease(share_id)
        except MB.MailboxError as error:
            if error.code == 'revoked':
                self._removed(folder, record, client, error.detail.get('revocation'), now)
                return
            if error.code == 'burnt':
                self._burn(record, error.detail.get('reason', 'burnt'), 'the exchange burnt at the mailbox')
                return
            if error.code == 'not-sealed':
                return                                         # the sender has not confirmed yet
            raise
        marks = list(record['marks'])
        envelopes = folder.path / 'envelopes'
        slots = []
        for env in answer.get('envelopes') or []:
            ctx = {'at': _iso(now), 'shareId': share_id, 'ownKem': record['keys']['kem'],
                   'senderSig': record['senderKeys']['sig'], 'marks': marks}
            check = PS.check_peer_share_envelope(env, ctx)
            if not check['ok']:
                record.setdefault('refused', []).append({'slot': env.get('header', {}).get('slot'),
                                                         'refusal': check['refusal'], 'at': _iso(now)})
                continue
            mark = check['mark']
            marks = [m for m in marks if m['slot'] != mark['slot']] + [mark]
            _write(envelopes / f"{mark['slot']}.json", json.dumps(env, sort_keys=True))
            slots.append({'slot': mark['slot'], 'provider': env['header']['provider'], 'exp': env['header']['exp'],
                          'sequence': mark['sequence']})
        lease = answer.get('lease') or {}
        record.update(marks=marks, state='active' if marks else record['state'],
                      lease={'grantedAt': lease.get('grantedAt'), 'expiresAt': lease.get('expiresAt'),
                             'receivedAt': _iso(now)})
        if slots:
            known = {s['slot']: s for s in record['slots']}
            known.update({s['slot']: s for s in slots})
            record['slots'] = sorted(known.values(), key=lambda s: s['slot'])
        folder.save(record)
        self._report(folder, record, client, now)

    def _removed(self, folder: _Folder, record: dict, client: MB.MailboxClient, revocation: Any, now: int) -> None:
        """The sender took the share back (PS-9 step 2): the ciphertexts and the lease are deleted and a signed deletion
        receipt naming the revocation is returned."""
        senders = record['senderKeys']
        if not senders or not PS.peer_record_stands(revocation, PS.PEER_REVOCATION_TYPE, record['shareId'],
                                                    senders['sig'])['ok']:
            # A mailbox's word alone is not the sender's: stop using it, and wait for a revocation that stands.
            record['lease'] = None
            record['warning'] = 'the mailbox refuses this share without a revocation the sender signed'
            return
        record.update(lease=None, marks=[])
        try:
            shutil.rmtree(folder.path / 'envelopes')
        except FileNotFoundError:
            pass
        except OSError as error:
            # A receipt says the copies are deleted: it is signed only once they are.
            record['problem'] = {'code': 'delete-failed', 'message': f'the sealed copies could not be deleted '
                                 f'({type(error).__name__}); retried at the next pass', 'at': _iso(now)}
            folder.save(record)
            return
        record.pop('problem', None)
        record.update(revocation=revocation, state='removed', removedAt=_iso(now))
        receipt = PS.sign_peer_record({'type': PS.PEER_DELETION_TYPE, 'shareId': record['shareId'], 'at': _iso(now),
                                       'revocation': PS.peer_digest(revocation)}, record['keys']['sig'],
                                      folder.key('sig'))
        record['deletion'] = receipt
        folder.save(record)
        client.deletion(receipt)

    def _lease_live(self, record: dict, now: int) -> bool:
        lease = record.get('lease') or {}
        expires, received = relay.instant(lease.get('expiresAt')), relay.instant(lease.get('receivedAt'))
        granted = relay.instant(lease.get('grantedAt'))
        if expires is None or received is None or granted is None:
            return False
        # Never open on a lease older than 24 h from its grant, whatever the mailbox said (PS-10).
        return now < min(expires, granted + PS.MAX_LEASE_SECONDS * 1000, received + PS.MAX_LEASE_SECONDS * 1000)

    def _uses(self, folder: _Folder) -> list:
        """The use journal's readable lines: a line cut short or damaged is skipped, never fatal to every later use."""
        out = []
        for line in (folder.text('uses.jsonl') or '').splitlines():
            try:
                use = json.loads(line)
            except ValueError:
                continue
            if isinstance(use, dict) and relay.instant(use.get('at')) is not None \
                    and PS.SLOT.fullmatch(str(use.get('slot', ''))) and PS.AGENT_NAME.fullmatch(str(use.get('agent', ''))):
                out.append(use)
        return out

    def _report(self, folder: _Folder, record: dict, client: MB.MailboxClient, now: int) -> None:
        """The usage report (PS-8), sealed to the sender's share `#kem`, at each renewal: the days since the last day
        reported (that day again, as it grows), the consumers since the last report; never a value or a command."""
        if record['senderKeys'] is None:
            return
        with private_io.lock(folder.path / 'uses.lock'):
            self._report_locked(folder, record, client, now)

    def _report_locked(self, folder: _Folder, record: dict, client: MB.MailboxClient, now: int) -> None:
        """The report and the journal's rewrite under the journal's lock: no use lands between the read and the
        rewrite, so none is dropped."""
        uses = self._uses(folder)
        since_day = record['lastReportDay'] or '0000-01-01'
        since_at = relay.instant(record['lastReportAt']) or 0
        days: dict = {}
        consumers: list = []
        for use in uses:
            if use['at'][:10] >= since_day:
                entry = days.setdefault((use['at'][:10], use['slot']), {'uses': 0, 'lastUse': None})
                entry['uses'] += 1
                entry['lastUse'] = max(entry['lastUse'] or '', use['at'])
            if (relay.instant(use["at"]) or 0) >= since_at:          # the sender keeps a set
                pair = {'slot': use['slot'], 'agent': use['agent']}
                if pair not in consumers:
                    consumers.append(pair)
        today = _day(now)
        for slot in record['slots']:
            days.setdefault((today, slot['slot']), {'uses': 0, 'lastUse': None})
        day_rows = [{'date': d, 'slot': s, 'uses': v['uses'], 'lastUse': v['lastUse']} for (d, s), v in sorted(days.items())]
        pages = max(1, -(-len(day_rows) // 400), -(-len(consumers) // 400))
        # A list past 400 entries is split over reports of consecutive sequences, sent together (PS-8).
        for page in range(pages):
            body = {'days': day_rows[page * 400:(page + 1) * 400], 'consumers': consumers[page * 400:(page + 1) * 400],
                    'exports': []}
            sequence = record['reportSequence'] + 1
            record['reportSequence'] = sequence
            folder.save(record)                                # a sequence is never used twice
            report = PS.seal_peer_report(recipient_kem=record['senderKeys']['kem'], signer_sig=record['keys']['sig'],
                                         signer=folder.key('sig'), share_id=record['shareId'], sequence=sequence,
                                         iat_ms=now, body=body)
            client.post_report(report)
        record.update(lastReportDay=today, lastReportAt=_iso(now))
        # The journal keeps what the next report still needs: today's uses, which it reports again as they grow.
        kept = [json.dumps(u, sort_keys=True) + '\n' for u in uses if u['at'][:10] >= today]
        _write(folder.path / 'uses.jsonl', ''.join(kept))

    def open_value(self, ref: str, slot: str, *, agent: str) -> str:
        """The use path (PS-3, PS-7): the slot's value, opened in memory for one use, under a live lease (renewed here
        when it is not), counted for the report. Raises `ShareError`; the value is never written anywhere."""
        side, share_id = self.find(ref)
        if side != 'received':
            raise ShareError('not-received', 'this share was sent from this machine; its value is in your vault')
        folder = self._received(share_id)
        with self._locked(folder):
            record = folder.record()
            if not PS.SLOT.fullmatch(slot or ''):
                raise ShareError('bad-slot', 'a slot is an upper-case variable name')
            agent = agent if PS.AGENT_NAME.fullmatch(agent or '') else 'unknown'
            now = self.clock()
            if record['state'] != 'active' or not self._lease_live(record, now):
                record = self.sync_received(share_id)
                now = self.clock()
            if record['state'] == 'removed':
                raise ShareError('revoked', 'the sender took this share back; its keys were removed from this machine')
            if record['state'] == 'burnt':
                raise ShareError('burnt', f"this share burnt ({record['burnt']['reason']}); ask the sender for a new link")
            if record['state'] != 'active':
                raise ShareError('not-active', f"this share is {record['state']}: compare the code with the sender, then "
                                               f"`full share status {share_id[:8]}`")
            if not self._lease_live(record, now):
                raise ShareError('no-lease', "the share's lease ended and its mailbox did not renew it; nothing is opened")
            path = folder.path / 'envelopes' / f'{slot}.json'
            if not path.is_file():
                raise ShareError('no-slot', f"this share carries no {slot}: {', '.join(s['slot'] for s in record['slots'])}")
            env = json.loads(_read(path))
            ctx = {'at': _iso(now), 'shareId': share_id, 'ownKem': record['keys']['kem'],
                   'senderSig': record['senderKeys']['sig'], 'marks': record['marks'], 'revoked': False}
            opened = PS.open_peer_share(env, ctx, folder.key('kem'))
            if not opened['ok']:
                raise ShareError(opened['refusal'], f'the envelope for {slot} was refused: {opened["refusal"]}')
            with private_io.lock(folder.path / 'uses.lock'):
                _append(folder.path / 'uses.jsonl', json.dumps({'at': _iso(now), 'slot': slot, 'agent': agent},
                                                               sort_keys=True) + '\n')
            return opened['secret'].decode('utf-8')

    # ================================================================================================================
    # Both
    # ================================================================================================================

    def sync(self, ref: Optional[str] = None, *, due_only: bool = False) -> list:
        """One pass over one share or every share this node holds. A share whose pass fails in a way its own record
        cannot hold is answered as a failure and the others still run."""
        targets = [self.find(ref)] if ref else [('sent', i) for i in self._ids('sent')] + \
            [('received', i) for i in self._ids('received')]
        out = []
        for side, share_id in targets:
            try:
                out.append((side, self.sync_sent(share_id) if side == 'sent'
                            else self.sync_received(share_id, due_only=due_only)))
            except (ShareError, OSError, ValueError, KeyError, RuntimeError) as error:
                out.append((side, {'shareId': share_id, 'failed': f'{type(error).__name__}: {getattr(error, "code", "")}'}))
        return out

    def records(self) -> list:
        return [('sent', self._sent(i).record()) for i in self._ids('sent')] + \
            [('received', self._received(i).record()) for i in self._ids('received')]


# ---- What a person reads --------------------------------------------------------------------------------------------

def summary(side: str, record: dict) -> dict:
    """A share as the CLI and the dashboard show it: names, states, counts and who measured each; never a value, a
    claim code, a private key or the comparison code (`codeReady` says one waits; the CLI shows the digits only on a
    terminal, so a JSON reader or a piped output never carries them)."""
    common = {'side': side, 'shareId': record['shareId'], 'label': record['label'], 'state': record['state'],
              'mailbox': record['origin'], 'burnt': record.get('burnt'), 'mailboxError': record.get('mailbox'),
              'warning': record.get('warning'), 'problem': record.get('problem')}
    if side == 'sent':
        usage = record['usage']
        uses7 = sum(v['uses'] for d, slots in usage['days'].items() for v in slots.values()
                    if d >= _day(_now_ms() - 7 * PS.DAY_MS))
        return {**common, 'project': record['project'], 'env': record['env'],
                'slots': [{'slot': s['slot'], 'provider': s['provider'], 'sequence': s['sequence'],
                           'sealedAt': s['sealedAt'], 'exp': s['exp']} for s in record['slots']],
                'claimDeadline': record['claimDeadline'], 'endsAt': record['endsAt'],
                'codeReady': record['state'] == 'comparing' and record['code'] is not None,
                'confirmed': record['confirmed'],
                'usage': {'uses7d': uses7, 'reportedBy': 'their machine', 'consumers': usage['consumers'],
                          'exportsWithTheButton': usage['exports'], 'lastReportAt': usage.get('lastReportAt')},
                'deliveries': {'count': record['deliveries'], 'lastRenewal': record['lastRenewal'],
                               'seenBy': 'your mailbox'},
                'takingBack': None if record['revocation'] is None else {
                    'mailboxRefuses': record['revocationObserved'], 'recipientDeleted': record['deletion'] is not None,
                    'providerEnded': record['providerEnded'],
                    'takenBack': bool(record['revocationObserved'] and record['providerEnded'])}}
    return {**common, 'slots': record['slots'], 'codeReady': record['state'] == 'comparing' and record['code'] is not None,
            'pinned': record['pinned'], 'lease': record['lease'],
            'revocation': record['revocation'] is not None, 'deletionReceiptSent': record['deletion'] is not None}
