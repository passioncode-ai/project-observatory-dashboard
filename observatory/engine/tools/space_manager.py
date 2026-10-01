"""Bounded cache maintenance. Docs: docs/runs/2026-10-01-space-manager/README.md.

The register reports occupied bytes, never promises reclaimable bytes. Only native
cache owners remove data. No caller supplies a path, shell fragment or executable.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import configuration
import paths
import workspace

GB = 1_000_000_000
TRIGGER = 10 * GB
TARGET = 15 * GB
COOLDOWN = 1800
PLAN_TTL = 600
# Fixed roots only. Shared caches, environments and build trees with no safe owner
# operation are still measured, but neither UI nor automation can delete them.
CACHES = {
    'npm': ('npm', '.npm', 'package', ('node', 'npm', 'npx', 'pnpm', 'yarn', 'bun')),
    'uv': ('uv', '.cache/uv', 'package', ('uv', 'uvx', 'python', 'pip')),
    'pip': ('pip', 'Library/Caches/pip' if sys.platform == 'darwin' else '.cache/pip', 'package', ()),
    'pnpm': ('pnpm', 'Library/pnpm/store' if sys.platform == 'darwin' else '.local/share/pnpm/store', 'package', ()),
    'pnpm-cache': ('pnpm metadata', 'Library/Caches/pnpm' if sys.platform == 'darwin' else '.cache/pnpm', 'package', ()),
    'cocoapods': ('CocoaPods', 'Library/Caches/CocoaPods', 'package', ()),
    'swiftpm': ('SwiftPM', 'Library/Caches/org.swift.swiftpm' if sys.platform == 'darwin' else '.cache/org.swift.swiftpm', 'package', ()),
    'yarn': ('Yarn', 'Library/Caches/Yarn' if sys.platform == 'darwin' else '.cache/yarn', 'package', ()),
    'bun': ('Bun', '.bun/install/cache', 'package', ()),
    'homebrew': ('Homebrew', 'Library/Caches/Homebrew', 'package', ()),
    'gradle': ('Gradle', '.gradle/caches', 'build', ()),
    'cargo': ('Cargo', '.cargo/registry', 'package', ()),
    'xcode': ('Xcode', 'Library/Developer/Xcode/DerivedData', 'build', ()),
    'docker-build': ('Docker BuildKit', None, 'build', ()),
}
CLEANABLE = frozenset({'uv', 'docker-build'})
AUTOMATIC = frozenset({'uv', 'docker-build'})


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def linked(path: Path) -> bool:
    return any(p.is_symlink() for p in (path, *path.parents))


def read_json(path: Path, default):
    if linked(path):
        raise ValueError('linked-state')
    if not path.exists():
        return default
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict): raise ValueError()
        return value
    except (ValueError, OSError):
        raise ValueError('unreadable-state') from None


class Manager:
    def __init__(self):
        self.home = Path.home().resolve()
        self.state_file = paths.SCRATCH / 'space-state.json'
        self.registry_file = paths.REGISTRY / 'caches.json'
        self.lock_dir = self.home / '.local/state/project-observatory'

    def state(self):
        state = read_json(self.state_file, {'runs': [], 'notifications': []})
        for field in ('runs', 'notifications'):
            if not isinstance(state.get(field, []), list) or any(not isinstance(r, dict) for r in state.get(field, [])):
                raise ValueError('unreadable-state')
        if state.get('running') is not None and not isinstance(state['running'], dict):
            raise ValueError('unreadable-state')
        if state.get('plan') is not None and not isinstance(state['plan'], dict):
            raise ValueError('unreadable-state')
        return state

    def recover(self):
        # Called only under the machine lock. Native children inherit this lock,
        # so a still-running child cannot be mistaken for an interrupted cleanup.
        state = self.state()
        if state.get('running'):
            run = state.pop('running')
            run.update(status='interrupted', finished_at=stamp())
            state['runs'] = (state.get('runs', []) + [run])[-100:]
            state['notifications'] = (state.get('notifications', []) +
                                      [{'at': stamp(), 'kind': 'cleanup-interrupted'}])[-100:]
            self.write_state(state)

    def write_state(self, state):
        workspace.write_json(self.state_file, state)

    @contextmanager
    def cleanup_lock(self):
        # Machine-wide across workspaces, not merely one server's thread lock.
        if linked(self.lock_dir): raise ValueError('linked-lock')
        self.lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.lock_dir / 'space.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd = fd
            self.recover()
            yield
        finally:
            self._lock_fd = None
            os.close(fd)

    def free_bytes(self):
        return shutil.disk_usage(self.home).free

    def enabled(self):
        return configuration.enabled('space_auto_cleanup', 'features')

    def set_auto(self, enabled):
        if type(enabled) is not bool: raise ValueError('invalid-policy')
        with workspace.lock(configuration.home()):
            cfg = configuration.load()
            cfg.setdefault('features', {})['space_auto_cleanup'] = enabled
            workspace.write_json(configuration.home() / 'config/settings.json', cfg)
        return {'auto_enabled': enabled}

    def run_command(self, argv, timeout=60):
        # No inherited provider credentials or shell execution; stderr may contain
        # private paths/URLs, so neither stream is journalled or returned to the UI.
        env = {k: os.environ[k] for k in ('PATH', 'HOME', 'TMPDIR', 'SystemRoot') if k in os.environ}
        if argv[0] == 'notify-send':
            env.update({k: os.environ[k] for k in ('DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR') if k in os.environ})
        env.update({'LC_ALL': 'C', 'UV_LOCK_TIMEOUT': '1', 'UV_NO_CONFIG': '1'})
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, cwd=self.home, start_new_session=True,
                                pass_fds=(self._lock_fd,) if getattr(self, '_lock_fd', None) is not None else ())
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try: os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            proc.communicate()
            raise TimeoutError('command-timeout') from None
        return proc.returncode, out.decode('utf-8', errors='replace')

    def consumers(self):
        """Only executable families leave the probe. Failed visibility is unknown.

        Checking processes is an additional guard, not a lock. Native uv/BuildKit
        coordination remains responsible for races with newly starting consumers.
        Caches with no native concurrency guarantee stay inventory-only.
        """
        try:
            code, out = self.run_command(['ps', '-axo', 'pid=,comm='], timeout=10)
            if code or not out.strip(): return None
            names = set()
            for line in out.splitlines():
                pid, exe = line.strip().split(None, 1)
                if int(pid) == os.getpid(): continue
                name = Path(exe).name.lower()
                if name.startswith('python'): name = 'python'
                if name.startswith('pip'): name = 'pip'
                names.add(name)
            return names
        except (OSError, ValueError, TimeoutError):
            return None

    def cache_path(self, key):
        rel = CACHES[key][1]
        if key == 'uv' and os.environ.get('UV_CACHE_DIR'):
            return Path(os.environ['UV_CACHE_DIR']).expanduser()
        return self.home / rel if rel else None

    def command(self, key, endpoint='unix:///var/run/docker.sock'):
        if key == 'uv': return ['uv', 'cache', 'prune', '--cache-dir', str(self.cache_path(key)), '--offline']
        if key == 'docker-build': return ['docker', '--host', endpoint, 'builder', 'prune', '--force', '--filter', 'until=168h']
        raise ValueError('unsupported-cache')

    def docker_endpoint(self):
        # Never send maintenance to DOCKER_HOST or a remote selected context.
        if os.environ.get('DOCKER_HOST') or os.environ.get('DOCKER_CONTEXT'):
            return None
        try:
            code, out = self.run_command(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'], 10)
            return out.strip() if code == 0 and out.strip().startswith('unix://') else None
        except (OSError, TimeoutError): return None

    def cache_row(self, key, *, measure=True, consumers=None):
        try:
            return self._cache_row(key, measure=measure, consumers=consumers)
        except OSError:
            label, _, kind, _ = CACHES[key]
            return {'id': key, 'owner': label, 'kind': kind, 'path': str(self.cache_path(key)),
                    'size_bytes': None, 'measured_at': None, 'eligible': False,
                    'automatic': key in AUTOMATIC, 'reason': 'cache-unavailable',
                    'measurement_error': 'unreadable'}

    def _cache_row(self, key, *, measure=True, consumers=None):
        label, _, kind, families = CACHES[key]
        path = self.cache_path(key)
        row = {'id': key, 'owner': label, 'kind': kind, 'path': str(path) if path else 'local Docker daemon',
               'size_bytes': None, 'measured_at': None, 'eligible': False,
               'automatic': key in AUTOMATIC, 'reason': 'inventory-only'}
        if path:
            if not path.is_absolute() or '..' in path.parts or path == self.home or not path.is_relative_to(self.home):
                row['reason'] = 'outside-home'; return row
            if linked(path): row['reason'] = 'linked-path'; return row
            if not path.is_dir(): row['reason'] = 'not-present'; return row
            if path.stat().st_dev != self.home.stat().st_dev:
                row['reason'] = 'different-volume'; return row
            if measure:
                try:
                    code, out = self.run_command(['du', '-sk', str(path)], 8)
                    if code == 0:
                        row['size_bytes'] = int(out.split()[0]) * 1024
                        row['measured_at'] = stamp()
                    else: row['measurement_error'] = 'unreadable'
                except (OSError, ValueError, IndexError, TimeoutError):
                    row['measurement_error'] = 'measurement-unavailable'
        if key not in CLEANABLE: return row
        if not shutil.which(self.command(key)[0]): row['reason'] = 'tool-missing'; return row
        if key == 'docker-build':
            row['endpoint'] = self.docker_endpoint()
            if not row['endpoint']: row['reason'] = 'local-daemon-unverified'; return row
            if measure:
                try:
                    code, out = self.run_command(['docker', '--host', row['endpoint'], 'system', 'df', '--format', '{{json .}}'], 15)
                    if code == 0:
                        # Docker's formatted total is deliberately retained as text;
                        # never reinterpret the VM total as reclaimable host bytes.
                        rows = [json.loads(line) for line in out.splitlines()]
                        if any(not isinstance(r, dict) for r in rows): raise ValueError('invalid-daemon-response')
                        build = next((r for r in rows if r.get('Type') == 'Build Cache'), None)
                        if build:
                            row['size_display'] = build.get('Size')
                            row['reclaimable_display'] = build.get('Reclaimable')
                            row['measured_at'] = stamp()
                    else: row['measurement_error'] = 'daemon-unavailable'
                except (OSError, ValueError, TimeoutError): row['measurement_error'] = 'measurement-unavailable'
        else:
            active = consumers if consumers is not None else self.consumers()
            if active is None: row['reason'] = 'activity-unknown'; return row
            if any(f in active for f in families): row['reason'] = 'in-use'; return row
        if key == 'uv':
            try:
                code, help_text = self.run_command(['uv', 'cache', 'prune', '--help'], 5)
                if code or 'ignoring in-use checks' not in help_text:
                    row['reason'] = 'native-lock-unverified'; return row
            except (OSError, TimeoutError):
                row['reason'] = 'native-lock-unverified'; return row
        row.update(eligible=True, reason='native-cache-maintenance')
        return row

    def scan(self):
        with self.cleanup_lock():
            return self._scan()

    def recover_pending(self):
        with self.cleanup_lock():
            pass

    def _scan(self):
        active = self.consumers()
        rows = [self.cache_row(key, consumers=active) for key in CACHES]
        doc = {'schema_version': 1, 'measured_at': stamp(), 'caches': rows}
        workspace.write_json(self.registry_file, doc)
        return doc

    def status(self):
        state = self.state()
        reg = read_json(self.registry_file, {'caches': [], 'measured_at': None})
        if not isinstance(reg.get('caches'), list) or any(not isinstance(r, dict) for r in reg['caches']):
            raise ValueError('unreadable-registry')
        free = self.free_bytes()
        return {**reg, 'free_bytes': free, 'observed_at': stamp(), 'trigger_bytes': TRIGGER, 'target_bytes': TARGET,
                'auto_enabled': self.enabled(), 'pressure': 'critical' if free < TRIGGER else 'normal',
                'runs': state.get('runs', [])[-20:], 'notifications': state.get('notifications', [])[-30:],
                'running': state.get('running'), 'cooldown_seconds': COOLDOWN}

    def plan(self, ids=None):
        with self.cleanup_lock():
            return self._plan(ids)

    def _plan(self, ids=None):
        ids = list(CLEANABLE) if ids is None else ids
        if not isinstance(ids, list) or any(not isinstance(k, str) or k not in CLEANABLE for k in ids) or len(ids) != len(set(ids)):
            raise ValueError('unsupported-cache')
        state = self.state()
        plan = {'id': secrets.token_hex(16), 'created_epoch': time.time(), 'created_at': stamp(),
                'actions': [self.cache_row(k, measure=False) for k in sorted(ids)]}
        state['plan'] = plan
        self.write_state(state)
        return plan

    def notify(self, state, kind):
        if state.get('pressure_notified') == kind: return
        state['pressure_notified'] = kind
        event = {'at': stamp(), 'kind': kind}
        state['notifications'] = (state.get('notifications', []) + [event])[-100:]
        self.write_state(state)  # durable notice even if the desktop is unavailable
        event['desktop'] = self.desktop_notice(kind)

    def desktop_notice(self, kind):
        from dashboard.i18n import Translator
        t = Translator(configuration.interface_locale())
        message = t('Disk space is critically low') if kind == 'critical' else t('Disk space has recovered')
        if sys.platform == 'darwin':
            argv = ['osascript', '-e', 'display notification ' + json.dumps(message, ensure_ascii=False) + ' with title "Observatory"']
        elif sys.platform.startswith('linux') and shutil.which('notify-send'):
            argv = ['notify-send', 'Observatory', message]
        else: return 'unavailable'
        try:
            code, _ = self.run_command(argv, 5)
            return 'requested' if code == 0 else 'unavailable'
        except (OSError, TimeoutError): return 'unavailable'

    def cancel(self, plan_id):
        with self.cleanup_lock():
            state = self.state()
            if (state.get('plan') or {}).get('id') == plan_id:
                state.pop('plan')
                self.write_state(state)
        return {'status': 'cancelled'}

    def execute(self, plan_id, *, automatic=False):
        with self.cleanup_lock():
            state = self.state()
            plan = state.get('plan') or {}
            if (not isinstance(plan_id, str) or len(plan_id) != 32 or plan.get('id') != plan_id
                    or not isinstance(plan.get('created_epoch'), (int, float))
                    or not 0 <= time.time() - plan['created_epoch'] <= PLAN_TTL):
                raise ValueError('expired-plan')
            actions = plan.get('actions')
            if not isinstance(actions, list) or any(not isinstance(a, dict) or
                    not isinstance(a.get('id'), str) or a['id'] not in CLEANABLE for a in actions):
                raise ValueError('invalid-plan')
            state.pop('plan', None)  # consumed before the first destructive operation
            run = {'id': plan_id, 'started_at': stamp(), 'mode': 'automatic' if automatic else 'manual',
                   'before_free_bytes': self.free_bytes(), 'actions': [], 'status': 'running', 'pid': os.getpid()}
            state['running'] = run
            self.write_state(state)  # a full disk that cannot journal means no deletion
            try:
                for planned in plan['actions']:
                    key = planned['id']
                    if automatic and (not self.enabled() or self.free_bytes() >= TARGET): break
                    if automatic and key not in AUTOMATIC: continue
                    row = self.cache_row(key, measure=False)
                    action = {'cache_id': key, 'at': stamp()}
                    if planned.get('path') != row['path'] or planned.get('endpoint') != row.get('endpoint'):
                        action.update(result='skipped', reason='cache-moved')
                    elif not planned['eligible'] or not row['eligible']:
                        action.update(result='skipped', reason=row['reason'] if not row['eligible'] else 'not-in-preview')
                    else:
                        try:
                            code, _ = self.run_command(self.command(key, row.get('endpoint')), 90)
                            action.update(result='completed' if code == 0 else 'failed', exit_code=code)
                        except (OSError, TimeoutError): action.update(result='failed', reason='command-failed')
                    run['actions'].append(action)
                    self.write_state(state)
                run['status'] = ('partial' if any(a['result'] == 'failed' for a in run['actions']) else
                                 'completed' if any(a['result'] == 'completed' for a in run['actions']) else 'skipped')
            finally:
                if run['status'] == 'running': run['status'] = 'interrupted'
                run['finished_at'] = stamp()
                run['after_free_bytes'] = self.free_bytes()
                run['free_delta_bytes'] = run['after_free_bytes'] - run['before_free_bytes']
                state['running'] = None
                state['runs'] = (state.get('runs', []) + [run])[-100:]
                state['notifications'] = (state.get('notifications', []) +
                                          [{'at': stamp(), 'kind': 'cleanup-finished', 'run_id': plan_id}])[-100:]
                if run['after_free_bytes'] < TRIGGER:
                    state['notifications'].append({'at': stamp(), 'kind': 'still-critical'})
                    state['notifications'] = state['notifications'][-100:]
                self.write_state(state)
            return run

    def monitor(self):
        result = self.check_pressure()
        reg = read_json(self.registry_file, {})
        try:
            age = time.time() - datetime.fromisoformat(reg.get('measured_at', '')).timestamp()
        except (ValueError, TypeError):
            age = 901
        if age > 900:
            self.scan()
        return result

    def check_pressure(self):
        with self.cleanup_lock():
            state = self.state()
            free = self.free_bytes()
            if free >= TARGET:
                if state.get('pressure_notified') == 'critical': self.notify(state, 'recovered')
            elif free < TRIGGER:
                self.notify(state, 'critical')
            now = time.time()
            # Machine-wide cooldown survives service restarts and different workspaces.
            cooldown = read_json(self.lock_dir / 'space-last-auto.json', {})
            epoch = cooldown.get('attempt_epoch', 0)
            if not isinstance(epoch, (int, float)): raise ValueError('unreadable-cooldown')
            due = now - epoch >= COOLDOWN
            should_run = free < TRIGGER and self.enabled() and due
            if should_run:
                workspace.write_json(self.lock_dir / 'space-last-auto.json', {'attempt_epoch': now})
            self.write_state(state)
        if should_run:
            plan = self.plan(sorted(AUTOMATIC))
            return self.execute(plan['id'], automatic=True)
        return {'status': 'observed', 'free_bytes': free}


class Supervisor:
    """One background task per server; disk pressure never stalls its heartbeat."""
    def __init__(self):
        self.lock = threading.Lock()
        self.busy = False
        self.last_check = 0.0
        self.error = None
        self.recovered = False

    def start(self, action, *args):
        with self.lock:
            if self.busy: return False
            self.busy = True
        def work():
            try:
                getattr(Manager(), action)(*args)
                self.error = None
            except (OSError, ValueError, TimeoutError, configuration.ConfigurationError) as exc:
                self.error = type(exc).__name__
            finally:
                with self.lock: self.busy = False
        threading.Thread(target=work, daemon=True).start()
        return True

    def poll(self):
        if not self.recovered:
            if self.start('recover_pending'): self.recovered = True
            return
        try:
            if not (configuration.enabled('machine_watch', 'features') or
                    configuration.enabled('space_auto_cleanup', 'features')):
                return
        except configuration.ConfigurationError:
            self.error = 'ConfigurationError'
            return
        if time.monotonic() - self.last_check < 60: return
        if self.start('monitor'): self.last_check = time.monotonic()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('action', choices=['status', 'scan', 'plan', 'clean', 'auto', 'enable', 'disable'], nargs='?', default='status')
    ap.add_argument('--plan', help='fresh preview id, required for clean')
    args = ap.parse_args(argv)
    try:
        m = Manager()
        if args.action in ('enable', 'disable'): result = m.set_auto(args.action == 'enable')
        elif args.action == 'clean': result = m.execute(args.plan)
        elif args.action == 'auto': result = m.check_pressure()
        else: result = getattr(m, args.action)()
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({'error': str(exc) if isinstance(exc, ValueError) else type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
