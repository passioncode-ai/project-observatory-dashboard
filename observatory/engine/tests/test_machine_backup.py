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
import machine_identity as MI
import machine_move as MM

PASS = 'correct horse battery staple'
#: The two machines of a move (OBS-283): the bundle is taken on ALPHA and restored on BETA.
ALPHA, BETA = 'm-machine-alpha-0001', 'm-machine-beta-0002'
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
            'HOME': str(self.user), 'USERPROFILE': str(self.user), 'OBSERVATORY_HOME': str(self.ws), 'OBSERVATORY_BACKUPS': str(self.root),
            'FABRIC_SERVICES_DIR': str(self.user / 'fabric-services'), 'PATH': os.environ.get('PATH', ''),
            'OBSERVATORY_SYSTEM_SETUP': '0', 'TMPDIR': os.environ.get('TMPDIR', tempfile.gettempdir()),
            # One machine for every case unless a case moves to another (OBS-283).
            'OBSERVATORY_MACHINE_ID': ALPHA, 'OBSERVATORY_MACHINE_ID_FOR_TESTS': '1'},
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


class ExternalSecretStore(Base):
    """OBS-350: a secret store kept outside the workspace travels encrypted in the workspace snapshot, so
    the machine bundle never carries a second copy that could drift, even when an include names it."""

    def setUp(self):
        super().setUp()
        from secret_store_fixture import make_external_store, all_values
        self.values = all_values()
        vault.set_passphrase(self.ws, PASS)
        self.store = make_external_store(self.user / '.config' / 'example-store')
        self.write('.config/example-agent/agent.toml', 'operator extra\n')
        doc = config.read_json(self.ws / 'config' / 'settings.json')
        doc.setdefault('sources', {})['secret_store'] = str(self.store)
        workspace.write_json(self.ws / 'config' / 'settings.json', doc)
        self.settings({'include': ['~/.config'], 'roots': []})

    def test_an_include_inside_the_external_store_is_skipped_with_its_reason(self):
        self.take(at=AT)
        stage, manifest = self.open(MB.newest(self.ws))
        self.addCleanup(shutil.rmtree, stage, True)
        self.assertIn({'source': '~/.config/example-store',
                       'reason': 'carried encrypted in the workspace snapshot (sources.secret_store)'},
                      manifest['skipped'])
        carried = [p for p in stage.rglob('*') if p.is_file()]
        self.assertTrue(any(p.name == 'agent.toml' for p in carried), 'the rest of the include travels')
        for path in carried:
            self.assertNotIn('example-store', path.as_posix())
            for value in self.values:
                self.assertNotIn(value.encode(), path.read_bytes(), path)

    def test_the_workspace_pointer_names_where_the_store_travels(self):
        pointer = MB.workspace_pointer(self.ws, self.user)
        self.assertEqual(pointer['secret_store'],
                         {'external': True, 'carried_in': 'workspace snapshot', 'newest_snapshot_carried': None})
        record = self.ws / 'store' / 'raw' / 'secret-store-carry.json'
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({'at': '2026-11-06T00:00:00Z', 'kind': 'daily', 'file': 'daily-x.obsnap',
                                      'state': 'carried', 'reason': None, 'files': 9, 'size': 120}))
        self.assertTrue(MB.workspace_pointer(self.ws, self.user)['secret_store']['newest_snapshot_carried'])
        record.write_text(json.dumps({'at': '2026-11-06T00:00:00Z', 'state': 'not-carried', 'reason': 'no-passphrase'}))
        self.assertFalse(MB.workspace_pointer(self.ws, self.user)['secret_store']['newest_snapshot_carried'])
        doc = config.read_json(self.ws / 'config' / 'settings.json')
        doc['sources'].pop('secret_store')
        workspace.write_json(self.ws / 'config' / 'settings.json', doc)
        self.assertEqual(MB.workspace_pointer(self.ws, self.user)['secret_store'], {'external': False})

    def test_a_store_no_snapshot_carries_is_not_skipped(self):
        # A setting naming a folder that holds the workspace is never carried by the snapshot; the
        # bundle must not drop what its entries name because of it.
        doc = config.read_json(self.ws / 'config' / 'settings.json')
        doc['sources']['secret_store'] = str(self.base)
        workspace.write_json(self.ws / 'config' / 'settings.json', doc)
        self.assertEqual(MB.workspace_pointer(self.ws, self.user)['secret_store'],
                         {'external': True, 'carried_in': None, 'reason': 'contains-workspace'})
        self.take(at=AT)
        stage, manifest = self.open(MB.newest(self.ws))
        self.addCleanup(shutil.rmtree, stage, True)
        self.assertFalse([s for s in manifest['skipped'] if 'workspace snapshot' in s['reason']], manifest['skipped'])
        self.assertTrue(any(p.name == 'agent.toml' for p in stage.rglob('*')))


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

    def test_projects_clones_only_the_named_checkouts(self):
        result = MB.restore(self.bundle, self.fresh, secret=PASS, do_clone=True, projects=['beta-api'])
        self.assertEqual([r['checkout'] for r in result['clone']], ['~/src/beta-api'])
        self.assertEqual(result['clone'][0]['result'], 'cloned', result['clone'])
        self.assertFalse((self.fresh / 'src' / 'alpha-web').exists())
        self.assertEqual([c['path'] for c in result['checkouts'] if c['selected']], ['~/src/beta-api'])
        planned = MB.restore(self.bundle, self.base / 'plan-only', secret=PASS, projects=['beta-api'])
        self.assertIn('project-observatory full machine-restore --clone --projects=beta-api  (the recorded checkouts)',
                      planned['next'])

    def test_a_worktree_named_brings_its_main_checkout_and_a_path_names_what_is_under_it(self):
        result = MB.restore(self.bundle, self.fresh, secret=PASS, do_clone=True, projects=['alpha-feature'])
        self.assertEqual({r['checkout']: r['result'] for r in result['clone']},
                         {'~/src/alpha-web': 'cloned', '~/src/alpha-feature': 'cloned'})
        stage, manifest = self.open(self.bundle)
        self.addCleanup(shutil.rmtree, stage, True)
        every = sorted(r['path'] for r in manifest['checkouts'])
        chosen, unmatched = MB.select_checkouts(manifest['checkouts'], ['~/src'], self.fresh)
        self.assertEqual((sorted(r['path'] for r in chosen), unmatched), (every, []))
        chosen, _ = MB.select_checkouts(manifest['checkouts'], ['gamma-*,beta-api'], self.fresh)
        self.assertEqual(sorted(r['path'] for r in chosen), ['~/src/beta-api', '~/src/gamma-new'])

    def test_a_worktree_with_no_common_folder_brings_no_other_checkout(self):
        records = [{'path': '~/src/alpha-feature', 'worktree': True, 'common_dir': None},
                   {'path': '~/src/beta-api', 'common_dir': None},
                   {'path': '~/src/gamma-new'}]
        chosen, unmatched = MB.select_checkouts(records, ['alpha-feature'], self.fresh)
        self.assertEqual(([r['path'] for r in chosen], unmatched), (['~/src/alpha-feature'], []))

    def test_a_project_no_checkout_matches_is_refused_before_anything_is_written(self):
        with self.assertRaisesRegex(MB.MachineBackupError, 'no-such-project'):
            MB.restore(self.bundle, self.fresh, secret=PASS, mode='apply', do_clone=True, projects=['no-such-project'])
        self.assertEqual(list(self.fresh.iterdir()), [], 'refused before the apply')

    def test_a_conflict_is_kept_unless_overwrite_moves_it_aside(self):
        (self.fresh / '.zshrc').write_text('mine\n')
        planned = MB.restore(self.bundle, self.fresh, secret=PASS)
        self.assertIn({'kind': 'file', 'source': '~/.zshrc', 'destination': '~/.zshrc', 'category': 'shell',
                       'state': 'conflict'}, planned['plan'])
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
        with patch.object(MB, 'extract', lossy), patch.dict(os.environ, {'HOME': str(self.fresh), 'USERPROFILE': str(self.fresh), vault.PASS_ENV: PASS}), \
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
        with patch.dict(os.environ, {'HOME': str(self.fresh), 'USERPROFILE': str(self.fresh), vault.PASS_ENV: PASS}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(MB.restore_main(['--latest']), 0, err.getvalue())
            self.assertEqual(MB.restore_main(['--latest', '--overwrite']), 2)
            self.assertEqual(MB.restore_main([]), 2)
        doc = json.loads(out.getvalue())
        self.assertEqual((doc['mode'], doc['bundle']), ('plan', self.bundle.name))
        self.assertIn('needs --apply', err.getvalue())
        self.assertEqual(list(self.fresh.iterdir()), [])


#: A private key's first line, built so this file holds no key-shaped text.
PRIVATE_HEAD = b'-----BEGIN OPENSSH ' + b'PRIVATE KEY-----\n'
JOB_LABEL = 'com.example.alpha-bot'


def pub_line(seed: bytes, comment: str = 'example') -> tuple[str, str]:
    """A synthetic ed25519 public line and its fingerprint as `ssh-keygen -l` spells it."""
    import base64
    import hashlib
    kind = b'ssh-ed25519'
    blob = len(kind).to_bytes(4, 'big') + kind + (32).to_bytes(4, 'big') + hashlib.sha256(seed).digest()
    fp = 'SHA256:' + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip('=')
    return f'ssh-ed25519 {base64.b64encode(blob).decode()} {comment}\n', fp


def openssh_private(seed: bytes) -> tuple[bytes, str]:
    """A synthetic OpenSSH private key file whose clear header carries an ed25519 public key, and that
    key's fingerprint. The private section is a marker, never key material."""
    import base64
    import hashlib

    def string(b: bytes) -> bytes:
        return len(b).to_bytes(4, 'big') + b
    blob = string(b'ssh-ed25519') + string(hashlib.sha256(seed).digest())
    body = (b'openssh-key-v1\0' + string(b'none') + string(b'none') + string(b'') + (1).to_bytes(4, 'big')
            + string(blob) + string(SECRET_MARKER * 4))
    encoded = base64.b64encode(body)
    lines = b'\n'.join(encoded[i:i + 70] for i in range(0, len(encoded), 70))
    fp = 'SHA256:' + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip('=')
    return PRIVATE_HEAD + lines + b'\n-----END OPENSSH ' + b'PRIVATE KEY-----\n', fp


def job_rel():
    """Where this platform keeps a user job's definition (what the bundle's job entry walks)."""
    # The file name differs from the plist's Label on purpose: the plan and the placement file both name the Label.
    return (f'Library/LaunchAgents/{JOB_LABEL}.job.plist' if sys.platform == 'darwin'
            else f'.config/systemd/user/{JOB_LABEL}.service')


def job_label():
    """The label a job is named by: launchd's `Label`, or the systemd unit's file name."""
    return JOB_LABEL if sys.platform == 'darwin' else f'{JOB_LABEL}.service'


def job_bytes():
    if sys.platform == 'darwin':
        import plistlib
        return plistlib.dumps({'Label': JOB_LABEL, 'ProgramArguments': ['/usr/bin/true']})
    return b'[Service]\nExecStart=/usr/bin/true\n'


@unittest.skipIf(sys.platform == 'win32', 'no launchd or systemd user jobs on Windows')
class SecondMachineKeys(unittest.TestCase):
    """Which withheld `.ssh` files get the steps that make a key, read off `_Second` directly."""

    def keys(self, files: dict[str, bytes], config_written: bool = True, config_kept: bool = False) -> dict[str, dict]:
        root = Path(tmp.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        home, stage, items = root / 'home', root / 'stage', []
        home.mkdir()
        (stage / 'files').mkdir(parents=True)
        for i, (source, data) in enumerate(files.items()):
            (stage / f'files/{i}').write_bytes(data)
            items.append({'kind': 'file', 'source': source, 'arcname': f'files/{i}', 'sha256': 'x'})
        manifest = {'items': items}
        return {k['file']: k for k in MB._Second(manifest, home, stage).ssh_keys(
            manifest, config_written=config_written, config_kept=config_kept)}

    def test_the_host_alias_rule_is_the_ssh_keys_doors(self):
        # OBS-306 (1): the plan passes an alias as --server only when rekey would take it.
        self.assertEqual(MB.HOST_ALIAS.pattern, MB._ssh_keys_module().HOST_ARG.pattern)

    def test_an_alias_rekey_refuses_is_sent_to_its_address_or_given_a_plain_alias(self):
        # OBS-306 (2): `send --to server:ALIAS` refuses such an alias by the same rule, so the note names
        # the host's HostName, or says to add an alias both take.
        config = (b'Host my/box\n  HostName box.invalid\n  IdentityFile /opt/elsewhere/.ssh/work\n'
                  b'Host other/box\n  IdentityFile /opt/elsewhere/.ssh/work\n')
        key = self.keys({'~/.ssh/config': config, '~/.ssh/work': PRIVATE_HEAD + SECRET_MARKER})['~/.ssh/work']
        self.assertEqual(key['steps'][0], 'project-observatory full ssh-keys rekey work')
        self.assertIn('Host my/box', key['note'])
        self.assertIn('ssh-keys send work --to server:box.invalid --verify', key['note'])
        # Review of #414 (L3): the address alone drops what the Host block sets.
        self.assertIn('not the User, Port or ProxyJump Host my/box sets', key['note'])
        self.assertNotIn('server:my/box', key['note'])
        self.assertNotIn('server:HOST', key['note'])
        self.assertIn('Host other/box by name, and it names no address', key['note'])
        self.assertIn('add a plain Host alias', key['note'])
        sk = MB._ssh_keys_module()
        self.assertTrue(sk.HOST_ARG.fullmatch('box.invalid'), 'the address the note names is one send takes')
        self.assertFalse(sk.HOST_ARG.fullmatch('my/box'))

    def test_a_config_kept_here_names_the_hosts_and_the_overwrite_step(self):
        # OBS-306 (3): a restore without --overwrite keeps this machine's own different ~/.ssh/config.
        config = b'Host plain\n  IdentityFile ~/.ssh/work\nHost upper\n  IdentityFile ~/.SSH/work\n'
        key = self.keys({'~/.ssh/config': config, '~/.ssh/work': PRIVATE_HEAD + SECRET_MARKER},
                        config_written=False, config_kept=True)['~/.ssh/work']
        self.assertEqual(key['steps'][0], 'project-observatory full ssh-keys rekey work')
        self.assertIn('differs from the carried one', key['note'])
        self.assertIn('Host plain, upper', key['note'])
        # Review of #414 (M2): no step replaces ~/.ssh/config alone, so the note says to merge by hand and
        # names what the category-wide --overwrite would replace.
        self.assertIn('copy those Host blocks from the other machine', key['note'])
        self.assertIn('No restore step replaces ~/.ssh/config alone', key['note'])
        self.assertIn('authorized_keys can shut out a remote login', key['note'])
        named = self.keys({'~/.ssh/config': config, '~/.ssh/work': PRIVATE_HEAD + SECRET_MARKER},
                          config_written=False,
                          config_kept=['~/.ssh/config', '~/.ssh/authorized_keys'])['~/.ssh/work']
        self.assertIn('replaces ~/.ssh/config, ~/.ssh/authorized_keys (each moved aside)', named['note'])

    def test_other_keys_say_whether_a_key_sat_directly_in_ssh(self):
        # Review of #414 (L1): M3 offers a rekey step only where the restore does.
        root = Path(tmp.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        home, stage = root / 'home', root / 'stage'
        home.mkdir()
        (stage / 'files').mkdir(parents=True)
        direct, fp_direct = openssh_private(b'alpha-direct')
        deploy, fp_deploy = openssh_private(b'alpha-deploy')
        items = []
        for i, (source, data) in enumerate({'~/.ssh/id_work': direct, '~/dotfiles/.ssh/deploy_key': deploy}.items()):
            (stage / f'files/{i}').write_bytes(data)
            items.append({'kind': 'file', 'source': source, 'arcname': f'files/{i}', 'sha256': 'x'})
        got = MB._other_keys({'items': items}, stage, home)
        self.assertIn({'key': 'id_work', 'fingerprint': fp_direct, 'in_ssh': True}, got)
        self.assertIn({'key': 'deploy_key', 'fingerprint': fp_deploy, 'in_ssh': False}, got)
        # One key in two .ssh folders, the other one listed first: the copy directly in ~/.ssh names it.
        (stage / 'files/2').write_bytes(direct)
        both = [{'kind': 'file', 'source': '~/dotfiles/.ssh/also', 'arcname': 'files/2', 'sha256': 'x'}, *items]
        got = MB._other_keys({'items': both}, stage, home)
        self.assertEqual([k for k in got if k['fingerprint'] == fp_direct],
                         [{'key': 'id_work', 'fingerprint': fp_direct, 'in_ssh': True}])

    def test_a_default_key_in_an_upper_case_folder_keeps_its_steps(self):
        key = self.keys({'~/.SSH/id_ed25519': PRIVATE_HEAD + SECRET_MARKER})['~/.SSH/id_ed25519']
        self.assertEqual(key['steps'], ['project-observatory full ssh-keys rekey id_ed25519',
                                        'project-observatory full ssh-keys rekey id_ed25519 --apply'])
        # ssh offers a default key to every host with no IdentityFile line: rekey names those, never guesses.
        self.assertIn('--server ALIAS', key['note'])

    def test_a_key_in_a_subfolder_is_named_by_its_file_and_made_by_hand(self):
        keys = self.keys({'~/.ssh/keys/work': PRIVATE_HEAD + SECRET_MARKER})
        self.assertEqual((keys['~/.ssh/keys/work']['key'], keys['~/.ssh/keys/work']['steps']), ('work', []))
        self.assertIn('not directly in ~/.ssh', keys['~/.ssh/keys/work']['note'])

    def test_an_identity_file_in_a_subfolder_never_names_a_fragment(self):
        keys = self.keys({'~/.ssh/config': b'Host w\n  IdentityFile ~/.ssh/k/config.local\nInclude config.local\n',
                          '~/.ssh/config.local': b'Host x\n  User y\n'})
        self.assertEqual(keys, {}, 'no key is made over a settings fragment')

    def test_a_name_with_a_space_is_quoted_for_the_shell(self):
        keys = self.keys({'~/.ssh/config': b'Host w\n  IdentityFile = "~/.ssh/work key"\n',
                          '~/.ssh/work key': b'opaque'})
        self.assertEqual(keys['~/.ssh/work key']['steps'], ["project-observatory full ssh-keys rekey 'work key'",
                                                            "project-observatory full ssh-keys rekey 'work key' --apply"])

    def test_one_rekey_step_takes_the_place_of_create_and_send(self):
        # OBS-300: `rekey` makes the key and sends it where the other machine's key is registered.
        keys = self.keys({'~/.ssh/config': b'Host w\n  IdentityFile ~/.ssh/work\nHost gh\n  HostName github.com\n'
                                           b'  IdentityFile ~/.ssh/work\n',
                          '~/.ssh/work': PRIVATE_HEAD + SECRET_MARKER})
        key = keys['~/.ssh/work']
        self.assertEqual(key['steps'], ['project-observatory full ssh-keys rekey work',
                                        'project-observatory full ssh-keys rekey work --apply'],
                         'the plan, then the acting form: rekey without --apply changes nothing')
        self.assertEqual([h['host'] for h in key['hosts']], ['w', 'gh'], 'the hosts stay named for the person')
        self.assertFalse(any(' create ' in s or ' send ' in s for s in key['steps']))

    def test_the_rekey_step_carries_the_other_keys_fingerprint_from_its_public_half(self):
        # A second machine never gets OLD.pub, so rekey alone could not match the key on GitHub.
        line, fp = pub_line(b'alpha-work')
        keys = self.keys({'~/.ssh/config': b'Host gh\n  HostName github.com\n  IdentityFile ~/.ssh/id_work\n',
                          '~/.ssh/id_work': PRIVATE_HEAD + SECRET_MARKER, '~/.ssh/id_work.pub': line.encode()})
        key = keys['~/.ssh/id_work']
        step = f'project-observatory full ssh-keys rekey id_work --fingerprint {fp}'
        self.assertEqual(key['steps'], [step, step + ' --apply'])
        self.assertEqual(key['fingerprint'], fp)
        self.assertNotIn('note', key)

    def test_without_its_fingerprint_the_plan_says_how_github_learns_it(self):
        key = self.keys({'~/.ssh/config': b'Host gh\n  HostName github.com\n  IdentityFile ~/.ssh/id_work\n',
                         '~/.ssh/id_work': PRIVATE_HEAD + SECRET_MARKER})['~/.ssh/id_work']
        self.assertEqual(key['steps'][0], 'project-observatory full ssh-keys rekey id_work')
        self.assertIsNone(key['fingerprint'])
        self.assertIn('ssh-keys list', key['note'])
        self.assertIn('--fingerprint', key['note'])

    def test_a_key_no_host_names_still_gets_its_rekey_step(self):
        # rekey with nothing to send still makes the key under its name and says nothing was found to send to.
        keys = self.keys({'~/.ssh/odd': PRIVATE_HEAD + SECRET_MARKER})
        self.assertEqual(keys['~/.ssh/odd']['steps'], ['project-observatory full ssh-keys rekey odd',
                                                       'project-observatory full ssh-keys rekey odd --apply'])
        self.assertEqual(keys['~/.ssh/odd']['hosts'], [])

    def test_a_host_rekey_would_not_see_is_named_to_it_with_server(self):
        # rekey reaches a Host only through a line naming exactly ~/.ssh/NAME (review of #409, L1).
        config = (b'Host plain\n  IdentityFile ~/.ssh/work\nHost upper\n  IdentityFile ~/.SSH/work\n'
                  b'Host theirs\n  IdentityFile /opt/elsewhere/.ssh/work\n')
        key = self.keys({'~/.ssh/config': config, '~/.ssh/work': PRIVATE_HEAD + SECRET_MARKER})['~/.ssh/work']
        self.assertEqual(key['steps'][0], 'project-observatory full ssh-keys rekey work --server upper --server theirs')
        self.assertEqual(key['steps'][1], key['steps'][0] + ' --apply')
        # OBS-306 (4): the carried line still names another path, so the note says to point it here.
        self.assertIn('Host upper, theirs name this key by another path', key['note'])
        self.assertIn('IdentityFile line at ~/.ssh/work', key['note'])
        left_out = self.keys({'~/.ssh/config': config, '~/.ssh/work': PRIVATE_HEAD + SECRET_MARKER},
                             config_written=False)['~/.ssh/work']
        self.assertNotIn('--server', left_out['steps'][0], 'no config here: an alias would not resolve')
        self.assertIn('Host plain, upper, theirs', left_out['note'])
        self.assertIn('--only=ssh-config', left_out['note'])

    def test_the_fingerprint_is_read_from_the_private_files_clear_header(self):
        # No .pub staged: the OpenSSH container's clear header carries the public key (review of #409, L3).
        private, fp = openssh_private(b'alpha-header')
        key = self.keys({'~/.ssh/id_head': private})['~/.ssh/id_head']
        self.assertEqual(key['fingerprint'], fp)
        self.assertEqual(key['steps'][0], f'project-observatory full ssh-keys rekey id_head --fingerprint {fp}')

    def test_the_emitted_step_parses_as_rekey_with_its_name_and_fingerprint(self):
        line, fp = pub_line(b'alpha-parse')
        key = self.keys({'~/.ssh/config': b'Host up\n  IdentityFile ~/.SSH/id_parse\n',
                         '~/.ssh/id_parse': PRIVATE_HEAD + SECRET_MARKER,
                         '~/.ssh/id_parse.pub': line.encode()})['~/.ssh/id_parse']
        import shlex
        words = shlex.split(key['steps'][1])
        self.assertEqual(words[:3], ['project-observatory', 'full', 'ssh-keys'])
        args = MB._ssh_keys_module().parser().parse_args(words[3:])
        self.assertEqual((args.command, args.old, args.fingerprint, args.server, args.apply),
                         ('rekey', 'id_parse', fp, ['up'], True))


class SecondMachine(Machine):
    """OBS-283: a bundle restored on another machine never writes a private SSH key and never
    loads a job; it stages the jobs and names them in the placement file."""

    def setUp(self):
        super().setUp()
        # What `full ssh-keys create --host` writes: IdentitiesOnly, so only this key is offered (M3).
        self.write('.ssh/config', 'Host example\n  HostName example.invalid\n  IdentityFile ~/.ssh/id_example\n'
                                  '  IdentitiesOnly yes\n\nHost gh\n  HostName github.com\n'
                                  '  IdentityFile ~/.ssh/id_example\n'
                                  'Host tabbed\n\tHostName tabbed.invalid\n\tIdentityFile\t~/.ssh/id_example\n'
                                  'Include config.local\n')
        # A fragment that is a setting, not a key: written, and never offered as a key to create (review of #394).
        self.write('.ssh/config.local', 'Host extra\n  User someone\n')
        self.write('.ssh/known_hosts', 'example.invalid ssh-ed25519 AAAA\n')
        example_line, self.example_fp = pub_line(b'alpha-example')
        self.write('.ssh/id_example.pub', example_line)
        self.write('.ssh/lone.pub', 'ssh-ed25519 AAAA someone-elses\n')
        # An operator's own include into a .ssh folder and into a job folder: the rules go by path.
        self.write('dotfiles/.ssh/deploy_key', PRIVATE_HEAD + SECRET_MARKER, 0o600)
        import plistlib
        self.write('Library/LaunchAgents/nested/com.example.nested.plist',
                   plistlib.dumps({'Label': 'com.example.nested', 'ProgramArguments': ['/usr/bin/true']}))
        self.settings({'include': ['~/.config/example-agent', '~/dotfiles/.ssh/deploy_key',
                                   '~/Library/LaunchAgents/nested/com.example.nested.plist'],
                       'exclude': ['~/.ssh/*.old'], 'roots': ['~/src'], 'limits': {'file_mb': 0.02}})
        # A private key under a name that reads as public: withheld by its content.
        self.write('.ssh/config.d/disguised', PRIVATE_HEAD + SECRET_MARKER, 0o600)
        self.write(job_rel(), job_bytes())
        self.write(str(Path(job_rel()).parent / ('org.project-observatory.0123456789abcdef.maintain.plist'
                                                if sys.platform == 'darwin'
                                                else 'project-observatory-0123456789abcdef-maintain.timer')),
                   b'synthetic own job')
        self.write('fabric-services/alpha-bot.default.json', json.dumps(
            {'protocol': 'fabric-service/0.1', 'id': 'alpha-bot', 'instance': 'default',
             'lifecycle': {'manager': 'launchd', 'label': job_label()}}))
        self.take(at=AT)
        self.bundle = MB.newest(self.ws)
        self.fresh = self.base / 'fresh-user'
        self.fresh.mkdir()

    def on(self, machine):
        return patch.dict(os.environ, {'OBSERVATORY_MACHINE_ID': machine})

    def test_a_different_config_here_is_named_unless_overwrite_replaces_it(self):
        # OBS-306 (3): without --overwrite the restore keeps this machine's own ~/.ssh/config, so rekey would
        # read that one; the plan says so instead of counting the carried hosts as reached.
        (self.fresh / '.ssh').mkdir(mode=0o700)
        (self.fresh / '.ssh/config').write_text('Host mine\n  HostName mine.invalid\n')
        (self.fresh / '.ssh/known_hosts').write_text('mine.invalid ssh-ed25519 BBBB\n')
        with self.on(BETA):
            kept = MB.restore(self.bundle, self.fresh, secret=PASS)
            replaced = MB.restore(self.bundle, self.fresh, secret=PASS, overwrite=True)
        self.assertEqual({r['source']: r for r in kept['plan']}['~/.ssh/config']['state'], 'conflict')
        key = {k['key']: k for k in kept['ssh_keys']}['id_example']
        self.assertIn('differs from the carried one', key['note'])
        self.assertIn('Host example, tabbed', key['note'])
        self.assertIn('--overwrite --only=ssh-config', key['note'])
        self.assertIn('replaces ~/.ssh/config, ~/.ssh/known_hosts (each moved aside)', key['note'],
                      'the note names every ssh-config file the category-wide overwrite would replace here')
        self.assertIn('by hand', key['note'])
        again = {k['key']: k for k in replaced['ssh_keys']}['id_example']
        self.assertNotIn('note', again, 'with --overwrite the carried config is the live one')
        self.assertEqual(again['steps'], key['steps'])

    def test_a_bundle_names_the_machine_it_was_taken_on(self):
        stage, manifest = self.open(self.bundle)
        self.addCleanup(shutil.rmtree, stage, True)
        self.assertEqual(manifest['machine_id'], ALPHA)

    def test_another_machine_gets_no_private_key_and_no_loaded_job(self):
        with self.on(BETA):
            planned = MB.restore(self.bundle, self.fresh, secret=PASS)
            self.assertEqual((planned['machine']['mode'], planned['machine']['why']),
                             ('second-machine', 'another-machine'))
            self.assertEqual((planned['machine']['taken_on'], planned['machine']['this_machine']), (ALPHA, BETA))
            rows = {r['source']: r for r in planned['plan']}
            self.assertEqual(rows['~/.ssh/id_example']['state'], 'withheld')
            self.assertIn('ssh-private', rows['~/.ssh/id_example']['reason'])
            for public in ('~/.ssh/config', '~/.ssh/known_hosts', '~/.ssh/lone.pub'):
                self.assertEqual(rows[public]['state'], 'missing', public)
            self.assertEqual(rows['~/.ssh/id_example.pub']['state'], 'withheld',
                             'its private half stays behind, and it would block `ssh-keys create id_example`')
            self.assertEqual(rows['~/.ssh/config.d/disguised']['state'], 'withheld', 'a private key by its text')
            self.assertEqual(rows['~/dotfiles/.ssh/deploy_key']['state'], 'withheld', 'an include, by its path')
            self.assertTrue(rows['~/Library/LaunchAgents/nested/com.example.nested.plist']['staged'])
            job = rows['~/' + job_rel()]
            self.assertTrue(job['staged'])
            self.assertEqual(job['label'], job_label(), 'the Label, as the placement file names it')
            keys = {k['key']: k for k in planned['ssh_keys']}
            self.assertEqual(set(keys), {'id_example', 'deploy_key'})
            self.assertEqual(keys['deploy_key']['steps'], [], 'outside ~/.ssh: `ssh-keys create` cannot put it there')
            rekey = f'project-observatory full ssh-keys rekey id_example --fingerprint {self.example_fp}'
            self.assertEqual(keys['id_example']['steps'], [rekey, rekey + ' --apply'],
                             'the rekey plan, then the step that makes, sends and verifies')
            self.assertEqual([h['host'] for h in keys['id_example']['hosts']], ['example', 'gh', 'tabbed'])
            self.assertNotIn('config.local', keys, 'a withheld setting is never offered as a key to create')
            nxt = planned['next']
            clone = next(i for i, n in enumerate(nxt) if '--clone' in n)
            self.assertLess(nxt.index(rekey), nxt.index(rekey + ' --apply'))
            self.assertLess(nxt.index(rekey + ' --apply'), clone, 'the key is made before --clone needs it')
            self.assertIn('--apply` steps above', nxt[clone])
            self.assertTrue(job['destination'].startswith('~/.local/share/project-observatory/machine-move/'), job)
            own = [r for r in planned['plan'] if 'project-observatory' in r['source'] and r['kind'] == 'file'
                   and r['source'].startswith('~/' + str(Path(job_rel()).parent))]
            self.assertEqual([r['state'] for r in own], ['withheld'], 'each machine installs its own engine jobs')
            self.assertIn('installs its own', own[0]['reason'])
            self.assertEqual(planned['counts']['withheld'], 6)
            self.assertEqual(planned['counts']['staged'], 2)
            self.assertEqual(list(self.fresh.iterdir()), [], 'the plan writes nothing, the placement file included')
            for step in MB.SECOND_MACHINE_NEXT:
                self.assertIn(step, planned['next'])
            applied = MB.restore(self.bundle, self.fresh, secret=PASS, mode='apply')
        self.assertEqual(applied['applied']['failed'], [])
        self.assertEqual(applied['applied']['withheld'], 6,
                         'id_example by name, its .pub, config.d/disguised by content, deploy_key by path, '
                         'config.local as any other non-public ~/.ssh file, own job')
        self.assertEqual([p.name for p in (self.fresh / job_rel()).parent.iterdir()]
                         if (self.fresh / job_rel()).parent.exists() else [], [])
        self.assertFalse((self.fresh / '.ssh/id_example').exists())
        self.assertFalse((self.fresh / '.ssh/config.d/disguised').exists())
        self.assertFalse((self.fresh / '.ssh/config.local').exists(), 'a missing Include file is skipped by ssh')
        self.assertFalse((self.fresh / '.ssh/id_example.pub').exists())
        self.assertFalse((self.fresh / 'dotfiles/.ssh/deploy_key').exists())
        self.assertFalse((self.fresh / 'Library/LaunchAgents/nested').exists())
        for public in ('config', 'known_hosts', 'lone.pub'):
            self.assertTrue((self.fresh / '.ssh' / public).is_file(), public)
        for p in self.fresh.rglob('*'):
            if p.is_file() and not p.is_symlink():
                self.assertNotIn(SECRET_MARKER, p.read_bytes(), str(p))
        self.assertFalse((self.fresh / job_rel()).exists(), 'nothing loads the job at the next login')
        system = 'launchd' if sys.platform == 'darwin' else 'systemd'
        staged = MI.staging_dir(self.fresh, system) / Path(job_rel()).name
        self.assertEqual(staged.read_bytes(), job_bytes())
        placement = MI.placement_path(self.fresh)
        self.assertEqual(stat.S_IMODE(placement.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(placement.parent.stat().st_mode), 0o700)
        doc = json.loads(placement.read_text())
        self.assertEqual((doc['schema'], doc['machine']), ('observatory-placement/1', BETA))
        nested = 'com.example.nested'
        self.assertEqual(sorted(([{k: e[k] for k in ('label', 'serviceId', 'placedOn', 'by')} for e in doc['elsewhere']]),
                                key=lambda e: e['label']),
                         sorted([{'label': job_label(), 'serviceId': 'alpha-bot.default', 'placedOn': ALPHA,
                                  'by': 'machine-restore'},
                                 {'label': nested, 'serviceId': None, 'placedOn': ALPHA, 'by': 'machine-restore'}],
                                key=lambda e: e['label']))
        self.assertTrue(doc['updatedAt'] and doc['elsewhere'][0]['since'])
        self.assertEqual(applied['placement']['elsewhere'], sorted([job_label(), nested]))
        self.assertFalse([p for p in placement.parent.iterdir() if p.name.endswith('.tmp')], 'written atomically')

    def test_the_same_machine_keeps_todays_behaviour(self):
        planned = MB.restore(self.bundle, self.fresh, secret=PASS)
        self.assertEqual((planned['machine']['mode'], planned['machine']['why']), ('same-machine', 'same-machine'))
        self.assertEqual(set(planned['counts']), {'missing', 'same', 'conflict'})
        self.assertFalse([r for r in planned['plan'] if r.get('staged') or r['state'] == 'withheld'])
        self.assertNotIn(MB.SECOND_MACHINE_NEXT[0], planned['next'])
        applied = MB.restore(self.bundle, self.fresh, secret=PASS, mode='apply')
        self.assertNotIn('withheld', applied['applied'])
        self.assertEqual((self.fresh / '.ssh/id_example').read_bytes(), SECRET_MARKER)
        self.assertEqual((self.fresh / job_rel()).read_bytes(), job_bytes())
        self.assertFalse(MI.placement_path(self.fresh).exists(), 'a same-machine restore writes no placement file')

    def test_a_bundle_that_names_no_machine_is_another_machines(self):
        with patch.object(MB.machine_identity, 'machine_id', lambda home=None, **kw: None):
            self.take(at=AT + datetime.timedelta(minutes=1))
        old = MB.newest(self.ws)
        planned = MB.restore(old, self.fresh, secret=PASS)
        self.assertEqual((planned['machine']['mode'], planned['machine']['why'], planned['machine']['taken_on']),
                         ('second-machine', 'names-no-machine', None))
        MB.restore(old, self.fresh, secret=PASS, mode='apply')
        self.assertFalse((self.fresh / '.ssh/id_example').exists())
        doc = json.loads(MI.placement_path(self.fresh).read_text())
        self.assertEqual(doc['elsewhere'][0]['placedOn'], 'unknown')
        whole = self.base / 'whole-user'
        whole.mkdir()
        MB.restore(old, whole, secret=PASS, mode='apply', machine='same')
        self.assertEqual((whole / '.ssh/id_example').read_bytes(), SECRET_MARKER)

    def test_second_machine_can_be_asked_for_on_the_same_machine(self):
        planned = MB.restore(self.bundle, self.fresh, secret=PASS, machine='second')
        self.assertEqual((planned['machine']['mode'], planned['machine']['why']), ('second-machine', 'asked'))

    def test_a_job_this_machine_already_defines_is_not_named_elsewhere(self):
        self.write_to(self.fresh, job_rel(), job_bytes())
        with self.on(BETA):
            applied = MB.restore(self.bundle, self.fresh, secret=PASS, mode='apply')
        self.assertEqual((applied['placement']['elsewhere'], applied['placement']['already_here']), (['com.example.nested'], [job_label()]))

    def test_a_placement_file_that_cannot_be_written_exits_non_zero(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {'HOME': str(self.fresh), vault.PASS_ENV: PASS, 'OBSERVATORY_MACHINE_ID': BETA}), \
                patch.object(MI, 'place_elsewhere', side_effect=OSError('read-only')), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(MB.restore_main(['--latest', '--apply']), 1)
        self.assertIn('placement file', err.getvalue())

    def test_only_writes_the_named_categories_and_the_plan_says_what_each_would_write(self):
        with self.on(BETA):
            planned = MB.restore(self.bundle, self.fresh, secret=PASS, only=['git', 'shell'])
            cats = planned['categories']
            self.assertEqual(list(cats), list(MB.CATEGORIES))
            self.assertTrue(cats['git']['selected'] and cats['shell']['selected'])
            self.assertFalse(cats['claude']['selected'])
            self.assertIn('~/.gitconfig', cats['git']['writes'])
            self.assertIn('~/.zshrc', cats['shell']['writes'])
            self.assertEqual(cats['claude']['would_write'], 0)
            self.assertGreater(cats['claude']['left_out'], 0)
            rows = {r['source']: r for r in planned['plan']}
            self.assertEqual((rows['~/.claude/settings.json']['state'], rows['~/.claude/settings.json']['category']),
                             ('left-out', 'claude'))
            self.assertEqual(rows['~/.gitconfig']['category'], 'git')
            self.assertEqual(rows['~/.config/example-agent/agent.toml']['category'], 'operator')
            self.assertEqual(rows['~/.ssh/config']['category'], 'ssh-config')
            self.assertEqual(rows['~/' + job_rel()]['category'], 'launchd')
            self.assertEqual(rows['~/fabric-services/alpha-bot.default.json']['category'], 'fabric-services')
            self.assertEqual(planned['counts']['left_out'], sum(1 for r in planned['plan'] if r['state'] == 'left-out'))
            self.assertEqual(planned['selection'], {'only': ['git', 'shell'], 'skip': [],
                                                    'left_out': [c for c in MB.CATEGORIES if c not in ('git', 'shell')]})
            self.assertIn("project-observatory full machine-restore --apply --only=git,shell  (writes only what is missing)",
                          planned['next'])
            self.assertNotIn('project-observatory full machine-restore --packages  (Homebrew, npm, pipx, uv)',
                             planned['next'], 'packages is left out')
            self.assertEqual(list(self.fresh.iterdir()), [], 'the plan writes nothing')
            applied = MB.restore(self.bundle, self.fresh, secret=PASS, mode='apply', only=['git,shell'])
        self.assertEqual(applied['applied']['failed'], [])
        self.assertGreater(applied['applied']['left_out'], 0)
        self.assertTrue((self.fresh / '.gitconfig').is_file())
        self.assertTrue((self.fresh / '.zshrc').is_file())
        self.assertFalse((self.fresh / '.claude').exists())
        self.assertFalse((self.fresh / '.ssh').exists())
        system = 'launchd' if sys.platform == 'darwin' else 'systemd'
        self.assertFalse(MI.staging_dir(self.fresh, system).exists())
        self.assertFalse(MI.placement_path(self.fresh).exists(), 'no job was staged, so none is named elsewhere')
        self.assertNotIn('placement', applied)

    def test_skip_leaves_out_what_it_names(self):
        with self.on(BETA):
            applied = MB.restore(self.bundle, self.fresh, secret=PASS, mode='apply', skip=['launchd', 'claude'])
        self.assertEqual(applied['applied']['failed'], [])
        self.assertTrue((self.fresh / '.ssh/config').is_file())
        self.assertTrue((self.fresh / '.gitconfig').is_file())
        self.assertFalse((self.fresh / '.claude').exists())
        system = 'launchd' if sys.platform == 'darwin' else 'systemd'
        self.assertFalse(MI.staging_dir(self.fresh, system).exists())
        self.assertFalse(applied['categories']['launchd']['selected'])
        self.assertNotIn('placement', applied)

    def test_ssh_private_stays_withheld_on_a_second_machine_whatever_only_says(self):
        with self.on(BETA):
            applied = MB.restore(self.bundle, self.fresh, secret=PASS, mode='apply', only=['ssh-private'])
        self.assertFalse((self.fresh / '.ssh/id_example').exists())
        self.assertFalse((self.fresh / '.ssh/config.d/disguised').exists())
        for p in self.fresh.rglob('*'):
            if p.is_file() and not p.is_symlink():
                self.assertNotIn(SECRET_MARKER, p.read_bytes(), str(p))
        self.assertTrue(any('ssh-private' in n and '--same-machine' in n for n in applied['notes']), applied['notes'])
        self.assertEqual(applied['categories']['ssh-private']['would_write'], 0)
        self.assertGreater(applied['categories']['ssh-private']['withheld'], 0)

    def test_the_same_machine_restores_only_the_keys_when_asked(self):
        applied = MB.restore(self.bundle, self.fresh, secret=PASS, mode='apply', only=['ssh-private'])
        self.assertEqual(applied['machine']['mode'], 'same-machine')
        self.assertEqual(applied['applied']['failed'], [])
        self.assertEqual((self.fresh / '.ssh/id_example').read_bytes(), SECRET_MARKER)
        self.assertEqual(stat.S_IMODE((self.fresh / '.ssh').stat().st_mode), 0o700, 'the folder keeps its mode')
        self.assertEqual(stat.S_IMODE((self.fresh / '.ssh/id_example').stat().st_mode), 0o600)
        self.assertTrue((self.fresh / '.ssh/id_example.pub').is_file(), 'a key comes with its public half')
        self.assertTrue((self.fresh / '.ssh/config.d/disguised').is_file(), 'a private key by its text')
        for other in ('.ssh/config', '.ssh/known_hosts', '.ssh/lone.pub', '.gitconfig', '.zshrc'):
            self.assertFalse((self.fresh / other).exists(), other)
        skipped = self.base / 'skip-user'
        skipped.mkdir()
        MB.restore(self.bundle, skipped, secret=PASS, mode='apply', skip=['ssh-private'])
        self.assertFalse((skipped / '.ssh/id_example').exists())
        self.assertFalse((skipped / '.ssh/config.d/disguised').exists(), 'withheld by its text as well')
        self.assertTrue((skipped / '.ssh/config').is_file())

    def test_a_category_is_checked_before_anything_is_read(self):
        with self.assertRaisesRegex(MB.MachineBackupError, 'unknown category .nope.; the categories are ssh-private'):
            MB.restore(self.bundle, self.fresh, secret=PASS, only=['nope'])
        with self.assertRaisesRegex(MB.MachineBackupError, 'not both'):
            MB.restore(self.bundle, self.fresh, secret=PASS, only=['git'], skip=['shell'])
        with self.assertRaisesRegex(MB.MachineBackupError, '--packages installs the `packages` category'):
            MB.restore(self.bundle, self.fresh, secret=PASS, do_packages=True, skip=['packages'])
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {'HOME': str(self.fresh), vault.PASS_ENV: PASS}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(MB.restore_main(['--latest', '--packages', '--only', 'git']), 2)
            self.assertIn('`packages` category', err.getvalue())
        sys.path.insert(0, str(ROOT))
        import observatory as cli
        refused = cli.refusal(['machine-restore', '--latest', '--only', 'nope'])
        self.assertIn('unknown category', refused)
        self.assertIn('fabric-services', refused, 'the refusal names the valid ones')
        self.assertNotEqual(cli.refusal(['machine-restore', '--latest', '--only', 'git', '--skip', 'shell']), '')
        self.assertEqual(cli.refusal(['machine-restore', '--latest', '--apply', '--only', 'git,shell', '--only',
                                      'claude', '--clone', '--projects', 'alpha-*']), '')
        self.assertEqual(list(self.fresh.iterdir()), [])

    def test_a_receipt_names_the_restore_and_the_other_machines_keys(self):
        line, fp = pub_line(b'alpha-work')
        self.write('.ssh/id_work', PRIVATE_HEAD + SECRET_MARKER, 0o600)
        self.write('.ssh/id_work.pub', line)
        self.take(at=AT + datetime.timedelta(minutes=1))
        bundle = MB.newest(self.ws)
        with self.on(BETA):
            planned = MB.restore(bundle, self.fresh, secret=PASS)
            self.assertNotIn('receipt', planned, 'a plan writes no receipt')
            self.assertFalse(MI.restore_receipt_path(self.fresh).exists())
            applied = MB.restore(bundle, self.fresh, secret=PASS, mode='apply', skip=['claude'])
            receipt = MI.read_restore_receipt(self.fresh)
        path = MI.restore_receipt_path(self.fresh)
        self.assertEqual(applied['receipt'], '~/.local/share/project-observatory/machine-move/restore.json')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        part = receipt['apply']
        self.assertEqual((receipt['schema'], receipt['machine']), ('observatory-machine-restore/1', BETA))
        self.assertEqual((part['mode'], part['taken_on'], part['this_machine']), ('second-machine', ALPHA, BETA))
        self.assertEqual((part['skip'], part['only'], part['failed']), (['claude'], [], 0))
        self.assertIn('claude', part['left_out'])
        self.assertIn({'key': 'id_work', 'fingerprint': fp, 'in_ssh': True}, part['other_keys'])
        self.assertNotIn(SECRET_MARKER.decode(), path.read_text())
        self.assertNotIn('PRIVATE', path.read_text())

    def write_to(self, home, rel, data):
        p = home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def test_the_command_line_takes_the_machine_flags_and_same_machine_needs_a_terminal(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {'HOME': str(self.fresh), vault.PASS_ENV: PASS, 'OBSERVATORY_MACHINE_ID': BETA}), \
                patch.object(sys, 'stdin', io.StringIO('')), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(MB.restore_main(['--latest', '--same-machine']), 2)
            self.assertIn('a terminal', err.getvalue())
            self.assertEqual(MB.restore_main(['--latest']), 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())['machine']['mode'], 'second-machine')
        self.assertEqual(list(self.fresh.iterdir()), [])
        sys.path.insert(0, str(ROOT))
        import observatory as cli
        self.assertNotEqual(cli.refusal(['machine-restore', '--latest', '--same-machine', '--second-machine']), '')
        self.assertEqual(cli.refusal(['machine-restore', '--latest', '--apply', '--second-machine']), '')


def YES(label, machine):
    """The person typed the label back: `leave` ran on the other machine."""
    return True


class PlanByPath(unittest.TestCase):
    """OBS-283: the second-machine rules go by the path an item is written to, whatever its category."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()

    def rows(self, items):
        return {r['source']: r for r in MB.plan({'items': items}, None, self.home, second=True)}

    def test_a_linked_job_folder_or_ssh_folder_is_never_written_as_a_link(self):
        rows = self.rows([{'kind': 'link', 'source': '~/Library/LaunchAgents', 'target': '~/dotfiles/agents',
                           'category': 'launchd'},
                          {'kind': 'link', 'source': '~/.config/systemd/user', 'target': '~/dotfiles/units',
                           'category': 'systemd'},
                          {'kind': 'link', 'source': '~/.ssh', 'target': '~/dotfiles/ssh', 'category': 'ssh'},
                          {'kind': 'link', 'source': '~/.ssh/id_work', 'target': '~/keys/id_work', 'category': 'ssh'},
                          {'kind': 'link', 'source': '~/.ssh/config', 'target': '~/dotfiles/ssh_config',
                           'category': 'ssh'}])
        for source in ('~/Library/LaunchAgents', '~/.config/systemd/user', '~/.ssh', '~/.ssh/id_work'):
            self.assertEqual(rows[source]['state'], 'withheld', source)
        self.assertEqual(rows['~/.ssh/config']['state'], 'missing', 'a link named config leads to settings')

    def test_an_absolute_path_from_another_home_is_held_to_the_same_rules(self):
        if sys.platform == 'win32':
            # A POSIX absolute path names no place on Windows: the manifest refuses it as unsafe,
            # so nothing of another home's .ssh or jobs is written there at all.
            with self.assertRaisesRegex(MB.MachineBackupError, 'unsafe path'):
                self.rows([{'kind': 'file', 'source': '/var/empty/example-account/.ssh/id_old', 'arcname': 'abs/x',
                            'sha256': '0', 'category': 'operator'}])
            return
        rows = self.rows([{'kind': 'file', 'source': '/var/empty/example-account/.ssh/id_old', 'arcname': 'abs/x',
                           'sha256': '0', 'category': 'operator'},
                          {'kind': 'file', 'source': '/var/empty/example-account/Library/LaunchAgents/com.example.a.plist',
                           'arcname': 'abs/y', 'sha256': '0', 'category': 'operator'}])
        self.assertEqual(rows['/var/empty/example-account/.ssh/id_old']['state'], 'withheld')
        self.assertTrue(rows['/var/empty/example-account/Library/LaunchAgents/com.example.a.plist']['staged'])


class Recorder:
    """A stand-in for subprocess.run: records each argv, answers from `codes` by its first two words."""

    def __init__(self, codes=None):
        self.calls, self.codes = [], codes or {}

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        code = self.codes.get(tuple(argv[:2]), 0)
        if callable(code):
            code = code()
        return subprocess.CompletedProcess(argv, code, '', 'synthetic refusal' if code else '')


@unittest.skipIf(sys.platform == 'win32', 'no launchd or systemd user jobs on Windows')
class MachineMove(Base):
    """OBS-283: one job moves between two machines; the placement file follows it."""

    def setUp(self):
        super().setUp()
        self.home = self.user
        self.write('fabric-services/alpha-bot.default.json', json.dumps(
            {'protocol': 'fabric-service/0.1', 'id': 'alpha-bot', 'instance': 'default',
             'lifecycle': {'manager': 'launchd', 'label': JOB_LABEL}}))
        self.allow = patch.dict(os.environ, {'OBSERVATORY_SYSTEM_SETUP': '1'})

    def plist(self):
        import plistlib
        return plistlib.dumps({'Label': JOB_LABEL, 'ProgramArguments': ['/usr/bin/true']})

    def stage(self, system='launchd', name=f'{JOB_LABEL}.plist', data=None):
        folder = MI.staging_dir(self.home, system)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data if data is not None else self.plist())
        MI.place_elsewhere([{'label': JOB_LABEL, 'placedOn': ALPHA, 'by': 'machine-restore'}], self.home)
        return folder / name

    def test_cutover_loads_the_staged_job_here_and_clears_it_from_elsewhere(self):
        self.stage()
        run = Recorder({('launchctl', 'print'): 113})
        with self.allow:
            result = MM.cutover(JOB_LABEL, self.home, runner=run, platform='darwin', uid=501, confirm=YES)
        live = self.home / 'Library/LaunchAgents' / f'{JOB_LABEL}.plist'
        self.assertEqual(live.read_bytes(), self.plist())
        self.assertEqual(list(MI.staging_dir(self.home).iterdir()), [])
        self.assertIn(['launchctl', 'enable', f'gui/501/{JOB_LABEL}'], run.calls)
        self.assertIn(['launchctl', 'bootstrap', 'gui/501', str(live)], run.calls)
        self.assertEqual(json.loads(MI.placement_path(self.home).read_text())['elsewhere'], [])
        self.assertEqual((result['load'], result['was_elsewhere']), ('loaded', True))
        self.assertIn(f'machine-move leave {JOB_LABEL} --to {ALPHA}', result['next'][0])

    def test_a_failed_load_puts_the_job_back_in_staging(self):
        staged = self.stage()
        run = Recorder({('launchctl', 'print'): 113, ('launchctl', 'bootstrap'): 5})
        with self.allow, self.assertRaises(MM.MachineMoveFailed):
            MM.cutover(JOB_LABEL, self.home, runner=run, platform='darwin', uid=501, confirm=YES)
        self.assertTrue(staged.is_file())
        self.assertFalse((self.home / 'Library/LaunchAgents' / staged.name).exists())
        self.assertEqual([e['label'] for e in json.loads(MI.placement_path(self.home).read_text())['elsewhere']],
                         [JOB_LABEL])

    def test_a_different_definition_already_here_is_never_overwritten(self):
        self.stage()
        live = self.home / 'Library/LaunchAgents' / f'{JOB_LABEL}.plist'
        live.parent.mkdir(parents=True)
        live.write_bytes(b'mine')
        with self.allow, self.assertRaisesRegex(MM.MachineMoveError, 'differs'):
            MM.cutover(JOB_LABEL, self.home, runner=Recorder(), platform='darwin', uid=501, confirm=YES)
        self.assertEqual(live.read_bytes(), b'mine')

    # OBS-390: a definition staged by a restore days ago may name a release folder that a newer release
    # has since replaced; cutover refuses it by name instead of loading a job that cannot start.
    def test_a_stale_staged_plist_is_refused_before_anything_moves(self):
        import plistlib
        gone = self.base / 'releases' / 'old-release' / 'bin' / 'run'
        cases = {'program': ({'ProgramArguments': [str(gone), '--serve']}, str(gone)),
                 'Program key': ({'Program': str(gone), 'ProgramArguments': [str(gone)]}, str(gone)),
                 'working directory': ({'ProgramArguments': ['/usr/bin/true'],
                                        'WorkingDirectory': str(self.base / 'releases' / 'old-release')},
                                       str(self.base / 'releases' / 'old-release'))}
        for name, (doc, missing) in cases.items():
            with self.subTest(name):
                staged = self.stage(data=plistlib.dumps({'Label': JOB_LABEL, **doc}))
                run = Recorder({('launchctl', 'print'): 113})
                asked = []
                with self.allow, self.assertRaises(MM.MachineMoveError) as caught:
                    MM.cutover(JOB_LABEL, self.home, runner=run, platform='darwin', uid=501,
                               confirm=lambda *a: asked.append(a) or True)
                message = str(caught.exception)
                self.assertIn('staged-plist-stale', message)
                self.assertIn(missing, message)
                self.assertIn(str(MI.staging_dir(self.home)), message, 'the remedy names the staging folder')
                self.assertIn('cutover again', message)
                self.assertEqual(asked, [], 'refused before the person is asked')
                self.assertTrue(staged.is_file(), 'the staged definition stays where it was')
                self.assertFalse((self.home / 'Library/LaunchAgents' / staged.name).exists())
                self.assertNotIn('bootstrap', [c[1] for c in run.calls])
                self.assertEqual([e['label'] for e in MI.read_placement(self.home)['elsewhere']], [JOB_LABEL])
                staged.unlink()

    def test_a_fresh_staged_plist_whose_paths_exist_is_loaded(self):
        import plistlib
        release = self.base / 'releases' / 'new-release'
        (release / 'bin').mkdir(parents=True)
        program = release / 'bin' / 'run'
        program.write_text('#!/bin/sh\n')
        program.chmod(0o755)
        data = plistlib.dumps({'Label': JOB_LABEL, 'ProgramArguments': [str(program)], 'WorkingDirectory': str(release)})
        self.stage(data=data)
        run = Recorder({('launchctl', 'print'): 113})
        with self.allow:
            result = MM.cutover(JOB_LABEL, self.home, runner=run, platform='darwin', uid=501, confirm=YES)
        self.assertEqual(result['load'], 'loaded')
        self.assertEqual((self.home / 'Library/LaunchAgents' / f'{JOB_LABEL}.plist').read_bytes(), data)

    def test_a_definition_already_in_the_job_folder_is_not_judged_as_staged(self):
        # Only a staged copy can be days old; this machine's own definition keeps today's path.
        import plistlib
        live = self.home / 'Library/LaunchAgents' / f'{JOB_LABEL}.plist'
        live.parent.mkdir(parents=True)
        live.write_bytes(plistlib.dumps({'Label': JOB_LABEL, 'ProgramArguments': [str(self.base / 'absent')]}))
        MI.place_elsewhere([{'label': JOB_LABEL, 'placedOn': ALPHA, 'by': 'machine-restore'}], self.home)
        result = MM.cutover(JOB_LABEL, self.home, runner=Recorder(), platform='darwin', uid=501, confirm=YES)
        self.assertEqual(result['definition'], str(live))

    def test_leave_stops_the_job_stages_it_and_names_where_it_runs(self):
        live = self.home / 'Library/LaunchAgents' / f'{JOB_LABEL}.plist'
        live.parent.mkdir(parents=True)
        live.write_bytes(self.plist())
        loaded = iter([0, 113])
        run = Recorder({('launchctl', 'print'): lambda: next(loaded)})
        with self.allow:
            result = MM.leave(JOB_LABEL, BETA, self.home, runner=run, platform='darwin', uid=501)
        self.assertIn(['launchctl', 'bootout', f'gui/501/{JOB_LABEL}'], run.calls)
        self.assertFalse(live.exists(), 'the next login does not start it')
        self.assertTrue((MI.staging_dir(self.home) / live.name).is_file())
        doc = json.loads(MI.placement_path(self.home).read_text())
        self.assertEqual(doc['machine'], ALPHA)
        self.assertEqual([{k: e[k] for k in ('label', 'serviceId', 'placedOn', 'by')} for e in doc['elsewhere']],
                         [{'label': JOB_LABEL, 'serviceId': 'alpha-bot.default', 'placedOn': BETA,
                           'by': 'machine-move leave'}])
        self.assertEqual((result['stop'], result['next']), ('stopped', [f'on {BETA}: project-observatory full '
                                                                         f'machine-move cutover {JOB_LABEL}']))

    def test_a_job_that_will_not_stop_is_not_moved(self):
        live = self.home / 'Library/LaunchAgents' / f'{JOB_LABEL}.plist'
        live.parent.mkdir(parents=True)
        live.write_bytes(self.plist())
        run = Recorder({('launchctl', 'bootout'): 1})
        with self.allow, self.assertRaisesRegex(MM.MachineMoveError, 'still running'):
            MM.leave(JOB_LABEL, BETA, self.home, runner=run, platform='darwin', uid=501)
        self.assertTrue(live.is_file())
        self.assertFalse(MI.placement_path(self.home).exists())

    def test_refusals_name_their_reason(self):
        for call, reason in ((lambda: MM.leave(JOB_LABEL, ALPHA, self.home, runner=Recorder(), platform='darwin'),
                              'names this machine'),
                             (lambda: MM.leave(JOB_LABEL, 'x', self.home, runner=Recorder(), platform='darwin'),
                              'other machine'),
                             (lambda: MM.leave('bad label!', BETA, self.home, runner=Recorder(), platform='darwin'),
                              'label'),
                             (lambda: MM.cutover(JOB_LABEL, self.home, runner=Recorder(), platform='darwin'),
                              'no job'),
                             (lambda: MM.cutover(JOB_LABEL, self.home, runner=Recorder(), platform='win32'),
                              'Windows')):
            with self.subTest(reason=reason), self.assertRaisesRegex(MM.MachineMoveError, reason):
                call()

    def test_cutover_asks_that_leave_ran_on_the_other_machine(self):
        staged = self.stage()
        with self.allow, self.assertRaisesRegex(MM.MachineMoveError, 'not confirmed'):
            MM.cutover(JOB_LABEL, self.home, runner=Recorder(), platform='darwin', uid=501,
                       confirm=lambda label, machine: False)
        with self.allow, patch.object(sys, 'stdin', io.StringIO('')), \
                self.assertRaisesRegex(MM.MachineMoveError, 'terminal'):
            MM.cutover(JOB_LABEL, self.home, runner=Recorder(), platform='darwin', uid=501)
        self.assertTrue(staged.is_file(), 'nothing moved')
        with patch.object(MM.osprivacy, 'is_terminal', return_value=True), \
                patch('builtins.input', return_value=JOB_LABEL), contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(MM.ask_left(JOB_LABEL, BETA))
        with patch.object(MM.osprivacy, 'is_terminal', return_value=True), \
                patch('builtins.input', return_value='yes'), contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(MM.ask_left(JOB_LABEL, BETA), 'only the label typed back confirms')

    def test_a_failed_cutover_of_this_machines_own_job_leaves_it_where_it_was(self):
        live = self.home / 'Library/LaunchAgents' / f'{JOB_LABEL}.plist'
        live.parent.mkdir(parents=True)
        live.write_bytes(self.plist())
        run = Recorder({('launchctl', 'print'): 113, ('launchctl', 'bootstrap'): 5})
        with self.allow, self.assertRaisesRegex(MM.MachineMoveFailed, 'stays where it was'):
            MM.cutover(JOB_LABEL, self.home, runner=run, platform='darwin', uid=501, confirm=YES)
        self.assertTrue(live.is_file())
        self.assertFalse(MI.staging_dir(self.home).exists())

    def test_the_placement_file_loses_no_change_when_two_write_at_once(self):
        import threading
        labels = [f'com.example.job-{n}' for n in range(16)]
        threads = [threading.Thread(target=MI.place_elsewhere, args=([{'label': lab, 'placedOn': BETA}], self.home))
                   for lab in labels]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(e['label'] for e in MI.read_placement(self.home)['elsewhere']), sorted(labels))

    def test_systemd_off_macos(self):
        self.stage('systemd', f'{JOB_LABEL}.service', b'[Service]\nExecStart=/usr/bin/true\n')
        run = Recorder()
        with self.allow:
            MM.cutover(f'{JOB_LABEL}.service', self.home, runner=run, platform='linux', uid=1000, confirm=YES)
        self.assertTrue((self.home / '.config/systemd/user' / f'{JOB_LABEL}.service').is_file())
        self.assertEqual(run.calls, [['systemctl', '--user', 'daemon-reload'],
                                     ['systemctl', '--user', 'enable', '--now', f'{JOB_LABEL}.service']])

    def test_a_sandbox_moves_files_and_never_calls_the_job_system(self):
        self.stage()
        run = Recorder()
        result = MM.cutover(JOB_LABEL, self.home, runner=run, platform='darwin', uid=501, confirm=YES)
        self.assertEqual((run.calls, result['load']), ([], 'skipped: OBSERVATORY_SYSTEM_SETUP=0'))

    def test_show_names_this_machine_and_the_staged_jobs(self):
        self.stage()
        shown = MM.show(self.home, platform='darwin')
        self.assertEqual(shown['machine'], ALPHA)
        self.assertEqual(shown['staged'], [{'label': JOB_LABEL, 'file': f'{JOB_LABEL}.plist'}])
        self.assertEqual([e['label'] for e in shown['elsewhere']], [JOB_LABEL])

    def test_a_placement_file_that_is_not_one_is_refused_not_replaced(self):
        path = MI.placement_path(self.home)
        path.parent.mkdir(parents=True)
        path.write_text('{"schema": "something-else"}')
        with self.assertRaisesRegex(MI.MachineIdentityError, 'move it aside'):
            MI.place_elsewhere([{'label': JOB_LABEL}], self.home)
        self.assertEqual(path.read_text(), '{"schema": "something-else"}')

    def test_the_full_cli_routes_machine_move(self):
        sys.path.insert(0, str(ROOT))
        import observatory as cli
        self.assertIn('machine-move', cli.WORKSPACE_COMMANDS)
        self.assertIn('machine-move', cli.PUBLIC_HELP)
        self.assertEqual(cli.refusal(['machine-move', 'cutover', JOB_LABEL]), '')
        self.assertNotEqual(cli.refusal(['machine-move', 'leave', JOB_LABEL]), '', '--to is required')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(workspace.main(['machine-move', 'show']), 0)
        self.assertEqual(json.loads(out.getvalue())['machine'], ALPHA)


class Launchd:
    """A stand-in for subprocess.run for launchctl alone: `print gui/UID` answers `gui` (112 is launchctl's
    "could not find domain", 125 "Domain does not support specified action"), `print gui/UID/LABEL` 113, `print system/LABEL` 0 for a label in `system`."""

    def __init__(self, gui=112, system=()):
        self.calls, self.gui, self.system = [], gui, set(system)

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        code = 0
        if argv[:2] == ['launchctl', 'print']:
            target = argv[2]
            if target.startswith('system/'):
                code = 0 if target[len('system/'):] in self.system else 113
            elif target.count('/') == 1:
                code = self.gui
            else:
                code = 113
        return subprocess.CompletedProcess(argv, code, '', '')


@unittest.skipIf(sys.platform == 'win32', 'no launchd jobs on Windows')
class HeadlessSpace(Base):
    """OBS-361 part 1: an account with no GUI session runs its jobs as system daemons root loads."""

    UID = 501

    def setUp(self):
        super().setUp()
        self.home = self.user
        self.sysroot = self.base / 'system'
        self.allow = patch.dict(os.environ, {'OBSERVATORY_SYSTEM_SETUP': '1',
                                             'OBSERVATORY_SPACE_DAEMONS_FOR_TESTS': '1',
                                             'OBSERVATORY_SPACE_DAEMONS_TEST_ROOT': str(self.sysroot)})
        self.allow.start()
        self.addCleanup(self.allow.stop)
        name = patch.object(MM, 'account_name', return_value='space')
        name.start()
        self.addCleanup(name.stop)

    def plist(self):
        import plistlib
        return plistlib.dumps({'Label': JOB_LABEL, 'ProgramArguments': ['/usr/bin/true']})

    def stage(self):
        folder = MI.staging_dir(self.home)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f'{JOB_LABEL}.plist').write_bytes(self.plist())
        MI.place_elsewhere([{'label': JOB_LABEL, 'placedOn': BETA, 'by': 'machine-restore'}], self.home)
        return folder / f'{JOB_LABEL}.plist'

    def register(self, uid=UID, label=JOB_LABEL):
        path = self.sysroot / 'Library/Application Support/ProjectObservatory/space-daemons.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'schema': 'observatory-space-daemons/1', 'entries': [
            {'label': label, 'user': 'space', 'uid': uid, 'source': '/var/empty/space/x.plist', 'sourceSha256': '0',
             'written': f'/Library/LaunchDaemons/{label}.plist', 'writtenSha256': '0',
             'at': '2026-11-07T00:00:00Z'}]}))

    def cutover(self, run):
        return MM.cutover(JOB_LABEL, self.home, runner=run, platform='darwin', uid=self.UID, confirm=YES)

    def test_without_a_gui_session_cutover_places_the_job_and_waits_for_root(self):
        staged = self.stage()
        run = Launchd()
        result = self.cutover(run)
        background = MI.background_dir(self.home) / staged.name
        self.assertEqual(background.read_bytes(), self.plist())
        self.assertEqual(stat.S_IMODE(background.stat().st_mode) & 0o022, 0, 'root reads it only unwritable by others')
        self.assertEqual(stat.S_IMODE(background.parent.stat().st_mode), 0o700)
        self.assertFalse(staged.exists())
        self.assertFalse((self.home / 'Library/LaunchAgents' / staged.name).exists())
        self.assertNotIn('bootstrap', [c[1] for c in run.calls], 'no agent load is tried in a domain that is absent')
        step = (f'sudo /usr/bin/python3 -I /usr/local/libexec/observatory-space-daemons apply --user space '
                f'--label {JOB_LABEL}')
        self.assertEqual((result['state'], result['load'], result['domain']),
                         ('waiting-for-root', 'waiting-for-root', 'system'))
        self.assertEqual(result['next'][0], step)
        install = '\n'.join(result['human_steps'][:-1])
        self.assertEqual(result['human_steps'][:-1], MM.install_steps())
        for part in ("gh release download vX.Y.Z -R passioncode-ai/project-observatory-dashboard -p 'SHA256SUMS*' "
                     "-p '*.whl'", 'curl -LO https://github.com/passioncode-ai/project-observatory-dashboard/releases/'
                     'download/vX.Y.Z/SHA256SUMS', 'shasum -a 256 -c SHA256SUMS --ignore-missing',
                     'passioncode-release-signing.asc', 'gpg is not on macOS by default',
                     'unzip -p project_observatory-X.Y.Z-py3-none-any.whl observatory/engine/tools/space_daemons.py',
                     'sudo install -d -o root -g wheel -m 0755 /usr/local/libexec',
                     'sudo install -o root -g wheel -m 0755 space_daemons.py '
                     '/usr/local/libexec/observatory-space-daemons', 'OWN account', 'project-observatory full-path'):
            self.assertIn(part, install)
        self.assertIn(step, result['human_steps'][-1])
        self.assertTrue(result['was_elsewhere'])
        self.assertEqual(MI.read_placement(self.home)['elsewhere'], [], 'placed here, as a cutover that loads')
        self.assertEqual(MM.show(self.home, platform='darwin', runner=run, uid=self.UID)['background'],
                         [{'label': JOB_LABEL, 'file': staged.name, 'daemon': 'waiting-for-root'}])
        self.assertEqual(MM.defined_jobs(self.home, 'darwin'), ([JOB_LABEL], None))
        with patch.object(MM.osprivacy, 'account', return_value=str(self.UID)):
            self.assertEqual(MM._space_evidence(self.home, 'darwin'), [f'{JOB_LABEL} waits for root: {step}'])

    def test_the_install_steps_pin_the_engines_release_key(self):
        import release_signature
        fingerprint = release_signature.PINNED[0]['fingerprint']
        self.assertEqual(MM.release_key_fingerprint(), fingerprint)
        steps = '\n'.join(MM.install_steps())
        self.assertIn(f'gpg --status-fd 1 --verify SHA256SUMS.asc SHA256SUMS | grep -q "VALIDSIG {fingerprint}"', steps)
        self.assertNotRegex(steps, r'gpg --verify SHA256SUMS\.asc SHA256SUMS(?! \|)', 'never a bare gpg --verify')

    def test_once_root_loaded_it_cutover_and_show_say_it_runs(self):
        self.stage()
        self.register()
        run = Launchd(system={JOB_LABEL})
        result = self.cutover(run)
        self.assertEqual((result['state'], result['load']), ('complete', 'loaded'))
        self.assertNotIn('human_steps', result)
        self.assertEqual(MM.show(self.home, platform='darwin', runner=run, uid=self.UID)['background'][0]['daemon'],
                         'loaded')
        self.assertTrue(MM._loaded(run, 'launchd', JOB_LABEL, self.UID))
        self.assertFalse(MM._loaded(run, 'launchd', JOB_LABEL, self.UID + 1), 'another account\'s daemon')
        with patch.object(MM.osprivacy, 'account', return_value=str(self.UID)):
            self.assertEqual(MM._space_evidence(self.home, 'darwin'),
                             [f'{JOB_LABEL} runs as a system daemon of this account (registered by root)'])

    def test_a_system_daemon_of_another_account_is_not_this_ones(self):
        self.stage()
        self.register(uid=self.UID + 1)
        result = self.cutover(Launchd(system={JOB_LABEL}))
        self.assertEqual(result['state'], 'waiting-for-root')

    def test_leave_is_refused_while_registered_even_unloaded_and_in_a_sandbox(self):
        self.stage()
        self.cutover(Launchd())
        self.register()
        for sandbox in ('1', '0'):
            with self.subTest(sandbox=sandbox), patch.dict(os.environ, {'OBSERVATORY_SYSTEM_SETUP': sandbox}), \
                    self.assertRaisesRegex(MM.MachineMoveError, 'registered by root'):
                MM.leave(JOB_LABEL, BETA, self.home, runner=Launchd(), platform='darwin', uid=self.UID)
        self.assertTrue((MI.background_dir(self.home) / f'{JOB_LABEL}.plist').is_file())

    def test_cutover_refuses_what_root_would_refuse_and_moves_nothing(self):
        import plistlib
        folder = MI.staging_dir(self.home)
        folder.mkdir(parents=True)
        staged = folder / f'{JOB_LABEL}.plist'
        staged.write_bytes(plistlib.dumps({'Label': JOB_LABEL, 'ProgramArguments': ['/usr/bin/true'],
                                           'Sockets': {'Listener': {'SockServiceName': '80'}}}))
        MI.place_elsewhere([{'label': JOB_LABEL, 'placedOn': BETA}], self.home)
        with self.assertRaisesRegex(MM.MachineMoveError, 'would refuse .*key_not_allowed \\(Sockets\\)'):
            self.cutover(Launchd())
        self.assertTrue(staged.is_file(), 'it stays where it was')
        self.assertFalse(MI.background_dir(self.home).exists())
        self.assertEqual([e['label'] for e in MI.read_placement(self.home)['elsewhere']], [JOB_LABEL])
        colon = 'org.example:job'
        (folder / f'{colon}.plist').write_bytes(plistlib.dumps({'Label': colon, 'ProgramArguments': ['/usr/bin/true']}))
        with self.assertRaisesRegex(MM.MachineMoveError, 'label_shape'):
            MM.cutover(colon, self.home, runner=Launchd(), platform='darwin', uid=self.UID, confirm=YES)

    def test_a_stale_staged_plist_never_waits_for_root(self):
        # OBS-390: root's tool would answer program_missing; cutover names the stale staged copy first.
        import plistlib
        gone = self.base / 'releases' / 'old-release' / 'bin' / 'run'
        staged = self.stage()
        staged.write_bytes(plistlib.dumps({'Label': JOB_LABEL, 'ProgramArguments': [str(gone)]}))
        with self.assertRaises(MM.MachineMoveError) as caught:
            self.cutover(Launchd())
        message = str(caught.exception)
        self.assertIn('staged-plist-stale', message)
        self.assertIn(str(gone), message)
        self.assertNotIn('program_missing', message)
        self.assertTrue(staged.is_file(), 'it stays where it was')
        self.assertFalse(MI.background_dir(self.home).exists())
        self.assertEqual([e['label'] for e in MI.read_placement(self.home)['elsewhere']], [JOB_LABEL])

    def test_cutover_parses_and_trusts_as_root_will(self):
        folder = MI.staging_dir(self.home)
        folder.mkdir(parents=True)
        (folder / 'renamed.plist').write_bytes(
            b'<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
            b'<!-- run with --install-launchd --><key>Label</key><string>' + JOB_LABEL.encode() +
            b'</string><key>ProgramArguments</key><array><string>/usr/bin/true</string></array></dict></plist>\n')
        sd = MM._space_daemons_module()
        with patch.object(sd, 'read_trust', wraps=sd.read_trust) as read_trust:
            result = MM.cutover('renamed.plist', self.home, runner=Launchd(), platform='darwin', uid=self.UID,
                                confirm=YES)
        self.assertTrue(read_trust.called, 'the trust file is read, read-only')
        self.assertEqual(result['state'], 'waiting-for-root')
        self.assertEqual(result['definition'], str(MI.background_dir(self.home) / f'{JOB_LABEL}.plist'),
                         'named after the Label the comment-tolerant parse found')
        (folder / 'open.plist').write_bytes(b'<?xml version="1.0"?><plist><dict><!-- never closed </dict></plist>')
        with self.assertRaisesRegex(MM.MachineMoveError, 'xml_comment'):
            MM.cutover('open.plist', self.home, runner=Launchd(), platform='darwin', uid=self.UID, confirm=YES)
        self.assertTrue((folder / 'open.plist').is_file())

    def test_a_homebrew_label_moves_and_is_named_after_its_label(self):
        import plistlib
        label = 'homebrew.mxcl.postgresql@14'
        folder = MI.staging_dir(self.home)
        folder.mkdir(parents=True)
        (folder / 'postgres.plist').write_bytes(plistlib.dumps({'Label': label, 'ProgramArguments': ['/usr/bin/true']}))
        result = MM.cutover(label, self.home, runner=Launchd(), platform='darwin', uid=self.UID, confirm=YES)
        self.assertEqual(result['state'], 'waiting-for-root')
        self.assertEqual(result['definition'], str(MI.background_dir(self.home) / f'{label}.plist'))
        self.assertEqual(sorted(p.name for p in MI.background_dir(self.home).iterdir()), [f'{label}.plist'])

    def test_a_job_defined_twice_is_refused_with_both_files_named(self):
        staged = self.stage()
        live = self.home / 'Library/LaunchAgents' / staged.name
        live.parent.mkdir(parents=True)
        live.write_bytes(self.plist())
        with self.assertRaises(MM.MachineMoveError) as caught:
            self.cutover(Launchd())
        self.assertIn(str(live), str(caught.exception))
        self.assertIn(str(staged), str(caught.exception))
        self.assertTrue(staged.is_file() and live.is_file())

    def test_both_answers_of_an_absent_gui_domain_wait_for_root(self):
        # 112 "could not find domain"; 125 "Domain does not support specified action", measured on macOS 26.6 for
        # an account nobody has logged in to graphically, where `bootstrap gui/UID` fails with the same 125.
        for code in (112, 125):
            with self.subTest(code=code):
                staged = self.stage()
                run = Launchd(gui=code)
                result = self.cutover(run)
                self.assertEqual(result['state'], 'waiting-for-root')
                self.assertTrue((MI.background_dir(self.home) / staged.name).is_file())
                self.assertNotIn('bootstrap', [c[1] for c in run.calls])
                (MI.background_dir(self.home) / staged.name).unlink()

    def test_other_answers_of_print_gui_keep_the_agent_path(self):
        for code in (1, 5, 113):
            with self.subTest(code=code):
                staged = self.stage()
                run = Launchd(gui=code)
                result = self.cutover(run)
                live = self.home / 'Library/LaunchAgents' / staged.name
                self.assertTrue(live.is_file())
                self.assertIn(['launchctl', 'bootstrap', f'gui/{self.UID}', str(live)], run.calls)
                self.assertNotIn('state', result)
                live.unlink()

    def test_with_a_gui_session_cutover_is_unchanged(self):
        staged = self.stage()
        run = Launchd(gui=0)
        result = self.cutover(run)
        live = self.home / 'Library/LaunchAgents' / staged.name
        self.assertTrue(live.is_file())
        self.assertIn(['launchctl', 'bootstrap', f'gui/{self.UID}', str(live)], run.calls)
        self.assertEqual(result['load'], 'loaded')
        self.assertNotIn('state', result)
        self.assertFalse(MI.background_dir(self.home).exists())

    def test_a_custom_xdg_data_home_is_refused_before_anything_moves(self):
        with patch.dict(os.environ, {'XDG_DATA_HOME': str(self.base / 'xdg')}):
            staged = self.stage()
            with self.assertRaisesRegex(MM.MachineMoveError, 'XDG_DATA_HOME'):
                self.cutover(Launchd())
            self.assertTrue(staged.is_file())

    def test_leave_is_refused_while_root_runs_it_then_works_from_the_background_folder(self):
        self.stage()
        self.register()
        run = Launchd(system={JOB_LABEL})
        self.cutover(run)
        with self.assertRaisesRegex(MM.MachineMoveError, 'only root removes: sudo /usr/bin/python3 -I '
                                    '/usr/local/libexec/observatory-space-daemons remove --user space '
                                    f'--label {JOB_LABEL}'):
            MM.leave(JOB_LABEL, BETA, self.home, runner=run, platform='darwin', uid=self.UID)
        self.assertTrue((MI.background_dir(self.home) / f'{JOB_LABEL}.plist').is_file(), 'nothing moved')
        run.system.clear()  # root ran `remove`: unloaded, and out of the registry
        (self.sysroot / 'Library/Application Support/ProjectObservatory/space-daemons.json').unlink()
        result = MM.leave(JOB_LABEL, BETA, self.home, runner=run, platform='darwin', uid=self.UID)
        self.assertEqual(result['stop'], 'stopped')
        self.assertFalse((MI.background_dir(self.home) / f'{JOB_LABEL}.plist').exists())
        self.assertTrue((MI.staging_dir(self.home) / f'{JOB_LABEL}.plist').is_file())
        self.assertEqual([e['placedOn'] for e in MI.read_placement(self.home)['elsewhere']], [BETA])


def job_file(home: Path, label: str, *, disabled: bool = False) -> Path:
    """A job definition in `home`'s job folder, as launchd or systemd keeps it."""
    import plistlib
    if sys.platform == 'darwin':
        path = home / 'Library/LaunchAgents' / f'{label}.plist'
        data = plistlib.dumps({'Label': label, 'ProgramArguments': ['/usr/bin/true'],
                               **({'Disabled': True} if disabled else {})})
    else:
        path = home / '.config/systemd/user' / label
        data = b'[Service]\nExecStart=/usr/bin/true\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def label_of(name: str) -> str:
    return name if sys.platform == 'darwin' else f'{name}.service'


def NO_VAULT(hours):
    return None, 'this machine holds no estate digest key'


def VAULT_SAME(hours):
    return {'all_match': True, 'summary': 'All 2 slot(s) match on 2 machines (synthetic).',
            'machines': [{'machine': 'beta', 'state': 'reachable'}]}, None


@unittest.skipIf(sys.platform == 'win32', 'no launchd or systemd user jobs on Windows')
class MoveStatus(Base):
    """OBS-286: the checklist M1 to M4, from what this machine holds and the other machine's facts."""

    def setUp(self):
        super().setUp()
        self.other = self.base / 'other-user'
        self.other_ws = self.base / 'other-workspace'
        (self.other_ws / 'node/device').mkdir(parents=True)
        self.alpha_line, self.alpha_fp = pub_line(b'alpha-key')
        self.beta_line, self.beta_fp = pub_line(b'beta-key')
        self.key(self.user, 'id_ed25519', self.alpha_line)
        self.device(self.ws, 'device-alpha-0001')
        self.device(self.other_ws, 'device-beta-0002')
        job_file(self.user, label_of('com.example.alpha-bot'))
        job_file(self.user, label_of('org.project-observatory.0123456789abcdef.tick'))
        job_file(self.other, label_of('com.example.beta-indexer'))
        job_file(self.other, label_of('org.project-observatory.fedcba9876543210.tick'))

    def key(self, home, name, line):
        folder = home / '.ssh'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        (folder / name).write_bytes(PRIVATE_HEAD + SECRET_MARKER)
        (folder / name).chmod(0o600)
        (folder / f'{name}.pub').write_text(line)

    def device(self, ws, value):
        folder = ws / 'node/device'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'device.json').write_text(json.dumps({'schema': 'device-keys/1', 'id': value}))

    def facts_of_beta(self, at=None, **change):
        with patch.dict(os.environ, {'OBSERVATORY_MACHINE_ID': BETA, 'HOME': str(self.other)}):
            doc = MM.facts(self.other, self.other_ws, at=at)
        doc.update(change)
        path = self.base / f'beta-facts-{len(list(self.base.glob("beta-facts-*")))}.json'
        path.write_text(json.dumps(doc))
        return path

    def check(self, *files, vault_view=VAULT_SAME, at=None):
        peers, bad = MM.load_peers(self.user, list(files) if files else None)
        view = MM.status(self.user, self.ws, peers=peers, unreadable=bad, vault_view=vault_view, at=at)
        text = json.dumps(view) + MM.render(view)
        self.assertNotIn(SECRET_MARKER.decode(), text)
        self.assertNotIn('PRIVATE', text)
        return view, {i['id']: i for i in view['items']}

    def receipt(self, **apply):
        part = {'bundle': 'machine-x.obmachine', 'mode': 'second-machine', 'taken_on': BETA, 'this_machine': ALPHA,
                'only': [], 'skip': [], 'left_out': [], 'failed': 0, 'placement_error': False,
                'counts': {'written': 10, 'same': 0, 'kept': 0, 'replaced': 0, 'withheld': 2}, 'other_keys': []}
        part.update(apply)
        MI.record_restore({'apply': part, 'clone': {'checkouts': 2, 'failed': 0}}, self.user)

    def test_with_nothing_to_compare_every_cross_machine_item_is_unknown_never_a_guess(self):
        view, items = self.check(vault_view=NO_VAULT)
        self.assertEqual({k: i['state'] for k, i in items.items()},
                         {'release': 'unknown', 'restore': 'unknown', 'ssh-key': 'unknown', 'device-id': 'unknown',
                          'vault': 'unknown', 'jobs': 'unknown'})
        self.assertEqual([i['step'] for i in view['items']], ['M1', 'M2', 'M3', 'M3', 'M3', 'M4'])
        self.assertFalse(view['all_ok'])
        self.assertIn('machine-move facts --out FILE', items['release']['next'][0])
        self.assertIn('device-alpha-0001', items['device-id']['evidence'][0])
        self.assertIn('1 job(s) defined here', items['jobs']['evidence'][0], 'the engine\'s own job is not counted')
        self.assertIn('estate digest key', items['vault']['evidence'][0])

    def test_a_peer_with_the_same_release_and_its_own_key_device_and_jobs_reads_ok(self):
        self.key(self.other, 'id_ed25519', self.beta_line)
        self.receipt()
        view, items = self.check(self.facts_of_beta())
        self.assertEqual({k: i['state'] for k, i in items.items()}, dict.fromkeys(items, 'ok'), items)
        self.assertTrue(view['all_ok'])
        self.assertEqual(view['counts'], {'ok': 6, 'missing': 0, 'unknown': 0})
        self.assertEqual(view['other_machines'][0]['machine'], BETA)
        self.assertIn(f'runs {config.VERSION}', items['release']['evidence'][1])
        self.assertIn('device-beta-0002', ' '.join(items['device-id']['evidence']))
        self.assertTrue(view['summary'].startswith('The move is done'))

    def test_what_the_two_machines_share_is_missing_and_named(self):
        self.key(self.other, 'id_ed25519', self.alpha_line)      # the key was copied, not made anew
        self.device(self.other_ws, 'device-alpha-0001')            # the workspace restored whole
        job_file(self.other, label_of('com.example.alpha-bot'))    # the bot defined on both
        view, items = self.check(self.facts_of_beta(engine_version='0.29.0'))
        for key in ('release', 'ssh-key', 'device-id', 'jobs'):
            self.assertEqual(items[key]['state'], 'missing', items[key])
            self.assertTrue(items[key].get('next'), key)
        self.assertIn(f'{BETA} (facts of', items['release']['evidence'][1])
        self.assertIn('runs 0.29.0', items['release']['evidence'][1])
        self.assertIn(f'~/.ssh/id_ed25519 ({self.alpha_fp}) is the same key as ~/.ssh/id_ed25519 on {BETA}',
                      items['ssh-key']['evidence'][0])
        self.assertIn('device-alpha-0001 is also', items['device-id']['evidence'][0])
        self.assertIn(f"{label_of('com.example.alpha-bot')} is defined here and on {BETA}", items['jobs']['evidence'][0])
        self.assertEqual(view['counts']['missing'], 4)

    def test_old_facts_say_nothing_about_todays_release_or_jobs(self):
        self.key(self.other, 'id_ed25519', self.beta_line)
        old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)
        view, items = self.check(self.facts_of_beta(at=old))
        self.assertEqual(items['release']['state'], 'unknown')
        self.assertEqual(items['jobs']['state'], 'unknown')
        self.assertEqual(items['ssh-key']['state'], 'ok', 'a key fingerprint does not age')
        self.assertEqual(items['device-id']['state'], 'ok')
        self.assertIn('older than 48 hours', view['other_machines'][0]['stale'])
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)
        view, items = self.check(self.facts_of_beta(at=future))
        self.assertIn('future', view['other_machines'][0]['stale'])
        self.assertEqual(items['release']['state'], 'unknown')

    def test_a_job_placed_elsewhere_and_defined_here_runs_on_both(self):
        MI.place_elsewhere([{'label': label_of('com.example.alpha-bot'), 'placedOn': BETA, 'by': 'machine-restore'}],
                           self.user)
        job_file(self.user, label_of('com.example.disabled'), disabled=True)
        view, items = self.check()
        self.assertEqual(items['jobs']['state'], 'missing')
        self.assertIn(f'named in this machine\'s placement file as running on {BETA}', items['jobs']['evidence'][0])
        if sys.platform == 'darwin':
            jobs, _ = MM.defined_jobs(self.user)
            self.assertNotIn('com.example.disabled', jobs, 'a plist marked Disabled does not load at login')

    def test_the_restore_reads_complete_only_without_gaps(self):
        self.receipt()
        _, items = self.check()
        self.assertEqual(items['restore']['state'], 'ok', items['restore'])
        self.receipt(counts={'written': 9, 'kept': 2}, failed=1, left_out=['claude'])
        _, items = self.check()
        self.assertEqual(items['restore']['state'], 'missing')
        evidence = ' '.join(items['restore']['evidence'])
        self.assertIn('2 conflict(s) kept', evidence)
        self.assertIn('1 item(s) failed', evidence)
        self.assertIn('left out on purpose (--only/--skip): claude', evidence)
        self.assertTrue(any('--overwrite' in step for step in items['restore']['next']))

    def test_the_machine_moved_from_reads_the_restore_from_the_other_machines_facts(self):
        with patch.dict(os.environ, {'OBSERVATORY_MACHINE_ID': BETA}):
            MI.record_restore({'apply': {'mode': 'second-machine', 'taken_on': ALPHA, 'failed': 0,
                                         'counts': {'kept': 0}}, 'clone': {'failed': 0}}, self.other)
        _, items = self.check(self.facts_of_beta())
        self.assertEqual(items['restore']['state'], 'ok', items['restore'])
        self.assertIn(f'{BETA} (facts of', items['restore']['evidence'][0])

    def test_a_key_the_restore_withheld_is_the_other_machines(self):
        self.receipt(other_keys=[{'key': 'id_ed25519', 'fingerprint': self.alpha_fp}])
        _, items = self.check()
        self.assertEqual(items['ssh-key']['state'], 'missing')
        self.assertIn('the restored bundle withheld', items['ssh-key']['evidence'][0])
        self.receipt(other_keys=[{'key': 'id_ed25519', 'fingerprint': self.beta_fp}])
        _, items = self.check()
        self.assertEqual(items['ssh-key']['state'], 'ok', items['ssh-key'])

    def test_no_key_or_no_device_here_is_missing(self):
        shutil.rmtree(self.user / '.ssh')
        shutil.rmtree(self.ws / 'node/device')
        _, items = self.check()
        self.assertEqual((items['ssh-key']['state'], items['device-id']['state']), ('missing', 'missing'))
        # Nothing names the other machine's keys: the step says where their names and fingerprints come from.
        self.assertIn('nothing here names the other machine', ' '.join(items['ssh-key']['evidence']))
        self.assertIn('ssh-keys list', items['ssh-key']['next'][0])
        self.assertIn('ssh-keys rekey NAME --fingerprint SHA256:...', items['ssh-key']['next'][1])
        self.assertFalse(any(' create ' in s or ' send ' in s for s in items['ssh-key']['next']))
        self.assertIn('node --init', items['device-id']['next'][0])

    def test_no_key_here_gets_the_restores_rekey_steps_from_the_receipt(self):
        # OBS-306 (6): M3 hands out what the restore does, built from the receipt's other_keys.
        shutil.rmtree(self.user / '.ssh')
        other_line, other_fp = pub_line(b'beta-deploy')
        self.receipt(other_keys=[{'key': 'id_ed25519', 'fingerprint': self.beta_fp, 'in_ssh': True},
                                 {'key': '-bad name', 'fingerprint': self.alpha_fp, 'in_ssh': True},
                                 # Not directly in ~/.ssh (the restore gives it a by-hand note), or a receipt
                                 # older than the field: no rekey step (review of #414, L1).
                                 {'key': 'deploy_key', 'fingerprint': other_fp, 'in_ssh': False},
                                 {'key': 'legacy', 'fingerprint': other_fp}])
        _, items = self.check()
        step = f'project-observatory full ssh-keys rekey id_ed25519 --fingerprint {self.beta_fp}'
        self.assertEqual(items['ssh-key']['state'], 'missing')
        self.assertEqual(items['ssh-key']['next'], [step, step + ' --apply'],
                         'a name rekey would refuse is left out, never repaired')
        import shlex
        args = MM._ssh_keys_module().parser().parse_args(shlex.split(items['ssh-key']['next'][1])[3:])
        self.assertEqual((args.command, args.old, args.fingerprint, args.apply),
                         ('rekey', 'id_ed25519', self.beta_fp, True))

    def test_no_key_here_takes_the_other_machines_keys_from_its_facts(self):
        shutil.rmtree(self.user / '.ssh')
        self.key(self.other, 'id_work', self.beta_line)
        _, items = self.check(self.facts_of_beta())
        step = f'project-observatory full ssh-keys rekey id_work --fingerprint {self.beta_fp}'
        self.assertEqual(items['ssh-key']['next'], [step, step + ' --apply'])

    def test_a_shared_copy_gets_its_own_key_under_a_new_name(self):
        # The copy holds the old name: rekey refuses to reuse a key whose fingerprint is the other machine's.
        self.key(self.other, 'id_ed25519', self.alpha_line)
        _, items = self.check(self.facts_of_beta())
        step = f'project-observatory full ssh-keys rekey id_ed25519 --name id_ed25519-own --fingerprint {self.alpha_fp}'
        steps = items['ssh-key']['next']
        self.assertEqual(steps[:2], [step, step + ' --apply'])
        self.assertIn('IdentityFile ~/.ssh/id_ed25519-own', steps[2])
        self.assertIn('rekey removes nothing', steps[2])
        # Review of #414 (M1): ssh offers a default name to hosts with no IdentityFile line, the new name never.
        self.assertIn('add `IdentityFile ~/.ssh/id_ed25519-own` for github.com', steps[2])
        self.assertIn('`Host *`', steps[2])
        self.assertIn('before removing the copy', steps[2])
        # The hosts ssh offered the default key to got the new one only when named to rekey (confirming round).
        self.assertIn('only if it was added to both rekey steps with --server ALIAS', steps[2])
        self.assertLess(steps[2].index('--server ALIAS'), steps[2].index('`Host *`'))
        import shlex
        args = MM._ssh_keys_module().parser().parse_args(shlex.split(steps[1])[3:])
        self.assertEqual((args.old, args.name, args.fingerprint, args.apply),
                         ('id_ed25519', 'id_ed25519-own', self.alpha_fp, True))

    def test_two_shared_copies_never_get_one_new_name(self):
        # Review of #414 (L2): `a/id` and `b/id` beside each other, and a name already taken here.
        line_a, fp_a = pub_line(b'shared-a')
        line_b, fp_b = pub_line(b'shared-b')
        for sub, line in (('a', line_a), ('b', line_b)):
            folder = self.user / '.ssh' / sub
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'id').write_bytes(PRIVATE_HEAD + SECRET_MARKER)
            (folder / 'id').chmod(0o600)
            (folder / 'id.pub').write_text(line)
        (self.user / '.ssh/a-id-own').write_text('taken')
        self.key(self.other, 'id_a', line_a)
        self.key(self.other, 'id_b', line_b)
        _, items = self.check(self.facts_of_beta())
        steps = items['ssh-key']['next']
        self.assertIn(f'project-observatory full ssh-keys rekey a/id --name a-id-own2 --fingerprint {fp_a}', steps)
        self.assertIn(f'project-observatory full ssh-keys rekey b/id --name b-id-own --fingerprint {fp_b}', steps)
        self.assertNotIn('`Host *`', ' '.join(steps), 'not a default name: its Host lines name it already')
        # A pair under the name whose key is the other machine's is not this machine's own: numbered too.
        (self.user / '.ssh/b-id-own').write_bytes(PRIVATE_HEAD + SECRET_MARKER)
        (self.user / '.ssh/b-id-own.pub').write_text(line_b)
        _, items = self.check(self.facts_of_beta())
        self.assertIn(f'project-observatory full ssh-keys rekey b/id --name b-id-own2 --fingerprint {fp_b}',
                      items['ssh-key']['next'])

    def test_a_rerun_after_rekey_apply_hands_out_the_same_name(self):
        # Confirming round of #414 (B1): `rekey --apply` makes ~/.ssh/NAME-own while the copy is still here
        # (removing it is the last step), so the checklist must name that key again, never NAME-own2.
        self.key(self.other, 'id_ed25519', self.alpha_line)
        peer = self.facts_of_beta()
        _, first = self.check(peer)
        step = f'project-observatory full ssh-keys rekey id_ed25519 --name id_ed25519-own --fingerprint {self.alpha_fp}'
        self.assertEqual(first['ssh-key']['next'][:2], [step, step + ' --apply'])
        made_line, made_fp = pub_line(b'alpha-own')
        self.key(self.user, 'id_ed25519-own', made_line)      # what `rekey ... --apply` left here
        _, again = self.check(peer)
        self.assertEqual(again['ssh-key']['next'], first['ssh-key']['next'], 'the same steps, the same name')
        self.assertNotIn('-own2', json.dumps(again['ssh-key']))
        self.assertIn(f'~/.ssh/id_ed25519-own ({made_fp}) is already this machine\'s own key',
                      ' '.join(again['ssh-key']['evidence']))

    def test_an_unlisted_ssh_folder_is_said_not_taken_as_empty(self):
        self.key(self.other, 'id_ed25519', self.alpha_line)
        with patch.object(MM, '_ssh_names', side_effect=PermissionError('denied')):
            _, items = self.check(self.facts_of_beta())
        self.assertEqual(items['ssh-key']['state'], 'missing')
        self.assertIn('~/.ssh could not be listed (PermissionError)', ' '.join(items['ssh-key']['evidence']))

    def test_facts_are_public_owner_only_and_never_replace_a_file(self):
        out = self.base / 'alpha-facts.json'
        doc = MM.facts(self.user, self.ws)
        self.assertEqual((doc['schema'], doc['machine'], doc['device']), ('observatory-machine-facts/1', ALPHA,
                                                                          'device-alpha-0001'))
        self.assertEqual(doc['ssh'], [{'file': '~/.ssh/id_ed25519', 'fingerprint': self.alpha_fp}])
        self.assertEqual(doc['jobs'], [label_of('com.example.alpha-bot')])
        MM.write_facts(doc, out)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertNotIn(SECRET_MARKER.decode(), out.read_text())
        with self.assertRaises(FileExistsError):
            MM.write_facts(doc, out)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(MM.main(['facts', '--out', str(out)]), 2)
        self.assertIn('already exists', err.getvalue())

    def test_a_peer_file_that_does_not_read_is_named_and_the_rest_still_compares(self):
        folder = MM.peers_dir(self.user)
        folder.mkdir(parents=True)
        (folder / 'broken.json').write_text('{"schema": "observatory-machine-facts/1", "machine": "x"}')
        (folder / 'beta.json').write_text(self.facts_of_beta().read_text())
        (folder / 'self.json').write_text(json.dumps(MM.facts(self.user, self.ws)))
        view, items = self.check()
        reasons = ' '.join(d['reason'] for d in view['degraded'])
        self.assertIn('broken.json: its machine id is malformed', reasons)
        self.assertIn('names this machine', reasons)
        self.assertEqual([m['machine'] for m in view['other_machines']], [BETA])
        self.assertEqual(items['release']['state'], 'ok')

    def test_a_hostile_facts_file_is_refused_by_name_and_status_still_runs(self):
        good = json.loads(self.facts_of_beta().read_text())
        good['restore'] = {'taken_on': ALPHA, 'complete': True, 'at': '2026-11-02T10:00:00Z', 'gaps': []}
        hostile = {
            'not-an-object': [], 'schema': {'schema': 1}, 'machine': {'machine': 7},
            'generated_at': {'generated_at': 12}, 'engine_version': {'engine_version': ['0.31.0']},
            'ssh-type': {'ssh': 'id_ed25519'}, 'ssh-zero': {'ssh': 0}, 'ssh-member': {'ssh': [5]},
            'ssh-fingerprint': {'ssh': [{'file': '~/.ssh/id', 'fingerprint': 5}]},
            'ssh-file': {'ssh': [{'file': ['~/.ssh/id'], 'fingerprint': self.alpha_fp}]},
            'device': {'device': 5}, 'jobs-type': {'jobs': 'com.example.a'}, 'jobs-member': {'jobs': [1]},
            'jobs-shape': {'jobs': ['has space']}, 'elsewhere': {'elsewhere': [None]},
            'restore-type': {'restore': ['complete']}, 'restore-complete': {'restore': {'complete': 'yes'}},
            'restore-taken-on': {'restore': {'complete': True, 'taken_on': 5}},
            'restore-at': {'restore': {'complete': True, 'at': 5}},
            'restore-gaps-int': {'restore': {'complete': True, 'gaps': 5}},
            'restore-gaps-true': {'restore': {'complete': True, 'gaps': True}},
            'restore-gaps-member': {'restore': {'complete': True, 'gaps': [1]}},
            'restore-gaps-dict': {'restore': {'complete': True, 'gaps': {'a': 'b'}}},
            'restore-gaps-long': {'restore': {'complete': True, 'gaps': ['x'] * 65}},
        }
        folder = MM.peers_dir(self.user)
        folder.mkdir(parents=True)
        for name, change in hostile.items():
            doc = [] if name == 'not-an-object' else {**good, **change}
            (folder / f'{name}.json').write_text(json.dumps(doc))
        (folder / 'beta.json').write_text(json.dumps(good))
        peers, bad = MM.load_peers(self.user)
        self.assertEqual(sorted(b['file'] for b in bad), sorted(f'{n}.json' for n in hostile), bad)
        self.assertEqual([p['machine'] for p in peers], [BETA])
        view, items = self.check()
        self.assertEqual(len([d for d in view['degraded'] if d['code'] == 'unreadable-facts']), len(hostile))
        self.assertIn('restore-gaps-int.json: its restore summary is malformed: gaps', json.dumps(view['degraded']))
        self.assertEqual(items['restore']['state'], 'ok', 'the good file still answers')
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch.object(MM, '_vault_view', NO_VAULT):
            self.assertEqual(MM.main(['status', '--json']), 1, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())['schema'], 'observatory-machine-move-status/1')

    def test_a_restore_receipt_edited_by_hand_reads_as_gaps_never_a_crash(self):
        path = MI.restore_receipt_path(self.user)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'schema': 'observatory-machine-restore/1',
                                    'apply': {'failed': 'many', 'counts': {'kept': [1]}, 'left_out': 5, 'at': 3,
                                              'mode': None, 'other_keys': {'k': 'v'}, 'placement_error': 'x'},
                                    'clone': {'failed': None}}))
        _, items = self.check()
        self.assertEqual(items['restore']['state'], 'missing')
        self.assertIn('1 item(s) failed to write', ' '.join(items['restore']['evidence']))

    def test_facts_hold_only_what_the_other_machine_reads(self):
        line, fp = pub_line(b'plus-key')
        self.key(self.user, 'id+work', line)
        job_file(self.user, label_of('com.example has space'))
        with patch.object(MI, 'read_placement', lambda home=None: {'elsewhere': [{'label': 'com.example.placed'},
                                                                                  {'label': 'not ok'}]}):
            doc = MM.facts(self.user, self.ws)
        self.assertIn({'file': '~/.ssh/name-not-shown-1', 'fingerprint': fp}, doc['ssh'], 'the fingerprint is kept')
        self.assertEqual(doc['omitted'], {'ssh_names_masked': 1, 'jobs_left_out': 1, 'elsewhere_left_out': 1})
        self.assertEqual(doc['jobs'], [label_of('com.example.alpha-bot')])
        self.assertEqual(doc['elsewhere'], ['com.example.placed'])
        out = self.base / 'alpha-facts.json'
        MM.write_facts(doc, out)
        self.assertEqual(MM.read_facts(out)['ssh'], doc['ssh'], 'what facts writes, the reader takes')
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(MM.main(['facts', '--out', str(self.base / 'cli-facts.json')]), 0)
        self.assertIn('1 key file name(s) masked', err.getvalue())

    def test_facts_are_written_whole_and_never_over_a_name_or_a_link(self):
        doc = MM.facts(self.user, self.ws)
        out = self.base / 'facts.json'
        MM.write_facts(doc, out)
        self.assertEqual(json.loads(out.read_text())['machine'], ALPHA)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.base.iterdir() if p.name.startswith('.facts.json.')], [],
                         'no temporary file is left')
        out.write_text('kept')
        with self.assertRaises(FileExistsError):
            MM.write_facts(doc, out)
        self.assertEqual(out.read_text(), 'kept')
        target = self.base / 'target.txt'
        target.write_text('untouched')
        link = self.base / 'link.json'
        link.symlink_to(target)
        with self.assertRaises(FileExistsError):
            MM.write_facts(doc, link)
        self.assertEqual(target.read_text(), 'untouched')

    def test_status_never_makes_this_machines_id(self):
        env = {k: v for k, v in os.environ.items() if k != 'OBSERVATORY_MACHINE_ID'}
        with patch.dict(os.environ, env, clear=True), patch.object(MI, '_platform_identifier', lambda runner=None: None):
            view, items = self.check()
        self.assertIsNone(view['machine'])
        self.assertIn('no-machine-id', [d['code'] for d in view['degraded']])
        self.assertFalse((MI.data_dir(self.user) / MI.ID_FILE).exists(), 'status wrote no machine id')
        self.assertIn('this machine: unknown', MM.render(view))

    def test_the_command_line_prints_the_checklist_and_exits_by_it(self):
        sys.path.insert(0, str(ROOT))
        import observatory as cli
        self.assertIn('machine-move', cli.PUBLIC_HELP)
        self.assertIn('status', cli.PUBLIC_HELP[cli.PUBLIC_HELP.index('machine-move'):])
        self.assertEqual(cli.refusal(['machine-move', 'status', '--json', '--peer', 'x.json', '--max-age-hours', '12']), '')
        self.assertEqual(cli.refusal(['machine-move', 'facts', '--out', 'x.json']), '')
        self.assertNotEqual(cli.refusal(['machine-move', 'status', '--max-age-hours', '0']), '')
        peer = self.facts_of_beta()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                patch.object(MM, '_vault_view', NO_VAULT):
            self.assertEqual(workspace.main(['machine-move', 'status', '--json', '--peer', str(peer)]), 1)
        self.assertEqual(json.loads(out.getvalue())['schema'], 'observatory-machine-move-status/1')
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(MM, '_vault_view', NO_VAULT):
            self.assertEqual(MM.main(['status', '--peer', str(peer)]), 1)
        self.assertIn('M1 release', out.getvalue())
        self.assertIn('M4 jobs', out.getvalue())


