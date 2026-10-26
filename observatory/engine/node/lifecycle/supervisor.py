"""The lifecycle supervisor: the broker's supervision pass on a cadence (OBS-79 step L4, docs/design/LIFECYCLE.md).

The broker decides (`Broker.supervise`: always_on restarts, idle stops) and enforces every rule that keeps it out of a
person's way (`in_use_by_person`, `held_off`, leases, the crash circuit, the restart budget); this module only decides
WHEN to ask. It never starts, stops or probes a target itself: it calls the broker, and the broker calls the adapter.

What it keeps, and why:

- **No busy timer.** It wakes on a watcher event for an enrolled app, on a slow safety sweep (`SWEEP_SECONDS`, 600 s,
  on a monotonic deadline of its own, so no stream of events can postpone it), and once a minute
  (`IDLE_CHECK_SECONDS`) only while a process the broker started can still be idled out: its target has
  `idleStopMinutes`, is approved, is not held off, and the person has not taken it over. That minute's pass looks at
  those targets only; it is not a sweep.
- **The watcher.** The adapter's `watch` (macOS: the helper's launch, terminate and activate events) runs on a thread
  of its own. An activation is the person bringing the app forward and goes to `Broker.note_activation`; a launch or
  a termination of an enrolled app wakes a pass over that app's targets at once. Events are matched against the
  enrolled bundles read at the last sweep, so an event of any other app reads no file; an app enrolled since is
  matched from the next sweep. A watcher that ends (no helper yet,
  a helper built from older source, a crash past its own backoff) is started again by the next sweep, never in a
  tight loop.
- **The sweep** also prunes the store (the broker prunes only when it opens, and a server runs for weeks) and queues
  every enrolled app's signature check off the request path (`warm`), so an agent's first call after a restart does
  not pay a deep `codesign` check.
- **One pass that fails is one pass.** An exception inside `supervise`, `prune`, the warm-up or the watcher is logged
  and the loop goes on; the first failure after a good pass is one `notify` event, not one per minute.
- **One supervisor per broker.** `start` is idempotent and the broker holds the state folder's exclusive lock, so two
  supervisors can never act over one state folder.
- **Only what the adapter supports.** Over an adapter that answers `unsupported_platform` (Linux and Windows today)
  the supervisor runs no pass, no warm-up and no watcher; it only keeps the store pruned.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from node.lifecycle.broker import LifecycleError

#: The safety sweep: always_on targets are looked at even when no event arrived.
SWEEP_SECONDS = 600
#: While a process the broker started is alive, its idle lease is checked this often.
IDLE_CHECK_SECONDS = 60
#: How long `close` waits for each of its threads.
JOIN_SECONDS = 3


def supports_supervision(adapter: Any) -> bool:
    """False for the adapter of a platform with none yet: it can only answer `unsupported_platform`."""
    from node.lifecycle import macos
    return not isinstance(adapter, macos.UnsupportedAdapter)


class Supervisor:
    """Runs `broker.supervise` on its cadence and feeds the adapter's watcher events to the broker.

    `registry` returns the broker's registry document (`{'targets': {...}}`); `on_failure(what, exc, first)` is told
    of a failed pass, prune, warm-up or watcher (`first` is True for the first failure after a good pass), and
    `on_watch_end()` when the watcher ended while the supervisor still runs."""

    def __init__(self, broker, adapter, registry: Callable[[], dict], *,
                 on_failure: Optional[Callable[[str, BaseException, bool], None]] = None,
                 on_watch_end: Optional[Callable[[], None]] = None,
                 supported: Optional[bool] = None,
                 thread: Callable[..., threading.Thread] = threading.Thread,
                 monotonic: Callable[[], float] = time.monotonic):
        self.broker, self.adapter, self.registry = broker, adapter, registry
        self.on_failure, self.on_watch_end = on_failure, on_watch_end
        self.supported = supports_supervision(adapter) if supported is None else supported
        self.thread = thread
        self.monotonic = monotonic
        #: The next safety sweep, on the monotonic clock. The start's own pass stands for the first one.
        self.next_sweep = monotonic() + SWEEP_SECONDS
        #: Enrolled app bundles -> their target ids, read at the last sweep (or at the first event before one).
        self._bundles: Optional[dict] = None
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.pending: set = set()
        self._lock = threading.Lock()
        self._loop: Optional[threading.Thread] = None
        self._watcher: Optional[threading.Thread] = None
        self._failing: set = set()

    # -- what wakes it -------------------------------------------------------------------------------------------------

    def _targets(self) -> dict:
        try:
            return dict(self.registry()['targets'])
        except (LifecycleError, KeyError, TypeError):
            return {}

    def idle_targets(self) -> list:
        """Targets whose broker-started process the idle pass can still stop: `idleStopMinutes` set, approved, not held
        off, and a launch the person has not taken over."""
        candidates = [key for key, t in self._targets().items()
                      if t.get('idleStopMinutes') and t.get('desiredState') != 'held_off' and t.get('approved') is True]
        if not candidates:
            return []
        try:
            return self.broker.idle_launches(candidates)
        except Exception:  # noqa: BLE001 - a store that cannot be read now waits for the sweep, not a tight loop
            return []

    def interval(self) -> float:
        """Seconds until the next wake-up: the sweep's deadline, or a minute while an idle lease can run out."""
        left = max(0.0, self.next_sweep - self.monotonic())
        return min(left, IDLE_CHECK_SECONDS) if self.idle_targets() else left

    def refresh_bundles(self) -> None:
        bundles: dict = {}
        for key, t in self._targets().items():
            if t.get('kind') == 'app' and isinstance(t.get('identity'), dict):
                bundles.setdefault(t['identity'].get('bundleId'), set()).add(key)
        self._bundles = bundles

    def on_event(self, event: dict) -> None:
        """One watcher event: `{'event': launched | terminated | activated, 'bundleId', 'pid'}`."""
        if self._bundles is None:
            self.refresh_bundles()
        ids = set(self._bundles.get(event.get('bundleId'), ()))
        if not ids:
            return
        if event.get('event') == 'activated':
            pid = event.get('pid')
            if type(pid) is not int:
                return
            try:
                self.broker.note_activation(ids, pid)
            except Exception as exc:  # noqa: BLE001 - a closed store at shutdown is not the watcher's death
                self._failed('activation', exc)
            return
        with self._lock:
            self.pending |= ids
        self.wake.set()

    # -- one pass ------------------------------------------------------------------------------------------------------

    def tick(self, only=None) -> list:
        """One supervision pass (all targets, or `only`); the operation ids it admitted."""
        if not self.supported:
            return []
        try:
            admitted = self.broker.supervise(only)
        except Exception as exc:  # noqa: BLE001 - supervision must outlive one bad pass
            self._failed('pass', exc)
            return []
        self._recovered('pass')
        return admitted

    def warm(self) -> None:
        """Queue every enrolled target's identity check off the request path. The deadline is now: the adapter starts
        the check and answers `verification_pending` at once; the result is cached for the next caller."""
        if not self.supported:
            return
        try:
            targets = list(self.registry()['targets'].values())
        except (LifecycleError, KeyError, TypeError):
            return
        for target in targets:
            if self.stop_event.is_set():
                return
            try:
                self.adapter.identity_matches(target, deadline=self.broker.clock())
            except Exception:  # noqa: BLE001 - a check that decided nothing is the next caller's honest answer
                continue

    def prune(self) -> None:
        try:
            self.broker.prune()
        except Exception as exc:  # noqa: BLE001
            self._failed('prune', exc)
            return
        self._recovered('prune')

    def sweep(self) -> None:
        """The safety sweep's own work, before its pass: the enrolled bundles read again, the warm-up, the store
        bounded, the watcher alive."""
        self.refresh_bundles()
        self.warm()
        self.prune()
        self.ensure_watcher()

    def step(self, woke: bool) -> list:
        """What one wake-up does, whatever woke it: the sweep and a pass over every target when the sweep's deadline
        has come; otherwise a pass over the targets an event named and, on the minute's timeout, the targets an idle
        lease can still stop. Nothing to look at is no pass."""
        now = self.monotonic()
        swept = now >= self.next_sweep
        if swept:
            self.next_sweep = now + SWEEP_SECONDS
            self.sweep()
        with self._lock:
            pending, self.pending = self.pending, set()
        if swept:
            return self.tick(None)
        only = set(pending)
        if not woke:
            only |= set(self.idle_targets())
        return self.tick(only) if only else []

    def run(self) -> None:
        self.refresh_bundles()
        self.warm()
        self.ensure_watcher()
        self.tick()
        self.next_sweep = self.monotonic() + SWEEP_SECONDS
        while not self.stop_event.is_set():
            woke = self.wake.wait(self.interval())
            self.wake.clear()
            if self.stop_event.is_set():
                return
            self.step(woke)

    # -- the watcher ---------------------------------------------------------------------------------------------------

    def ensure_watcher(self) -> bool:
        """Start the watcher unless it runs, the adapter has none, or the supervisor is stopping. True when started."""
        if not self.supported or not callable(getattr(self.adapter, 'watch', None)):
            return False
        with self._lock:
            if self.stop_event.is_set() or (self._watcher is not None and self._watcher.is_alive()):
                return False
            self._watcher = self.thread(target=self._watch, name='lifecycle-watch', daemon=True)
            self._watcher.start()
            return True

    def _watch(self) -> None:
        try:
            self.adapter.watch(self.on_event, self.stop_event)
        except Exception as exc:  # noqa: BLE001 - a watcher that raises is started again by the next sweep
            self._failed('watch', exc)
        if not self.stop_event.is_set() and self.on_watch_end:
            try:
                self.on_watch_end()
            except Exception:  # noqa: BLE001
                pass

    # -- start and stop ------------------------------------------------------------------------------------------------

    def start(self) -> bool:
        """Start the loop once. A second call, or a call after `close`, starts nothing."""
        with self._lock:
            if self._loop is not None or self.stop_event.is_set():
                return False
            self._loop = self.thread(target=self.run, name='lifecycle-supervisor', daemon=True)
            self._loop.start()
            return True

    @property
    def running(self) -> bool:
        return self._loop is not None and self._loop.is_alive()

    def close(self) -> None:
        """Stop the loop and the watcher; called before the broker closes, so no pass starts on a closed store."""
        with self._lock:
            self.stop_event.set()
            loop, watcher = self._loop, self._watcher
        self.wake.set()
        stop_watch = getattr(self.adapter, 'stop_watch', None)
        if callable(stop_watch):
            try:
                stop_watch()
            except Exception:  # noqa: BLE001 - the adapter's close ends the helper too
                pass
        for thread in (loop, watcher):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=JOIN_SECONDS)

    # -- failures ------------------------------------------------------------------------------------------------------

    def _failed(self, what: str, exc: BaseException) -> None:
        if self.stop_event.is_set():
            return                  # a pass cut short by shutdown (a store closing under it) is not a failure
        first = what not in self._failing
        self._failing.add(what)
        if self.on_failure:
            try:
                self.on_failure(what, exc, first)
            except Exception:  # noqa: BLE001 - a log that cannot be written never stops supervision
                pass

    def _recovered(self, what: str) -> None:
        self._failing.discard(what)
