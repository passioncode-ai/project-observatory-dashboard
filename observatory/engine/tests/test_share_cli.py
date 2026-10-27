#!/usr/bin/env python3
"""`full share` between two workspaces, as two people run it (OBS-264): separate processes, separate workspaces, a
trial mailbox over loopback HTTP (`mailbox.serve_trial`, reached through `OBSERVATORY_CLOUD_RESOLVE`), and an agent's
command run with `use_secret.py run --share`. The person's acts (accept, confirm) run on a pseudo-terminal; without a
terminal they are refused. The value is checked absent from every argv, environment, file and log the run leaves, and
from everything the mailbox received; the agent's command sees it and its printed output is scrubbed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import cryptography                                 # noqa: F401
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

HAVE_PTY = os.name == 'posix'
VALUE = 'planted-cli-share-value-3be9'
ORIGIN = 'https://mailbox.localhost'


@unittest.skipUnless(HAVE_CRYPTO and HAVE_PTY, 'needs cryptography and a POSIX pseudo-terminal')
class ShareBetweenTwoWorkspaces(unittest.TestCase):
    def setUp(self):
        from node import mailbox as MB
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.box = MB.FakeMailbox(ORIGIN)
        self.server = MB.serve_trial(self.box, 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.homes = {}
        for who in ('sam', 'alex'):
            home = base / who
            (home / 'config').mkdir(parents=True)
            self.homes[who] = home
        slot = self.homes['sam'] / 'secrets' / 'projects' / 'alpha-web' / 'prod' / 'OPENROUTER_API_KEY'
        slot.parent.mkdir(parents=True)
        for folder in (slot.parent, slot.parent.parent, slot.parent.parent.parent, slot.parent.parent.parent.parent):
            folder.chmod(0o700)
        fd = os.open(slot, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, VALUE.encode())
        os.close(fd)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def env(self, who: str) -> dict:
        home = self.homes[who]
        keep = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'LC_ALL', 'TMPDIR', 'SYSTEMROOT')}
        return {**keep, 'HOME': str(home), 'OBSERVATORY_HOME': str(home),
                'OBSERVATORY_VAULT_DIR': str(home / 'secrets' / 'projects'),
                'OBSERVATORY_SECRETS': str(home / 'secrets'),
                'OBSERVATORY_CLOUD_RESOLVE': f'mailbox.localhost=127.0.0.1:{self.server.server_port}'}

    def share(self, who: str, *args: str, terminal: bool = False, stdin_text: str = '') -> subprocess.CompletedProcess:
        command = [sys.executable, str(ROOT / 'tools' / 'share_cli.py'), *args]
        if not terminal:
            return subprocess.run(command, env=self.env(who), capture_output=True, text=True, timeout=60,
                                  stdin=subprocess.DEVNULL)
        import pty
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(command, env=self.env(who), stdin=slave, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            os.write(master, stdin_text.encode())
            out, err = proc.communicate(timeout=60)
        finally:
            os.close(master)
            os.close(slave)
        return subprocess.CompletedProcess(command, proc.returncode, out, err)

    def test_two_people(self):
        # Sam shares a key with Alex: the link once, never in a log.
        created = self.share('sam', 'create', 'alpha-web', 'prod', 'OPENROUTER_API_KEY', '--to', 'Alex laptop',
                             '--mailbox', ORIGIN, '--show-link')
        self.assertEqual(created.returncode, 0, created.stderr)
        link = next(line for line in created.stdout.splitlines() if line.startswith(ORIGIN + '/s/'))
        share_id = re.search(r'/s/([0-9a-f]{32})#', link).group(1)
        # Alex's agent cannot accept for Alex: a person's act needs a terminal.
        refused = self.share('alex', 'accept', '-', stdin_text=link + '\n')
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('terminal', refused.stderr)
        accepted = self.share('alex', 'accept', '-', '--label', 'from Sam', terminal=True,
                              stdin_text=link + '\nyes\n')
        self.assertEqual(accepted.returncode, 0, accepted.stderr + accepted.stdout)
        self.assertIn('The sender will see', accepted.stdout)
        # Each side's next pass: Sam's node commits and reveals, Alex's reveals and shows the code.
        self.share('sam', 'status', share_id[:8])
        alex = json.loads(self.share('alex', 'status', share_id[:8], '--json').stdout)
        sam = json.loads(self.share('sam', 'status', share_id[:8], '--json').stdout)
        self.assertEqual(alex['code'], sam['code'])
        self.assertRegex(sam['code'], r'^[0-9]{6}$')
        # The comparison is Sam's act.
        self.assertNotEqual(self.share('sam', 'confirm', share_id[:8], sam['code']).returncode, 0)
        confirmed = self.share('sam', 'confirm', share_id[:8], sam['code'], terminal=True)
        self.assertEqual(confirmed.returncode, 0, confirmed.stderr + confirmed.stdout)
        # Alex's agent uses the key at use time; its output is scrubbed.
        self.share('alex', 'status', share_id[:8])
        run = subprocess.run([sys.executable, str(ROOT / 'tools' / 'use_secret.py'), 'run', '--share', '--consumer',
                              'claude-code', share_id[:8], 'OPENROUTER_API_KEY', '--', sys.executable, '-c',
                              'import os; v = os.environ["OPENROUTER_API_KEY"]; print(len(v)); print(v)'],
                             env=self.env('alex'), capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.splitlines()[0], str(len(VALUE)))
        self.assertIn('«OPENROUTER_API_KEY»', run.stdout)
        self.assertNotIn(VALUE, run.stdout + run.stderr)
        # A metered agent (a delegation token in its environment) does not use a share (CT-187).
        metered = subprocess.run([sys.executable, str(ROOT / 'tools' / 'use_secret.py'), 'run', '--share',
                                  share_id[:8], 'OPENROUTER_API_KEY', '--', sys.executable, '-c', 'print(1)'],
                                 env={**self.env('alex'), 'FABRIC_DELEGATION_TOKEN': 'x.y.z'}, capture_output=True,
                                 text=True, timeout=60)
        self.assertNotEqual(metered.returncode, 0)
        self.assertIn('no-grant', metered.stderr)
        # Sam reads the usage at the next renewal, then takes the share back.
        self.share('alex', 'sync')
        sam = json.loads(self.share('sam', 'status', share_id[:8], '--json').stdout)
        self.assertIn('claude-code', {c['agent'] for c in sam['usage']['consumers']})
        revoked = self.share('sam', 'revoke', share_id[:8])
        self.assertIn('mailbox refuses yes', revoked.stdout)
        self.assertIn('not taken back yet', revoked.stdout)
        # Alex's machine learns it at its next contact and deletes; the lease no longer opens anything.
        self.share('alex', 'sync')
        after = subprocess.run([sys.executable, str(ROOT / 'tools' / 'use_secret.py'), 'run', '--share',
                                share_id[:8], 'OPENROUTER_API_KEY', '--', sys.executable, '-c', 'print(1)'],
                               env=self.env('alex'), capture_output=True, text=True, timeout=60)
        self.assertNotEqual(after.returncode, 0)
        sam = json.loads(self.share('sam', 'status', share_id[:8], '--json').stdout)
        self.assertTrue(sam['takingBack']['recipientDeleted'])
        # Nothing the run left holds the value: no file in either workspace, nothing the mailbox received.
        for who, home in self.homes.items():
            for path in home.rglob('*'):
                if path.is_file() and path.parts[-5:-1] != ('secrets', 'projects', 'alpha-web', 'prod'):
                    self.assertNotIn(VALUE, path.read_text(errors='replace'), f'{who}: {path.name}')
        self.assertNotIn(VALUE.encode(), b''.join(self.box.seen_bodies))


if __name__ == '__main__':
    unittest.main()
