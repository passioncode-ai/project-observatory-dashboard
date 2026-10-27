#!/usr/bin/env python3
"""`full cloud deploy|status|url` (OBS-263): the mailbox deployed into the person's Cloudflare account through the
Cloudflare door, against a fake Cloudflare API and the real Worker served on loopback as the deployed address.

What is held: the door's narrow token is minted when the slot is empty and read from the vault in memory, never
printed; the upload is one module with the Durable Object binding, the owner's public key and the code's digest, and
the SQLite migration only on the first deploy; the deployed address answers with this code and this owner before the
deployment is recorded; `full share create` then uses it; with no admin token stashed the person's step is named with
its address and nothing is called; an address that answers with other code is refused, not recorded.
"""
from __future__ import annotations

import contextlib
import io
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
_HOME = tempfile.TemporaryDirectory(prefix='obs-cloud-')          # removed when the module ends
os.environ['OBSERVATORY_HOME'] = str(Path(_HOME.name).resolve())
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))

try:
    import cryptography                                 # noqa: F401
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

NODE = shutil.which('node')
PLANTED = ''.join(['planted', '-deploy-', 'q' * 19])
ACCOUNT = {'id': '0123456789abcdef0123456789abcdef', 'name': 'Example Account', 'slug': 'example-account'}
ORIGIN = 'https://observatory-cloud.example-sub.workers.dev'


def node_ok() -> bool:
    if not NODE:
        return False
    out = subprocess.run([NODE, '--version'], capture_output=True, text=True, timeout=30).stdout
    m = re.match(r'v(\d+)', out.strip())
    return bool(m) and int(m.group(1)) >= 20


class FakeCloudflare:
    def __init__(self, scripts=()):
        self.calls, self.uploads, self.scripts, self.subdomain = [], [], list(scripts), 'example-sub'
        self.bindings = [{'type': 'plain_text', 'name': 'GREETING', 'text': 'hello'}]

    def __call__(self, method, path, token, body=None, content_type='application/json'):
        assert token == PLANTED
        self.calls.append((method, path))
        if method == 'GET' and path.endswith('/workers/scripts'):
            return {'success': True, 'result': self.scripts}
        if method == 'PUT' and '/workers/scripts/' in path:
            self.uploads.append((body, content_type))
            self.scripts = [{'id': path.rsplit('/', 1)[1], 'migration_tag': 'v1'}]
            return {'success': True, 'result': {}}
        if method == 'POST' and path.endswith('/subdomain'):
            return {'success': True, 'result': {'enabled': True}}
        if method == 'GET' and path.endswith('/settings'):
            return {'success': True, 'result': {'bindings': self.bindings}}
        if method == 'GET' and path.endswith('/workers/subdomain'):
            return {'success': True, 'result': {'subdomain': self.subdomain}}
        raise AssertionError((method, path))


def metadata_of(body: bytes) -> dict:
    part = body.split(b'name="metadata"', 1)[1].split(b'\r\n\r\n', 1)[1].split(b'\r\n--', 1)[0]
    return json.loads(part)


