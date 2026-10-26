#!/usr/bin/env python3
"""The lifecycle supervisor (OBS-79 step L4, docs/design/LIFECYCLE.md): the broker's supervision pass on its cadence,
the watcher's events fed to the broker, the sweep's prune and warm-up, and its threads' start and stop.

Two kinds of case. Against a recording fake broker: what the supervisor asks and when (it decides nothing itself).
Against the real broker over the process seam of `test_node_lifecycle_broker.py` with its controllable clock: that
the rules the broker enforces (`in_use_by_person`, `held_off`, leases, the takeover grace) still hold when the
supervisor drives it. No case waits for an interval: the loop is stepped by hand (`step`) under a fake monotonic clock
(`Mono`) that decides when the sweep is due, and the threaded cases wait on events the fakes set. No installed application is touched.

Fixtures are neutral: targets `alpha.app`, `beta.app`; bundles `com.example.alpha`, `com.example.beta`; every file
in a temporary folder.
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tmp as tmpdir                                   # noqa: E402,F401  a sandboxed workspace for `paths`

from node.lifecycle import broker, macos               # noqa: E402
from node.lifecycle import supervisor as S             # noqa: E402
from test_node_lifecycle_broker import BrokerFixture, app_identity, target  # noqa: E402

ALPHA_BUNDLE, BETA_BUNDLE = 'com.example.alpha', 'com.example.beta'
WAIT = 10          # seconds a threaded case may wait for an event a fake sets; never a cadence


def registry_doc():
    beta = target('beta.app') | {'identity': app_identity(BETA_BUNDLE), 'path': '/Applications/Beta.app'}
    return {'targets': {'alpha.app': target('alpha.app'), 'beta.app': beta}}


class Mono:
    """The supervisor's monotonic clock, moved by hand."""

    def __init__(self):
        self.now = 50_000.0

    def __call__(self):
        return self.now


def sweep_step(sup, woke=False):
    """A wake-up after the sweep's deadline: the sweep runs, whatever woke the loop."""
    sup.monotonic.now = max(sup.monotonic.now, sup.next_sweep)
    return sup.step(woke)


class FakeBroker:
    """Records what the supervisor asks; `supervise` can be made to raise."""

    def __init__(self):
        self.passes, self.activations, self.prunes = [], [], 0
        self.idle: list = []
        self.idle_asks: list = []
        self.raises: list = []
        self.prune_raises: list = []
        self.passed = threading.Event()
        self.now = 1_000_000.0

    def clock(self):
        return self.now

    def supervise(self, only=None):
        self.passes.append(None if only is None else set(only))
        self.passed.set()
        if self.raises:
            raise self.raises.pop(0)
        return []

    def note_activation(self, target_ids, pid):
        self.activations.append((set(target_ids), pid))

    def prune(self):
        self.prunes += 1
        if self.prune_raises:
            raise self.prune_raises.pop(0)
        return 0

    def idle_launches(self, target_ids):
        self.idle_asks.append(set(target_ids))
        if isinstance(self.idle, Exception):
            raise self.idle
        return [t for t in self.idle if t in target_ids]


class FakeAdapter:
    """No probe, start or stop: the supervisor never calls them. `watch` behaves as the test sets."""

    def __init__(self, watch='return'):
        self.checks: list = []
        self.watches = 0
        self.watch_mode = watch
        self.watching = threading.Event()
        self.stopped_watch = 0

    def identity_matches(self, target, *, deadline=None):
        self.checks.append((target['targetId'], deadline))
        raise broker.LifecycleError('verification_pending')

    def watch(self, on_event, stop_event):
        self.watches += 1
        self.watching.set()
        if self.watch_mode == 'raise':
            raise OSError('the helper could not be started')
        if self.watch_mode == 'block':
            stop_event.wait(WAIT)

    def stop_watch(self):
        self.stopped_watch += 1


