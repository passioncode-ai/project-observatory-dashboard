#!/usr/bin/env python3
"""The lifecycle broker's macOS adapter (OBS-79 step L2, docs/design/LIFECYCLE.md) against injected tools.

`codesign`, `launchctl`, the NSWorkspace helper and Switchboard's CLI are fakes handed to the adapter as its runner
(`FakeMac`); no installed application is opened, quit or signalled. Two cases run the real `codesign` on an ad hoc
bundle they build (macOS only), one checks the engine's own installed app when there is one, and one compiles the
Swift helper when `swiftc` exists: elsewhere they are skipped, never counted as passed.

The fixtures are neutral: signing teams `ABCDE12345` and `ZZZZZZZZZZ`, bundle ids `com.example.*`, every file in a
temporary folder. Fabric's and Switchboard's public bundle ids appear because the readiness profiles name them.
"""
from __future__ import annotations

import concurrent.futures
import http.server
import io
import json
import os
import plistlib
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tmp as tmpdir                                   # noqa: E402,F401  a sandboxed workspace for `paths`

from node.lifecycle import broker, macos               # noqa: E402
from test_node_lifecycle_broker import BrokerFixture, target, write_private  # noqa: E402

TEAM = 'ABCDE12345'
OTHER_TEAM = 'ZZZZZZZZZZ'
POSIX = os.name != 'nt'


def done(stdout='', returncode=0, stderr=''):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class FakeCodesign:
    """`/usr/bin/codesign` for one bundle: a signature with an identifier and a team.

    `-R` verification succeeds only when the signature is valid, not ad hoc, and the requirement names exactly this
    identifier and this team, as Security.framework decides `anchor apple generic and identifier "..." and
    certificate leaf[subject.OU] = "..."`."""

    def __init__(self, identifier='com.example.alpha', team=TEAM):
        self.identifier, self.team = identifier, team
        self.adhoc, self.valid = False, True
        self.calls = []

    def __call__(self, args, timeout=10):
        self.calls.append(list(args))
        assert args[0] == macos.CODESIGN, args
        if '-r-' in args:
            text = (f'designated => identifier "{self.identifier}" and anchor apple generic and '
                    f'certificate leaf[subject.OU] = {self.team}\n')
            return done('' if self.adhoc else text, 0, 'Executable=x\n')
        if '-d' in args:
            lines = [f'Identifier={self.identifier}', 'Format=app bundle with Mach-O thin (arm64)',
                     'Signature=adhoc' if self.adhoc else 'Authority=Developer ID Application',
                     'TeamIdentifier=' + ('not set' if self.adhoc else self.team)]
            return done('', 0 if self.valid else 1, '\n'.join(lines) + '\n')
        assert '--verify' in args and '--strict' in args, args
        if not self.valid:
            return done('', 1, 'a sealed resource is missing or invalid')
        if '-R' not in args:
            return done()
        requirement = args[args.index('-R') + 1]
        satisfied = (
            not self.adhoc
            and requirement.startswith('=anchor apple generic and ')
            and f'identifier "{self.identifier}"' in requirement
            and f'certificate leaf[subject.OU] = "{self.team}"' in requirement
        )
        return done('', 0 if satisfied else 3)

    def verifications(self):
        return [call for call in self.calls if '--verify' in call]


class FakeMac:
    """The adapter's runner: routes `codesign` to a FakeCodesign and everything else to a table of answers."""

    def __init__(self, codesign=None):
        self.codesign = codesign or FakeCodesign()
        self.answers = {}                               # tuple(args) or args[0] -> CompletedProcess or list of them
        self.calls = []

    def __call__(self, args, timeout=10):
        args = [str(a) for a in args]
        if args[0] == macos.CODESIGN:
            return self.codesign(args, timeout)
        self.calls.append((args, timeout))
        for key in (tuple(args), tuple(args[:2]), args[0]):
            if key in self.answers:
                answer = self.answers[key]
                return answer.pop(0) if isinstance(answer, list) else answer
        return done('', 0)


class Folder:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)


# -- the designated requirement ---------------------------------------------------------------------------------------