@unittest.skipIf(sys.platform == 'win32', 'no launchd or systemd user jobs on Windows')
class MoveStatusVault(unittest.TestCase):
    """OBS-286 with the real vault digests (OBS-194): two vaults, one estate key, the other's manifest."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        (self.base / 'a-peers').mkdir()
        self.tools = ROOT / 'tools'

    def env(self, name, peers):
        root = self.base / name
        (root / 'home').mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith('OBSERVATORY_')}
        env.update({'HOME': str(root / 'home'), 'OBSERVATORY_HOME': str(root / 'workspace'),
                    'OBSERVATORY_VAULT_DIR': str(root / 'secrets/projects'), 'OBSERVATORY_STATE': str(root / 'state'),
                    'OBSERVATORY_DATA': str(root / 'projects'), 'OBSERVATORY_REGISTRY': str(root / 'registry'),
                    'OBSERVATORY_MACHINE_NAME': name, 'OBSERVATORY_VAULT_PEERS': str(peers),
                    'OBSERVATORY_SYSTEM_SETUP': '0', 'OBSERVATORY_MACHINE_ID': f'm-{name}-0000000001',
                    'OBSERVATORY_MACHINE_ID_FOR_TESTS': '1'})
        env.pop('XDG_DATA_HOME', None)
        return env

    def run_tool(self, env, *args, value=None, script=None):
        argv = [str(script), *args] if script else [str(self.tools / args[0]), *args[1:]]
        p = subprocess.run([sys.executable, *argv], input=value, text=True,
                           capture_output=True, env=env, timeout=120)
        return p

    def vault_item(self, env):
        p = self.run_tool(env, 'status', '--json', script=ROOT / 'machine_move.py')
        self.assertEqual(p.returncode, 1, p.stderr)
        view = json.loads(p.stdout)
        self.assertNotIn('synthetic-value', p.stdout + p.stderr)
        return next(i for i in view['items'] if i['id'] == 'vault')

    def test_the_vault_item_reads_the_digests_and_never_makes_a_key(self):
        a, b = self.env('alpha', self.base / 'a-peers'), self.env('beta', self.base / 'b-peers')
        item = self.vault_item(a)
        self.assertEqual(item['state'], 'unknown')
        self.assertIn('no estate digest key', item['evidence'][0])
        self.assertFalse((self.base / 'alpha/secrets/projects/.estate-digest-key').exists(), 'status made no key')
        for env in (a, b):
            self.assertEqual(self.run_tool(env, 'vault.py', 'put', 'alpha-web', 'prod', 'API_TOKEN',
                                           value='synthetic-value-0001').returncode, 0)
        self.assertEqual(self.run_tool(a, 'vault_digests.py', 'write').returncode, 0)
        exported = self.base / 'estate-key'
        self.assertEqual(self.run_tool(a, 'vault_digests.py', 'key', 'export', str(exported)).returncode, 0)
        self.assertEqual(self.run_tool(b, 'vault_digests.py', 'key', 'import', value=exported.read_text()).returncode, 0)
        exported.unlink()
        item = self.vault_item(a)
        self.assertEqual(item['state'], 'unknown', 'no peer manifest yet')
        self.assertEqual(self.run_tool(b, 'vault_digests.py', 'write').returncode, 0)
        (self.base / 'a-peers/beta.json').write_text((self.base / 'beta/state/vault-digests/local.json').read_text())
        item = self.vault_item(a)
        self.assertEqual(item['state'], 'ok', item)
        self.assertIn('All 1 slot(s) match on 2 machines', item['evidence'][0])
        self.assertEqual(self.run_tool(b, 'vault.py', 'put', 'alpha-web', 'prod', 'API_TOKEN', '--force',
                                       value='synthetic-value-0002').returncode, 0)
        self.assertEqual(self.run_tool(b, 'vault_digests.py', 'write').returncode, 0)
        (self.base / 'a-peers/beta.json').write_text((self.base / 'beta/state/vault-digests/local.json').read_text())
        item = self.vault_item(a)
        self.assertEqual(item['state'], 'missing', item)
        self.assertIn('1 differs', item['evidence'][0])
        key = (self.base / 'alpha/secrets/projects/.estate-digest-key').read_text().strip()
        self.assertNotIn(key, json.dumps(item))


class MachineId(unittest.TestCase):
    """The id tells two machines apart and never writes the platform's own identifier."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        self.env = patch.dict(os.environ, {'HOME': str(self.home)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_the_platform_identifier_is_hashed_never_kept(self):
        raw = '01234567-89AB-CDEF-0123-456789ABCDEF'

        def ioreg(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, f'  "IOPlatformUUID" = "{raw}"\n', '')
        with patch.object(MI.sys, 'platform', 'darwin'):
            first = MI.machine_id(self.home, runner=ioreg)
            again = MI.machine_id(self.home, runner=ioreg)
        self.assertEqual(first, again)
        self.assertRegex(first, r'^m-[0-9a-f]{24}$')
        self.assertNotIn(raw.lower().replace('-', '')[:12], first)
        self.assertFalse(MI.data_dir(self.home).exists(), 'nothing written when the platform has an identifier')

    @unittest.skipIf(sys.platform == 'win32', 'owner-only is an access list on Windows, not a mode')
    def test_without_one_a_random_id_is_kept_once_owner_only(self):
        with patch.object(MI, '_platform_identifier', lambda runner=None: None):
            first = MI.machine_id(self.home)
            self.assertEqual(MI.machine_id(self.home), first)
        kept = MI.data_dir(self.home) / MI.ID_FILE
        self.assertEqual(stat.S_IMODE(kept.stat().st_mode), 0o600)

    def test_the_override_is_validated_and_honoured_only_in_a_sandbox(self):
        sandbox = {'OBSERVATORY_SYSTEM_SETUP': '0'}
        with patch.dict(os.environ, {**sandbox, MI.ENV: 'bad id'}), self.assertRaises(MI.MachineIdentityError):
            MI.machine_id(self.home)
        with patch.dict(os.environ, {**sandbox, MI.ENV: ALPHA}):
            self.assertEqual(MI.machine_id(self.home), ALPHA)
        with patch.dict(os.environ, {MI.ENV: ALPHA}), patch.object(MI, '_platform_identifier', lambda runner=None: 'X'):
            self.assertNotEqual(MI.machine_id(self.home), ALPHA, 'outside a sandbox the variable is ignored')

    def test_the_restore_mode(self):
        mode = lambda recorded, asked=None: MI.restore_mode(recorded, asked=asked, here=ALPHA)  # noqa: E731
        self.assertEqual(mode(ALPHA)['mode'], 'same-machine')
        self.assertEqual((mode(BETA)['mode'], mode(BETA)['why']), ('second-machine', 'another-machine'))
        self.assertEqual((mode(None)['mode'], mode(None)['why']), ('second-machine', 'names-no-machine'))
        self.assertEqual(mode('not an id!')['why'], 'names-no-machine')
        self.assertEqual(mode(ALPHA, 'second')['mode'], 'second-machine')
        self.assertEqual(mode(None, 'same')['mode'], 'same-machine')


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

    def test_maintenance_status_joins_backup_items(self):
        # OBS-349: the backup warnings no maintenance item already says reach Health and doctor's
        # verdict; the two with a maintenance twin (`passphrase`, `not-encrypted`) do not repeat.
        import maintenance as M
        store = self.base / 'external-store'
        store.mkdir()
        file = self.ws / 'config' / 'settings.json'
        doc = json.loads(file.read_text())
        doc.setdefault('sources', {})['secret_store'] = str(store)
        file.write_text(json.dumps(doc))
        self.assertIn('backups-no-passphrase', [w['code'] for w in vault.status(self.ws)['warning_items']])
        status = M.status(self.ws)
        codes = [w['code'] for w in status['warning_items']]
        self.assertIn('backups-vault-store-not-carried', codes)
        self.assertNotIn('backups-no-passphrase', codes)
        self.assertNotIn('backups-unencrypted-local', codes)
        self.assertEqual(len(status['warnings']), len(status['warning_items']))


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


