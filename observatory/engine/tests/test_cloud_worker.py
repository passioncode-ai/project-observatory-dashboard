#!/usr/bin/env python3
"""The deployed mailbox, `observatory-cloud/1` as a Cloudflare Worker (OBS-263, contract DEC-0142 PS-10), held to the
engine's in-process mailbox (`node/mailbox.py`) as its specification.

Two halves. `cloud/worker.test.mjs` runs the Worker's ports over the contract's vectors and its handler's own answers
under Node. Then the Worker is served on loopback (`cloud/dev_server.mjs`, an in-memory Durable Object namespace) and
the engine's two share stores (`node/shares.py`) run a whole share through it over HTTP, signed requests and all: the
sender's creation needs the owner key, the claim, both reveals and one code, the seal, the recipient's use, the sealed
report, the revocation and the deletion receipt; and the refusals the in-process mailbox makes, made by the Worker
too: a second claim burns the link, a failing MAC spends it, codes that differ seal nothing, a stranger cannot file a
share, a replayed request is refused. Skipped without Node 20 or later.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import cryptography                                 # noqa: F401
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

NODE = shutil.which('node')


def node_major() -> int:
    if not NODE:
        return 0
    out = subprocess.run([NODE, '--version'], capture_output=True, text=True, timeout=30).stdout.strip()
    m = re.match(r'v(\d+)', out)
    return int(m.group(1)) if m else 0


HAVE_NODE = node_major() >= 20
ORIGIN = 'https://mailbox.example'
VALUE = 'planted-worker-share-value-77c1'


@unittest.skipUnless(HAVE_NODE, 'needs Node 20 or later')
class WorkerUnit(unittest.TestCase):
    def test_worker_ports_and_handler(self):
        run = subprocess.run([NODE, '--test', '--test-reporter=tap', 'cloud/worker.test.mjs'], cwd=ROOT, capture_output=True, text=True,
                             timeout=300)
        self.assertEqual(run.returncode, 0, run.stdout[-4000:] + run.stderr[-2000:])
        self.assertRegex(run.stdout, r'(?m)^# pass [1-9]')
        self.assertRegex(run.stdout, r'(?m)^# fail 0$')


@unittest.skipUnless(HAVE_NODE and HAVE_CRYPTO, 'needs Node 20 or later and cryptography')
class WorkerAgainstTheStores(unittest.TestCase):
    def setUp(self):
        from node import mailbox as MB, shares as SH
        self.MB, self.SH = MB, SH
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.vault = {('alpha-web', 'prod', 'OPENROUTER_API_KEY'): VALUE}
        self.sender = SH.Shares(base / 'sender', transport=MB.http_transport(), read_value=self._read)
        owner_jwk, _ = self.sender.owner()
        self.server = subprocess.Popen([NODE, str(ROOT / 'cloud' / 'dev_server.mjs'), ORIGIN, json.dumps(owner_jwk)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        port = json.loads(self.server.stdout.readline())['port']
        self.old = os.environ.get(MB.RESOLVE_ENV)
        os.environ[MB.RESOLVE_ENV] = f'mailbox.example=127.0.0.1:{port}'
        self.recipient = SH.Shares(base / 'recipient', transport=MB.http_transport())
        self.other = SH.Shares(base / 'other', transport=MB.http_transport())

    def tearDown(self):
        self.server.terminate()
        self.server.wait(timeout=30)
        self.server.stdout.close()
        self.server.stderr.close()
        if self.old is None:
            os.environ.pop(self.MB.RESOLVE_ENV, None)
        else:
            os.environ[self.MB.RESOLVE_ENV] = self.old
        self.tmp.cleanup()

    def _read(self, project, env, slot):
        return self.vault[(project, env, slot)]

    def _create(self):
        record, link = self.sender.create(project='alpha-web', env='prod', slots=['OPENROUTER_API_KEY'],
                                          label='Alex', origin=ORIGIN)
        return record['shareId'], link

    def _exchange(self):
        share_id, link = self._create()
        self.assertEqual(self.recipient.accept(link)['state'], 'exchanging')
        self.sender.sync_sent(share_id)
        received = self.recipient.sync_received(share_id)
        sent = self.sender.sync_sent(share_id)
        self.assertEqual((sent['state'], received['state']), ('comparing', 'comparing'))
        return share_id, sent, received

    def test_a_whole_share(self):
        share_id, sent, received = self._exchange()
        self.assertEqual(sent['code'], received['code'])
        sent = self.sender.confirm(share_id, code=received['code'])
        self.assertEqual(sent['state'], 'active', sent.get('mailbox'))
        self.recipient.sync_received(share_id)
        self.assertEqual(self.recipient.open_value(share_id, 'OPENROUTER_API_KEY', agent='claude-code'), VALUE)
        self.recipient.sync_received(share_id)                     # renews, and reports the use
        sent = self.sender.sync_sent(share_id)
        self.assertIn('claude-code', {c['agent'] for c in sent['usage']['consumers']})
        self.assertGreaterEqual(sent['deliveries'], 1)
        sent = self.sender.revoke(share_id)
        self.assertTrue(sent['revocationObserved'])
        received = self.recipient.sync_received(share_id)
        self.assertEqual(received['state'], 'removed')
        sent = self.sender.sync_sent(share_id)
        self.assertIsNotNone(sent['deletion'])
        # What the stores keep holds no value: only the sealed copy, and it is gone on the recipient.
        for path in Path(self.tmp.name).rglob('*'):
            if path.is_file() and not path.name.endswith('.pem'):
                self.assertNotIn(VALUE, path.read_text(errors='replace'), path.name)

    def test_a_second_machine_burns_the_link(self):
        share_id, link = self._create()
        self.recipient.accept(link)
        self.assertEqual(self.other.accept(link)['state'], 'burnt')
        sent = self.sender.sync_sent(share_id)
        self.assertEqual((sent['state'], sent['burnt']['reason']), ('burnt', 'repeated'))

    def test_a_claim_whose_mac_fails_spends_the_link(self):
        from node import peer_share as PS
        share_id, link = self._create()
        parsed = PS.parse_share_link(link)['link']
        self.recipient.accept(PS.share_link({**parsed, 'code': PS.new_link_code()}))
        sent = self.sender.sync_sent(share_id)
        self.assertEqual((sent['state'], sent['burnt']['reason']), ('burnt', 'claim-mac'))

    def test_codes_that_differ_seal_nothing(self):
        share_id, sent, _ = self._exchange()
        sent = self.sender.confirm(share_id, code=str((int(sent['code']) + 1) % 10 ** 6).zfill(6))
        self.assertEqual(sent['state'], 'burnt')
        received = self.recipient.sync_received(share_id)
        self.assertNotEqual(received['state'], 'active')
        with self.assertRaises(self.SH.ShareError):
            self.recipient.open_value(share_id, 'OPENROUTER_API_KEY', agent='claude-code')

    def test_a_stranger_files_no_share_and_a_request_is_not_replayed(self):
        from node import peer_share as PS, relay
        import time
        stranger = PS.SharePrivateKey.generate()
        client = self.MB.MailboxClient(ORIGIN, stranger.jwk(), stranger, self.MB.http_transport())
        with self.assertRaises(self.MB.MailboxError) as refused:
            client.create(PS.new_share_id(), relay.rfc3339(int(time.time() * 1000) + 3600_000), 3600)
        self.assertEqual(refused.exception.code, 'not-owner')
        share_id, _ = self._create()
        folder = self.sender._sent(share_id)
        record, key = folder.record(), folder.key('sig')
        headers = self.MB.sign_request([(self.MB.LABEL, record['keys']['sig'], key)], method='GET', origin=ORIGIN,
                                       target=f'/v1/shares/{share_id}', body=b'')
        send = self.MB.http_transport()
        self.assertEqual(send('GET', f'{ORIGIN}/v1/shares/{share_id}', headers, b'')[0], 200)
        status, _, raw = send('GET', f'{ORIGIN}/v1/shares/{share_id}', headers, b'')
        self.assertEqual((status, json.loads(raw)['error']['code']), (401, 'signature-replayed'))


if __name__ == '__main__':
    unittest.main()