class DesignatedRequirementTests(Folder, unittest.TestCase):
    """An app is the same app when its team and bundle id still match, not when its bytes do."""

    def setUp(self):
        super().setUp()
        self.home = self.root / 'home'
        self.app = self.home / 'Applications' / 'Alpha.app'
        (self.app / 'Contents/MacOS').mkdir(parents=True)
        self.write_bundle('com.example.alpha', '0.2.0', b'build one')
        self.mac = FakeMac()
        self.codesign = self.mac.codesign
        self.adapter = macos.MacAdapter(run=self.mac, home=self.home, helper=self.root / 'helper',
                                        fabric_hub_file=self.root / 'hub.json', uid=501, platform='darwin')
        self.addCleanup(self.adapter.close)

    def write_bundle(self, bundle_id, version, executable):
        info = {'CFBundleIdentifier': bundle_id, 'CFBundleExecutable': 'Alpha', 'CFBundleShortVersionString': version}
        (self.app / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
        (self.app / 'Contents/MacOS/Alpha').write_bytes(executable)

    def enrolled(self):
        return {'kind': 'app', 'path': str(self.app), 'identity': self.adapter.fingerprint('app', self.app),
                'readiness': {'kind': 'process'}}

    def test_requirement_pins_team_and_bundle_id_only(self):
        self.assertEqual(
            macos.requirement_for('com.example.alpha', TEAM),
            'anchor apple generic and identifier "com.example.alpha" and certificate leaf[subject.OU] = "ABCDE12345"',
        )

    def test_requirement_refuses_injected_or_malformed_parts(self):
        for bundle_id, team in (('x" or anchor apple', TEAM), ('', TEAM), ('com.example.alpha', TEAM + '" or true'),
                                ('com.example.alpha', 'abcde12345'), ('com.example.alpha', None)):
            with self.assertRaisesRegex(broker.LifecycleError, 'identity_mismatch'):
                macos.requirement_for(bundle_id, team)

    def test_enrollment_records_requirement_and_no_volatile_metadata(self):
        identity = self.adapter.fingerprint('app', self.app)
        self.assertEqual(identity, {
            'scheme': broker.IDENTITY_SCHEME,
            'bundleId': 'com.example.alpha',
            'teamId': TEAM,
            'requirement': macos.requirement_for('com.example.alpha', TEAM),
            'designatedRequirement': 'identifier "com.example.alpha" and anchor apple generic '
                                     'and certificate leaf[subject.OU] = ABCDE12345',
        })
        broker.app_identity(identity)                   # what is enrolled is what the registry accepts
        verify = self.codesign.verifications()[-1]
        self.assertEqual(verify[:4], [macos.CODESIGN, '--verify', '--deep', '--strict'])
        self.assertEqual(verify[-3:], ['-R', '=' + identity['requirement'], str(self.app)])

    def test_same_team_rebuild_and_update_still_match(self):
        enrolled = self.enrolled()
        self.assertTrue(self.adapter.identity_matches(enrolled))
        # A reinstall: new inode, times, version and executable bytes, hence a new code hash.
        shutil.rmtree(self.app)
        (self.app / 'Contents/MacOS').mkdir(parents=True)
        self.write_bundle('com.example.alpha', '0.3.0', b'build two, same team')
        self.assertTrue(self.adapter.identity_matches(enrolled))
        self.assertEqual(len(self.codesign.verifications()), 3)     # enroll, first match, after the update

    def test_different_team_is_refused(self):
        enrolled = self.enrolled()
        self.write_bundle('com.example.alpha', '0.2.0', b'impostor team')
        self.codesign.team = OTHER_TEAM
        self.assertFalse(self.adapter.identity_matches(enrolled))

    def test_adhoc_signature_is_refused_at_enrollment_and_at_match(self):
        enrolled = self.enrolled()
        self.write_bundle('com.example.alpha', '0.2.0', b'ad hoc rebuild')
        self.codesign.adhoc = True
        self.assertFalse(self.adapter.identity_matches(enrolled))
        with self.assertRaisesRegex(broker.LifecycleError, 'identity_mismatch'):
            self.adapter.fingerprint('app', self.app)

    def test_signature_without_team_is_refused_before_any_requirement_check(self):
        self.codesign.adhoc = True
        with self.assertRaisesRegex(broker.LifecycleError, 'identity_mismatch'):
            self.adapter.fingerprint('app', self.app)
        self.assertEqual(self.codesign.verifications(), [])

    def test_unsigned_or_broken_seal_is_refused(self):
        enrolled = self.enrolled()
        self.write_bundle('com.example.alpha', '0.2.0', b'tampered')
        self.codesign.valid = False
        self.assertFalse(self.adapter.identity_matches(enrolled))
        with self.assertRaisesRegex(broker.LifecycleError, 'identity_mismatch'):
            self.adapter.fingerprint('app', self.app)

    def test_other_bundle_id_from_the_same_team_is_refused(self):
        enrolled = self.enrolled()
        self.write_bundle('com.example.other', '0.8.2', b'another product')
        self.codesign.identifier = 'com.example.other'
        self.assertFalse(self.adapter.identity_matches(enrolled))

    def test_signing_identifier_must_equal_info_plist_bundle_id(self):
        self.codesign.identifier = 'com.example.other'
        with self.assertRaisesRegex(broker.LifecycleError, 'identity_mismatch'):
            self.adapter.fingerprint('app', self.app)

    def test_unchanged_bundle_is_deep_verified_once(self):
        enrolled = self.enrolled()
        before = len(self.codesign.verifications())
        for _ in range(3):
            self.assertTrue(self.adapter.identity_matches(enrolled))
        self.assertEqual(len(self.codesign.verifications()) - before, 1)

    def test_bundle_changing_during_verification_is_not_trusted_or_cached(self):
        enrolled = self.enrolled()
        hashes = iter(['current', 'before', 'after', 'current2', 'before2', 'before2'])
        with mock.patch.object(macos, 'bundle_metadata_hash', side_effect=lambda _p, **_k: next(hashes)):
            self.assertFalse(self.adapter.identity_matches(enrolled))
            self.assertTrue(self.adapter.identity_matches(enrolled))

    def test_an_identity_of_another_scheme_is_a_mismatch_without_codesign(self):
        enrolled = self.enrolled()
        enrolled['identity'] = {'bundleId': 'com.example.alpha', 'codeHash': 'old-scheme'}
        before = len(self.codesign.calls)
        self.assertFalse(self.adapter.identity_matches(enrolled))
        self.assertEqual(self.adapter.probe(enrolled)['state'], 'identity_mismatch')
        self.assertEqual(len(self.codesign.calls), before)

    def test_outside_installed_application_roots_is_not_installed(self):
        elsewhere = macos.MacAdapter(run=self.mac, home=self.root / 'another-home', helper=self.root / 'helper',
                                     fabric_hub_file=self.root / 'hub.json', uid=501, platform='darwin')
        with self.assertRaisesRegex(broker.LifecycleError, 'not_installed'):
            elsewhere.fingerprint('app', self.app)

    def test_verification_timeout_is_not_a_false_identity_mismatch(self):
        enrolled = self.enrolled()
        self.write_bundle('com.example.alpha', '0.2.0', b'slow verification')

        def slow(args, timeout=10):
            raise broker.LifecycleError('verification_timeout')

        self.mac.codesign = slow
        with self.assertRaisesRegex(broker.LifecycleError, 'verification_timeout'):
            self.adapter.identity_matches(enrolled)

        class Hung:
            """A codesign whose first wait times out: the Runner kills its group and names the timeout."""
            pid = 999999
            returncode = None

            def __init__(self, *a, **kw):
                self.calls = 0

            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired([macos.CODESIGN], 60)
                return '', ''

        with mock.patch.object(macos.subprocess, 'Popen', Hung), mock.patch.object(macos.osproc, 'signal_group') as kill:
            with self.assertRaisesRegex(broker.LifecycleError, 'verification_timeout'):
                macos.Runner()([macos.CODESIGN, '--verify', str(self.app)], timeout=60)
        kill.assert_called_once_with(999999, macos._KILL)


@unittest.skipUnless(sys.platform == 'darwin' and Path(macos.CODESIGN).exists(), 'needs macOS codesign')
class RealCodesignTests(unittest.TestCase):
    """The requirement text is checked by the real Security framework, not only by the fake."""

    def test_adhoc_signed_bundle_has_a_valid_signature_but_fails_the_team_requirement(self):
        run = macos.Runner()
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp).resolve() / 'AdHoc.app'
            (app / 'Contents/MacOS').mkdir(parents=True)
            info = {'CFBundleIdentifier': 'com.example.alpha', 'CFBundleExecutable': 'AdHoc'}
            (app / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
            shutil.copyfile('/usr/bin/true', app / 'Contents/MacOS/AdHoc')
            (app / 'Contents/MacOS/AdHoc').chmod(0o755)
            signed = subprocess.run([macos.CODESIGN, '-s', '-', '--force', '--deep', str(app)], capture_output=True)
            self.assertEqual(signed.returncode, 0, signed.stderr)
            plain = run([macos.CODESIGN, '--verify', '--deep', '--strict', str(app)])
            self.assertEqual(plain.returncode, 0, plain.stderr)
            signature = macos.signature_info(app, run=run)
            self.assertIsNone(signature['teamId'])
            self.assertTrue(signature['adhoc'])
            self.assertFalse(macos.satisfies(app, macos.requirement_for('com.example.alpha', TEAM), deep=True, run=run))

    def test_an_installed_signed_bundle_satisfies_its_own_team_only(self):
        import app_update

        app = app_update.installed_app()
        if app is None or app_update.bundle_info(app) is None:
            self.skipTest("the engine's own app is not installed here")
        run = macos.Runner()
        signature = macos.signature_info(app, run=run)
        if signature['teamId'] != app_update.TEAM_ID:
            self.skipTest('the installed app is a local build, not the released one')
        own = app_update.BUNDLE_ID
        self.assertTrue(macos.satisfies(app, macos.requirement_for(own, app_update.TEAM_ID), deep=True, run=run))
        self.assertFalse(macos.satisfies(app, macos.requirement_for(own, OTHER_TEAM), deep=True, run=run))
        self.assertFalse(macos.satisfies(app, macos.requirement_for('com.example.alpha', app_update.TEAM_ID),
                                         deep=True, run=run))


# -- installed places, interpreters, launchd identities --------------------------------------------------------------

@unittest.skipUnless(POSIX, 'POSIX ownership and modes')
class InstalledPathTests(unittest.TestCase):
    def test_only_the_system_applications_parent_may_be_admin_writable(self):
        app = Path('/Applications/Enrolled.app')

        def info(path, *args, **kwargs):
            if path == app:
                return SimpleNamespace(st_uid=os.getuid(), st_gid=20, st_mode=0o40755)
            if path == Path('/Applications'):
                return SimpleNamespace(st_uid=0, st_gid=80, st_mode=0o40775)
            return SimpleNamespace(st_uid=0, st_gid=0, st_mode=0o40755)

        with mock.patch.object(Path, 'stat', info), mock.patch.object(Path, 'resolve', lambda p: p):
            self.assertEqual(macos.trusted_path(app, True), app)

        def wrong_group(path, *args, **kwargs):
            result = info(path)
            if path == Path('/Applications'):
                result.st_gid = 20
            return result

        with mock.patch.object(Path, 'stat', wrong_group), mock.patch.object(Path, 'resolve', lambda p: p):
            with self.assertRaisesRegex(broker.LifecycleError, 'identity_mismatch'):
                macos.trusted_path(app, True)

    def test_the_homebrew_exception_is_only_for_a_pinned_interpreter(self):
        executable = Path('/opt/homebrew/Cellar/python/3/bin/python3')

        def info(path, *args, **kwargs):
            if path == executable:
                return SimpleNamespace(st_uid=os.getuid(), st_gid=80, st_mode=0o100755)
            mode = 0o40775 if path == Path('/opt/homebrew/Cellar') else 0o40755
            return SimpleNamespace(st_uid=os.getuid(), st_gid=80, st_mode=mode)

        with mock.patch.object(Path, 'stat', info), mock.patch.object(Path, 'resolve', lambda p: p):
            self.assertEqual(macos.trusted_path(executable, interpreter=True), executable)
            with self.assertRaisesRegex(broker.LifecycleError, 'identity_mismatch'):
                macos.trusted_path(executable)

    def test_stable_interpreter_paths(self):
        for ok in ('/opt/homebrew/opt/python@3.14/bin/python3.14', '/usr/bin/python3', '/usr/local/opt/node/bin/node'):
            self.assertTrue(macos.stable_executable(ok), ok)
        for bad in ('/opt/homebrew/Cellar/python@3.14/3.14.7/bin/python3.14', '/var/empty/.venv/bin/python',
                    '/opt/homebrew/opt/../Cellar/x'):
            self.assertFalse(macos.stable_executable(bad), bad)

    def test_a_stable_interpreter_is_pinned_by_path_not_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            agents = home / 'Library/LaunchAgents'
            agents.mkdir(parents=True)
            script = home / 'job.sh'
            script.write_text('#!/bin/sh\n')
            os.chmod(script, 0o755)
            plist = agents / 'com.example.job.plist'
            plist.write_bytes(plistlib.dumps({'Label': 'com.example.job', 'ProgramArguments': ['/bin/sh', str(script)]}))
            identity = macos.launchd_fingerprint(plist, home=home)
            broker.launchd_identity(identity)           # what is enrolled is what the registry accepts
            self.assertEqual(identity['artifacts']['/bin/sh'], macos.STABLE_PIN)
            self.assertNotEqual(identity['artifacts'][str(script)], macos.STABLE_PIN, "the job's own code is hashed")
            self.assertEqual(identity['label'], 'com.example.job')
            elsewhere = home / 'job.plist'
            elsewhere.write_bytes(plist.read_bytes())
            with self.assertRaisesRegex(broker.LifecycleError, 'not_installed'):
                macos.launchd_fingerprint(elsewhere, home=home)


# -- the metadata walk and the verification cache --------------------------------------------------------------------

def app_target(path='/Applications/X.app', bundle_id='com.example.x'):
    return {'kind': 'app', 'path': path, 'readiness': {'kind': 'process'},
            'identity': {'scheme': broker.IDENTITY_SCHEME, 'bundleId': bundle_id, 'teamId': TEAM,
                         'requirement': macos.requirement_for(bundle_id, TEAM)}}


def adapter(root, **kw):
    kw.setdefault('run', FakeMac())
    kw.setdefault('helper', Path(root) / 'helper')
    kw.setdefault('fabric_hub_file', Path(root) / 'hub.json')
    return macos.MacAdapter(uid=501, home=Path(root) / 'home', platform='darwin', **kw)


class VerificationCacheTests(Folder, unittest.TestCase):
    def test_concurrent_first_checks_of_one_bundle_run_codesign_once(self):
        calls = []

        def slow_verify(path, identity, **_kw):
            calls.append(path)
            time.sleep(0.3)
            return 'h1'

        a = adapter(self.root)
        self.addCleanup(a.close)
        results = []
        with mock.patch.object(macos, 'verify_app', side_effect=slow_verify), \
                mock.patch.object(macos, 'bundle_metadata_hash', return_value='h1'):
            threads = [threading.Thread(target=lambda: results.append(a._app_matches(app_target()))) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(calls), 1, 'one codesign for five concurrent callers')
        self.assertEqual(results, [True] * 5)

    def test_a_waiter_gives_up_honestly(self):
        a = adapter(self.root)
        self.addCleanup(a.close)
        key = ('/Applications/X.app', broker.canonical(app_target()['identity']))
        a._inflight[key] = concurrent.futures.Future()  # a check of this bundle that never finishes
        with mock.patch.object(macos, 'VERIFY_WAIT_SECONDS', 0.1), \
                mock.patch.object(macos, 'bundle_metadata_hash', return_value='h1'):
            with self.assertRaises(broker.LifecycleError) as pending:
                a._app_matches(app_target())
        self.assertEqual(pending.exception.code, 'verification_pending')

    def test_a_slow_deep_check_answers_pending_within_the_deadline_and_caches_later(self):
        def slow_verify(path, identity, **_kw):
            time.sleep(1.5)
            return 'h1'

        a = adapter(self.root)
        self.addCleanup(a.close)
        with mock.patch.object(macos, 'verify_app', slow_verify), \
                mock.patch.object(macos, 'bundle_metadata_hash', return_value='h1'):
            started = time.monotonic()
            with self.assertRaises(broker.LifecycleError) as caught:
                a.identity_matches(app_target(), deadline=time.time() + 0.3)
            self.assertEqual(caught.exception.code, 'verification_pending')
            self.assertLess(time.monotonic() - started, 1.0, "the operation's deadline bounds the wait")
            time.sleep(1.6)
            started = time.monotonic()
            self.assertTrue(a.identity_matches(app_target(), deadline=time.time() + 0.3))
            self.assertLess(time.monotonic() - started, 0.2, 'the finished check was cached')

    def test_an_unchanged_bundle_is_not_rechecked_after_a_restart(self):
        cache = self.root / 'verified-bundles.json'
        calls = []
        verify = lambda path, identity, **_kw: calls.append(path) or 'h1'  # noqa: E731
        with mock.patch.object(macos, 'verify_app', side_effect=verify), \
                mock.patch.object(macos, 'bundle_metadata_hash', return_value='h1'):
            for _ in range(2):
                a = adapter(self.root, cache_path=cache)
                self.assertTrue(a._app_matches(app_target()))
                a.close()
        self.assertEqual(len(calls), 1, 'the second process trusted the persisted check')
        if POSIX:
            self.assertEqual(oct(cache.stat().st_mode & 0o777), '0o600')
        with mock.patch.object(macos, 'verify_app', side_effect=verify), \
                mock.patch.object(macos, 'bundle_metadata_hash', return_value='h2'):
            a = adapter(self.root, cache_path=cache)
            a._app_matches(app_target())
            a.close()
        self.assertEqual(len(calls), 2, 'a changed bundle is verified again')

    def test_a_failed_check_is_not_persisted_as_trusted(self):
        cache = self.root / 'verified-bundles.json'
        with mock.patch.object(macos, 'verify_app', return_value=None), \
                mock.patch.object(macos, 'bundle_metadata_hash', return_value='h1'):
            a = adapter(self.root, cache_path=cache)
            self.assertFalse(a._app_matches(app_target()))
            a.close()
        self.assertEqual(json.loads(cache.read_text()), {})

    @unittest.skipUnless(POSIX, 'POSIX modes')
    def test_a_cache_that_is_not_private_is_not_trusted(self):
        cache = self.root / 'verified-bundles.json'
        cache.write_text(json.dumps({'/Applications/X.app\nforged-key': 'forged-verdict'}))
        os.chmod(cache, 0o600)
        self.assertEqual(len(adapter(self.root, cache_path=cache)._verified), 1, 'control: a private cache loads')
        os.chmod(cache, 0o644)
        self.assertEqual(adapter(self.root, cache_path=cache)._verified, {}, 'a readable cache is ignored, not trusted')
        os.chmod(cache, 0o600)
        link = self.root / 'linked.json'
        link.symlink_to(cache)
        self.assertEqual(adapter(self.root, cache_path=link)._verified, {}, 'a linked cache is ignored')

    def test_the_cache_is_bounded_and_drops_the_oldest(self):
        cache = self.root / 'verified-bundles.json'
        with mock.patch.object(macos, 'CACHE_LIMIT', 3), \
                mock.patch.object(macos, 'verify_app', side_effect=lambda path, identity, **_kw: 'h-' + path), \
                mock.patch.object(macos, 'bundle_metadata_hash', side_effect=lambda p, **_kw: 'h-' + str(p)):
            a = adapter(self.root, cache_path=cache)
            for n in range(5):
                self.assertTrue(a._app_matches(app_target(f'/Applications/X{n}.app')))
            a.close()
            kept = sorted(json.loads(cache.read_text()))
            self.assertEqual([k.split('\n')[0] for k in kept],
                             ['/Applications/X2.app', '/Applications/X3.app', '/Applications/X4.app'])
            self.assertEqual(len(adapter(self.root, cache_path=cache)._verified), 3, 'a reload stays bounded too')

    def test_a_walk_that_runs_out_of_time_is_pending_not_terminal(self):
        app = self.root / 'Slow.app'
        (app / 'Contents').mkdir(parents=True)
        ticks = iter([0.0] + [100.0] * 10)
        with self.assertRaisesRegex(broker.LifecycleError, 'verification_pending'):
            macos.bundle_metadata_hash(app, clock=lambda: next(ticks))
        self.assertIn('verification_pending', broker.TERMINAL)

    def test_the_walk_sees_an_in_place_write_and_refuses_a_link_out_of_the_bundle(self):
        app = self.root / 'Alpha.app'
        (app / 'Contents').mkdir(parents=True)
        (app / 'Contents/file').write_bytes(b'one')
        first = macos.bundle_metadata_hash(app)
        stat_before = (app / 'Contents/file').stat()
        time.sleep(0.01)
        (app / 'Contents/file').write_bytes(b'two')
        os.utime(app / 'Contents/file', ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
        self.assertNotEqual(macos.bundle_metadata_hash(app), first, 'the change time still moved')
        if POSIX:
            (app / 'Contents/out').symlink_to(self.root)
            with self.assertRaisesRegex(broker.LifecycleError, 'identity_mismatch'):
                macos.bundle_metadata_hash(app)

    def test_the_deep_check_gets_a_longer_timeout_than_a_plain_one(self):
        run = mock.Mock(return_value=done())
        macos.satisfies('/Applications/X.app', 'r', deep=True, run=run)
        self.assertEqual(run.call_args.kwargs['timeout'], macos.DEEP_VERIFY_TIMEOUT)
        macos.satisfies('/Applications/X.app', 'r', deep=False, run=run)
        self.assertEqual(run.call_args.kwargs['timeout'], macos.PLAIN_VERIFY_TIMEOUT)


# -- process groups ----------------------------------------------------------------------------------------------

@unittest.skipUnless(POSIX, 'POSIX process groups')
class RunnerTests(Folder, unittest.TestCase):
    def test_a_timed_out_child_takes_its_children_with_it(self):
        marker = self.root / 'child.pid'
        started = time.monotonic()
        with self.assertRaises(broker.LifecycleError) as caught:
            macos.Runner()(['/bin/sh', '-c', f'sleep 30 & echo $! > {marker}; wait'], timeout=0.5)
        self.assertEqual(caught.exception.code, 'delivery_unknown')
        self.assertLess(time.monotonic() - started, 5, 'a timeout answers now, not when the orphan finishes')
        time.sleep(0.2)
        import osproc
        self.assertFalse(osproc.alive(int(marker.read_text())), 'the grandchild died with its group')

    def test_closing_ends_a_running_child_at_once(self):
        run = macos.Runner()
        answers = []
        worker = threading.Thread(target=lambda: answers.append(run(['/bin/sleep', '30'], timeout=60)))
        worker.start()
        time.sleep(0.3)
        started = time.monotonic()
        run.close()
        worker.join(5)
        self.assertFalse(worker.is_alive(), 'the child is gone; nothing waits 30 s')
        self.assertLess(time.monotonic() - started, 3)

    def test_a_child_gets_only_the_system_path(self):
        out = macos.Runner(home=self.root)(['/usr/bin/env'])
        env = dict(line.split('=', 1) for line in out.stdout.splitlines() if '=' in line)
        self.assertEqual(env['PATH'], macos.SYSTEM_PATH)
        self.assertEqual(env['HOME'], str(self.root))
        self.assertLessEqual(set(env), {'PATH', 'HOME', 'USER', '__CF_USER_TEXT_ENCODING'})


# -- start, stop, probe ------------------------------------------------------------------------------------------

class EffectTests(Folder, unittest.TestCase):
    def setUp(self):
        super().setUp()
        (self.root / 'helper').write_text('')
        self.mac = FakeMac()
        self.adapter = adapter(self.root, run=self.mac)
        self.adapter.identity_matches = lambda target, *, deadline=None: True
        self.helper = str(self.root / 'helper')

    def helper_says(self, *answers):
        self.mac.answers[self.helper] = [done(json.dumps(a)) for a in answers]

    def test_an_app_stop_signals_only_the_pid_the_os_reports_now(self):
        app = {'kind': 'app', 'path': '/Applications/X.app'}
        self.helper_says({'state': 'ready', 'pid': 42}, {'state': 'quit_requested', 'pid': 42})
        with mock.patch('os.kill') as kill:
            self.assertEqual(self.adapter.stop(app, 42), 'quit_requested')
            self.assertEqual(self.mac.calls[-1][0][1:], ['quit', '/Applications/X.app', '42'])
            kill.assert_not_called()
        self.helper_says({'state': 'ready', 'pid': 42}, {'state': 'quit_refused', 'pid': 42})
        with mock.patch('os.kill') as kill:
            self.assertEqual(self.adapter.stop(app, 42), 'sigterm', 'a refused quit falls through to SIGTERM')
            kill.assert_called_once_with(42, signal.SIGTERM)
        self.helper_says({'state': 'ready', 'pid': 42})
        with mock.patch('os.kill') as kill:
            self.assertEqual(self.adapter.stop(app, 42, mode='term'), 'sigterm')
            kill.assert_called_once_with(42, signal.SIGTERM)
        self.helper_says({'state': 'ready', 'pid': 43})
        with mock.patch('os.kill') as kill:
            with self.assertRaisesRegex(broker.LifecycleError, 'identity_mismatch'):
                self.adapter.stop(app, 42)
            kill.assert_not_called()
        if POSIX:
            self.helper_says({'state': 'ready', 'pid': 42})
            with mock.patch('os.kill') as kill:
                self.assertEqual(self.adapter.stop(app, 42, force=True), 'sigkill')
                kill.assert_called_once_with(42, signal.SIGKILL)
        self.helper_says({'state': 'ready', 'pid': 42})
        with mock.patch('os.kill', side_effect=ProcessLookupError):
            self.assertEqual(self.adapter.stop(app, 42, mode='term'), 'already_exited')

    def test_a_quit_answered_identity_mismatch_signals_nothing(self):
        app = {'kind': 'app', 'path': '/Applications/X.app'}
        self.helper_says({'state': 'ready', 'pid': 42}, {'state': 'identity_mismatch'})
        with mock.patch('os.kill') as kill:
            with self.assertRaisesRegex(broker.LifecycleError, 'identity_mismatch'):
                self.adapter.stop(app, 42)
            kill.assert_not_called()
        self.helper_says({'state': 'ready', 'pid': 42}, {'state': 'not_installed'})
        with mock.patch('os.kill') as kill:
            with self.assertRaisesRegex(broker.LifecycleError, 'failed'):
                self.adapter.stop(app, 42)             # only `quit_refused` falls through to SIGTERM
            kill.assert_not_called()

    @unittest.skipUnless(POSIX, 'POSIX modes and links')
    def test_the_helpers_folder_is_checked_before_it_runs(self):
        app = {'kind': 'app', 'path': '/Applications/X.app', 'readiness': {'kind': 'process'}}
        self.helper_says({'state': 'ready', 'pid': 9})
        self.assertEqual(self.adapter.probe(app)['state'], 'ready')
        os.chmod(self.root, 0o777)                  # someone else could swap the helper between check and exec
        try:
            self.helper_says({'state': 'ready', 'pid': 9})
            self.assertEqual(self.adapter.probe(app)['state'], 'setup_required')
            with self.assertRaisesRegex(broker.LifecycleError, 'setup_required'):
                self.adapter.start(app)
        finally:
            os.chmod(self.root, 0o700)
        linked = self.root / 'linked'
        linked.symlink_to(self.root, target_is_directory=True)
        through_link = adapter(self.root, run=self.mac, helper=linked / 'helper')
        through_link.identity_matches = lambda target, *, deadline=None: True
        self.assertEqual(through_link.probe(app)['state'], 'setup_required')

    def test_an_unreadable_disabled_list_is_not_an_operator_hold(self):
        job = {'kind': 'launchd', 'path': '/var/empty/Library/LaunchAgents/com.example.job.plist',
               'identity': {'label': 'com.example.job'}, 'readiness': {'kind': 'process'}}
        self.adapter.identity_matches = lambda target, *, deadline=None: True
        self.mac.answers[(macos.LAUNCHCTL, 'print-disabled')] = [done('', 5), done('', 5)]
        seen = self.adapter.probe(job)
        self.assertEqual((seen['state'], seen['reason']), ('failed', 'launchd_disabled_state_unknown'))
        self.mac.answers[(macos.LAUNCHCTL, 'print-disabled')] = [done('', 5), done('', 5)]
        with self.assertRaisesRegex(broker.LifecycleError, 'failed'):
            self.adapter.start(job)
        self.assertFalse(any(c[0][1] in ('kickstart', 'bootstrap', 'enable') for c in self.mac.calls))
        self.mac.answers[(macos.LAUNCHCTL, 'print', 'gui/501/com.example.job')] = done('state = waiting\n')
        self.mac.answers[(macos.LAUNCHCTL, 'print-disabled')] = [done('', 5), done('{}\n')]
        self.assertEqual(self.adapter.probe(job)['state'], 'stopped', 'a transient failure is read once more')

    def test_a_launchd_stop_is_a_bootout_and_force_is_sigkill(self):
        self.adapter._launchd_state = lambda target: {'state': 'ready', 'pid': 7}
        job = {'kind': 'launchd', 'path': '/x.plist', 'identity': {'label': 'com.example.job'}}
        self.assertEqual(self.adapter.stop(job, 7), 'launchd_bootout')
        self.assertEqual(self.mac.calls[-1], ([macos.LAUNCHCTL, 'bootout', 'gui/501/com.example.job'], 30))
        self.assertEqual(self.adapter.stop(job, 7, force=True), 'launchd_sigkill')
        self.assertEqual(self.mac.calls[-1][0][:3], [macos.LAUNCHCTL, 'kill', 'SIGKILL'])
        with self.assertRaisesRegex(broker.LifecycleError, 'identity_mismatch'):
            self.adapter.stop(job, 8)
        self.mac.answers[macos.LAUNCHCTL] = done('', 5)
        with self.assertRaisesRegex(broker.LifecycleError, 'delivery_unknown'):
            self.adapter.stop(job, 7)

    def started_with(self, target):
        self.helper_says({'state': 'accepted_by_os', 'pid': 5})
        self.adapter.start(target)
        return self.mac.calls[-1][0]

    def test_the_background_flag_is_passed_only_when_enrolled(self):
        app = {'kind': 'app', 'path': '/Applications/X.app'}
        self.assertEqual(self.started_with(app), [self.helper, 'open', '/Applications/X.app'])
        self.assertEqual(self.started_with(app | {'backgroundLaunch': False})[-1], '/Applications/X.app')
        self.assertEqual(self.started_with(app | {'backgroundLaunch': True}),
                         [self.helper, 'open', '/Applications/X.app', '--background'])

    def test_an_app_start_needs_a_login_session_and_the_helper(self):
        app = {'kind': 'app', 'path': '/Applications/X.app'}
        self.mac.answers[(macos.LAUNCHCTL, 'print')] = done('', 113)
        with self.assertRaisesRegex(broker.LifecycleError, 'login_required'):
            self.adapter.start(app)
        del self.mac.answers[(macos.LAUNCHCTL, 'print')]
        self.helper_says({'state': 'delivery_unknown'})
        with self.assertRaisesRegex(broker.LifecycleError, 'delivery_unknown'):
            self.adapter.start(app)
        (self.root / 'helper').unlink()
        with self.assertRaisesRegex(broker.LifecycleError, 'setup_required'):
            self.adapter.start(app)
        self.assertEqual(self.adapter.probe(app | {'readiness': {'kind': 'process'}})['state'], 'setup_required')

    def test_a_launchd_start_kickstarts_a_loaded_job_and_bootstraps_one_that_is_not(self):
        job = {'kind': 'launchd', 'path': '/var/empty/Library/LaunchAgents/com.example.job.plist',
               'identity': {'label': 'com.example.job'}}
        self.mac.answers[(macos.LAUNCHCTL, 'print', 'gui/501/com.example.job')] = done('state = waiting\n')
        self.assertEqual(self.adapter.start(job), 'launchd_kickstart')
        self.assertEqual(self.mac.calls[-1][0], [macos.LAUNCHCTL, 'kickstart', 'gui/501/com.example.job'])
        self.mac.answers[(macos.LAUNCHCTL, 'print', 'gui/501/com.example.job')] = done('', 113)
        self.assertEqual(self.adapter.start(job), 'launchd_bootstrap')
        self.assertEqual(self.mac.calls[-1][0], [macos.LAUNCHCTL, 'bootstrap', 'gui/501', job['path']])
        self.mac.answers[(macos.LAUNCHCTL, 'print', 'gui/501/com.example.job')] = done('\tpid = 77\n')
        self.assertEqual(self.adapter.start(job), 'already_running')
        self.assertEqual(self.adapter.probe(job | {'readiness': {'kind': 'process'}})['pid'], 77)
        for listing in ('\t"com.example.job" => disabled\n', '\t"com.example.job" => true\n'):
            self.mac.answers[(macos.LAUNCHCTL, 'print-disabled')] = done(listing)
            with self.assertRaisesRegex(broker.LifecycleError, 'held_off'):
                self.adapter.start(job)                 # an operator's disable is a hold, never undone here
        self.assertFalse(any(c[0][1] in ('enable', 'kill') for c in self.mac.calls))

    def test_a_probe_keeps_only_the_helpers_known_fields(self):
        self.helper_says({'state': 'ready', 'pid': 9, 'windows': 1, 'active': True, 'inputIdleSeconds': 3,
                          'injected': 'ignored'})
        seen = self.adapter.probe({'kind': 'app', 'path': '/Applications/X.app', 'readiness': {'kind': 'process'}})
        self.assertEqual((seen['state'], seen['pid'], seen['active'], seen['capability']), ('ready', 9, True, 'process'))
        self.assertNotIn('injected', seen)
        self.mac.answers[self.helper] = done('not json')
        self.assertEqual(self.adapter.probe({'kind': 'app', 'path': '/Applications/X.app',
                                             'readiness': {'kind': 'process'}})['state'], 'failed')


# -- the product profiles ----------------------------------------------------------------------------------------

class SwitchboardProbeTests(Folder, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = self.root / 'Switchboard.app'
        self.cli = self.app / broker.SWITCHBOARD_CLI
        self.cli.parent.mkdir(parents=True)
        self.cli.write_bytes(b'a disposable CLI')
        self.target = {'kind': 'app', 'path': str(self.app), 'readiness': {'kind': 'switchboard-runtime'}, 'identity': {
            'scheme': broker.IDENTITY_SCHEME, 'bundleId': broker.SWITCHBOARD_BUNDLE_ID, 'teamId': TEAM,
            'requirement': macos.requirement_for(broker.SWITCHBOARD_BUNDLE_ID, TEAM),
            'readinessCli': {'path': broker.SWITCHBOARD_CLI, 'identifier': 'switchboard'}}}
        self.mac = FakeMac()
        self.adapter = adapter(self.root, run=self.mac)
        satisfied = mock.patch.object(macos, 'satisfies', return_value=True)
        self.satisfies = satisfied.start()
        self.addCleanup(satisfied.stop)

    def probe(self, app_running, online):
        data = {'live_mode': macos.SWITCHBOARD_ONLINE if online else 'Offline',
                'proxy_address': '127.0.0.1:50001' if online else None}
        self.mac.answers[str(self.cli)] = done(json.dumps({'ok': True, 'data': data}))
        result = self.adapter._switchboard_probe(self.target, macos.observation('ready' if app_running else 'stopped'))
        self.assertEqual(self.mac.calls[-1], ([str(self.cli), '--json', 'status'], 3))
        return result

    def test_an_online_runtime_needs_the_enrolled_app_running(self):
        self.assertEqual(self.probe(True, True)['state'], 'ready')
        self.assertEqual(self.probe(True, True)['capability'], 'runtime-online')
        self.assertEqual(self.probe(False, True)['state'], 'identity_mismatch')

    def test_an_offline_runtime_of_a_running_app_is_capability_missing_not_ready(self):
        result = self.probe(True, False)
        self.assertEqual((result['state'], result['reason']), ('capability_missing', 'app_runtime_offline'))
        self.assertFalse(result['capabilityReady'])

    def test_a_stopped_app_with_its_runtime_offline_may_be_started(self):
        self.assertEqual(self.probe(False, False)['state'], 'stopped')

    def test_a_replaced_cli_is_refused_before_it_runs(self):
        self.cli.write_bytes(b'replacement')
        self.satisfies.return_value = False
        result = self.adapter._switchboard_probe(self.target, macos.observation('ready'))
        self.assertEqual(self.mac.calls, [], 'the CLI was never run')
        self.assertEqual(result['state'], 'identity_mismatch')
        self.satisfies.assert_called_once_with(self.cli, macos.requirement_for('switchboard', TEAM), deep=False,
                                               run=self.mac)

    def test_an_updated_cli_of_the_same_team_is_trusted_and_checked_once_per_file(self):
        self.probe(True, True)
        self.probe(True, True)
        self.assertEqual(self.satisfies.call_count, 1)
        time.sleep(0.002)
        self.cli.write_bytes(b'rebuilt by the same team')
        self.assertEqual(self.probe(True, True)['state'], 'ready')
        self.assertEqual(self.satisfies.call_count, 2)

    def test_the_enrollment_of_switchboard_records_its_cli(self):
        home = self.root / 'home'
        app = home / 'Applications' / 'Switchboard.app'
        (app / 'Contents/MacOS').mkdir(parents=True)
        (app / 'Contents/Info.plist').write_bytes(plistlib.dumps(
            {'CFBundleIdentifier': broker.SWITCHBOARD_BUNDLE_ID, 'CFBundleExecutable': 'Switchboard'}))
        (app / 'Contents/MacOS/Switchboard').write_bytes(b'app')
        (app / broker.SWITCHBOARD_CLI).write_bytes(b'cli')
        self.satisfies.stop()
        signatures = {str(app): broker.SWITCHBOARD_BUNDLE_ID, str(app / broker.SWITCHBOARD_CLI): 'switchboard'}

        def codesign(args, timeout=10):
            ident = signatures[args[-1]]
            if '-r-' in args:
                return done('', 0)
            if '-d' in args:
                return done('', 0, f'Identifier={ident}\nTeamIdentifier={TEAM}\n')
            return done('', 0 if f'identifier "{ident}"' in args[-2] else 3)

        mac = FakeMac(codesign=codesign)
        identity = adapter(self.root, run=mac).fingerprint('app', app)
        self.assertEqual(identity['readinessCli'], {'path': broker.SWITCHBOARD_CLI, 'identifier': 'switchboard'})
        broker.app_identity(identity)
        self.satisfies.start()


class _Hub(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - Fabric's surface answers 404 to anything but POST /mcp
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


class FabricHubProbeTests(Folder, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.hub_file = self.root / 'hub.json'
        self.token_file = self.root / 'hub-door-token'
        self.token_file.write_text('door-token-never-read')
        os.chmod(self.token_file, 0o600)
        self.adapter = adapter(self.root)
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def serve(self):
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Hub)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.server.server_address[1]

    def publish(self, pid=4242, port=None, mode=0o600, **override):
        doc = {'protocol': 'fabric-hub/0.1', 'origin': f'http://127.0.0.1:{port or 1}', 'mcp': '/mcp',
               'doorTokenFile': str(self.token_file), 'pid': pid, 'startedAt': '2026-10-12T20:00:00.000Z',
               'features': ['session-hooks/1']} | override
        self.hub_file.write_text(json.dumps(doc))
        os.chmod(self.hub_file, mode)

    def probe(self, state='ready', pid=4242):
        return self.adapter._fabric_hub_probe(macos.observation(state, pid=pid if state == 'ready' else None))

    def test_a_running_fabric_without_a_hub_is_not_serving(self):
        result = self.probe()                           # a pid stuck in a startup-failure dialog publishes no hub
        self.assertEqual((result['state'], result['reason']), ('capability_missing', 'hub_not_published'))
        self.assertTrue(result['processReady'])
        self.assertFalse(result['capabilityReady'])
        self.assertIn(result['state'], broker.START_TRANSIENT, 'a hub still coming up is progress')

    def test_a_hub_answering_for_this_pid_is_ready(self):
        self.publish(port=self.serve())
        opened = []
        real_open = macos.osprivacy.open

        def spy(path, *a, **kw):
            opened.append(str(path))
            return real_open(path, *a, **kw)

        with mock.patch.object(macos.osprivacy, 'open', side_effect=spy):
            result = self.probe()
        self.assertEqual((result['state'], result['capability']), ('ready', 'fabric-hub'))
        self.assertTrue(result['transportReady'] and result['capabilityReady'])
        self.assertNotIn(str(self.token_file), opened, 'the door token is never read')
        self.assertNotIn('door-token-never-read', json.dumps(result))
        self.assertNotIn('doorTokenFile', result)

    def test_a_hub_of_another_process_is_not_this_fabric(self):
        self.publish(pid=1111, port=self.serve())       # left by a Fabric that died: only its pid proves it
        result = self.probe(pid=4242)
        self.assertEqual((result['state'], result['reason']), ('capability_missing', 'hub_names_another_process'))

    def test_a_hub_that_does_not_answer_is_remote_unreachable(self):
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()                                       # nothing listens there now
        self.publish(port=port)
        result = self.probe()
        self.assertEqual((result['state'], result['reason']), ('remote_unreachable', 'hub_not_answering'))
        self.assertFalse(result['transportReady'])

    def test_a_malformed_or_foreign_hub_is_refused(self):
        port = self.serve()
        for override in ({'protocol': 'fabric-hub/9'}, {'origin': f'http://localhost:{port}'},
                         {'origin': f'http://10.0.0.1:{port}'}, {'origin': f'http://127.0.0.1:{port}/x'},
                         {'pid': '4242'}, {'mcp': '/other'}, {'origin': 4242}, {'origin': None},
                         {'origin': ['http://127.0.0.1:1']}):
            self.publish(port=port, **override)
            result = self.probe()
            self.assertEqual((result['state'], result.get('reason')), ('capability_missing', 'hub_invalid'), override)

    @unittest.skipUnless(POSIX, 'POSIX modes and links')
    def test_a_hub_file_others_can_write_or_a_link_is_refused(self):
        port = self.serve()
        self.publish(port=port, mode=0o622)
        self.assertEqual(self.probe()['reason'], 'hub_invalid')
        self.publish(port=port)
        link = self.root / 'link.json'
        link.symlink_to(self.hub_file)
        linked = adapter(self.root, fabric_hub_file=link)
        self.assertEqual(linked._fabric_hub_probe(macos.observation('ready', pid=4242))['reason'], 'hub_invalid')

    def test_a_stopped_target_is_reported_as_stopped(self):
        self.assertEqual(self.probe(state='stopped')['state'], 'stopped')

    def test_the_probe_routes_fabric_hub_readiness(self):
        (self.root / 'helper').write_text('')
        mac = FakeMac()
        mac.answers[str(self.root / 'helper')] = done(json.dumps({'state': 'ready', 'pid': 4242, 'windows': 2}))
        routed = adapter(self.root, run=mac)
        routed.identity_matches = lambda target, *, deadline=None: True
        result = routed.probe({'kind': 'app', 'path': '/Applications/Fabric.app', 'readiness': {'kind': 'fabric-hub'}})
        self.assertEqual((result['state'], result['reason']), ('capability_missing', 'hub_not_published'))

    def test_the_default_hub_file_is_fabrics_public_place(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('OBSERVATORY_FABRIC_HUB_JSON', None)
            a = macos.MacAdapter(run=FakeMac(), helper=self.root / 'helper', uid=501, platform='darwin')
        self.assertEqual(a.fabric_hub_file,
                         Path.home() / 'Library' / 'Application Support' / 'ai.passioncode.fabric' / 'hub.json')


class FabricServiceProbeTests(Folder, unittest.TestCase):
    """`fabric-service` readiness: the well-known document must name this service, instance, build and pid."""

    def serve(self, doc):
        body = json.dumps(doc).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200 if self.path == '/.well-known/fabric-service' else 404)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_address[1]}'

    def test_the_document_must_name_this_service_and_pid(self):
        readiness = {'kind': 'fabric-service', 'serviceId': 'alpha-service', 'instance': 'default',
                     'buildDigest': 'sha256:' + '0' * 64}
        doc = {'status': 'ready', 'service': {'id': 'alpha-service', 'instance': 'default',
                                              'build': {'digest': readiness['buildDigest']}}, 'process': {'pid': 9}}
        readiness['origin'] = self.serve(doc)
        a = adapter(self.root)
        self.assertEqual(a._service_probe(readiness, macos.observation('ready', pid=9))['state'], 'ready')
        self.assertEqual(a._service_probe(readiness, macos.observation('ready', pid=10))['state'], 'identity_mismatch')
        doc['status'] = 'starting'
        readiness['origin'] = self.serve(doc)
        self.assertEqual(a._service_probe(readiness, macos.observation('ready', pid=9))['state'], 'starting')

    def test_a_document_of_the_wrong_shape_is_a_mismatch_not_a_crash(self):
        readiness = {'kind': 'fabric-service', 'serviceId': 'alpha-service', 'instance': 'default',
                     'buildDigest': 'sha256:' + '0' * 64}
        a = adapter(self.root)
        good = {'id': 'alpha-service', 'instance': 'default', 'build': {'digest': readiness['buildDigest']}}
        for doc in ({'status': 'ready', 'service': 'alpha-service', 'process': {'pid': 9}},
                    {'status': 'ready', 'service': good, 'process': 'pid 9'},
                    {'status': 'ready', 'service': good | {'build': 'sha256:x'}, 'process': {'pid': 9}}):
            readiness['origin'] = self.serve(doc)
            self.assertEqual(a._service_probe(readiness, macos.observation('ready', pid=9))['state'],
                             'identity_mismatch', doc)
        doc = {'status': 'ready', 'service': good, 'process': {'pid': 9},
               'surfaces': {'mcp': {'capabilities': 'alpha.capability.read'}}}
        readiness['origin'] = self.serve(doc)
        seen = a._service_probe(readiness | {'capability': 'alpha.capability'}, macos.observation('ready', pid=9))
        self.assertEqual(seen['state'], 'capability_missing', 'a string of capabilities is not a list of them')
        doc['surfaces'] = 'mcp'
        readiness['origin'] = self.serve(doc)
        self.assertEqual(a._service_probe(readiness, macos.observation('ready', pid=9))['state'], 'ready')


# -- the registry's product profiles ---------------------------------------------------------------------------------

def fabric_identity(bundle_id=broker.FABRIC_BUNDLE_ID):
    return {'scheme': broker.IDENTITY_SCHEME, 'bundleId': bundle_id, 'teamId': TEAM,
            'requirement': macos.requirement_for(bundle_id, TEAM)}


def switchboard_identity(**extra):
    return fabric_identity(broker.SWITCHBOARD_BUNDLE_ID) | {
        'readinessCli': {'path': broker.SWITCHBOARD_CLI, 'identifier': 'switchboard'}} | extra


class ProfileRegistryTests(Folder, unittest.TestCase):
    def load(self, t):
        write_private(self.root / 'registry.json', {'schemaVersion': 1, 'targets': {t['targetId']: t}})
        return broker.registry(self.root / 'registry.json')

    def refused(self, t):
        with self.assertRaises(broker.LifecycleError):
            self.load(t)

    def test_fabric_hub_is_accepted_for_the_fabric_app(self):
        t = target('fabric.app') | {'identity': fabric_identity(), 'readiness': {'kind': 'fabric-hub'}}
        self.assertEqual(self.load(t)['targets']['fabric.app']['readiness'], {'kind': 'fabric-hub'})

    def test_fabric_hub_is_refused_elsewhere_or_with_fields(self):
        base = target('fabric.app') | {'identity': fabric_identity(), 'readiness': {'kind': 'fabric-hub'}}
        self.refused(base | {'identity': fabric_identity('com.example.other')})
        self.refused(base | {'readiness': {'kind': 'fabric-hub', 'origin': 'http://127.0.0.1:47070'}})
        self.refused(base | {'readiness': {'kind': 'fabric-hub', 'capability': 'x'}})
        self.refused(base | {'identity': {'fixture': True}})

    def test_switchboard_runtime_needs_the_switchboard_app_and_its_cli(self):
        base = target('switchboard.app') | {'identity': switchboard_identity(),
                                            'readiness': {'kind': 'switchboard-runtime'}}
        self.load(base)
        bare = dict(switchboard_identity())
        del bare['readinessCli']
        self.refused(base | {'identity': bare})
        self.refused(base | {'identity': fabric_identity()})
        self.refused(base | {'identity': switchboard_identity(readinessCli={'path': 'Contents/MacOS/other',
                                                                             'identifier': 'switchboard'})})
        self.refused(base | {'identity': switchboard_identity(readinessCli={'path': broker.SWITCHBOARD_CLI,
                                                                             'identifier': 'x" or true'})})

    def test_a_cli_is_recorded_only_on_switchboards_identity(self):
        cli = {'readinessCli': {'path': broker.SWITCHBOARD_CLI, 'identifier': 'switchboard'}}
        self.refused(target() | {'identity': fabric_identity('com.example.alpha') | cli})


class ProfileAdmissionTests(BrokerFixture, unittest.TestCase):
    def test_a_profile_capability_may_be_required(self):
        self.policy['targets']['alpha.app'] |= {'identity': fabric_identity(), 'readiness': {'kind': 'fabric-hub'}}
        self.save_policy()
        receipt = self.broker.ensure(self.agent, self.args('cap-1', requiredCapability='fabric-hub'))
        self.assertNotEqual(receipt['state'], 'capability_missing')
        with self.assertRaisesRegex(broker.LifecycleError, 'capability_missing'):
            self.broker.ensure(self.agent, self.args('cap-2', requiredCapability='runtime-online'))


# -- other platforms, the watcher, the helper build ----------------------------------------------------------------

class UnsupportedPlatformTests(BrokerFixture, unittest.TestCase):
    def test_every_call_answers_unsupported_platform(self):
        for platform in ('linux', 'win32'):
            a = macos.platform_adapter(platform)
            self.assertIsInstance(a, macos.UnsupportedAdapter)
            self.assertEqual(a.probe(app_target())['state'], 'unsupported_platform')
            for call in (lambda: a.start(app_target()), lambda: a.stop(app_target(), 1),
                         lambda: a.identity_matches(app_target()), lambda: a.fingerprint('app', '/x')):
                with self.assertRaisesRegex(broker.LifecycleError, 'unsupported_platform'):
                    call()
        self.assertIsInstance(macos.platform_adapter('darwin', run=FakeMac(), helper=self.data / 'h', uid=501,
                                                     fabric_hub_file=self.data / 'hub.json'), macos.MacAdapter)

    def test_the_broker_reports_it_and_never_a_start(self):
        self.broker.close()                             # one broker per state folder
        self.broker = self.make_broker(adapter=macos.platform_adapter('linux'))
        got = self.wait(self.broker.ensure(self.agent, self.args('linux-1')), self.agent)
        self.assertEqual(got['state'], 'unsupported_platform')

    def test_a_mac_adapter_told_another_platform_does_nothing(self):
        a = macos.MacAdapter(run=FakeMac(), helper=self.data / 'h', uid=501, fabric_hub_file=self.data / 'hub.json',
                             platform='linux')
        self.assertEqual(a.probe(app_target())['state'], 'unsupported_platform')
        with self.assertRaisesRegex(broker.LifecycleError, 'unsupported_platform'):
            a.start(app_target())


class WatchTests(Folder, unittest.TestCase):
    def test_events_are_filtered_and_the_watch_ends_on_stop(self):
        (self.root / 'helper').write_text('')
        a = adapter(self.root)
        stop = threading.Event()
        lines = [{'event': 'launched', 'bundleId': 'com.example.x', 'pid': 3},
                 {'event': 'activated', 'bundleId': 'com.example.x', 'pid': 3, 'extra': 'dropped'},
                 {'event': 'renamed', 'bundleId': 'com.example.x', 'pid': 3},
                 {'event': 'terminated', 'bundleId': 'com.example.x', 'pid': '3'}]
        started = []

        def popen(argv, **kw):
            started.append((argv, kw))
            stop.set()
            return SimpleNamespace(stdout=io.StringIO('\n'.join([json.dumps(x) for x in lines] + ['garbage'])),
                                   wait=lambda timeout=None: 0, poll=lambda: 0, pid=1)

        events = []
        a.watch(events.append, stop, popen=popen)
        self.assertEqual(events, [{'event': 'launched', 'bundleId': 'com.example.x', 'pid': 3},
                                  {'event': 'activated', 'bundleId': 'com.example.x', 'pid': 3}])
        argv, kw = started[0]
        self.assertEqual(argv, [str(self.root / 'helper'), 'watch'])
        self.assertEqual(kw['env']['PATH'], macos.SYSTEM_PATH)
        self.assertEqual(len(started), 1, 'a stopped watch does not start the helper again')

    def test_without_a_helper_there_is_nothing_to_watch(self):
        events = []
        adapter(self.root).watch(events.append, threading.Event(), popen=lambda *a, **k: self.fail('started'))
        self.assertEqual(events, [])


class HelperBuildTests(Folder, unittest.TestCase):
    def test_the_build_writes_an_owner_only_executable_whole(self):
        calls = []

        def run(args, timeout=10):
            calls.append(args)
            Path(args[args.index('-o') + 1]).write_bytes(b'\xcf\xfa\xed\xfe binary')
            return done()

        dest = self.root / 'state' / 'bin' / macos.HELPER_NAME
        self.assertEqual(macos.build_helper(dest, swiftc='/usr/bin/swiftc', sdk='/sdk', run=run, platform='darwin'), dest)
        self.assertEqual(calls[0][:5], ['/usr/bin/swiftc', '-sdk', '/sdk', '-O', '-o'])
        self.assertEqual(calls[0][-1], str(macos.HELPER_SOURCE))
        if POSIX:
            self.assertEqual(oct(dest.stat().st_mode & 0o777), '0o700')
            self.assertEqual(oct(dest.parent.stat().st_mode & 0o777), '0o700')
        self.assertEqual(sorted(p.name for p in dest.parent.iterdir()), [macos.HELPER_NAME], 'no build file left')

    def test_a_refused_build_leaves_nothing_and_names_why(self):
        dest = self.root / 'bin' / macos.HELPER_NAME
        failing = lambda args, timeout=10: done('', 1, 'error: one\nerror: two')  # noqa: E731
        with self.assertRaises(broker.LifecycleError) as caught:
            macos.build_helper(dest, swiftc='/usr/bin/swiftc', sdk='/sdk', run=failing, platform='darwin')
        self.assertEqual(caught.exception.code, 'helper_build_failed')
        self.assertIn('error: two', caught.exception.detail)
        self.assertEqual(list(dest.parent.iterdir()), [])

    @unittest.skipUnless(POSIX, 'POSIX modes')
    def test_a_shared_destination_folder_is_refused_not_re_moded(self):
        shared = self.root / 'shared'
        shared.mkdir()
        os.chmod(shared, 0o777)
        with self.assertRaisesRegex(broker.LifecycleError, 'unsafe_config'):
            macos.build_helper(shared / macos.HELPER_NAME, swiftc='/usr/bin/swiftc', sdk='/sdk',
                               run=lambda args, timeout=10: done(), platform='darwin')
        self.assertEqual(oct(shared.stat().st_mode & 0o777), '0o777', 'the folder is left as it was')

    @unittest.skipUnless(POSIX, 'POSIX modes')
    def test_a_helper_others_may_change_is_never_run(self):
        helper = self.root / 'helper'
        helper.write_text('')
        mac = FakeMac()
        a = adapter(self.root, run=mac)
        a.identity_matches = lambda target, *, deadline=None: True
        os.chmod(helper, 0o666)
        app = {'kind': 'app', 'path': '/Applications/X.app', 'readiness': {'kind': 'process'}}
        self.assertEqual(a.probe(app)['state'], 'setup_required')
        with self.assertRaisesRegex(broker.LifecycleError, 'setup_required'):
            a.start(app)
        link = self.root / 'linked-helper'
        os.chmod(helper, 0o700)
        link.symlink_to(helper)
        linked = adapter(self.root, run=mac, helper=link)
        linked.identity_matches = lambda target, *, deadline=None: True
        self.assertEqual(linked.probe(app)['state'], 'setup_required', 'a linked helper is not run either')
        self.assertFalse(any(c[0][0] in (str(helper), str(link)) for c in mac.calls), 'the helper never ran')

    def test_no_compiler_or_another_platform_is_named(self):
        with self.assertRaisesRegex(broker.LifecycleError, 'swiftc_missing'):
            macos.build_helper(self.root / 'h', run=lambda args, timeout=10: done('', 1), platform='darwin')
        with self.assertRaisesRegex(broker.LifecycleError, 'unsupported_platform'):
            macos.build_helper(self.root / 'h', swiftc='/usr/bin/swiftc', platform='linux')

    def test_the_source_ships_beside_the_adapter(self):
        source = macos.HELPER_SOURCE.read_text(encoding='utf-8')
        for command in ('"watch"', '"quit"', '"status"', '"open"', '--background'):
            self.assertIn(command, source)
        self.assertNotIn('NSAppleScript', source, 'Launch Services only, no Apple Events')

    @unittest.skipUnless(sys.platform == 'darwin', 'compiles with swiftc on macOS')
    def test_the_helper_compiles_and_refuses_an_unknown_command(self):
        swiftc = macos.find_swiftc()
        if not swiftc:
            self.skipTest('swiftc is not installed (xcode-select --install)')
        built = macos.build_helper(self.root / 'bin' / macos.HELPER_NAME, swiftc=swiftc)
        answer = macos.Runner()([str(built), 'nonsense'])
        self.assertEqual(answer.returncode, 2)
        self.assertEqual(json.loads(answer.stdout), {'state': 'invalid_argument'})
        outside = macos.Runner()([str(built), 'status', str(self.root / 'Elsewhere.app')])
        self.assertEqual(json.loads(outside.stdout), {'state': 'not_installed'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