class Recorder:
    def __init__(self):
        self.failures: list = []
        self.watch_ends = 0

    def failed(self, what, exc, first):
        self.failures.append((what, type(exc).__name__, first))

    def ended(self):
        self.watch_ends += 1


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.broker = FakeBroker()
        self.adapter = FakeAdapter()
        self.doc = registry_doc()
        self.rec = Recorder()
        self.mono = Mono()
        self.sup = self.make()

    def tearDown(self):
        self.sup.close()

    def make(self, adapter=None, **kwargs):
        kwargs.setdefault('monotonic', self.mono)
        return S.Supervisor(self.broker, adapter or self.adapter, lambda: self.doc, on_failure=self.rec.failed,
                            on_watch_end=self.rec.ended, **kwargs)

    def idle_alpha(self):
        self.doc['targets']['alpha.app']['idleStopMinutes'] = 15
        self.broker.idle = ['alpha.app']

    def join_watcher(self):
        if self.sup._watcher is not None:
            self.sup._watcher.join(WAIT)

    # -- the cadence

    def test_the_cadence_is_the_sweep_and_a_minute_while_an_idle_lease_can_run_out(self):
        self.assertEqual(self.sup.interval(), S.SWEEP_SECONDS)
        self.assertEqual(S.SWEEP_SECONDS, 600)
        self.idle_alpha()
        self.assertEqual(self.sup.interval(), S.IDLE_CHECK_SECONDS)
        self.assertEqual(S.IDLE_CHECK_SECONDS, 60)
        self.mono.now += S.SWEEP_SECONDS - 20
        self.assertEqual(self.sup.interval(), 20, 'never past the sweep deadline')

    def test_only_launches_the_idle_pass_can_stop_keep_the_minute_cadence(self):
        # A launch of an on_demand target without idle minutes, of a held-off or unapproved one, is never looked at by
        # the idle pass, so it never clears: it must not hold the loop at one minute for ever.
        self.broker.idle = ['alpha.app', 'beta.app']
        self.assertEqual(self.sup.interval(), S.SWEEP_SECONDS)
        self.assertEqual(self.broker.idle_asks, [], 'no target has idle minutes: the store is not even asked')
        self.doc['targets']['alpha.app'].update(idleStopMinutes=15, desiredState='held_off')
        self.doc['targets']['beta.app'].update(idleStopMinutes=15, approved=False)
        self.assertEqual(self.sup.interval(), S.SWEEP_SECONDS)
        self.doc['targets']['beta.app']['approved'] = True
        self.assertEqual(self.sup.interval(), S.IDLE_CHECK_SECONDS)
        self.assertEqual(self.broker.idle_asks[-1], {'beta.app'})

    def test_a_store_that_cannot_be_read_waits_for_the_sweep_not_a_tight_loop(self):
        self.doc['targets']['alpha.app']['idleStopMinutes'] = 15
        self.broker.idle = RuntimeError('closed')
        self.assertEqual(self.sup.interval(), S.SWEEP_SECONDS)

    def test_the_minute_timeout_is_not_a_sweep_and_passes_only_over_idle_targets(self):
        self.idle_alpha()
        self.mono.now += S.IDLE_CHECK_SECONDS
        self.sup.step(woke=False)
        self.assertEqual((self.broker.passes, self.broker.prunes, self.adapter.checks), ([{'alpha.app'}], 0, []))
        self.broker.idle = []
        self.mono.now += S.IDLE_CHECK_SECONDS
        self.assertEqual(self.sup.step(woke=False), [])
        self.assertEqual(len(self.broker.passes), 1, 'nothing to look at is no pass')

    def test_a_stream_of_events_never_starves_the_sweep(self):
        for _ in range(30):                             # a launch every 30 s for 15 minutes
            self.mono.now += 30
            self.sup.on_event({'event': 'launched', 'bundleId': BETA_BUNDLE, 'pid': 7})
            self.sup.step(woke=True)
        self.assertEqual(self.broker.prunes, 1, 'the sweep ran on its own deadline')
        self.assertIn(None, self.broker.passes, 'and passed over every target, an exited always_on one included')
        self.assertEqual(self.sup.next_sweep, 50_000.0 + 600 + 600)

    # -- the watcher's events

    def test_an_activation_goes_to_the_broker_for_every_target_of_that_bundle(self):
        self.sup.on_event({'event': 'activated', 'bundleId': ALPHA_BUNDLE, 'pid': 4242})
        self.assertEqual(self.broker.activations, [({'alpha.app'}, 4242)])
        self.assertFalse(self.sup.wake.is_set(), 'an activation needs no pass: the broker records it')

    def test_an_event_of_an_unenrolled_bundle_or_without_a_pid_changes_nothing(self):
        self.sup.on_event({'event': 'activated', 'bundleId': 'com.example.other', 'pid': 1})
        self.sup.on_event({'event': 'activated', 'bundleId': ALPHA_BUNDLE, 'pid': '1'})
        self.sup.on_event({'event': 'terminated', 'bundleId': 'com.example.other', 'pid': 1})
        self.assertEqual((self.broker.activations, self.sup.pending, self.sup.wake.is_set()), ([], set(), False))

    def test_a_launch_or_termination_wakes_a_pass_over_that_apps_targets_only(self):
        self.sup.on_event({'event': 'terminated', 'bundleId': BETA_BUNDLE, 'pid': 7})
        self.assertTrue(self.sup.wake.is_set())
        self.sup.step(woke=True)
        self.assertEqual(self.broker.passes, [{'beta.app'}])
        self.assertEqual(self.broker.prunes, 0, 'a wake-up is not a sweep')
        self.assertEqual(self.sup.pending, set())

    def test_an_unreadable_registry_drops_the_event(self):
        def refused():
            raise broker.LifecycleError('invalid_registry')
        sup = S.Supervisor(self.broker, self.adapter, refused)
        sup.on_event({'event': 'launched', 'bundleId': ALPHA_BUNDLE, 'pid': 7})
        self.assertFalse(sup.wake.is_set())

    def test_events_are_matched_against_the_bundles_read_at_the_sweep(self):
        reads = []

        def counted():
            reads.append(1)
            return self.doc
        sup = S.Supervisor(self.broker, self.adapter, counted, monotonic=self.mono)
        for _ in range(5):
            sup.on_event({'event': 'activated', 'bundleId': 'com.example.other', 'pid': 1})
        self.assertEqual(len(reads), 1, 'one read for the first event, none for the next ones')
        gamma = target('gamma.app') | {'identity': app_identity('com.example.gamma')}
        self.doc['targets']['gamma.app'] = gamma
        sup.on_event({'event': 'activated', 'bundleId': 'com.example.gamma', 'pid': 3})
        self.assertEqual(self.broker.activations, [], 'enrolled since the last sweep')
        sweep_step(sup)
        sup.on_event({'event': 'activated', 'bundleId': 'com.example.gamma', 'pid': 3})
        self.assertEqual(self.broker.activations, [({'gamma.app'}, 3)])

    # -- the sweep

    def test_the_sweep_warms_prunes_and_passes_over_every_target(self):
        self.sup.pending = {'beta.app'}
        sweep_step(self.sup)
        self.assertEqual(self.broker.prunes, 1)
        self.assertEqual(self.broker.passes, [None], 'a sweep looks at every target, not only the pending ones')
        self.assertEqual(sorted(t for t, _ in self.adapter.checks), ['alpha.app', 'beta.app'])
        self.assertEqual({d for _, d in self.adapter.checks}, {self.broker.now},
                         'the warm-up never waits: its deadline is now')

    def test_a_pass_that_raises_is_reported_once_and_the_next_pass_runs(self):
        self.broker.raises = [RuntimeError('boom'), RuntimeError('boom')]
        self.assertEqual(self.sup.tick(), [])
        self.assertEqual(self.sup.tick(), [])
        self.assertEqual(self.sup.tick(), [])
        self.broker.raises = [RuntimeError('boom')]
        self.sup.tick()
        self.assertEqual(self.rec.failures, [('pass', 'RuntimeError', True), ('pass', 'RuntimeError', False),
                                             ('pass', 'RuntimeError', True)],
                         'the first failure after a good pass is the one a person hears about')
        self.assertEqual(len(self.broker.passes), 4)

    def test_a_pass_cut_short_by_shutdown_is_not_a_failure(self):
        self.sup.close()
        self.broker.raises = [RuntimeError('Cannot operate on a closed database.')]
        self.sup.tick()
        self.assertEqual(self.rec.failures, [])

    def test_a_prune_that_fails_does_not_stop_the_pass(self):
        self.broker.prune_raises = [OSError('disk full')]
        sweep_step(self.sup)
        self.assertEqual(self.broker.passes, [None])
        self.assertEqual(self.rec.failures, [('prune', 'OSError', True)])

    # -- the watcher

    def test_a_watcher_that_ends_is_started_again_by_the_next_sweep_not_at_once(self):
        self.assertTrue(self.sup.ensure_watcher())
        self.join_watcher()
        self.assertEqual((self.adapter.watches, self.rec.watch_ends), (1, 1))
        self.sup.step(woke=True)
        self.join_watcher()
        self.assertEqual(self.adapter.watches, 1, 'an event is not a reason to restart the watcher')
        self.sup.step(woke=False)
        self.join_watcher()
        self.assertEqual(self.adapter.watches, 1, 'nor is a minute timeout')
        sweep_step(self.sup)
        self.join_watcher()
        self.assertEqual(self.adapter.watches, 2, 'the sweep starts it again')

    def test_a_watcher_that_raises_is_a_failure_and_is_retried_on_the_sweep(self):
        adapter = FakeAdapter(watch='raise')
        self.sup = self.make(adapter)
        self.sup.ensure_watcher()
        self.join_watcher()
        self.assertEqual(self.rec.failures, [('watch', 'OSError', True)])
        sweep_step(self.sup)
        self.join_watcher()
        self.assertEqual(adapter.watches, 2)

    def test_a_running_watcher_is_never_started_twice(self):
        adapter = FakeAdapter(watch='block')
        self.sup = self.make(adapter)
        self.assertTrue(self.sup.ensure_watcher())
        self.assertTrue(adapter.watching.wait(WAIT))
        self.assertFalse(self.sup.ensure_watcher())
        sweep_step(self.sup)
        self.assertEqual(adapter.watches, 1)
        self.sup.close()
        self.assertFalse(self.sup._watcher.is_alive())
        self.assertEqual(adapter.stopped_watch, 1)
        self.assertEqual(self.rec.watch_ends, 0, 'ending because the supervisor stops is not worth a line')

    def test_an_adapter_without_a_watcher_runs_none(self):
        class NoWatch:
            def identity_matches(self, target, *, deadline=None):
                return True
        sup = self.make(NoWatch(), supported=True)
        self.assertFalse(sup.ensure_watcher())

    # -- threads

    def test_start_runs_one_loop_and_close_stops_it_without_waiting_for_the_interval(self):
        adapter = FakeAdapter(watch='block')
        self.sup = self.make(adapter)
        self.assertTrue(self.sup.start())
        self.assertFalse(self.sup.start(), 'one loop per supervisor')
        self.assertTrue(self.broker.passed.wait(WAIT), 'the loop passes once at its start')
        self.assertTrue(adapter.watching.wait(WAIT), 'and starts the watcher')
        self.assertTrue(self.sup.running)
        self.sup.close()                                # the loop is inside a 600 s wait: close must wake it
        self.assertFalse(self.sup.running)
        self.assertFalse(self.sup._watcher.is_alive())
        passes = len(self.broker.passes)
        self.assertFalse(self.sup.start(), 'a closed supervisor never starts again')
        self.assertEqual(len(self.broker.passes), passes)

    def test_a_pass_that_raises_does_not_end_the_loop(self):
        self.broker.raises = [RuntimeError('boom')]
        self.sup.start()
        self.assertTrue(self.broker.passed.wait(WAIT))
        self.broker.passed.clear()
        self.sup.on_event({'event': 'launched', 'bundleId': ALPHA_BUNDLE, 'pid': 9})
        self.assertTrue(self.broker.passed.wait(WAIT), 'the loop survived the failed pass and woke on the event')
        self.assertTrue(self.sup.running)

    # -- platforms

    def test_an_unsupported_platform_runs_no_pass_no_warm_up_no_watcher_and_only_prunes(self):
        adapter = macos.UnsupportedAdapter('linux')
        sup = S.Supervisor(self.broker, adapter, lambda: self.doc, monotonic=self.mono)
        self.assertFalse(sup.supported)
        self.assertEqual(sweep_step(sup), [])
        self.assertEqual((self.broker.passes, self.broker.prunes), ([], 1))
        self.assertFalse(sup.ensure_watcher())
        self.assertTrue(S.supports_supervision(FakeAdapter()))


