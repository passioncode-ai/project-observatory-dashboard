#!/usr/bin/env python3
"""A port forward for a service moved to another machine (OBS-480).

`full machine-move leave LABEL --to MACHINE --via USER@HOST` stops a service here and, when its Fabric
descriptor's origin is a local port, installs one launchd job that forwards that port to the space on the
other machine where the service now runs; the `elsewhere` entry names that job in `forward`. A hand-made
forward of the same port to the same space is adopted, never installed twice. `full machine-move return
LABEL` removes the forward this engine installed, and only clears the record of an adopted one.

Hermetic: a temporary HOME, a fake launchctl (no launchd is asked), no ssh, no network; whether the
port is free is the test's answer, never a socket of this machine.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmp  # noqa: E402,F401  the sandbox defaults for a suite run by hand
import machine_identity as MI  # noqa: E402
import machine_move as MM  # noqa: E402
import machine_forward as MF  # noqa: E402

ALPHA, BETA = 'm-machine-alpha-0001', 'm-machine-beta-0002'
JOB = 'com.example.alpha-bot'
SERVICE = 'alpha-bot.default'
PORT = 47123
VIA = 'svc@studio.example'
OWN = f'org.project-observatory.forward.{SERVICE}'
HAND = 'com.example.alpha-bot-tunnel'
UID = 501


class Launchctl:
    """A stand-in for subprocess.run that keeps launchd's loaded labels: `print gui/UID/LABEL` answers 0 for a
    loaded one and 113 otherwise, `bootstrap` loads the plist's Label (or answers `fail_bootstrap`), `bootout`
    unloads (or answers `fail_bootout` and keeps it loaded). Every other command answers 0. Nothing outside the
    test is run."""

    def __init__(self, loaded=(), fail_bootstrap=0, fail_bootout=0):
        self.calls, self.loaded, self.fail_bootstrap, self.fail_bootout = [], set(loaded), fail_bootstrap, fail_bootout

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        code = 0
        if argv[:2] == ['launchctl', 'print']:
            parts = argv[2].split('/')
            code = 0 if len(parts) == 3 and parts[2] in self.loaded else (113 if len(parts) == 3 else 0)
        elif argv[:2] == ['launchctl', 'bootstrap']:
            if self.fail_bootstrap:
                code = self.fail_bootstrap
            else:
                self.loaded.add(plistlib.loads(Path(argv[3]).read_bytes())['Label'])
        elif argv[:2] == ['launchctl', 'bootout']:
            if self.fail_bootout:
                code = self.fail_bootout
            else:
                self.loaded.discard(argv[2].split('/')[-1])
        return subprocess.CompletedProcess(argv, code, '', 'synthetic refusal' if code else '')

    def verbs(self, verb):
        return [c for c in self.calls if c[:2] == ['launchctl', verb]]


@unittest.skipIf(sys.platform == 'win32', 'no launchd jobs on Windows')
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='observatory-forward-')
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / 'user'
        self.home.mkdir()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('OBSERVATORY_', 'FABRIC_', 'XDG_'))}
        env.update({'HOME': str(self.home), 'FABRIC_SERVICES_DIR': str(self.home / 'fabric-services'),
                    'OBSERVATORY_SYSTEM_SETUP': '1', 'OBSERVATORY_MACHINE_ID': ALPHA,
                    'OBSERVATORY_MACHINE_ID_FOR_TESTS': '1'})
        self.env = patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.agents = self.home / 'Library/LaunchAgents'
        self.agents.mkdir(parents=True)
        self.descriptor(origin=f'http://127.0.0.1:{PORT}')
        self.job()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def descriptor(self, *, origin, placement=None, label=JOB):
        folder = self.home / 'fabric-services'
        folder.mkdir(exist_ok=True)
        doc = {'protocol': 'fabric-service/0.1', 'id': 'alpha-bot', 'instance': 'default', 'origin': origin,
               'lifecycle': {'manager': 'launchd', 'label': label}}
        if placement:
            doc['placement'] = placement
        (folder / f'{SERVICE}.json').write_text(json.dumps(doc))

    def job(self):
        (self.agents / f'{JOB}.plist').write_bytes(plistlib.dumps({'Label': JOB, 'ProgramArguments': ['/usr/bin/true']}))

    def hand_forward(self, args, label=HAND):
        path = self.agents / f'{label}.plist'
        path.write_bytes(plistlib.dumps({'Label': label, 'ProgramArguments': args, 'KeepAlive': True}))
        return path

    def leave(self, run, *, via=VIA, free=True, platform='darwin'):
        return MM.leave(JOB, BETA, self.home, runner=run, platform=platform, uid=UID, via=via,
                        port_free=lambda port: free)

    def entry(self, label=JOB):
        doc = json.loads(MI.placement_path(self.home).read_text())
        return next(e for e in doc['elsewhere'] if e['label'] == label)

    def own_plist(self):
        return self.agents / f'{OWN}.plist'


class Leave(Base):
    def test_leave_installs_one_forward_job_toward_the_space_where_the_service_now_runs(self):
        run = Launchctl(loaded={JOB})
        result = self.leave(run)
        doc = plistlib.loads(self.own_plist().read_bytes())
        self.assertEqual(doc['Label'], OWN)
        self.assertEqual(doc['ProgramArguments'], [
            '/usr/bin/ssh', '-N', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
            '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
            '-L', f'127.0.0.1:{PORT}:127.0.0.1:{PORT}', '--', VIA])
        self.assertTrue(doc['KeepAlive'])
        self.assertTrue(doc['RunAtLoad'])
        if os.name != 'nt':
            self.assertEqual(self.own_plist().stat().st_mode & 0o777, 0o600)
        self.assertIn(['launchctl', 'bootstrap', f'gui/{UID}', str(self.own_plist())], run.calls)
        self.assertEqual((self.entry()['forward'], self.entry()['forwardAdopted']), (OWN, False))
        self.assertEqual(self.entry()['placedOn'], BETA)
        fwd = result['forward']
        self.assertEqual((fwd['state'], fwd['label'], fwd['port'], fwd['via'], fwd['running']),
                         ('installed', OWN, PORT, VIA, True))
        # The service itself moved as before: stopped, staged, placed elsewhere.
        self.assertTrue((MI.staging_dir(self.home) / f'{JOB}.plist').is_file())

    def test_the_ssh_command_is_an_argument_array_bound_to_loopback(self):
        args = MF.forward_document(OWN, PORT, 'svc', 'studio.example')['ProgramArguments']
        self.assertEqual(args[0], '/usr/bin/ssh')
        self.assertNotIn('-c', args)
        self.assertNotIn('sh', [Path(a).name for a in args])
        spec = args[args.index('-L') + 1]
        self.assertTrue(spec.startswith('127.0.0.1:'), 'the local end binds loopback only')
        self.assertEqual(args[-2:], ['--', 'svc@studio.example'], 'the destination never reads as an option')

    def test_a_second_leave_changes_nothing_and_installs_nothing_twice(self):
        run = Launchctl(loaded={JOB})
        self.leave(run)
        before = self.own_plist().read_bytes()
        second = self.leave(run)
        self.assertEqual(second['forward']['state'], 'unchanged')
        self.assertEqual(len(run.verbs('bootstrap')), 1)
        self.assertEqual(self.own_plist().read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.agents.glob('*.plist')), [f'{OWN}.plist'])
        self.assertEqual(self.entry()['forward'], OWN)

    def test_a_leave_without_via_keeps_the_forward_already_recorded(self):
        run = Launchctl(loaded={JOB})
        self.leave(run)
        again = self.leave(run, via=None)
        self.assertEqual(again['forward']['state'], 'kept')
        self.assertEqual(self.entry()['forward'], OWN)
        self.assertTrue(self.own_plist().is_file())

    def test_a_leave_without_via_names_the_port_and_how_to_forward_it(self):
        result = self.leave(Launchctl(loaded={JOB}), via=None)
        self.assertEqual(result['forward']['state'], 'not-requested')
        self.assertEqual(result['forward']['port'], PORT)
        self.assertIn('--via USER@HOST', result['forward']['hint'])
        self.assertNotIn('forward', self.entry())
        self.assertFalse(self.own_plist().exists())

    def test_a_hand_made_forward_of_that_port_to_that_space_is_adopted(self):
        for args in (['/usr/bin/ssh', '-N', '-L', f'127.0.0.1:{PORT}:127.0.0.1:{PORT}', VIA],
                     ['ssh', '-NT', f'-L{PORT}:localhost:{PORT}', '-o', 'ServerAliveInterval=60', VIA],
                     ['/usr/bin/ssh', '-N', '-l', 'svc', '-L', f'localhost:{PORT}:127.0.0.1:{PORT}', 'studio.example']):
            with self.subTest(args=args):
                path = self.hand_forward(args)
                run = Launchctl(loaded={JOB, HAND})
                result = self.leave(run)
                self.assertEqual(result['forward']['state'], 'adopted')
                self.assertEqual((result['forward']['label'], result['forward']['running']), (HAND, True))
                self.assertFalse(self.own_plist().exists(), 'never installed a second time')
                self.assertEqual(run.verbs('bootstrap'), [])
                self.assertEqual((self.entry()['forward'], self.entry()['forwardAdopted']), (HAND, True))
                path.unlink()
                MI.remove_elsewhere(JOB, self.home)
                self.job()

    def test_an_adopted_forward_that_is_not_loaded_says_so(self):
        self.hand_forward(['/usr/bin/ssh', '-N', '-L', f'{PORT}:127.0.0.1:{PORT}', VIA])
        result = self.leave(Launchctl(loaded={JOB}))
        self.assertEqual((result['forward']['state'], result['forward']['running']), ('adopted', False))
        self.assertIn('not loaded', result['forward']['hint'])

    def test_a_hand_made_forward_of_that_port_to_another_space_is_refused(self):
        self.hand_forward(['/usr/bin/ssh', '-N', '-L', f'127.0.0.1:{PORT}:127.0.0.1:{PORT}', 'other@studio.example'])
        result = self.leave(Launchctl(loaded={JOB, HAND}))
        self.assertEqual((result['forward']['state'], result['forward']['reason']), ('refused', 'forward-elsewhere'))
        self.assertIn(HAND, result['forward']['message'])
        self.assertFalse(self.own_plist().exists())
        self.assertNotIn('forward', self.entry())

    def test_a_port_held_by_something_that_is_not_a_forward_is_refused_with_why(self):
        run = Launchctl(loaded={JOB})
        result = self.leave(run, free=False)
        fwd = result['forward']
        self.assertEqual((fwd['state'], fwd['reason']), ('refused', 'port-held'))
        self.assertIn(f'lsof -nP -iTCP:{PORT} -sTCP:LISTEN', fwd['message'])
        self.assertFalse(self.own_plist().exists())
        self.assertEqual(run.verbs('bootstrap'), [])
        self.assertNotIn('forward', self.entry())
        # The move itself is done: the job is stopped and staged, never running on both machines.
        self.assertTrue((MI.staging_dir(self.home) / f'{JOB}.plist').is_file())

    def test_a_forward_that_does_not_load_is_removed_and_named(self):
        result = self.leave(Launchctl(loaded={JOB}, fail_bootstrap=5))
        self.assertEqual((result['forward']['state'], result['forward']['reason']), ('failed', 'load-failed'))
        self.assertFalse(self.own_plist().exists())
        self.assertNotIn('forward', self.entry())

    def test_a_forward_that_cannot_be_written_still_records_the_move(self):
        def refuse(path, data):
            raise PermissionError('synthetic')
        with patch.object(MF, '_write_private', refuse):
            result = self.leave(Launchctl(loaded={JOB}))
        self.assertEqual((result['forward']['state'], result['forward']['reason']), ('failed', 'error'))
        self.assertEqual(self.entry()['placedOn'], BETA, 'the job moved, so it is placed elsewhere')
        self.assertNotIn('forward', self.entry())

    def test_a_new_via_replaces_the_forward_this_engine_installed(self):
        run = Launchctl(loaded={JOB})
        self.leave(run)
        result = self.leave(run, via='svc@other-studio.example')
        self.assertEqual(result['forward']['state'], 'installed')
        self.assertTrue(result['forward']['replaced'])
        self.assertIn(['launchctl', 'bootout', f'gui/{UID}/{OWN}'], run.calls)
        self.assertEqual(plistlib.loads(self.own_plist().read_bytes())['ProgramArguments'][-1],
                         'svc@other-studio.example')

    def test_a_replacement_that_cannot_unload_the_old_forward_keeps_its_record(self):
        run = Launchctl(loaded={JOB})
        self.leave(run)
        run.fail_bootout = 5
        result = self.leave(run, via='svc@other-studio.example')
        self.assertEqual(result['forward']['state'], 'failed')
        self.assertIn(OWN, run.loaded, 'the old forward is still loaded')
        self.assertEqual(self.entry()['forward'], OWN, 'so its record stays and cutover still refuses')

    def test_a_rerun_to_another_space_keeps_an_adopted_forward_recorded(self):
        self.hand_forward(['/usr/bin/ssh', '-N', '-L', f'127.0.0.1:{PORT}:127.0.0.1:{PORT}', VIA])
        run = Launchctl(loaded={JOB, HAND})
        self.leave(run)
        result = self.leave(run, via='other@studio.example')
        self.assertEqual((result['forward']['state'], result['forward']['reason']), ('refused', 'forward-elsewhere'))
        self.assertEqual((self.entry()['forward'], self.entry()['forwardAdopted']), (HAND, True),
                         'the adopted forward still holds the port')

    def test_a_malformed_via_is_refused_before_anything_moves(self):
        for bad in ('-oProxyCommand=x@studio.example', 'svc@-oProxyCommand=x', 'svc@studio.example;true',
                    '-x@studio.example', 'svc@-x',
                    'studio.example', 'svc@studio.example:22', 'sv c@studio.example', 'svc@', '@studio.example',
                    'svc@studio..example', 'svc@studio.example\n', 'svc@[::1]'):
            with self.subTest(via=bad), self.assertRaisesRegex(MM.MachineMoveError, 'nothing moved'):
                self.leave(Launchctl(loaded={JOB}), via=bad)
            self.assertTrue((self.agents / f'{JOB}.plist').is_file())
        self.assertFalse(MI.placement_path(self.home).exists())

    def test_via_for_a_service_without_a_local_origin_is_refused_before_anything_moves(self):
        self.descriptor(origin='https://alpha-bot.example.com', placement='remote')
        with self.assertRaisesRegex(MM.MachineMoveError, 'no local origin'):
            self.leave(Launchctl(loaded={JOB}))
        (self.home / 'fabric-services' / f'{SERVICE}.json').unlink()
        with self.assertRaisesRegex(MM.MachineMoveError, 'no Fabric service descriptor'):
            self.leave(Launchctl(loaded={JOB}))
        self.assertTrue((self.agents / f'{JOB}.plist').is_file())

    def test_off_macos_a_forward_is_refused_honestly_before_anything_moves(self):
        units = self.home / '.config/systemd/user'
        units.mkdir(parents=True)
        (units / JOB).write_text('[Service]\nExecStart=/usr/bin/true\n')
        self.descriptor(origin=f'http://127.0.0.1:{PORT}')
        with self.assertRaisesRegex(MM.MachineMoveError, 'macOS only'):
            self.leave(Launchctl(), platform='linux')
        self.assertTrue((units / JOB).is_file())
        with self.assertRaisesRegex(MM.MachineMoveError, 'macOS only'):
            MM.return_job(JOB, self.home, runner=Launchctl(), platform='linux', uid=UID)


class Return(Base):
    def test_return_removes_the_forward_this_engine_installed_and_clears_the_field(self):
        run = Launchctl(loaded={JOB})
        self.leave(run)
        result = MM.return_job(JOB, self.home, runner=run, platform='darwin', uid=UID)
        self.assertEqual((result['forward']['state'], result['forward']['label']), ('removed', OWN))
        self.assertIn(['launchctl', 'bootout', f'gui/{UID}/{OWN}'], run.calls)
        self.assertFalse(self.own_plist().exists())
        entry = self.entry()
        self.assertNotIn('forward', entry)
        self.assertNotIn('forwardAdopted', entry)
        self.assertEqual(entry['placedOn'], BETA, 'the job still runs there until leave and cutover')
        self.assertTrue(any(f'machine-move cutover {JOB}' in step for step in result['next']))
        again = MM.return_job(JOB, self.home, runner=run, platform='darwin', uid=UID)
        self.assertEqual(again['forward']['state'], 'none')

    def test_return_leaves_an_adopted_forward_running_and_says_it_still_holds_the_port(self):
        path = self.hand_forward(['/usr/bin/ssh', '-N', '-L', f'127.0.0.1:{PORT}:127.0.0.1:{PORT}', VIA])
        run = Launchctl(loaded={JOB, HAND})
        self.leave(run)
        result = MM.return_job(JOB, self.home, runner=run, platform='darwin', uid=UID)
        self.assertEqual(result['forward']['state'], 'adopted-left')
        self.assertIn('--remove-adopted', result['forward']['hint'])
        self.assertIn(str(PORT), result['forward']['hint'])
        self.assertTrue(path.is_file())
        self.assertIn(HAND, run.loaded)
        self.assertEqual(run.verbs('bootout'), [['launchctl', 'bootout', f'gui/{UID}/{JOB}']])
        self.assertNotIn('forward', self.entry())

    def test_return_with_remove_adopted_unloads_it_and_sets_its_plist_aside(self):
        path = self.hand_forward(['/usr/bin/ssh', '-N', '-L', f'127.0.0.1:{PORT}:127.0.0.1:{PORT}', VIA])
        run = Launchctl(loaded={JOB, HAND})
        self.leave(run)
        result = MM.return_job(JOB, self.home, runner=run, platform='darwin', uid=UID, remove_adopted=True)
        self.assertEqual(result['forward']['state'], 'adopted-removed')
        self.assertNotIn(HAND, run.loaded)
        self.assertFalse(path.exists())
        aside = Path(result['forward']['set_aside'])
        self.assertEqual(plistlib.loads(aside.read_bytes())['Label'], HAND, 'set aside, never deleted')
        self.assertNotIn('forward', self.entry())

    def test_remove_adopted_refuses_a_plist_that_is_no_longer_a_forward(self):
        path = self.hand_forward(['/usr/bin/ssh', '-N', '-L', f'127.0.0.1:{PORT}:127.0.0.1:{PORT}', VIA])
        run = Launchctl(loaded={JOB, HAND})
        self.leave(run)
        path.write_bytes(plistlib.dumps({'Label': HAND, 'ProgramArguments': ['/bin/sleep', '60']}))
        with self.assertRaisesRegex(MM.MachineMoveFailed, 'no longer an ssh forward'):
            MM.return_job(JOB, self.home, runner=run, platform='darwin', uid=UID, remove_adopted=True)
        self.assertIn(HAND, run.loaded)
        self.assertTrue(path.is_file(), 'nothing moved')
        self.assertEqual(self.entry()['forward'], HAND)

    def test_return_of_a_label_not_placed_elsewhere_is_refused_and_changes_nothing(self):
        self.leave(Launchctl(loaded={JOB}))
        before = MI.placement_path(self.home).read_bytes()
        with self.assertRaisesRegex(MM.MachineMoveError, 'not placed elsewhere'):
            MM.return_job('com.example.unknown', self.home, runner=Launchctl(), platform='darwin', uid=UID)
        self.assertEqual(MI.placement_path(self.home).read_bytes(), before)
        self.assertTrue(self.own_plist().is_file())

    def test_return_never_deletes_a_job_outside_this_engines_names(self):
        # A placement file edited by hand claims a foreign job as installed: return treats it as adopted.
        path = self.hand_forward(['/usr/bin/ssh', '-N', '-L', f'127.0.0.1:{PORT}:127.0.0.1:{PORT}', VIA])
        self.leave(Launchctl(loaded={JOB}), via=None)
        MI.set_forward(JOB, self.home, forward=HAND, adopted=False)
        result = MM.return_job(JOB, self.home, runner=Launchctl(loaded={HAND}), platform='darwin', uid=UID)
        self.assertEqual(result['forward']['state'], 'adopted-left')
        self.assertTrue(path.is_file())

    def test_cutover_refuses_while_a_forward_holds_the_port(self):
        self.leave(Launchctl(loaded={JOB}))
        with self.assertRaisesRegex(MM.MachineMoveError, f'machine-move return {JOB}'):
            MM.cutover(JOB, self.home, runner=Launchctl(), platform='darwin', uid=UID, confirm=lambda *a: True)
        self.assertTrue((MI.staging_dir(self.home) / f'{JOB}.plist').is_file())


class PlacementCompatibility(Base):
    def test_an_old_record_without_forward_still_loads_and_keeps_its_shape(self):
        path = MI.placement_path(self.home)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'schema': 'observatory-placement/1', 'machine': ALPHA, 'updatedAt': None,
                                    'elsewhere': [{'label': JOB, 'serviceId': SERVICE, 'placedOn': BETA,
                                                   'since': '2026-11-01T00:00:00Z', 'by': 'machine-restore'}]}))
        doc = MI.read_placement(self.home)
        self.assertEqual(doc['schema'], 'observatory-placement/1')
        self.assertNotIn('forward', doc['elsewhere'][0])
        MI.place_elsewhere([{'label': 'com.example.beta-bot', 'placedOn': BETA}], self.home)
        self.assertNotIn('forward', self.entry())

    def test_forward_survives_another_writer_and_an_old_reader_ignores_it(self):
        self.leave(Launchctl(loaded={JOB}))
        MI.place_elsewhere([{'label': 'com.example.beta-bot', 'placedOn': BETA}], self.home)
        MI.place_elsewhere([{'label': JOB, 'placedOn': BETA, 'by': 'machine-restore'}], self.home)
        self.assertEqual(self.entry()['forward'], OWN)
        doc = json.loads(MI.placement_path(self.home).read_text())
        self.assertEqual(doc['schema'], 'observatory-placement/1', 'the schema id is unchanged')
        # A reader that knows only the 0.30.1 keys reads every entry as before.
        old = [{k: e.get(k) for k in ('label', 'serviceId', 'placedOn', 'since', 'by')} for e in doc['elsewhere']]
        self.assertEqual([e['label'] for e in old], ['com.example.alpha-bot', 'com.example.beta-bot'])
        MI.place_elsewhere([{'label': JOB, 'placedOn': BETA, 'forward': None}], self.home)
        self.assertNotIn('forward', self.entry())

    def test_a_malformed_forward_field_is_read_as_no_forward(self):
        self.leave(Launchctl(loaded={JOB}), via=None)
        path = MI.placement_path(self.home)
        doc = json.loads(path.read_text())
        doc['elsewhere'][0]['forward'] = ['not', 'a', 'label']
        path.write_text(json.dumps(doc))
        self.assertIsNone(MF.recorded(MI.read_placement(self.home)['elsewhere'][0])[0])


class CommandLine(Base):
    def test_the_parser_takes_via_and_return(self):
        a = MM.parser().parse_args(['leave', JOB, '--to', BETA, '--via', VIA])
        self.assertEqual((a.action, a.via), ('leave', VIA))
        a = MM.parser().parse_args(['return', JOB, '--remove-adopted'])
        self.assertEqual((a.action, a.label, a.remove_adopted), ('return', JOB, True))

    def test_the_full_cli_accepts_return(self):
        import observatory as cli
        self.assertEqual(cli.refusal(['machine-move', 'return', JOB]), '')
        self.assertEqual(cli.refusal(['machine-move', 'leave', JOB, '--to', BETA, '--via', VIA]), '')
        self.assertIn('machine-move return LABEL', cli.PUBLIC_HELP)

    def test_return_of_an_unknown_label_exits_2_with_its_reason(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()), \
                patch.object(MM.sys, 'platform', 'darwin'):
            code = MM.main(['return', 'com.example.unknown'])
        self.assertEqual(code, 2)
        self.assertIn('not placed elsewhere', err.getvalue())


if __name__ == '__main__':
    unittest.main(verbosity=2)