@unittest.skipUnless(HAVE_CRYPTO, 'needs cryptography')
class Deploy(unittest.TestCase):
    def setUp(self):
        import cloud_cli
        self.C = cloud_cli
        self.cf = FakeCloudflare()
        self.issued = []
        self.slot = {'present': False}
        self.patches = {'cf': self.cf, 'accounts': lambda: [ACCOUNT],
                        'token_present': lambda: self.slot['present'],
                        'issue_token': lambda account: (self.issued.append(account['slug']),
                                                        self.slot.__setitem__('present', True)),
                        'read_token': lambda: PLANTED}
        self.saved = {k: getattr(cloud_cli, k) for k in self.patches}
        for k, v in self.patches.items():
            setattr(cloud_cli, k, v)
        self.server = None

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(self.C, k, v)
        if self.server:
            self.server.terminate()
            self.server.wait(timeout=30)
            self.server.stdout.close()
            self.server.stderr.close()
        os.environ.pop('OBSERVATORY_CLOUD_RESOLVE', None)
        self.C.CONFIG.unlink(missing_ok=True)

    def serve(self, digest=None):
        owner = self.C.owner_jwk()
        digest = digest or self.C.digest_of(self.C.worker_code())
        self.server = subprocess.Popen([NODE, str(ROOT / 'cloud' / 'dev_server.mjs'), ORIGIN, json.dumps(owner), digest],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        port = json.loads(self.server.stdout.readline())['port']
        os.environ['OBSERVATORY_CLOUD_RESOLVE'] = f'observatory-cloud.example-sub.workers.dev=127.0.0.1:{port}'

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.C.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_calls_nothing(self):
        code, out, _ = self.run_cli('deploy', '--dry-run')
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertEqual((plan['account'], plan['tokenSlot'], plan['tokenPresent']),
                         ('example-account', 'observatory-cloud/prod/CLOUDFLARE_WORKERS_TOKEN', False))
        self.assertEqual(self.cf.calls, [])
        self.assertEqual(self.issued, [])

    def test_no_admin_names_the_persons_step(self):
        self.C.accounts = lambda: []
        code, out, err = self.run_cli('deploy')
        self.assertEqual(code, 1)
        self.assertIn('https://dash.cloudflare.com/profile/api-tokens', err)
        self.assertIn('cloudflare.py" stash < FILE', err)
        self.assertEqual(self.cf.calls, [])

    @unittest.skipUnless(node_ok(), 'needs Node 20 or later')
    def test_deploy_status_url_and_a_share_uses_it(self):
        self.serve()
        code, out, err = self.run_cli('deploy')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.issued, ['example-account'])          # the door minted the token once
        self.assertIn(ORIGIN, out)
        self.assertNotIn(PLANTED, out + err)
        body, content_type = self.cf.uploads[0]
        self.assertNotIn(PLANTED.encode(), body)
        meta = metadata_of(body)
        self.assertEqual(meta['main_module'], 'worker.mjs')
        self.assertEqual(meta['migrations'], {'new_tag': 'v1', 'new_sqlite_classes': ['ShareBox']})
        names = {b['name']: b for b in meta['bindings']}
        self.assertEqual(names['SHARES']['class_name'], 'ShareBox')
        owner = json.loads(names['OWNER_JWK']['text'])
        self.assertEqual(set(owner), {'kty', 'crv', 'x', 'y'})               # the public half only
        self.assertIn(self.C.worker_code(), body)
        record = json.loads(self.C.CONFIG.read_text())
        self.assertEqual(record['origin'], ORIGIN)
        self.assertEqual(self.run_cli('url')[1].strip(), ORIGIN)
        code, out, _ = self.run_cli('status', '--json')
        self.assertEqual((code, json.loads(out)['state']), (0, 'current'))
        # A second deploy keeps the token and sends no migration.
        self.assertEqual(self.run_cli('deploy')[0], 0)
        self.assertEqual(self.issued, ['example-account'])
        self.assertNotIn('migrations', metadata_of(self.cf.uploads[1][0]))
        # `full share create` takes the recorded mailbox, and the owner's signature lets it file the share.
        import share_cli
        self.assertEqual(share_cli.mailbox_origin(None), ORIGIN)
        from node import mailbox as MB, shares as SH
        store = SH.Shares(Path(os.environ['OBSERVATORY_HOME']) / 'node' / 'shares', transport=MB.http_transport(),
                          read_value=lambda *a: 'planted-share-value')
        record, link = store.create(project='alpha-web', env='prod', slots=['OPENROUTER_API_KEY'], label='Alex',
                                    origin=ORIGIN)
        self.assertTrue(link.startswith(ORIGIN + '/s/'))

    @unittest.skipUnless(node_ok(), 'needs Node 20 or later')
    def test_an_address_running_other_code_is_not_recorded(self):
        self.serve(digest='sha256:' + '0' * 64)
        code, _, err = self.run_cli('deploy')
        self.assertEqual(code, 1)
        self.assertIn('another code or owner key', err)
        self.assertFalse(self.C.CONFIG.exists())

    def test_a_dry_run_reads_the_doors_accounts_in_a_process_of_its_own(self):
        """The door's metadata only (no admin value read), and the engine's `node` package found although the door
        puts `tools/` (where `tools/node.py` lives) first on the path."""
        held = tempfile.TemporaryDirectory(prefix='obs-cloud-dry-')
        self.addCleanup(held.cleanup)
        home = Path(held.name).resolve()
        store = home / 'secrets' / 'cloudflare-admin'
        store.mkdir(parents=True, mode=0o700)
        for folder in (home / 'secrets', store):
            folder.chmod(0o700)
        for name, text in (('example-admin', 'not-read-by-a-dry-run'),
                           ('example-admin.meta.json', json.dumps({'accounts': [
                               {'id': ACCOUNT['id'], 'name': ACCOUNT['name'], 'can_issue': True}]}))):
            fd = os.open(store / name, os.O_WRONLY | os.O_CREAT, 0o600)
            os.write(fd, text.encode())
            os.close(fd)
        run = subprocess.run([sys.executable, str(ROOT / 'tools' / 'cloud_cli.py'), 'deploy', '--dry-run'],
                             env={**os.environ, 'OBSERVATORY_HOME': str(home), 'HOME': str(home)},
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stderr)
        plan = json.loads(run.stdout)
        self.assertEqual((plan['account'], plan['dryRun'], plan['tokenPresent']), ('example-account', True, False))

    def test_another_worker_of_that_name_is_never_replaced(self):
        """A Worker of this name that does not answer as an observatory-cloud mailbox is refused before any upload."""
        self.cf.scripts = [{'id': 'observatory-cloud', 'migration_tag': None}]
        os.environ['OBSERVATORY_CLOUD_RESOLVE'] = 'observatory-cloud.example-sub.workers.dev=127.0.0.1:9'
        self.slot['present'] = True
        code, _, err = self.run_cli('deploy')
        self.assertEqual(code, 1)
        self.assertIn('is not an observatory-cloud mailbox', err)
        self.assertEqual(self.cf.uploads, [])

    @unittest.skipUnless(node_ok(), 'needs Node 20 or later')
    def test_a_deploy_of_ours_that_stopped_halfway_is_finished(self):
        """A Worker of this name that does not answer yet, but whose settings are ours, is deployed again."""
        self.serve()
        self.cf.scripts = [{'id': 'observatory-cloud', 'migration_tag': 'v1'}]
        self.cf.bindings = [{'type': 'durable_object_namespace', 'name': 'SHARES', 'class_name': 'ShareBox'},
                            {'type': 'plain_text', 'name': 'WORKER_DIGEST', 'text': 'sha256:' + '1' * 64}]
        self.slot['present'] = True
        real, calls = self.C.well_known, []

        def first_fails(origin, timeout=15.0):
            calls.append(origin)
            if len(calls) == 1:
                raise self.C.CloudError('the mailbox did not answer (planted)')
            return real(origin, timeout)
        self.C.well_known = first_fails
        try:
            code, out, err = self.run_cli('deploy')
        finally:
            self.C.well_known = real
        self.assertEqual(code, 0, err)
        self.assertIn('did not finish deploying', out)
        self.assertEqual(len(self.cf.uploads), 1)

    def test_status_without_a_deployment(self):
        code, out, _ = self.run_cli('status', '--json')
        self.assertEqual((code, json.loads(out)['state']), (1, 'not-deployed'))
        self.assertEqual(self.run_cli('url')[0], 1)


if __name__ == '__main__':
    unittest.main()
