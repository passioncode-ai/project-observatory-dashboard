#!/usr/bin/env python3
"""Which NSWorkspace helper the lifecycle broker runs (OBS-79 step L2r, docs/design/LIFECYCLE.md).

The signed app carries a release build at `Contents/Helpers/lifecycle-native`; the macOS adapter prefers it while the
installed app satisfies the organization's requirement, the helper's own signature names its identifier under the same
team, and it answers the version of the source this engine ships. Otherwise the source-built helper serves, and
`full lifecycle status` and `full doctor` say which one runs and why.

Everything is a fake: `codesign` and both helpers are answered by `FakeTools`, the app is a folder of empty files under a
temporary home, and no installed application is looked at, opened, verified or signalled. Nothing here compiles Swift.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tmp as tmpdir                                   # noqa: E402,F401  a sandboxed workspace for `paths`

import app_update                                      # noqa: E402
import lifecycle_cli                                   # noqa: E402
from node.lifecycle import broker, macos               # noqa: E402

OTHER_TEAM = 'ZZZZZZZZZZ'
APP_TARGET = {'kind': 'app', 'path': '/Applications/X.app', 'readiness': {'kind': 'process'}}


def done(stdout='', returncode=0, stderr=''):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class FakeTools:
    """The adapter's runner. `codesign --verify ... -R =REQ PATH` passes when PATH's fake signature (`signatures`, by
    path: identifier and team) is the one REQ names; each helper answers `--version` with `versions[path]` (the engine's
    own by default) and `status` with `stopped`. Every call is recorded."""

    def __init__(self):
        self.signatures: dict = {}
        self.versions: dict = {}
        self.calls: list = []

    def __call__(self, args, timeout=10):
        args = [str(a) for a in args]
        self.calls.append(args)
        if args[0] == macos.CODESIGN:
            path = args[-1]
            identifier, team = self.signatures.get(path, (None, None))
            requirement = args[args.index('-R') + 1] if '-R' in args else ''
            ok = identifier is not None and requirement == '=' + macos.requirement_for(identifier, team)
            return done('', 0 if ok else 3)
        if args[0] == macos.LAUNCHCTL:
            return done()
        if args[1:] == ['--version']:
            version = self.versions.get(args[0], macos.expected_helper_version())
            return done(json.dumps({'state': 'version', 'version': version}))
        return done(json.dumps({'state': 'stopped'}))

    def ran(self, path) -> list:
        """The calls that executed `path` itself (not codesign reading it)."""
        return [c for c in self.calls if c[0] == str(path)]

    def verified(self, path) -> list:
        return [c for c in self.calls if c[0] == macos.CODESIGN and c[-1] == str(path)]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        os.chmod(self.root, 0o700)
        self.home = self.root / 'home'
        self.app = self.home / 'Applications' / app_update.APP_NAME
        (self.app / 'Contents/MacOS').mkdir(parents=True)
        (self.app / 'Contents/Info.plist').write_bytes(plistlib.dumps(
            {'CFBundleIdentifier': app_update.BUNDLE_ID, 'CFBundleExecutable': 'ProjectObservatory'}))
        (self.app / 'Contents/MacOS/ProjectObservatory').write_bytes(b'app')
        (self.app / 'Contents/Helpers').mkdir()
        self.app_helper = self.app / macos.APP_HELPER
        self.app_helper.write_bytes(b'release helper')
        os.chmod(self.app_helper, 0o755)
        (self.root / 'bin').mkdir(mode=0o700)
        self.source = self.root / 'bin' / macos.HELPER_NAME
        self.source.write_bytes(b'source helper')
        os.chmod(self.source, 0o700)
        self.tools = FakeTools()
        self.sign_app()

    def sign_app(self, team=app_update.TEAM_ID, helper_identifier=macos.APP_HELPER_IDENTIFIER):
        self.tools.signatures[str(self.app)] = (app_update.BUNDLE_ID, team)
        self.tools.signatures[str(self.app_helper)] = (helper_identifier, team)

    def adapter(self, **kw):
        kw.setdefault('app', self.app)
        a = macos.MacAdapter(run=self.tools, home=self.home, helper=self.source, uid=501, platform='darwin',
                             fabric_hub_file=self.root / 'hub.json', **kw)
        a.identity_matches = lambda target, *, deadline=None: True
        self.addCleanup(a.close)
        return a


class HelperSelectionTests(Fixture):
    def test_a_signed_app_that_passes_every_check_drives_apps_and_the_source_helper_is_not_run(self):
        a = self.adapter()
        seen = a.probe(APP_TARGET)
        self.assertEqual(seen['state'], 'stopped')
        self.assertEqual((a.last_choice.in_use, a.last_choice.app_reason), ('app', None))
        self.assertEqual(a.last_choice.path, self.app_helper)
        self.assertEqual([c[1] for c in self.tools.ran(self.app_helper)], ['--version', 'status'])
        # The source build is asked its version once, to know whether it could serve while the app is checked; it
        # is never asked to act.
        self.assertEqual([c[1] for c in self.tools.ran(self.source)], ['--version'])

    def test_the_app_is_checked_deep_and_the_helper_under_its_own_identifier(self):
        self.adapter().probe(APP_TARGET)
        app_check = self.tools.verified(self.app)
        helper_check = self.tools.verified(self.app_helper)
        self.assertEqual(len(app_check), 1)
        self.assertIn('--deep', app_check[0])
        self.assertIn('=' + macos.requirement_for(app_update.BUNDLE_ID, app_update.TEAM_ID), app_check[0])
        self.assertEqual(len(helper_check), 1)
        self.assertNotIn('--deep', helper_check[0])
        self.assertIn('=' + macos.requirement_for(macos.APP_HELPER_IDENTIFIER, app_update.TEAM_ID), helper_check[0])

    def test_an_app_signed_by_another_team_falls_back_and_its_helper_never_runs(self):
        self.sign_app(team=OTHER_TEAM)
        a = self.adapter()
        self.assertEqual(a.probe(APP_TARGET)['state'], 'stopped')
        self.assertEqual((a.last_choice.in_use, a.last_choice.app_reason), ('source', 'app_signature_invalid'))
        self.assertEqual(self.tools.ran(self.app_helper), [])
        self.assertEqual([c[1] for c in self.tools.ran(self.source)], ['--version', 'status'])

    def test_an_ad_hoc_qa_build_falls_back(self):
        self.tools.signatures.pop(str(self.app))               # no team: what an ad hoc signature satisfies
        a = self.adapter()
        a.probe(APP_TARGET)
        self.assertEqual((a.last_choice.in_use, a.last_choice.app_reason), ('source', 'app_signature_invalid'))
        self.assertEqual(self.tools.ran(self.app_helper), [])

    def test_a_helper_signed_under_another_identifier_falls_back_without_running_it(self):
        self.sign_app(helper_identifier='com.example.other')
        a = self.adapter()
        a.probe(APP_TARGET)
        self.assertEqual((a.last_choice.in_use, a.last_choice.app_reason), ('source', 'app_helper_signature_invalid'))
        self.assertEqual(self.tools.ran(self.app_helper), [], 'not even asked its version')

    def test_an_app_helper_from_another_source_falls_back(self):
        self.tools.versions[str(self.app_helper)] = '0000000000000000'
        a = self.adapter()
        a.probe(APP_TARGET)
        self.assertEqual((a.last_choice.in_use, a.last_choice.app_reason), ('source', 'app_helper_outdated'))
        self.assertEqual([c[1] for c in self.tools.ran(self.app_helper)], ['--version'])

    def test_an_app_that_carries_no_helper_is_not_verified_at_all(self):
        self.app_helper.unlink()
        a = self.adapter()
        a.probe(APP_TARGET)
        self.assertEqual((a.last_choice.in_use, a.last_choice.app_reason), ('source', 'app_helper_missing'))
        self.assertEqual(self.tools.verified(self.app), [], 'an app older than 0.30.0 costs no codesign')

    def test_a_helper_others_may_change_is_untrusted_and_never_run(self):
        os.chmod(self.app_helper, 0o777)
        a = self.adapter()
        a.probe(APP_TARGET)
        self.assertEqual((a.last_choice.in_use, a.last_choice.app_reason), ('source', 'app_untrusted'))
        self.assertEqual(self.tools.ran(self.app_helper), [])

    def test_no_installed_app_is_the_source_helper(self):
        a = self.adapter(app=self.root / 'absent.app')
        a.probe(APP_TARGET)
        self.assertEqual((a.last_choice.in_use, a.last_choice.app_reason), ('source', 'app_not_installed'))

    def test_a_released_install_without_swiftc_runs_the_apps_helper(self):
        self.source.unlink()
        a = self.adapter()
        self.assertEqual(a.probe(APP_TARGET)['state'], 'stopped')
        self.assertEqual(a.last_choice.in_use, 'app')

    def test_neither_helper_is_setup_required_naming_both_reasons(self):
        self.source.unlink()
        self.sign_app(team=OTHER_TEAM)
        a = self.adapter()
        seen = a.probe(APP_TARGET)
        self.assertEqual((seen['state'], seen['reason'], seen['appHelper']),
                         ('setup_required', 'helper_not_built', 'app_signature_invalid'))
        self.assertIsNone(a.last_choice.in_use)
        with self.assertRaises(broker.LifecycleError) as refused:
            a.start(APP_TARGET)
        self.assertEqual(refused.exception.code, 'setup_required')
        self.assertEqual(self.tools.ran(self.app_helper), [])

    def test_an_unchanged_app_is_not_checked_again_and_a_changed_one_is(self):
        a = self.adapter()
        a.probe(APP_TARGET)
        a.probe(APP_TARGET)
        self.assertEqual(len(self.tools.verified(self.app_helper)), 1, 'an unchanged bundle is not checked again')
        self.app_helper.write_bytes(b'another build')
        os.utime(self.app_helper, ns=(1, 1))
        self.tools.versions[str(self.app_helper)] = '0000000000000000'
        a.probe(APP_TARGET)
        self.assertEqual(len(self.tools.verified(self.app_helper)), 2, 'a changed bundle is checked again')
        self.assertEqual((a.last_choice.in_use, a.last_choice.app_reason), ('source', 'app_helper_outdated'))

    def test_a_pending_deep_check_does_not_hold_a_call_the_source_helper_can_serve(self):
        release = threading.Event()

        def slow(path, identity, **_kw):
            release.wait(10)
            return macos.bundle_metadata_hash(path)

        a = self.adapter()
        with mock.patch.object(macos, 'verify_app', side_effect=slow):
            started = time.monotonic()
            a.probe(APP_TARGET)
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual((a.last_choice.in_use, a.last_choice.app_reason), ('source', 'app_verification_pending'))
            release.set()
            for _ in range(100):
                a.probe(APP_TARGET)
                if a.last_choice.in_use == 'app':
                    break
                time.sleep(0.05)
        self.assertEqual(a.last_choice.in_use, 'app', 'the landed check is cached and the app helper takes over')

    def test_a_call_runs_the_helper_it_checked_whatever_another_call_chose_meanwhile(self):
        # Review of #383, MEDIUM-1: call A chose the app's helper; before A runs it, call B finds the app refused (being
        # updated) and the source build gone, and settles on no helper. A must still run the helper it checked, and
        # never a source build B refused or a helper nobody checked.
        a = self.adapter()
        real = a._choose_helper
        second = []

        def choose_then_let_b_run(**kw):
            choice = real(**kw)
            if not second:
                self.sign_app(team=OTHER_TEAM)
                self.app_helper.write_bytes(b'mid-update')
                os.utime(self.app_helper, ns=(2, 2))
                self.source.unlink()
                b = threading.Thread(target=lambda: second.append(real()))
                b.start()
                b.join(10)
            return choice

        a._choose_helper = choose_then_let_b_run
        seen = a.probe(APP_TARGET)
        self.assertEqual(second[0].path, None, 'B settled on no helper')
        self.assertEqual(second[0].app_reason, 'app_signature_invalid')
        self.assertEqual(seen['state'], 'stopped', 'A was answered by the helper it checked')
        runs = [c for c in self.tools.calls if c[1:2] == ['status']]
        self.assertEqual(runs, [[str(self.app_helper), 'status', APP_TARGET['path']]])
        self.assertEqual([c for c in self.tools.ran(self.source) if c[1] != '--version'], [])

    def test_a_refused_app_is_not_deep_checked_on_every_call(self):
        self.sign_app(team=OTHER_TEAM)
        a = self.adapter()
        for _ in range(3):
            a.probe(APP_TARGET)
        self.assertEqual(len(self.tools.verified(self.app)), 1, 'one deep check per refused bundle state')
        self.assertEqual(a.last_choice.app_reason, 'app_signature_invalid')
        self.sign_app()
        self.app_helper.write_bytes(b'fixed build')
        os.utime(self.app_helper, ns=(3, 3))
        a.probe(APP_TARGET)
        self.assertEqual(a.last_choice.in_use, 'app', 'a changed bundle is checked at once')
        self.assertEqual(len(self.tools.verified(self.app)), 2)

    def test_a_refusal_lasts_only_its_window(self):
        self.tools.versions[str(self.app_helper)] = '0000000000000000'
        a = self.adapter()
        a.probe(APP_TARGET)
        self.assertEqual(len(self.tools.ran(self.app_helper)), 1)
        with mock.patch.object(macos.time, 'monotonic', return_value=time.monotonic() + macos.APP_REFUSAL_SECONDS + 1):
            a.probe(APP_TARGET)
        self.assertEqual(len(self.tools.ran(self.app_helper)), 2, 'asked again once the window passed')

    def test_a_helper_that_does_not_answer_in_time_is_not_remembered_as_outdated(self):
        # Review of #383 (confirming round, LOW 1): a slow first run is not a lasting reason.
        real = macos.helper_version

        def slow(helper, *, run, strict=False):
            if strict:
                raise broker.LifecycleError('delivery_unknown')
            return real(helper, run=run, strict=strict)

        a = self.adapter()
        with mock.patch.object(macos, 'helper_version', side_effect=slow):
            a.probe(APP_TARGET)
        self.assertEqual(a.last_choice.app_reason, 'app_verification_failed')
        a.probe(APP_TARGET)
        self.assertEqual(a.last_choice.in_use, 'app', 'asked again at once, not refused for 300 s')

    def test_a_pending_check_is_not_remembered_as_a_refusal(self):
        a = self.adapter()
        with mock.patch.object(macos, 'check_app_helper',
                               side_effect=broker.LifecycleError('app_verification_pending')):
            a.probe(APP_TARGET)
        self.assertEqual(a.last_choice.app_reason, 'app_verification_pending')
        a.probe(APP_TARGET)
        self.assertEqual(a.last_choice.in_use, 'app')

    def test_the_watcher_runs_the_chosen_helper(self):
        started = []

        class Proc:
            stdout = iter(())
            stdin = None

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

        stop = threading.Event()

        def popen(argv, **_kw):
            started.append(argv)
            stop.set()
            return Proc()

        self.adapter().watch(lambda event: None, stop, popen=popen)
        self.assertEqual(started, [[str(self.app_helper), 'watch']])


class DefaultsTests(Fixture):
    def test_an_injected_helper_never_reaches_an_installed_app(self):
        with mock.patch.object(macos, 'installed_app') as found:
            a = macos.MacAdapter(run=self.tools, home=self.home, helper=self.source, uid=501, platform='darwin',
                                 fabric_hub_file=self.root / 'hub.json')
            a.identity_matches = lambda target, *, deadline=None: True
            a.probe(APP_TARGET)
            a.close()
        found.assert_not_called()
        self.assertEqual((a.last_choice.in_use, a.last_choice.app_reason), ('source', None))

    def test_the_default_adapter_looks_for_the_installed_app_in_its_home(self):
        with mock.patch.object(macos, 'installed_app', return_value=self.app) as found, \
                mock.patch.object(macos, 'default_helper', return_value=self.source):
            a = macos.MacAdapter(run=self.tools, home=self.home, uid=501, platform='darwin',
                                 fabric_hub_file=self.root / 'hub.json')
            a.identity_matches = lambda target, *, deadline=None: True
            a.probe(APP_TARGET)
            a.close()
        found.assert_called_with(self.home)
        self.assertEqual(a.last_choice.in_use, 'app')

    def test_the_apps_identity_is_the_pins_app_update_holds(self):
        identity = macos.app_identity()
        self.assertEqual((identity['bundleId'], identity['teamId']), (app_update.BUNDLE_ID, app_update.TEAM_ID))
        self.assertEqual(identity['requirement'], macos.requirement_for(app_update.BUNDLE_ID, app_update.TEAM_ID))
        self.assertTrue(macos.APP_HELPER_IDENTIFIER.startswith(app_update.BUNDLE_ID + '.'))
        self.assertTrue(broker.BUNDLE_ID.fullmatch(macos.APP_HELPER_IDENTIFIER))

class ChoiceReportTests(Fixture):
    def test_the_choice_names_the_apps_helper_when_it_serves(self):
        choice = macos.helper_choice(run=self.tools, home=self.home, helper=self.source, app=self.app,
                                     platform='darwin')
        self.assertEqual((choice['inUse'], choice['path']), ('app', str(self.app_helper)))
        self.assertEqual(choice['app'], {'path': str(self.app), 'state': 'in_use', 'reason': None})
        self.assertEqual(choice['source'], {'path': str(self.source), 'state': 'current'})

    def test_the_choice_says_why_the_app_is_not_used(self):
        self.sign_app(team=OTHER_TEAM)
        choice = macos.helper_choice(run=self.tools, home=self.home, helper=self.source, app=self.app,
                                     platform='darwin')
        self.assertEqual(choice['inUse'], 'source')
        self.assertEqual(choice['app'], {'path': str(self.app), 'state': 'not_used', 'reason': 'app_signature_invalid'})
        self.assertIn(choice['app']['reason'], macos.APP_HELPER_REASONS)

    def test_the_choice_with_no_app_and_no_build(self):
        self.source.unlink()
        choice = macos.helper_choice(run=self.tools, home=self.home, helper=self.source, app=None, platform='darwin')
        self.assertEqual((choice['inUse'], choice['path']), (None, None))
        self.assertEqual(choice['source']['state'], 'not_built')

    def test_off_macos_nothing_is_run(self):
        choice = macos.helper_choice(run=self.tools, home=self.home, helper=self.source, platform='linux')
        self.assertEqual((choice['inUse'], choice['reason']), (None, 'unsupported_platform'))
        self.assertEqual(self.tools.calls, [])

    def test_the_choice_keeps_no_cache_and_writes_nothing(self):
        before = sorted(p.relative_to(self.root) for p in self.root.rglob('*'))
        macos.helper_choice(run=self.tools, home=self.home, helper=self.source, app=self.app, platform='darwin')
        self.assertEqual(sorted(p.relative_to(self.root) for p in self.root.rglob('*')), before)


class StatusAndDoctorTests(Fixture):
    def status_text(self, choice, state='current') -> str:
        doc = {'platform': 'darwin', 'supported': True, 'configured': True, 'degraded': [], 'targets': [],
               'callers': [], 'events': [], 'helper': {'path': str(self.source), 'state': state},
               'helperInUse': choice}
        out = io.StringIO()
        with mock.patch.object(lifecycle_cli, 'status', return_value=doc), contextlib.redirect_stdout(out):
            self.assertEqual(lifecycle_cli.cmd_status(argparse.Namespace(json=False, no_probe=True)), 0)
        return out.getvalue()

    def test_status_names_the_apps_helper_in_use(self):
        text = self.status_text({'inUse': 'app', 'path': str(self.app_helper),
                                 'app': {'path': str(self.app), 'state': 'in_use', 'reason': None}},
                                state='not_built')
        self.assertIn(f'helper inside the signed app ({self.app_helper})', text)
        self.assertNotIn('build-helper', text, 'no repair is offered while the app helper serves')

    def test_status_says_why_the_source_build_serves(self):
        text = self.status_text({'inUse': 'source', 'path': str(self.source),
                                 'app': {'path': str(self.app), 'state': 'not_used',
                                         'reason': 'app_helper_outdated'}})
        self.assertIn(f'helper built from source ({self.source})', text)
        self.assertIn(macos.APP_HELPER_REASONS['app_helper_outdated'], text)
        text = self.status_text({'inUse': 'source', 'path': str(self.source),
                                 'app': {'path': None, 'state': 'not_installed', 'reason': 'app_not_installed'}})
        self.assertIn('no signed app is installed', text)

    def test_status_with_neither_names_the_app_and_the_repair(self):
        text = self.status_text({'inUse': None, 'path': None,
                                 'app': {'path': str(self.app), 'state': 'not_used',
                                         'reason': 'app_signature_invalid'}}, state='not_built')
        self.assertIn(macos.APP_HELPER_REASONS['app_signature_invalid'], text)
        self.assertIn('0.30.0 or later', text)
        self.assertIn('full lifecycle build-helper', text)

    def test_doctor_is_inert_until_a_target_is_enrolled(self):
        import workspace
        from node.lifecycle import service
        with mock.patch.object(macos, 'helper_choice', return_value={'inUse': 'app'}) as choice:
            self.assertEqual(workspace._lifecycle_helper(self.root), {'state': 'inert'}, 'no document')
            (self.root / 'config').mkdir()
            (self.root / 'config/node-lifecycle.json').write_text('{}')
            with mock.patch.object(service, 'read_config', return_value=service.empty_config()):
                self.assertEqual(workspace._lifecycle_helper(self.root), {'state': 'inert'}, 'a document, no target')
            choice.assert_not_called()
            with mock.patch.object(service, 'read_config', return_value={'targets': {'alpha.app': {}}}):
                self.assertEqual(workspace._lifecycle_helper(self.root), {'inUse': 'app'})
                choice.side_effect = OSError('boom')
                self.assertEqual(workspace._lifecycle_helper(self.root), {'state': 'unknown', 'reason': 'OSError'})
            with mock.patch.object(service, 'read_config', side_effect=broker.LifecycleError('unsafe_config')):
                self.assertEqual(workspace._lifecycle_helper(self.root), {'state': 'unknown', 'reason': 'unsafe_config'})

    def test_doctor_adds_the_engine_to_the_import_path_once(self):
        import configuration
        import workspace
        sys.path[:] = [p for p in sys.path if p != str(configuration.SOURCE)]
        self.addCleanup(sys.path.insert, 0, str(ROOT))
        (self.root / 'config').mkdir()
        (self.root / 'config/node-lifecycle.json').write_text('{}')
        for _ in range(3):
            workspace._lifecycle_helper(self.root)
        self.assertEqual(sys.path.count(str(configuration.SOURCE)), 1)


class StatusRunsNoCheckUnasked(Fixture):
    """`full lifecycle status --no-probe`, and status with nothing enrolled, run no codesign and no helper."""

    TARGET = {'targetId': 'alpha.app', 'kind': 'app', 'path': '/Applications/Alpha.app', 'desiredState': 'on_demand',
              'policyRevision': 1, 'allowedCallers': [], 'startupEffects': ['opens a window'],
              'readiness': {'kind': 'process'}}

    def status(self, *, probe, targets):
        fake = mock.Mock()
        fake.probe.return_value = {'state': 'stopped'}
        with mock.patch.object(lifecycle_cli, '_helper_choice', return_value={'inUse': 'app'}) as choice, \
                mock.patch.object(lifecycle_cli.S, 'read_config',
                                  return_value={'targets': targets, 'callers': {}}), \
                mock.patch.object(lifecycle_cli, '_adapter', return_value=fake), \
                mock.patch.object(macos, 'helper_state', return_value='current'), \
                mock.patch.object(lifecycle_cli.sys, 'platform', 'darwin'):
            doc = lifecycle_cli.status(probe=probe)
        return doc, choice

    def test_no_probe_checks_nothing(self):
        doc, choice = self.status(probe=False, targets={'alpha.app': self.TARGET})
        choice.assert_not_called()
        self.assertEqual((doc['helperInUse']['inUse'], doc['helperInUse']['checked']), (None, False))

    def test_nothing_enrolled_checks_nothing(self):
        doc, choice = self.status(probe=True, targets={})
        choice.assert_not_called()
        self.assertFalse(doc['helperInUse']['checked'])

    def test_a_probe_with_a_target_checks_the_choice(self):
        doc, choice = self.status(probe=True, targets={'alpha.app': self.TARGET})
        choice.assert_called_once()
        self.assertEqual(doc['helperInUse'], {'inUse': 'app'})

    def test_the_unchecked_choice_is_said(self):
        text = io.StringIO()
        doc = {'platform': 'darwin', 'supported': True, 'configured': True, 'degraded': [], 'targets': [],
               'callers': [], 'events': [], 'helper': {'path': str(self.source), 'state': 'current'},
               'helperInUse': dict(lifecycle_cli.NOT_CHECKED)}
        with mock.patch.object(lifecycle_cli, 'status', return_value=doc), contextlib.redirect_stdout(text):
            lifecycle_cli.cmd_status(argparse.Namespace(json=False, no_probe=True))
        self.assertIn('was not checked (--no-probe)', text.getvalue())

if __name__ == '__main__':
    unittest.main()
