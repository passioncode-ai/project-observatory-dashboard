#!/usr/bin/env python3
"""The machine bundle (OBS-157): what it carries, what it refuses, and a restore onto an empty home.

Every case builds a synthetic HOME (`alpha-web`, `beta-api`, `example.invalid`) in a temporary
directory, points OBSERVATORY_BACKUPS at another, and replaces Homebrew, npm and pipx with
scripts that print a fixed answer. Git is the real one, run against local repositories only:
no network, no live provider, no inherited credential.
"""
from __future__ import annotations
import contextlib
import datetime
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmp  # noqa: F401: the sandbox defaults for a suite run by hand
import configuration as config
import workspace
import backup_vault as vault
import machine_backup as MB

PASS = 'correct horse battery staple'
AT = datetime.datetime(2026, 10, 19, 12, 0, tzinfo=datetime.timezone.utc)
SECRET_MARKER = b'synthetic-private-key-material'
TOKEN = 'fake-fixture-value'
#: A remote URL with user information, built so the text holds no credential-shaped URL.
WITH_USER = 'https://' + 'user:' + TOKEN + '@' + 'example.invalid/alpha-web.git'


def git(*args, cwd=None, env=None):
    return subprocess.run(['git', *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.user = self.base / 'user'
        self.user.mkdir()
        self.ws = self.base / 'workspace'
        self.root = self.base / 'backups'
        self.bin = self.base / 'fake-bin'
        self.env = patch.dict(os.environ, {
            'HOME': str(self.user), 'OBSERVATORY_HOME': str(self.ws), 'OBSERVATORY_BACKUPS': str(self.root),
            'FABRIC_SERVICES_DIR': str(self.user / 'fabric-services'), 'PATH': os.environ.get('PATH', ''),
            'OBSERVATORY_SYSTEM_SETUP': '0', 'TMPDIR': os.environ.get('TMPDIR', tempfile.gettempdir()),
            'GIT_CONFIG_NOSYSTEM': '1'}, clear=True)
        self.env.start()
        workspace.initialize(self.ws)
        import paths
        importlib.reload(paths)
        self.fake_tools()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def fake_tools(self, uv=False):
        self.bin.mkdir(exist_ok=True)
        scripts = {
            'brew': 'printf \'brew "git"\\ncask "example-app"\\n\'',
            'npm': 'printf \'{"dependencies": {"example-cli": {"version": "1.2.3"}, "npm": {"version": "10.0.0"}}}\'',
            'pipx': 'printf \'{"venvs": {"example-tool": {"metadata": {"main_package": {"package": "example-tool"}}}}}\'',
        }
        if uv:
            scripts['uv'] = 'printf \'example-uv v0.1.0\\n- example-uv\\n\''
        for name, body in scripts.items():
            f = self.bin / name
            f.write_text('#!/bin/sh\n' + body + '\n')
            f.chmod(0o755)

    def write(self, rel, data=b'synthetic', mode=0o644):
        p = self.user / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
        p.chmod(mode)
        return p

    def settings(self, doc):
        (self.ws / 'config' / MB.CONFIG_NAME).write_text(json.dumps(doc))

    def take(self, **kw):
        return MB.take(self.ws, home=self.user, path=str(self.bin), **kw)

    def open(self, bundle, parent=None):
        return MB.open_bundle(bundle, PASS, parent or self.base)


class Machine(Base):
    """A home with every kind of thing the manifest names, and two checkouts."""

    def setUp(self):
        super().setUp()
        vault.set_passphrase(self.ws, PASS)
        self.write('.gitconfig', '[user]\n\tname = Example\n\temail = dev@example.invalid\n')
        self.write('.zshrc', 'export EXAMPLE=1\n')
        self.write('.claude/settings.json', '{"example": true}')
        self.write('.claude/CLAUDE.md', '# example\n')
        self.write('.claude/skills/.trash/old/SKILL.md', 'trashed')
        self.write('.agents/skills/alpha/SKILL.md', '---\nname: alpha\n---\n')
        (self.user / '.claude/skills/alpha').symlink_to('../../.agents/skills/alpha')
        (self.user / '.claude/skills/absolute').symlink_to(self.user / '.agents/skills/alpha')
        self.write('.claude/projects/session/transcript.jsonl', 'a transcript')
        self.write('.claude/plugins/cache/example/big.bin', b'x' * 1000)
        self.write('.claude/plugins/installed_plugins.json', '{}')
        self.write('.ssh/id_example', SECRET_MARKER, 0o600)
        self.write('.ssh/known_hosts.old', 'old')
        (self.user / '.ssh').chmod(0o700)
        self.write('.codex/config.toml', 'model = "example"\n')
        self.write('fabric-services/example.default.json', '{"id": "example"}')
        self.write('.config/example-agent/agent.toml', 'operator extra\n')
        self.write('.config/example-agent/huge.bin', b'y' * 30_000)
        self.settings({'include': ['~/.config/example-agent'], 'exclude': ['~/.ssh/*.old'],
                       'roots': ['~/src'], 'limits': {'file_mb': 0.02}})
        self.make_checkouts()

    def make_checkouts(self):
        env = {**os.environ, 'GIT_AUTHOR_DATE': '2026-01-01T00:00:00Z', 'GIT_COMMITTER_DATE': '2026-01-01T00:00:00Z'}
        self.remote = self.base / 'remote' / 'alpha-web.git'
        self.remote.parent.mkdir()
        git('init', '-q', '--bare', '-b', 'main', str(self.remote), env=env)
        alpha = self.user / 'src' / 'alpha-web'
        alpha.mkdir(parents=True)
        git('init', '-q', '-b', 'main', cwd=alpha, env=env)
        (alpha / 'README.md').write_text('alpha\n')
        git('add', '.', cwd=alpha, env=env)
        git('commit', '-qm', 'first', cwd=alpha, env=env)
        git('remote', 'add', 'origin', str(self.remote), cwd=alpha, env=env)
        git('push', '-q', '-u', 'origin', 'main', cwd=alpha, env=env)
        git('remote', 'add', 'mirror', WITH_USER, cwd=alpha, env=env)
        (alpha / 'local.txt').write_text('unpushed\n')
        git('add', '.', cwd=alpha, env=env)
        git('commit', '-qm', 'unpushed', cwd=alpha, env=env)
        (alpha / 'README.md').write_text('alpha, changed\n')
        (alpha / 'notes').mkdir()
        (alpha / 'notes' / 'draft.md').write_text('untracked\n')
        self.alpha_head = git('rev-parse', 'HEAD', cwd=alpha)
        git('worktree', 'add', '-q', '-b', 'feature', str(self.user / 'src' / 'alpha-feature'), cwd=alpha, env=env)
        beta = self.user / 'src' / 'beta-api'
        beta.mkdir()
        git('init', '-q', '-b', 'main', cwd=beta, env=env)
        (beta / 'app.py').write_text('print("beta")\n')
        git('add', '.', cwd=beta, env=env)
        git('commit', '-qm', 'only here', cwd=beta, env=env)
        self.beta_head = git('rev-parse', 'HEAD', cwd=beta)
        (self.user / 'src' / 'node_modules' / 'ignored' / '.git').mkdir(parents=True)


class Taking(Machine):
    def test_a_bundle_carries_the_manifest_and_names_what_it_did_not(self):
        result = self.take(at=AT)
        self.assertEqual(result['result'], 'taken')
        bundles = vault.artifacts(vault.root_info(self.ws)['path'], 'machine', '.obmachine')
        self.assertEqual([b.name for b in bundles], [result['file']])
        raw = bundles[0].read_bytes()
        self.assertTrue(raw.startswith(vault.MAGIC))
        self.assertNotIn(SECRET_MARKER, raw, 'nothing in the root is plaintext')
        self.assertEqual(stat.S_IMODE(bundles[0].stat().st_mode), 0o600)
        stage, manifest = self.open(bundles[0])
        self.addCleanup(shutil.rmtree, stage, True)
        items = {(i['kind'], i['source']): i for i in manifest['items']}
        for source in ('~/.gitconfig', '~/.zshrc', '~/.claude/settings.json', '~/.ssh/id_example',
                       '~/.codex/config.toml', '~/fabric-services/example.default.json',
                       '~/.config/example-agent/agent.toml', '~/.claude/plugins/installed_plugins.json'):
            self.assertIn(('file', source), items, source)
            item = items[('file', source)]
            self.assertEqual(len(item['sha256']), 64)
            self.assertEqual((stage / item['arcname']).read_bytes(), (self.user / source[2:]).read_bytes())
        self.assertEqual(items[('file', '~/.ssh/id_example')]['mode'], 0o600)
        self.assertEqual(items[('dir', '~/.ssh')]['mode'], 0o700)
        self.assertEqual(items[('link', '~/.claude/skills/alpha')]['target'], '../../.agents/skills/alpha')
        self.assertEqual(items[('link', '~/.claude/skills/absolute')]['target'], '~/.agents/skills/alpha',
                         'an absolute target under the home is recorded home-relative')
        self.assertNotIn(('file', '~/.claude/skills/alpha/SKILL.md'), items, 'a link is never followed')
        skipped = {s['source']: s['reason'] for s in manifest['skipped']}
        self.assertIn('opt-in', skipped['~/.claude/projects'])
        self.assertIn('~/.claude/plugins/cache', skipped)
        self.assertIn('~/.claude/skills/.trash', skipped)
        self.assertIn('~/.ssh/known_hosts.old', skipped)
        missed = {m['source']: m for m in manifest['not_carried']}
        self.assertIn('per-file cap', missed['~/.config/example-agent/huge.bin']['reason'])
        self.assertEqual((missed['~/.config/example-agent/huge.bin']['files'],
                          missed['~/.config/example-agent/huge.bin']['size']), (1, 30_000))
        self.assertEqual(result['not_carried_count'], len(manifest['not_carried']))
        self.assertIn('~/.config/example-agent/huge.bin', [m['source'] for m in result['not_carried']])
        self.assertEqual((stage / 'inventories/Brewfile').read_text(), 'brew "git"\ncask "example-app"\n')
        self.assertEqual({i['name']: i['status'] for i in manifest['inventories']},
                         {'brew': 'ok', 'npm': 'ok', 'pipx': 'ok', 'uv': 'absent'})
        pointer = json.loads((stage / 'inventories/observatory.json').read_text())
        self.assertEqual(pointer['engine_version'], config.VERSION)

    def test_checkouts_carry_unpushed_commits_changes_and_untracked_files(self):
        self.take(at=AT)
        stage, manifest = self.open(MB.newest(self.ws))
        self.addCleanup(shutil.rmtree, stage, True)
        records = {r['path']: r for r in manifest['checkouts']}
        self.assertEqual(set(records), {'~/src/alpha-web', '~/src/alpha-feature', '~/src/beta-api'},
                         'a dependency tree is never searched')
        alpha = records['~/src/alpha-web']
        self.assertEqual((alpha['branch'], alpha['head'], alpha['ahead'], alpha['behind']),
                         ('main', self.alpha_head, 1, 0))
        self.assertEqual(alpha['remotes']['mirror'], 'https://example.invalid/alpha-web.git')
        self.assertTrue(alpha['dirty'])
        self.assertEqual(alpha['untracked_count'], 1)
        self.assertEqual(sorted(alpha['carried']), ['bundle', 'patch', 'untracked'])
        self.assertTrue(records['~/src/alpha-feature']['worktree'])
        self.assertNotIn('bundle', records['~/src/alpha-feature'].get('carried', {}),
                         'one bundle per repository: the worktree shares it')
        self.assertIn('bundle', records['~/src/beta-api']['carried'])
        git('bundle', 'verify', str(stage / records['~/src/beta-api']['carried']['bundle']),
            cwd=self.user / 'src' / 'beta-api')

    def test_no_value_reaches_the_manifest_the_state_or_the_answer(self):
        result = self.take(at=AT)
        stage, manifest = self.open(MB.newest(self.ws))
        self.addCleanup(shutil.rmtree, stage, True)
        for text in (json.dumps(manifest), MB.state_path(self.ws).read_text(), json.dumps(result),
                     json.dumps(MB.status(self.ws, AT))):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn(SECRET_MARKER.decode(), text)
            self.assertNotIn(PASS, text)

    def test_files_a_cap_leaves_out_are_named_once_per_folder(self):
        for n in range(40):
            self.write(f'.agents/skills/bulky/part-{n:02}.md', b'z' * 2000)
        self.settings({'roots': [], 'limits': {'entry_mb': 0.01}})
        result = self.take(at=AT)
        bulky = [m for m in result['not_carried'] if m['source'] == '~/.agents/skills/bulky']
        self.assertEqual(len(bulky), 1)
        self.assertIn('limits.entry_mb', bulky[0]['reason'])
        self.assertGreater(bulky[0]['files'], 30)

    def test_rotation_keeps_three(self):
        for day in range(5):
            self.take(at=AT + datetime.timedelta(days=day))
        found = vault.artifacts(vault.root_info(self.ws)['path'], 'machine', '.obmachine')
        self.assertEqual(len(found), MB.KEEP)
        self.assertIn('20261023T', found[-1].name)
        listed = vault.status(self.ws)['artifacts']['machine']
        self.assertEqual((listed['count'], listed['newest']), (MB.KEEP, found[-1].name))


class Refusals(Base):
    def test_without_a_passphrase_nothing_is_written_anywhere(self):
        self.write('.ssh/id_example', SECRET_MARKER, 0o600)
        with self.assertRaisesRegex(MB.MachineBackupError, 'backup-passphrase set'):
            self.take(at=AT)
        self.assertFalse(self.root.exists() and any(self.root.rglob('*.obmachine')))
        self.assertFalse(list((self.ws / 'store').glob('.machine-backup-*')))
        self.assertEqual(MB.read_state(self.ws)['last']['result'], 'failed')

    def test_a_disk_under_the_floor_refuses_and_says_so(self):
        vault.set_passphrase(self.ws, PASS)
        self.settings({'limits': {'min_free_gb': 10 ** 9}})
        with self.assertRaises(MB.DiskRefused):
            self.take(at=AT)
        self.assertFalse(list(self.root.rglob('*.obmachine')) if self.root.exists() else [])
        status = MB.status(self.ws, AT)
        self.assertEqual(status['last_attempt']['result'], 'refused-disk')
        self.assertIn('machine-backup-refused-disk', [w['code'] for w in status['warning_items']])
        self.assertIn('min_free_gb', status['warnings'][0])

    def test_settings_are_validated_by_name(self):
        for doc, words in (({'includes': []}, 'unknown setting includes'),
                           ({'include': ['relative/path']}, 'absolute'),
                           ({'limits': {'total_mb': -1}}, 'at least 0'),
                           ({'limits': {'speed': 1}}, 'unknown limit speed'),
                           ({'transcripts': 'yes'}, 'true or false')):
            self.settings(doc)
            with self.assertRaisesRegex(MB.MachineBackupError, words):
                MB.load_settings(self.ws)
        self.assertIn('machine-backup-settings', [w['code'] for w in MB.status(self.ws, AT)['warning_items']])

    def test_transcripts_are_carried_only_when_asked(self):
        vault.set_passphrase(self.ws, PASS)
        self.write('.claude/projects/session/transcript.jsonl', 'a transcript')
        self.settings({'transcripts': True, 'roots': []})
        self.take(at=AT)
        stage, manifest = self.open(MB.newest(self.ws))
        self.addCleanup(shutil.rmtree, stage, True)
        self.assertIn('~/.claude/projects/session/transcript.jsonl', [i['source'] for i in manifest['items']])

    def test_a_remote_url_loses_its_user_information(self):
        self.assertEqual(MB.redact_url(WITH_USER), 'https://example.invalid/alpha-web.git')
        self.assertEqual(MB.redact_url('git@example.invalid:org/x.git'), 'git@example.invalid:org/x.git')
        self.assertEqual(MB.redact_url('ssh://git@example.invalid/x'), 'ssh://example.invalid/x')


class Restore(Machine):
    def setUp(self):
        super().setUp()
        self.take(at=AT)
        self.bundle = MB.newest(self.ws)
        self.fresh = self.base / 'fresh-user'
        self.fresh.mkdir()

    def files(self, home):
        out = {}
        for p in sorted(home.rglob('*')):
            rel = p.relative_to(home).as_posix()
            if rel.startswith(('src/', 'src')) or '.before-machine-restore-' in rel:
                continue
            if p.is_symlink():
                out[rel] = ('link', os.readlink(p))
            elif p.is_file():
                out[rel] = ('file', p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
        return out

    def test_round_trip_into_an_empty_home(self):
        planned = MB.restore(self.bundle, self.fresh, secret=PASS)
        self.assertEqual(planned['counts']['same'], 0)
        self.assertEqual(planned['counts']['conflict'], 0)
        self.assertEqual(list(self.fresh.iterdir()), [], 'the plan writes nothing')
        applied = MB.restore(self.bundle, self.fresh, secret=PASS, mode='apply')
        self.assertEqual(applied['applied']['failed'], [])
        self.assertEqual(applied['applied']['written'], planned['counts']['missing'])
        stage, manifest = self.open(self.bundle)
        self.addCleanup(shutil.rmtree, stage, True)
        carried = {i['source'][2:] for i in manifest['items'] if i['kind'] in ('file', 'link')}
        original = {k: v for k, v in self.files(self.user).items() if k in carried}
        restored = {k: v for k, v in self.files(self.fresh).items() if k in carried}
        self.assertEqual(set(original), set(restored))
        for rel, value in original.items():
            if value[0] == 'link' and value[1].startswith(str(self.user)):
                self.assertEqual(restored[rel], ('link', value[1].replace(str(self.user), str(self.fresh))),
                                 'a link into the old home points into the new one')
            else:
                self.assertEqual(restored[rel], value, rel)
        self.assertEqual(stat.S_IMODE((self.fresh / '.ssh').stat().st_mode), 0o700)
        again = MB.restore(self.bundle, self.fresh, secret=PASS)
        self.assertEqual((again['counts']['missing'], again['counts']['conflict']), (0, 0))
        self.assertFalse([p for p in self.fresh.iterdir() if p.name.startswith('.decrypted-')],
                         'the unpacked copy is removed')

    def test_a_conflict_is_kept_unless_overwrite_moves_it_aside(self):
        (self.fresh / '.zshrc').write_text('mine\n')
        planned = MB.restore(self.bundle, self.fresh, secret=PASS)
        self.assertIn({'kind': 'file', 'source': '~/.zshrc', 'destination': '~/.zshrc', 'state': 'conflict'},
                      planned['plan'])
        kept = MB.restore(self.bundle, self.fresh, secret=PASS, mode='apply')
        self.assertEqual((self.fresh / '.zshrc').read_text(), 'mine\n')
        self.assertGreaterEqual(kept['applied']['kept'], 1)
        MB.restore(self.bundle, self.fresh, secret=PASS, mode='apply', overwrite=True)
        self.assertEqual((self.fresh / '.zshrc').read_text(), 'export EXAMPLE=1\n')
        aside = list(self.fresh.glob('.zshrc.before-machine-restore-*'))
        self.assertEqual([p.read_text() for p in aside], ['mine\n'])

    def test_clone_brings_back_every_checkout_with_its_carried_work(self):
        result = MB.restore(self.bundle, self.fresh, secret=PASS, do_clone=True)
        rows = {r['checkout']: r for r in result['clone']}
        self.assertEqual(rows['~/src/alpha-web']['result'], 'cloned', rows)
        self.assertEqual(rows['~/src/beta-api']['result'], 'cloned', rows)
        self.assertEqual(rows['~/src/alpha-feature']['result'], 'cloned', rows)
        alpha = self.fresh / 'src' / 'alpha-web'
        self.assertEqual(git('rev-parse', 'HEAD', cwd=alpha), self.alpha_head, 'the unpushed commit is back')
        self.assertEqual((alpha / 'README.md').read_text(), 'alpha, changed\n', 'the uncommitted change is back')
        self.assertEqual((alpha / 'notes' / 'draft.md').read_text(), 'untracked\n')
        self.assertEqual(git('remote', 'get-url', 'mirror', cwd=alpha), 'https://example.invalid/alpha-web.git')
        self.assertEqual(git('rev-parse', 'HEAD', cwd=self.fresh / 'src' / 'beta-api'), self.beta_head,
                         'a repository with no remote comes back from its bundle')
        again = MB.restore(self.bundle, self.fresh, secret=PASS, do_clone=True)
        self.assertEqual({r['result'] for r in again['clone']}, {'exists'}, 'an existing checkout is never touched')

    def test_packages_installs_each_inventory_with_its_tool(self):
        calls = []

        def runner(argv, **kw):
            calls.append([Path(argv[0]).name, *argv[1:]])
            return subprocess.CompletedProcess(argv, 0, '', '')
        which = lambda name: f'/fake/{name}'  # noqa: E731
        result = MB.restore(self.bundle, self.fresh, secret=PASS, do_packages=True, runner=runner, which=which)
        self.assertEqual(calls[0][:3], ['brew', 'bundle', 'install'])
        self.assertIn(['npm', 'install', '-g', 'example-cli@1.2.3'], calls, 'npm itself is not reinstalled')
        self.assertIn(['pipx', 'install', 'example-tool'], calls)
        self.assertEqual({r['exit'] for r in result['packages']}, {0})
        missing = MB.restore(self.bundle, self.fresh, secret=PASS, do_packages=True, runner=runner,
                             which=lambda name: None)
        self.assertIn('install Homebrew first', missing['packages'][0]['detail'])

    def test_a_wrong_passphrase_or_kind_is_refused(self):
        with self.assertRaises(vault.BackupError):
            MB.restore(self.bundle, self.fresh, secret='another passphrase entirely')
        other = self.base / 'db.obsdb'
        source = self.base / 'plain.bin'
        source.write_bytes(b'not a machine bundle')
        vault.encrypt_file(source, other, PASS, kind='observatory-db')
        with self.assertRaisesRegex(MB.MachineBackupError, 'not a machine bundle'):
            MB.restore(other, self.fresh, secret=PASS)

    def test_the_command_line_plans_by_default_and_refuses_overwrite_alone(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {'HOME': str(self.fresh), vault.PASS_ENV: PASS}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(MB.restore_main(['--latest']), 0, err.getvalue())
            self.assertEqual(MB.restore_main(['--latest', '--overwrite']), 2)
            self.assertEqual(MB.restore_main([]), 2)
        doc = json.loads(out.getvalue())
        self.assertEqual((doc['mode'], doc['bundle']), ('plan', self.bundle.name))
        self.assertIn('needs --apply', err.getvalue())
        self.assertEqual(list(self.fresh.iterdir()), [])


class Schedule(Base):
    def setUp(self):
        super().setUp()
        self.calls = []

    def fake(self, base, *, trigger, at):
        self.calls.append(trigger)
        root = vault.root_info(base)['path']
        root.mkdir(parents=True, exist_ok=True)
        name = f"machine-{at.strftime('%Y%m%dT%H%M%SZ')}-0000000{len(self.calls)}.obmachine"
        (root / name).write_bytes(b'x')
        return {'result': 'taken', 'file': name, 'size': 1, 'items': 1, 'not_carried_count': 0}

    def test_daily_then_not_due_then_due_again(self):
        self.assertEqual(MB.step(self.ws, AT, take_fn=self.fake)['result'], 'taken')
        self.assertEqual(MB.step(self.ws, AT + datetime.timedelta(hours=23), take_fn=self.fake)['result'], 'not-due')
        self.assertEqual(MB.step(self.ws, AT + datetime.timedelta(hours=25), take_fn=self.fake)['result'], 'taken')
        self.assertEqual(self.calls, ['daily', 'daily'])

    def test_a_failure_backs_off_for_an_hour(self):
        def broken(base, *, trigger, at):
            MB._record(base, {'at': MB.iso(at), 'result': 'failed', 'detail': 'synthetic'})
            raise MB.MachineBackupError('synthetic')
        self.assertEqual(MB.step(self.ws, AT, take_fn=broken)['result'], 'failed')
        self.assertEqual(MB.step(self.ws, AT + datetime.timedelta(minutes=30), take_fn=self.fake)['result'],
                         'backing-off')
        self.assertEqual(MB.step(self.ws, AT + datetime.timedelta(minutes=61), take_fn=self.fake)['result'], 'taken')

    def test_a_test_or_sandbox_home_is_never_backed_up_by_the_schedule(self):
        self.assertEqual(MB.step(self.ws, AT)['result'], 'skipped')
        self.assertEqual(MB.before_update(self.ws, at=AT)['result'], 'skipped')

    def test_off_in_the_settings_is_off(self):
        self.settings({'enabled': False})
        self.assertEqual(MB.step(self.ws, AT, take_fn=self.fake)['result'], 'off')
        self.assertEqual(MB.before_update(self.ws, take_fn=self.fake, at=AT)['result'], 'off')
        self.assertEqual(MB.status(self.ws, AT)['warning_items'], [])

    def test_before_an_update_unless_one_is_fresh_and_never_raises(self):
        self.assertEqual(MB.before_update(self.ws, take_fn=self.fake, at=AT)['result'], 'taken')
        self.assertEqual(MB.before_update(self.ws, take_fn=self.fake, at=AT + datetime.timedelta(minutes=30))['result'],
                         'fresh')
        self.assertEqual(self.calls, ['before-update'])

        def broken(base, *, trigger, at):
            raise OSError('synthetic')
        self.assertEqual(MB.before_update(self.ws, take_fn=broken, at=AT + datetime.timedelta(hours=2))['result'],
                         'failed')

    def test_status_warns_when_missing_and_when_stale(self):
        self.assertEqual([w['code'] for w in MB.status(self.ws, AT)['warning_items']], ['machine-backup-missing'])
        self.fake(self.ws, trigger='daily', at=AT)
        self.assertEqual(MB.status(self.ws, AT + datetime.timedelta(hours=30))['warning_items'], [])
        late = MB.status(self.ws, AT + datetime.timedelta(hours=40))
        self.assertEqual([w['code'] for w in late['warning_items']], ['machine-backup-stale'])
        self.assertEqual(late['age_hours'], 40.0)

    def test_the_maintenance_pass_runs_the_step_and_its_status_carries_the_warnings(self):
        import maintenance as M
        seen = []
        report = M.run_pass(self.ws, at=AT, commands=_NoCommands(), services=None, app=_NoApp(),
                            store=_Store(), guard=lambda *a: {'result': 'off'},
                            machine=lambda base, at: seen.append(at) or {'result': 'taken'})
        self.assertEqual(report['machine'], {'result': 'taken'})
        self.assertEqual(seen, [AT])
        report = M.run_pass(self.ws, at=AT + datetime.timedelta(minutes=15), commands=_NoCommands(), services=None,
                            app=_NoApp(), store=_Store(), guard=lambda *a: {'result': 'off'},
                            machine=lambda base, at: 1 / 0)
        self.assertEqual(report['machine']['result'], 'error', 'a failed step never stops the pass')
        status = M.status(self.ws)
        self.assertIn('machine_backup', status)
        self.assertIn('machine-backup-missing', [w['code'] for w in status['warning_items']])
        self.assertIn('machine_backup', workspace.doctor(self.ws))


class _NoCommands:
    def apply(self, *args, **kw):
        return 0, {"status": "up-to-date"}, ""

    def full(self, *args, **kw):
        return 0, {"status": "up-to-date"}, ""


class _NoApp:
    def step(self, state, at):
        return {"result": "not-due"}


class _Store:
    def get(self, label):
        return "a synthetic passphrase value"

    def put(self, label, value, replace=False):
        return "file"

    def kind(self):
        return "file"


class CommandLine(Base):
    def test_the_full_cli_routes_both_commands(self):
        sys.path.insert(0, str(ROOT))
        import observatory as cli
        for command in ('machine-backup', 'machine-restore'):
            self.assertIn(command, cli.WORKSPACE_COMMANDS)
            self.assertIn(command, cli.PUBLIC_HELP)
        self.assertEqual(cli.refusal(['machine-restore', '--latest', '--apply', '--clone']), '')
        self.assertEqual(cli.refusal(['machine-backup', 'status']), '')
        self.assertNotEqual(cli.refusal(['machine-restore', '--plan', '--apply']), '')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(workspace.main(['machine-backup', 'status']), 0)
        self.assertIsNone(json.loads(out.getvalue())['newest'])


if __name__ == '__main__':
    unittest.main()
