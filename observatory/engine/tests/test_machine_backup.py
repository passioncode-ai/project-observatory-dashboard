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
import signal
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
from git_env import git_env  # noqa: E402
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
        # The global git file is the temporary HOME's (global_config=None), as a person's would be.
        self.env = patch.dict(os.environ, git_env({
            'HOME': str(self.user), 'OBSERVATORY_HOME': str(self.ws), 'OBSERVATORY_BACKUPS': str(self.root),
            'FABRIC_SERVICES_DIR': str(self.user / 'fabric-services'), 'PATH': os.environ.get('PATH', ''),
            'OBSERVATORY_SYSTEM_SETUP': '0', 'TMPDIR': os.environ.get('TMPDIR', tempfile.gettempdir())},
            global_config=None), clear=True)
        self.env.start()
        workspace.initialize(self.ws)
        import paths
        importlib.reload(paths)
        self.fake_tools()
        # Hermetic (L6): free space is the test's, not the machine's.
        # The key derivation's cost is the format's, not under test here: the header records the
        # parameters, so a reader follows them. Keeps the suite inside the runner's 120 s.
        cheap = patch.dict(vault.SCRYPT, {'n': 2 ** 12})
        cheap.start()
        self.addCleanup(cheap.stop)
        free = patch.object(MB, 'free_bytes', lambda path: 100 * 1024 ** 3)
        free.start()
        self.addCleanup(free.stop)

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
        git('tag', 'local-only', cwd=alpha, env=env)
        git('tag', 'pushed-tag', 'HEAD~1', cwd=alpha, env=env)
        # Two stash entries, the older first (M1).
        for n in (1, 2):
            (alpha / 'local.txt').write_text(f'stashed {n}\n')
            git('stash', 'push', '-q', '-m', f'stash {n}', cwd=alpha, env=env)
        self.stashes = git('reflog', 'show', '--format=%H', 'refs/stash', cwd=alpha).splitlines()
        # A person's diff configuration must not change the carried patch (L2).
        git('config', 'diff.noprefix', 'true', cwd=alpha, env=env)
        (alpha / 'README.md').write_text('alpha, changed\n')
        (alpha / 'notes').mkdir()
        (alpha / 'notes' / 'draft.md').write_text('untracked\n')
        (alpha / 'notes' / 'run.sh').write_text('#!/bin/sh\n')
        (alpha / 'notes' / 'run.sh').chmod(0o755)
        self.alpha_head = git('rev-parse', 'HEAD', cwd=alpha)
        git('worktree', 'add', '-q', '-b', 'feature', str(self.user / 'src' / 'alpha-feature'), cwd=alpha, env=env)
        # A detached worktree whose commits are on no branch and no remote (M1).
        detached = self.user / 'src' / 'alpha-detached'
        git('worktree', 'add', '-q', '--detach', str(detached), cwd=alpha, env=env)
        for n in (1, 2):
            (detached / f'experiment-{n}.txt').write_text(f'{n}\n')
            git('add', '.', cwd=detached, env=env)
            git('commit', '-qm', f'experiment {n}', cwd=detached, env=env)
        self.detached_head = git('rev-parse', 'HEAD', cwd=detached)
        # A repository with no commit yet, holding staged work (M1).
        gamma = self.user / 'src' / 'gamma-new'
        gamma.mkdir()
        git('init', '-q', '-b', 'main', cwd=gamma, env=env)
        (gamma / 'staged.txt').write_text('staged, never committed\n')
        git('add', 'staged.txt', cwd=gamma, env=env)
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
        self.assertEqual(set(records), {'~/src/alpha-web', '~/src/alpha-feature', '~/src/alpha-detached',
                                        '~/src/beta-api', '~/src/gamma-new'},
                         'a dependency tree is never searched')
        alpha = records['~/src/alpha-web']
        self.assertEqual((alpha['branch'], alpha['head'], alpha['ahead'], alpha['behind']),
                         ('main', self.alpha_head, 1, 0))
        self.assertEqual(alpha['remotes']['mirror'], 'https://example.invalid/alpha-web.git')
        self.assertTrue(alpha['dirty'])
        self.assertEqual(alpha['untracked_count'], 2)
        self.assertEqual(sorted(alpha['carried']), ['bundle', 'patch', 'untracked', 'untracked_modes'])
        self.assertEqual(alpha['carried']['untracked_modes']['notes/run.sh'], 0o755)
        # Every local tip counts: branches, tags, every stash entry; the detached worktree its HEAD too.
        tips = ['--branches', '--tags', *self.stashes]
        repo = self.user / 'src' / 'alpha-web'
        self.assertEqual(alpha['unpushed'], int(git('rev-list', '--count', *tips, '--not', '--remotes', cwd=repo)))
        self.assertEqual(records['~/src/alpha-detached']['unpushed'],
                         int(git('rev-list', '--count', *tips, self.detached_head, '--not', '--remotes', cwd=repo)))
        self.assertEqual(records['~/src/alpha-detached']['unpushed'] - alpha['unpushed'], 2)
        self.assertEqual([e['sha'] for e in alpha['stashes']], self.stashes)
        self.assertIn('local-only', alpha['tags'])
        detached = records['~/src/alpha-detached']
        self.assertEqual((detached['branch'], detached['head'], detached['worktree']), (None, self.detached_head, True))
        heads = git('bundle', 'list-heads', str(stage / alpha['carried']['bundle']), cwd=self.user / 'src' / 'alpha-web')
        for sha in (self.detached_head, *self.stashes):
            self.assertIn(sha, heads, 'the detached HEAD and every stash travel in the bundle')
        self.assertIn('refs/tags/local-only', heads)
        patch_text = (stage / alpha['carried']['patch']).read_text()
        self.assertIn('--- a/README.md', patch_text, 'fixed prefixes whatever diff.noprefix says')
        gamma = records['~/src/gamma-new']
        self.assertIsNone(gamma['head'])
        self.assertEqual(gamma['carried']['patch'], f"checkouts/{gamma['id']}/staged.patch")
        self.assertIn('staged, never committed', (stage / gamma['carried']['patch']).read_text())
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

    def test_a_bundle_over_its_cap_is_named_before_it_is_written(self):
        self.settings({'roots': ['~/src'], 'limits': {'bundle_mb': 0.0001}})
        self.take(at=AT)
        stage, manifest = self.open(MB.newest(self.ws))
        self.addCleanup(shutil.rmtree, stage, True)
        missed = [m for m in manifest['not_carried'] if 'limits.bundle_mb' in m['reason']]
        self.assertEqual({m['source'] for m in missed}, {'~/src/alpha-web', '~/src/beta-api'})
        self.assertFalse(any('bundle' in (r.get('carried') or {}) for r in manifest['checkouts']))

    def test_everything_not_carried_says_how_to_carry_it_and_commits_come_first(self):
        # 2026-10-20 on a real machine: 19 items not carried, ten of them unpushed commits over
        # the 16 MB bundle cap, named only as a count in a receipt. The bundle exists so the
        # machine can move; an item left behind must say what to do about it.
        (self.user / 'src' / 'alpha-web' / 'notes' / 'bulky.bin').write_bytes(b'u' * 40_000)
        self.settings({'roots': ['~/src'], 'limits': {'bundle_mb': 0.0001, 'patch_mb': 0.0000001,
                                                       'untracked_mb': 0.01, 'file_mb': 0.02},
                       'include': ['~/.config/example-agent']})
        result = self.take(at=AT)
        stage, manifest = self.open(MB.newest(self.ws))
        self.addCleanup(shutil.rmtree, stage, True)
        for record in manifest['not_carried']:
            self.assertTrue(record.get('fix'), record)
        by_cap = {}
        for record in manifest['not_carried']:
            for key in MB.LIMITS:
                if f'limits.{key}' in record['reason']:
                    by_cap.setdefault(key, []).append(record)
        for key in ('bundle_mb', 'patch_mb', 'untracked_mb', 'file_mb'):
            self.assertIn(key, by_cap, key)
            for record in by_cap[key]:
                self.assertIn(f'limits.{key}', record['fix'])
                self.assertIn(MB.CONFIG_NAME, record['fix'])
                # The value it names carries this item.
                needed = float(record['fix'].split(f'"{key}": ', 1)[1].split('}', 1)[0])
                self.assertGreaterEqual(needed * 1024 * 1024, record['size'])
        for record in by_cap['bundle_mb']:
            self.assertIn('push', record['fix'])
        # The receipt lists the only copies of work first: commits, then uncommitted changes.
        kinds = [MB.loss_rank(r) for r in result['not_carried']]
        self.assertEqual(kinds, sorted(kinds))
        self.assertIn('commit', result['not_carried'][0]['reason'])
        status = MB.status(self.ws, AT)
        item = next(w for w in status['warning_items'] if w['code'] == 'machine-backup-not-carried')
        self.assertGreaterEqual(item['commits'], 2)
        self.assertIn('commit', item['text'])

    def test_a_carried_bundle_is_streamed_from_its_file_never_read_whole(self):
        read = Path.read_bytes
        whole = []

        def watch(path):
            if path.suffix in ('.bundle', '.patch') or 'untracked' in path.parts:
                whole.append(path.name)
            return read(path)
        with patch.object(Path, 'read_bytes', watch):
            self.take(at=AT)
        self.assertEqual(whole, [])
        stage, manifest = self.open(MB.newest(self.ws))
        self.addCleanup(shutil.rmtree, stage, True)
        checkouts = [i for i in manifest['items'] if i['kind'] == 'checkout']
        self.assertTrue(checkouts)
        for item in checkouts:
            data = (stage / item['arcname']).read_bytes()
            self.assertEqual((len(data), MB._sha256(data)), (item['size'], item['sha256']))

    def test_untracked_files_are_spooled_by_number_never_by_name(self):
        # Two untracked names differing only in case are two files on a case-sensitive checkout
        # and one on a case-insensitive stage: spooled by name, the second replaced the first.
        copy = shutil.copyfile
        spooled = []

        def watch(src, dst, *a, **kw):
            if 'untracked' in Path(dst).parts:
                spooled.append(Path(dst).name)
            return copy(src, dst, *a, **kw)
        with patch.object(MB.shutil, 'copyfile', watch):
            self.take(at=AT)
        self.assertTrue(spooled)
        self.assertTrue(all(name.isdigit() for name in spooled), spooled)

    def test_the_default_caps_carry_what_the_measured_machine_left_behind(self):
        # The receipt of 2026-10-20 on a working agent machine, in its own order and exact bytes
        # (cap, size). Review of #325: a 128 MB cap still refused the 130.0 MiB bundle that held
        # 32 unpushed commits while the docs said it was carried. The claim is computed here.
        measured = [('bundle_mb', 136315663), ('bundle_mb', 423910252), ('bundle_mb', 20078651),
                    ('untracked_mb', 16661073), ('bundle_mb', 31750920), ('bundle_mb', 25166940),
                    ('patch_mb', 61986064), ('bundle_mb', 17470199), ('bundle_mb', 80559231),
                    ('bundle_mb', 27675287), ('bundle_mb', 31057757), ('bundle_mb', 115944523),
                    ('patch_mb', 24526032), ('untracked_mb', 169381330)]
        used, left = [0], []
        for key, size in measured:
            refused = MB.over_cap(size, key, used[0], MB.LIMITS)
            if refused:
                left.append((key, size, refused))
            else:
                used[0] += size
        self.assertEqual([(k, s) for k, s, _ in left], [('bundle_mb', 423910252), ('untracked_mb', 169381330)],
                         'every one but the 404 MiB single-commit bundle and the 161.5 MiB untracked set')

    def test_the_default_caps_are_the_documented_ones(self):
        # The caps are a promise in the onboarding page; a change to one changes both.
        text = (ROOT / 'docs' / 'ONBOARDING.md').read_text(encoding='utf-8')
        start = text.index('### This machine\'s agent setup')
        section = ' '.join(text[start:text.index('\n## ', start)].split())   # line breaks are layout
        for key in ('file_mb', 'entry_mb', 'total_mb', 'bundle_mb', 'patch_mb', 'untracked_mb', 'checkouts_mb'):
            self.assertIn(f'`{key}` {MB.LIMITS[key]} MB', section, key)
        self.assertIn(f'`min_free_gb` {MB.LIMITS["min_free_gb"]} GB', section)

    def test_a_submodule_is_not_a_worktree(self):
        sub_dir = self.base / 'kinds' / 'lib'
        sub_dir.mkdir(parents=True)
        (sub_dir / '.git').write_text('gitdir: ../.git/modules/lib\n')
        self.assertEqual(MB._checkout_kind(sub_dir), 'submodule')
        (sub_dir / '.git').write_text('gitdir: /x/repo/.git/worktrees/lib\n')
        self.assertEqual(MB._checkout_kind(sub_dir), 'worktree')
        self.assertEqual(MB._checkout_kind(self.user / 'src' / 'beta-api'), 'repository')

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
        self.assertEqual(rows['~/src/alpha-detached']['result'], 'cloned', rows)
        self.assertEqual(rows['~/src/gamma-new']['result'], 'cloned', rows)
        alpha = self.fresh / 'src' / 'alpha-web'
        self.assertEqual(git('rev-parse', 'HEAD', cwd=alpha), self.alpha_head, 'the unpushed commit is back')
        self.assertEqual((alpha / 'README.md').read_text(), 'alpha, changed\n', 'the uncommitted change is back')
        self.assertEqual((alpha / 'notes' / 'draft.md').read_text(), 'untracked\n')
        self.assertEqual(stat.S_IMODE((alpha / 'notes' / 'run.sh').stat().st_mode), 0o755, 'L3: its mode too')
        self.assertEqual(git('reflog', 'show', '--format=%H', 'refs/stash', cwd=alpha).splitlines(), self.stashes,
                         'both stashes, newest first')
        self.assertEqual(git('rev-parse', 'local-only', cwd=alpha), self.alpha_head)
        self.assertTrue(git('rev-parse', 'pushed-tag', cwd=alpha))
        self.assertEqual(git('for-each-ref', 'refs/carried', cwd=alpha), '', 'the carried names are removed')
        detached = self.fresh / 'src' / 'alpha-detached'
        self.assertEqual(git('rev-parse', 'HEAD', cwd=detached), self.detached_head, 'the detached commits are back')
        self.assertEqual(subprocess.run(['git', 'symbolic-ref', '-q', 'HEAD'], cwd=detached).returncode, 1,
                         'and HEAD is detached again')
        gamma = self.fresh / 'src' / 'gamma-new'
        self.assertEqual(git('diff', '--cached', '--name-only', cwd=gamma), 'staged.txt', 'staged work is staged again')
        self.assertEqual(git('remote', 'get-url', 'mirror', cwd=alpha), 'https://example.invalid/alpha-web.git')
        self.assertEqual(git('rev-parse', 'HEAD', cwd=self.fresh / 'src' / 'beta-api'), self.beta_head,
                         'a repository with no remote comes back from its bundle')
        again = MB.restore(self.bundle, self.fresh, secret=PASS, do_clone=True)
        self.assertEqual({r['result'] for r in again['clone']}, {'exists'}, 'an existing checkout is never touched')

    def test_no_decrypted_copy_is_left_in_the_home(self):
        MB.restore(self.bundle, self.fresh, secret=PASS, mode='apply', do_clone=True, base=self.ws)
        self.assertFalse(list(self.fresh.glob('.decrypted-*')))
        self.assertEqual([p.name for p in MB.stage_root(self.ws).iterdir()], ['.lock'])
        self.assertEqual(stat.S_IMODE(MB.stage_root(self.ws).stat().st_mode), 0o700)

    def killed_restore(self, sig):
        code = (f"import sys, time; sys.path[:0] = [{str(ROOT)!r}, {str(Path(__file__).resolve().parent)!r}]\n"
                "import machine_backup as MB\nfrom pathlib import Path\n"
                "MB.raise_on_signals()\n"
                f"MB.restore(Path({str(self.bundle)!r}), Path({str(self.fresh)!r}), base=Path({str(self.ws)!r}), "
                f"secret={PASS!r}, mode='apply', hold=lambda step, stage: (print('ready', flush=True), time.sleep(60)))\n")
        child = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True, env=dict(os.environ))
        self.assertEqual(child.stdout.readline().strip(), 'ready')
        root = MB.stage_root(self.ws)
        self.assertTrue(list(root.rglob('id_example')), 'the plaintext exists while the restore runs')
        child.send_signal(sig)
        child.wait(30)
        child.stdout.close()
        return root

    def test_a_restore_killed_by_sigterm_or_sighup_leaves_no_plaintext(self):
        for sig in (signal.SIGTERM, signal.SIGHUP):
            root = self.killed_restore(sig)
            self.assertEqual([p.name for p in root.iterdir()], ['.lock'], sig)

    def test_a_restore_killed_outright_is_swept_by_the_next_run(self):
        root = self.killed_restore(signal.SIGKILL)
        self.assertTrue(list(root.rglob('id_example')), 'SIGKILL cannot be caught')
        MB.restore(self.bundle, self.fresh, secret=PASS, base=self.ws)
        self.assertEqual([p.name for p in root.iterdir()], ['.lock'])

    def test_a_backup_sweeps_what_a_killed_one_left(self):
        root = MB.stage_root(self.ws)
        left = root / 'backup-killed'
        left.mkdir(parents=True)
        (left / 'abcdef012345.bundle').write_bytes(b'plaintext commits')
        legacy = self.ws / 'store' / '.machine-backup-old'
        legacy.mkdir()
        (legacy / 'x.bundle').write_bytes(b'plaintext commits')
        self.take(at=AT + datetime.timedelta(days=1))
        self.assertEqual([p.name for p in root.iterdir()], ['.lock'])
        self.assertFalse(legacy.exists())

    def test_a_refused_run_leaves_a_live_restores_stage_alone(self):
        # Round 2 of #305: a second restore, even --plan, or a backup, refused while a restore
        # held the stage, swept it anyway; the restore's later checkouts lost their commits.
        go = self.base / 'go'
        code = (f"import sys, time, json; sys.path[:0] = [{str(ROOT)!r}, {str(Path(__file__).resolve().parent)!r}]\n"
                "import machine_backup as MB\nfrom pathlib import Path\n"
                f"go = Path({str(go)!r})\n"
                "def hold(step, stage):\n"
                "    print('ready', flush=True)\n"
                "    while not go.exists(): time.sleep(0.1)\n"
                f"r = MB.restore(Path({str(self.bundle)!r}), Path({str(self.fresh)!r}), base=Path({str(self.ws)!r}), "
                f"secret={PASS!r}, do_clone=True, hold=hold)\n"
                "print(json.dumps(r.get('failed')))\n")
        child = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True, env=dict(os.environ))
        self.assertEqual(child.stdout.readline().strip(), 'ready')
        root = MB.stage_root(self.ws)
        before = sorted(p.relative_to(root).as_posix() for p in root.rglob('*'))
        with self.assertRaisesRegex(MB.MachineBackupError, 'is running'):
            MB.restore(self.bundle, self.fresh, secret=PASS, base=self.ws)
        with self.assertRaisesRegex(MB.MachineBackupError, 'is running'):
            self.take(at=AT + datetime.timedelta(days=1))
        self.assertEqual(sorted(p.relative_to(root).as_posix() for p in root.rglob('*')), before,
                         'the refused runs touched nothing')
        go.write_text('')
        out, _ = child.communicate(timeout=120)
        self.assertEqual(child.returncode, 0)
        self.assertEqual(json.loads(out.strip().splitlines()[-1]), [])
        self.assertEqual(git('rev-parse', 'HEAD', cwd=self.fresh / 'src' / 'alpha-detached'), self.detached_head)

    def test_a_missing_carried_file_fails_by_name_and_the_command_exits_1(self):
        real = MB.extract
        def lossy(bundle, secret, dest, want):
            return real(bundle, secret, dest, lambda n: want(n) and not n.endswith('unpushed.bundle'))
        out, err = io.StringIO(), io.StringIO()
        with patch.object(MB, 'extract', lossy), patch.dict(os.environ, {'HOME': str(self.fresh), vault.PASS_ENV: PASS}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = MB.restore_main(['--latest', '--clone'])
        self.assertEqual(code, 1)
        rows = {r['checkout']: r for r in json.loads(out.getvalue())['clone']}
        self.assertEqual(rows['~/src/alpha-web']['result'], 'carried-missing')
        self.assertIn('unpushed.bundle', rows['~/src/alpha-web']['detail'])
        self.assertIn('not restored', err.getvalue())

    def test_the_stage_used_before_a_workspace_exists_is_swept_too(self):
        other = MB.stage_root(None)
        left = other / 'restore-killed' / 'files'
        left.mkdir(parents=True)
        (left / 'id_example').write_bytes(SECRET_MARKER)
        MB.restore(self.bundle, self.fresh, secret=PASS, base=self.ws)
        self.assertFalse((other / 'restore-killed').exists())

    def test_a_detached_worktree_joins_a_main_checkout_already_here(self):
        MB.restore(self.bundle, self.fresh, secret=PASS, do_clone=True)
        detached = self.fresh / 'src' / 'alpha-detached'
        shutil.rmtree(detached)
        git('worktree', 'prune', cwd=self.fresh / 'src' / 'alpha-web')
        git('gc', '-q', '--prune=now', cwd=self.fresh / 'src' / 'alpha-web')
        again = MB.restore(self.bundle, self.fresh, secret=PASS, do_clone=True)
        rows = {r['checkout']: r for r in again['clone']}
        self.assertEqual(rows['~/src/alpha-web']['result'], 'exists')
        self.assertEqual(rows['~/src/alpha-detached']['result'], 'cloned', rows)
        self.assertEqual(git('rev-parse', 'HEAD', cwd=detached), self.detached_head)
        self.assertEqual(git('for-each-ref', 'refs/carried', cwd=self.fresh / 'src' / 'alpha-web'), '')

    def test_a_record_from_a_bundle_is_validated_before_use(self):
        good = {'id': 'abcdef012345', 'path': '~/src/x', 'branch': 'main', 'head': 'a' * 40}
        self.assertIsNone(MB.validate_checkout(good, self.fresh))
        for bad in ({'id': '../x'}, {'path': '~/../escape'}, {'branch': '--upload-pack=x'}, {'head': 'HEAD'},
                    {'remotes': {'origin': '--upload-pack=x'}}, {'remotes': {'-o': 'x'}},
                    {'stashes': [{'sha': 'nope'}]}, {'tags': {'-d': 'a' * 40}},
                    {'carried': {'bundle': '../../.ssh/id'}}, {'carried': {'patch': '/etc/passwd'}},
                    {'carried': {'untracked': ['../outside']}}):
            self.assertIsNotNone(MB.validate_checkout({**good, **bad}, self.fresh), bad)
        manifest = {'items': [{'kind': 'file', 'source': '~/x', 'arcname': '../x', 'sha256': '0' * 64, 'size': 1}]}
        done = MB.apply(manifest, self.base, self.fresh)
        self.assertEqual(len(done['failed']), 1)
        self.assertFalse((self.fresh / 'x').exists())

    def test_a_destination_never_leaves_its_home(self):
        for source in ('~/../outside', '~/a/../../b', '~/a/../b', 'relative/path', '/abs/../escape', ''):
            with self.assertRaises(MB.MachineBackupError, msg=source):
                MB._destination(source, self.fresh)
        self.assertEqual(MB._destination('~/.ssh/id', self.fresh), self.fresh / '.ssh/id')
        self.assertEqual(MB._destination('~', self.fresh), self.fresh)

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

    def test_a_restore_never_starts_a_background_gc(self):
        """Every git command a restore runs carries gc.auto=0 and maintenance.auto=false, as safe_git does:
        a detached `gc --auto` outlived the restore and broke a test's temporary folder on Linux CI."""
        calls = []

        def runner(argv, **kw):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, '', '')
        MB.restore(self.bundle, self.fresh, secret=PASS, do_clone=True, runner=runner)
        gits = [c for c in calls if Path(c[0]).name == 'git']
        self.assertTrue(gits, 'the restore ran no git command')
        for argv in gits:
            self.assertEqual(argv[1:5], ['-c', 'gc.auto=0', '-c', 'maintenance.auto=false'], argv[:6])

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

    def test_the_update_counts_the_bundle_it_is_about_to_take(self):
        self.assertEqual(MB.expected_before_update(self.ws, AT), 0, 'a test home is never backed up')
        with patch.object(MB, 'automatic_allowed', lambda: True):
            self.assertEqual(MB.expected_before_update(self.ws, AT), 0, 'no bundle yet: nothing to size it by')
            self.fake(self.ws, trigger='daily', at=AT)
            self.assertEqual(MB.expected_before_update(self.ws, AT + datetime.timedelta(minutes=30)), 0, 'fresh')
            self.assertEqual(MB.expected_before_update(self.ws, AT + datetime.timedelta(hours=2)), 1, 'due')

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