class SupervisedBrokerTests(BrokerFixture, unittest.TestCase):
    """The real broker driven by the supervisor: its rules hold, the supervisor only asks."""

    def supervisor(self):
        self.mono = getattr(self, 'mono', None) or Mono()
        return S.Supervisor(self.broker, self.adapter, lambda: broker.registry(self.data / 'registry.json'),
                            supported=True, monotonic=self.mono)

    def started_by_agent(self):
        receipt = self.wait(self.broker.ensure(self.agent, self.args('start-1')), self.agent)
        self.assertEqual(receipt['state'], 'ready')
        return self.broker.status(self.agent, 'alpha.app')['pid']

    def test_an_on_demand_app_without_idle_minutes_never_holds_the_minute_cadence(self):
        self.started_by_agent()                         # on_demand, no idle minutes: the idle pass never looks at it
        self.adapter.crash('alpha.app')                 # the person quits it; its launch row stays
        self.assertEqual(self.broker.live_launches(), 1)
        self.assertEqual(self.supervisor().interval(), S.SWEEP_SECONDS)

    def test_the_cadence_follows_what_the_broker_started(self):
        self.policy['targets']['alpha.app'] = target(idle=15)
        self.save_policy()
        sup = self.supervisor()
        self.assertEqual(sup.interval(), S.SWEEP_SECONDS)
        self.started_by_agent()
        self.assertEqual(sup.interval(), S.IDLE_CHECK_SECONDS)

    def test_an_always_on_target_that_exited_is_started_again_by_the_sweep(self):
        self.policy['targets']['alpha.app'] = target(desired='always_on')
        self.save_policy()
        [op] = sweep_step(self.supervisor())
        self.assertEqual(self.wait_internal(op)['state'], 'ready')
        self.adapter.crash('alpha.app')
        self.clock.now += 400                            # past the crash circuit and the early-exit window
        [op] = sweep_step(self.supervisor())
        self.assertEqual(self.wait_internal(op)['state'], 'ready')
        self.assertEqual(self.adapter.effects.count(('start', 'alpha.app')), 2)

    def test_a_held_off_target_is_never_started(self):
        self.policy['targets']['alpha.app'] = target(desired='held_off')
        self.save_policy()
        self.assertEqual(sweep_step(self.supervisor()), [])
        self.assertEqual(self.adapter.effects, [])

    def test_an_idle_app_is_stopped_but_never_while_the_person_is_in_front_of_it(self):
        self.policy['targets']['alpha.app'] = target(idle=15)
        self.save_policy()
        self.started_by_agent()
        (self.data / 'alpha.app.active').write_text('')
        (self.data / 'alpha.app.idle').write_text(str(5 * 60))
        self.clock.now += 60 * 60
        sup = self.supervisor()
        self.assertEqual(sup.step(woke=False), [], 'frontmost with input inside the idle window')
        (self.data / 'alpha.app.active').unlink()
        [op] = sup.step(woke=False)                     # the minute's timeout over the idle target
        receipt = self.wait_internal(op)
        self.assertEqual((receipt['state'], receipt['kind']), ('stopped', 'stop'))
        self.assertEqual(sup.interval(), S.SWEEP_SECONDS, 'nothing started runs: back to the sweep')

    def test_an_activation_from_the_watcher_after_the_grace_hands_the_app_to_the_person(self):
        self.policy['targets']['alpha.app'] = target(idle=15)
        self.save_policy()
        pid = self.started_by_agent()
        sup = self.supervisor()
        self.clock.now += broker.ADOPT_GRACE_SECONDS + 1
        sup.on_event({'event': 'activated', 'bundleId': ALPHA_BUNDLE, 'pid': pid})
        self.clock.now += 60 * 60
        self.assertEqual(sweep_step(sup), [], 'the person took it over: never idled out')
        self.assertEqual(sup.interval(), S.SWEEP_SECONDS, 'an adopted launch needs no minute-by-minute check')
        status = self.broker.status(self.agent, 'alpha.app')
        self.assertEqual(status['startedBy'], 'person')

    def test_an_activation_inside_the_launch_grace_is_the_app_bringing_itself_forward(self):
        self.policy['targets']['alpha.app'] = target(idle=15)
        self.save_policy()
        pid = self.started_by_agent()
        sup = self.supervisor()
        sup.on_event({'event': 'activated', 'bundleId': ALPHA_BUNDLE, 'pid': pid})
        self.clock.now += 60 * 60
        [op] = sup.step(woke=False)                     # the minute's timeout, not a sweep
        self.assertEqual(self.wait_internal(op)['state'], 'stopped')

    def test_the_sweep_prunes_the_store_while_the_broker_runs(self):
        self.started_by_agent()
        self.clock.now += broker.OPERATION_RETENTION_SECONDS + 60
        with self.broker.db_lock:
            before = self.broker.db.execute('SELECT count(*) FROM operations').fetchone()[0]
        sweep_step(self.supervisor())
        with self.broker.db_lock:
            after = self.broker.db.execute('SELECT count(*) FROM operations').fetchone()[0]
        self.assertEqual((before, after), (1, 0))


if __name__ == '__main__':
    unittest.main()