def cli_refusal(argv):
    sys.path.insert(0, str(ROOT))
    import observatory as cli
    return cli.refusal(argv)


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

    def test_machine_restore_reads_the_passphrase_from_stdin(self):
        # OBS-348: an agent hands another machine's passphrase over on stdin (a file or an ssh channel),
        # never in argv; it is used for this restore and kept nowhere.
        vault.set_passphrase(self.ws, PASS)
        self.write('.zshrc', 'export EXAMPLE=1\n')
        self.take(at=AT)
        bundle = MB.newest(self.ws)
        vault.passphrase_file(self.ws).unlink()   # this workspace no longer holds it
        out, err = io.StringIO(), io.StringIO()
        stdin = io.TextIOWrapper(io.BytesIO((PASS + '\n').encode()), encoding='utf-8')
        with patch.object(sys, 'stdin', stdin), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(workspace.main(['machine-restore', str(bundle), '--passphrase-stdin']), 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())['passphrase_source'], 'stdin')
        self.assertNotIn(PASS, out.getvalue() + err.getvalue())
        self.assertFalse(vault.passphrase_file(self.ws).exists(), 'nothing is stored')
        out, err = io.StringIO(), io.StringIO()
        stdin = io.TextIOWrapper(io.BytesIO(b'a wrong value, long enough\n'), encoding='utf-8')
        with patch.object(sys, 'stdin', stdin), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(workspace.main(['machine-restore', str(bundle), '--passphrase-stdin']), 2)
        self.assertIn('passphrase-refused', err.getvalue())
        self.assertIn('stdin', err.getvalue())
        self.assertEqual(cli_refusal(['machine-restore', '--latest', '--passphrase-stdin']), '')


if __name__ == '__main__':
    unittest.main()
