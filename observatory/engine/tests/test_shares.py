#!/usr/bin/env python3
"""Sharing a credential with another person's estate, end to end on one machine (OBS-264, OBS-276, contract DEC-0142):
two nodes' share stores and an in-process `observatory-cloud/1` mailbox. The sender makes a share, the recipient
accepts its link, both commit and reveal, both see one six-digit code, the sender confirms, the recipient's agent gets
the value at use time through the use path, the sender reads the usage report, takes the share back and sees the
recipient's deletion receipt.

The planted defects each must refuse: a value anywhere but the recipient's use (the mailbox's bodies, the records, the
use journal, the link), a second attempt on one link, a claim whose MAC fails, a claimant who holds the code but not the
recipient's keys, an expired link, a confirmation that was never compared, a renewal after the share was taken back,
"taken back" shown before the provider confirmed, and a delegated (metered) agent using a share.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import cryptography                                 # noqa: F401  the engine's `full` extra
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

from node import mailbox as MB, peer_share as PS, relay, shares as SH  # noqa: E402

VALUE = 'planted-share-value-6f1d2c'
ROTATED = 'planted-rotated-value-91aa'
ORIGIN = 'https://mailbox.example'
T0 = relay.instant('2026-10-27T10:00:00Z')


class Clock:
    def __init__(self):
        self.ms = T0

    def __call__(self) -> int:
        return self.ms

    def advance(self, seconds: float) -> None:
        self.ms += int(seconds * 1000)


@unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra (cryptography)')
class ShareFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.clock = Clock()
        self.vault = {('alpha-web', 'prod', 'OPENROUTER_API_KEY'): VALUE}
        self.sender = SH.Shares(base / 'sender' / 'shares', clock=self.clock, read_value=self._read)
        owner_jwk, _ = self.sender.owner()
        self.box = MB.FakeMailbox(ORIGIN, owner=owner_jwk, clock=self.clock)
        transport = MB.fake_transport(self.box)
        self.sender.transport = transport
        self.recipient = SH.Shares(base / 'recipient' / 'shares', transport=transport, clock=self.clock)
        self.other = SH.Shares(base / 'other' / 'shares', transport=transport, clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def _read(self, project, env, slot):
        try:
            return self.vault[(project, env, slot)]
        except KeyError:
            raise LookupError(f'{slot} is not in the vault under projects/{project}/{env}') from None

    def _create(self, **kw):
        record, link = self.sender.create(project='alpha-web', env='prod', slots=['OPENROUTER_API_KEY'],
                                          label='Alex laptop', origin=ORIGIN, **kw)
        return record['shareId'], link

    def _exchange(self):
        share_id, link = self._create()
        received = self.recipient.accept(link, label='from Sam')
        self.assertEqual(received['state'], 'exchanging')
        sent = self.sender.sync_sent(share_id)                 # reads the claim, commits, reveals
        received = self.recipient.sync_received(share_id)     # reveals once, makes the code
        sent = self.sender.sync_sent(share_id)
        self.assertEqual(sent['state'], 'comparing')
        self.assertEqual(received['state'], 'comparing')
        return share_id, link, sent, received

    def _everything_written(self) -> str:
        texts = [b.decode('utf-8', 'replace') for b in self.box.seen_bodies]
        for path in Path(self.tmp.name).rglob('*'):
            if path.is_file() and not path.name.endswith('.pem'):
                texts.append(path.read_text(encoding='utf-8', errors='replace'))
        return '\n'.join(texts)

    # ---- the whole path ----------------------------------------------------------------------------------------

    def test_share_use_report_take_back(self):
        share_id, link, sent, received = self._exchange()
        self.assertEqual(sent['code'], received['code'])
        self.assertRegex(sent['code'], r'^[0-9]{6}$')
        # Before the sender confirms, nothing is sealed and nothing opens.
        with self.assertRaises(SH.ShareError) as refused:
            self.recipient.open_value(share_id, 'OPENROUTER_API_KEY', agent='claude-code')
        self.assertEqual(refused.exception.code, 'not-active')
        sent = self.sender.confirm(share_id, code=received['code'])
        self.assertEqual((sent['state'], sent['confirmed']), ('active', True))
        received = self.recipient.sync_received(share_id)
        self.assertEqual(received['state'], 'active')
        # The value reaches the agent only at use time, through the use path.
        self.assertEqual(self.recipient.open_value(share_id, 'OPENROUTER_API_KEY', agent='claude-code'), VALUE)
        self.assertEqual(self.recipient.open_value('from Sam', 'OPENROUTER_API_KEY', agent='copywriter'), VALUE)
        # The value is nowhere else: not in what the mailbox received, not in a record, not in the use journal.
        self.assertNotIn(VALUE, self._everything_written())
        self.assertNotIn(VALUE, link)
        # The report reaches the sender at the next renewal, sealed; the mailbox counted the deliveries.
        self.clock.advance(3700)
        self.recipient.sync_received(share_id)
        sent = self.sender.sync_sent(share_id)
        today = SH._day(self.clock())
        self.assertEqual(sent['usage']['days'][today]['OPENROUTER_API_KEY']['uses'], 2)
        self.assertEqual({c['agent'] for c in sent['usage']['consumers']}, {'claude-code', 'copywriter'})
        self.assertGreaterEqual(sent['deliveries'], 2)
        view = SH.summary('sent', sent)
        self.assertEqual(view['usage']['reportedBy'], 'their machine')
        self.assertEqual(view['deliveries']['seenBy'], 'your mailbox')
        # Taking it back: the mailbox refuses at once, the recipient deletes and returns a receipt.
        sent = self.sender.revoke(share_id)
        self.assertTrue(sent['revocationObserved'])
        self.assertFalse(SH.summary('sent', sent)['takingBack']['takenBack'])   # the provider has not confirmed
        self.clock.advance(3700)                                                 # past the lease
        with self.assertRaises(SH.ShareError) as refused:
            self.recipient.open_value(share_id, 'OPENROUTER_API_KEY', agent='claude-code')
        self.assertEqual(refused.exception.code, 'revoked')
        received = self.recipient._received(share_id).record()
        self.assertEqual(received['state'], 'removed')
        self.assertFalse((self.recipient._received(share_id).path / 'envelopes').exists())
        sent = self.sender.sync_sent(share_id)
        self.assertIsNotNone(sent['deletion'])
        self.assertTrue(SH.summary('sent', sent)['takingBack']['recipientDeleted'])
        sent = self.sender.provider_ended(share_id)
        self.assertEqual(sent['state'], 'ended')
        self.assertTrue(SH.summary('sent', sent)['takingBack']['takenBack'])
        self.assertNotIn(VALUE, self._everything_written())

    def test_rotation_reseals(self):
        share_id, _, _, received = self._exchange()
        self.sender.confirm(share_id, code=received['code'])
        self.recipient.sync_received(share_id)
        self.vault[('alpha-web', 'prod', 'OPENROUTER_API_KEY')] = ROTATED
        sent = self.sender.sync_sent(share_id)
        self.assertEqual(sent['slots'][0]['sequence'], 2)
        self.clock.advance(3700)
        self.assertEqual(self.recipient.open_value(share_id, 'OPENROUTER_API_KEY', agent='claude-code'), ROTATED)

    def test_a_slot_gone_from_the_vault_waits_and_says_why(self):
        share_id, _, _, received = self._exchange()
        del self.vault[('alpha-web', 'prod', 'OPENROUTER_API_KEY')]
        sent = self.sender.confirm(share_id, code=received['code'])
        self.assertEqual(sent['problem']['code'], 'no-slot')
        self.assertEqual(self.box.shares[share_id]['envelopes'], {})
        self.vault[('alpha-web', 'prod', 'OPENROUTER_API_KEY')] = VALUE
        self.assertEqual(self.sender.sync_sent(share_id)['state'], 'active')
        self.assertIsNone(self.sender._sent(share_id).record().get('problem'))

    def test_the_use_journal_keeps_only_what_the_next_report_needs(self):
        share_id, _, _, received = self._exchange()
        self.sender.confirm(share_id, code=received['code'])
        self.recipient.sync_received(share_id)
        self.recipient.open_value(share_id, 'OPENROUTER_API_KEY', agent='claude-code')
        self.clock.advance(2 * 86400)
        self.recipient.open_value(share_id, 'OPENROUTER_API_KEY', agent='claude-code')   # renews and reports
        uses = self.recipient._uses(self.recipient._received(share_id))
        self.assertTrue(all(u['at'][:10] >= SH._day(self.clock()) for u in uses))

    def test_share_without_comparing_says_so(self):
        share_id, _, _, _ = self._exchange()
        sent = self.sender.confirm(share_id, without_comparing=True)
        self.assertEqual((sent['state'], sent['confirmed']), ('active', False))
        self.assertFalse(SH.summary('sent', sent)['confirmed'])

    # ---- one attempt per link, and the planted defects --------------------------------------------------------

    def test_codes_that_differ_seal_nothing(self):
        share_id, _, sent, _ = self._exchange()
        wrong = str((int(sent['code']) + 1) % 10 ** 6).zfill(6)
        sent = self.sender.confirm(share_id, code=wrong)
        self.assertEqual(sent['state'], 'burnt')
        self.assertEqual(self.box.shares[share_id]['envelopes'], {})
        received = self.recipient.sync_received(share_id)
        with self.assertRaises(SH.ShareError):
            self.recipient.open_value(share_id, 'OPENROUTER_API_KEY', agent='claude-code')
        self.assertNotEqual(received['state'], 'active')

    def test_confirm_needs_a_code_to_compare(self):
        share_id, link = self._create()
        with self.assertRaises(SH.ShareError) as refused:
            self.sender.confirm(share_id, code='123456')
        self.assertEqual(refused.exception.code, 'not-comparing')

    def test_a_link_opens_once(self):
        share_id, link = self._create()
        self.recipient.accept(link)
        with self.assertRaises(SH.ShareError) as refused:
            self.recipient.accept(link)
        self.assertEqual(refused.exception.code, 'already-opened')
        # Another machine with the same link: a second claim burns the exchange, and neither gets a value.
        late = self.other.accept(link)
        self.assertEqual(late['state'], 'burnt')
        sent = self.sender.sync_sent(share_id)
        self.assertEqual(sent['state'], 'burnt')
        self.assertEqual(self.box.shares[share_id]['verdict']['reason'], 'repeated')

    def test_a_claim_whose_mac_fails_spends_the_link(self):
        share_id, link = self._create()
        parsed = PS.parse_share_link(link)['link']
        forged = PS.share_link({**parsed, 'code': PS.new_link_code()})
        self.recipient.accept(forged)
        sent = self.sender.sync_sent(share_id)
        self.assertEqual((sent['state'], sent['burnt']['reason']), ('burnt', 'claim-mac'))
        self.assertFalse((self.sender._sent(share_id).path / 'claim-code').exists())
        self.assertEqual(self.box.shares[share_id]['envelopes'], {})

    def test_a_holder_of_the_code_gets_another_code(self):
        """A mailbox, or anyone who read the link first, claims with keys of its own: the MAC holds, the code it and
        the sender see is not the one the real recipient would read (PS-5, PS-6)."""
        share_id, link = self._create()
        thief = self.other.accept(link, label='thief')
        self.sender.sync_sent(share_id)
        thief = self.other.sync_received(share_id)
        sent = self.sender.sync_sent(share_id)
        self.assertEqual(sent['code'], thief['code'])       # the thief and the sender agree with each other
        # The real recipient's machine cannot claim any more: the link's one claim is spent.
        honest = self.recipient.accept(link)
        self.assertEqual(honest['state'], 'burnt')
        # The person compares with the real recipient, who has no code: "They differ", and nothing is sealed.
        sent = self.sender.differ(share_id)
        self.assertEqual(sent['state'], 'burnt')
        self.assertEqual(self.box.shares[share_id]['envelopes'], {})

    def test_an_expired_link(self):
        share_id, link = self._create(claim_days=1)
        self.clock.advance(2 * 86400)
        received = self.recipient.accept(link)
        self.assertEqual(received['state'], 'burnt')
        sent = self.sender.sync_sent(share_id)
        self.assertEqual((sent['state'], sent['burnt']['reason']), ('burnt', 'expired'))

    def test_a_pin_that_does_not_hold_aborts(self):
        """A link pasted as the sender sent it pins the sender's share #sig; a commit naming another key aborts."""
        share_id, link = self._create()
        self.recipient.accept(link)
        commit = self.box.shares[share_id]
        # Plant a sender commit under other keys, as a mailbox in the middle would.
        other = {'sig': PS.SharePrivateKey.generate().jwk(), 'kem': PS.SharePrivateKey.generate().jwk()}
        side = {'role': 'sender', 'shareId': share_id, 'keys': other, 'nonce': PS.new_nonce()}
        commit['frames'].append({'schema': PS.PEER_FRAME_SCHEMA, 'step': 'commit', 'shareId': share_id, 'keys': other,
                                 'commit': PS.peer_commit(side)['commit']})
        received = self.recipient.sync_received(share_id)
        self.assertEqual((received['state'], received['burnt']['reason']), ('burnt', 'pin-mismatch'))
        self.assertFalse(received['revealed'] and any(f.get('step') == 'reveal' and f.get('role') == 'recipient'
                                                      for f in commit['frames']))

    def test_the_mailbox_refuses_a_stranger(self):
        share_id, link = self._create()
        stranger = PS.SharePrivateKey.generate()
        client = MB.MailboxClient(ORIGIN, stranger.jwk(), stranger, MB.fake_transport(self.box), clock=self.clock)
        with self.assertRaises(MB.MailboxError) as refused:
            client.lease(share_id)
        self.assertEqual(refused.exception.code, 'signature-invalid')
        # A share filed by anyone but the owner is refused.
        with self.assertRaises(MB.MailboxError) as refused:
            client.create(PS.new_share_id(), relay.rfc3339(T0 + 3600_000), 3600)
        self.assertEqual(refused.exception.code, 'not-owner')

    def test_a_replayed_request_is_refused(self):
        share_id, link = self._create()
        record = self.sender._sent(share_id).record()
        key = self.sender._sent(share_id).key('sig')
        body = b''
        headers = MB.sign_request([(MB.LABEL, record['keys']['sig'], key)], method='GET', origin=ORIGIN,
                                  target=f'/v1/shares/{share_id}', body=body, created=self.clock() // 1000)
        self.assertEqual(self.box.handle('GET', f'/v1/shares/{share_id}', headers, body)[0], 200)
        status, _, raw = self.box.handle('GET', f'/v1/shares/{share_id}', headers, body)
        self.assertEqual((status, json.loads(raw)['error']['code']), (401, 'signature-replayed'))

    def test_what_the_mailbox_sees(self):
        share_id, link, sent, received = self._exchange()
        self.sender.confirm(share_id, code=received['code'])
        self.recipient.sync_received(share_id)
        self.recipient.open_value(share_id, 'OPENROUTER_API_KEY', agent='claude-code')
        self.recipient.sync_received(share_id)
        code = PS.parse_share_link(link)['link']['code']
        seen = '\n'.join(b.decode() for b in self.box.seen_bodies)
        self.assertNotIn(VALUE, seen)
        self.assertNotIn(code, seen)                          # the claim code never travels in a request
        self.assertNotIn('claude-code', seen)                 # the report is sealed: agent names travel inside it

    def test_a_metered_agent_cannot_use_a_share(self):
        """CT-187: until who pays for a share's use is decided, a delegated agent's use of a share is refused."""
        sys.path.insert(0, str(ROOT / 'tools'))
        import use_secret
        use_secret._DELEGATION_TOKEN = 'a-token'
        try:
            with self.assertRaises(use_secret.SlotRefused) as refused:
                use_secret.check_share_use()
            self.assertEqual(refused.exception.code, 'no-grant')
        finally:
            use_secret._DELEGATION_TOKEN = None


if __name__ == '__main__':
    unittest.main()
