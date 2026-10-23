"""`node.lifecycle` served: the six MCP tools on the engine's local server (OBS-79 step L3, docs/design/LIFECYCLE.md).

The broker (`broker.py`) decides and the adapter (`macos.py`) acts; this module is the door in front of them, and the
engine's local server (`tools/serverd.py`) is the only process that opens it, on its own port, at `POST /mcp`. What
it keeps, and why:

- **No credential of its own.** A caller proves who it is with the bearer of an access binding the operator issued
  (access-bindings/1, `full access-binding issue`): the server hashes the bearer, `access_binding.resolve` finds the
  binding for this workspace's audience, unexpired and unrevoked, and the binding's principal (`agent:<name>`) is the
  caller. What that caller may do with which target is a second, separate grant in `config/node-lifecycle.json`
  (`full lifecycle allow --binding ID`), and the grant names ONE binding by its id: the binding's scopes, projects and
  effect ceiling are memory's and say nothing about apps, so a binding the operator issued for reading memory never
  gains lifecycle powers by sharing a principal with the bound one. Another binding of the same principal is `denied`
  (403), as is a binding without a grant; the descriptor's service token, which opens the events feed, is not a
  binding and opens nothing here. Revoking the bound binding, or the grant, refuses the caller's next call and stops
  an admitted operation before its effect, because the broker reads both again then.
- **No owner over the wire.** The operator's authority (enrolling, holding a target off, a forced stop) is a terminal
  (`tools/lifecycle_cli.py`), never a bearer: a grant has no `owner` field, and `operatorProxy`, which may hold an
  always_on target off, is the most a caller is given.
- **One owner-only document.** `config/node-lifecycle.json` holds the enrolled targets (the broker's closed registry)
  and the callers' grants. It is read on every call, so an operator's change reaches the next request without a
  restart; a document that is not owner-only, is a link, or fails its schema refuses every call (`unsafe_config`,
  `invalid_config`) rather than serving a guess.
- **What happened, in words and in detail.** Every admitted operation and every result is one sentence in
  `<state>/node-lifecycle/events.jsonl` (the activity a person reads: it names the target and, when it failed, why),
  and one structured line in `<state>/node-lifecycle/logs/lifecycle.jsonl` (rotated 5 x 5 MB, owner-only). A correct
  refusal (`in_use_by_person`, `held_off`) wakes nobody; a failure asks to be noticed (`notify`).
- **Inert until enrolled.** With no `config/node-lifecycle.json` the server builds no broker, creates no state and
  answers every caller `denied`; nothing starts, stops or probes until the operator enrolls a target and allows a
  caller.

The supervisor (always_on restarts, idle stops) is step L4: until then an idle app the node started is not stopped by
itself, and an always_on target is not brought back after a crash.
"""
from __future__ import annotations

import copy
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import access_binding as AB
import fabric_service as fs
import interop
import private_io

from node.lifecycle import broker
from node.lifecycle.broker import TOOLS, LifecycleError

#: What the well-known document and the descriptor's hosts call this service.
SERVICE = 'node.lifecycle'
#: The well-known document's extension key for this service's entry (contract: extensions keyed by a URI).
EXTENSION_KEY = 'https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/LIFECYCLE.md'
MCP_PATH = '/mcp'
#: Not `config/lifecycle.json`, which configures the lifecycle watch (collectors/scan_lifecycle.py), another thing.
CONFIG_NAME = 'node-lifecycle.json'
SCHEMA_VERSION = 1
GRANT_FIELDS = ('bindingId', 'revision', 'revoked', 'allowedTools', 'allowedTargets')
GRANT_OPTIONAL = ('operatorProxy',)
EVENTS_FILE = 'events.jsonl'
EVENTS_KEEP = 5000
LOG_FILE = 'lifecycle.jsonl'
#: MCP revisions a client may name in `initialize`; anything else is answered with the newest.
MCP_REVISION = '2026-07-28'
HANDSHAKE_REVISIONS = ('2025-03-26', '2025-06-18', '2025-11-25', MCP_REVISION)
#: A request body larger than this is refused before it is parsed.
MAX_BODY_BYTES = 65536

DESCRIPTIONS = {
    'lifecycle.list': 'List enrolled targets visible to this credential, with current revisions.',
    'lifecycle.status': (
        'Observe an enrolled target without starting, stopping or renewing it: readiness, pid, visible windows, '
        'and who it belongs to (startedBy: broker, person or other).'
    ),
    'lifecycle.ensure_running': (
        'Admit one durable, authorized start; poll lifecycle.operation.get. Calling it again while you still use an '
        'app the node started renews your lease against the idle stop.'
    ),
    'lifecycle.stop': (
        'Admit one durable graceful stop (the app is asked to quit, then signalled, then waited for). You may stop only '
        'an app the node started that the person has not taken over and is not using right now; otherwise '
        'in_use_by_person. A forced stop is the operator\'s, at a terminal.'
    ),
    'lifecycle.restart': (
        'Admit one durable stop-then-start under the same rules as lifecycle.stop; use it for a running app that does '
        'not answer. Poll lifecycle.operation.get.'
    ),
    'lifecycle.operation.get': 'Read the receipt of an operation you started.',
}
#: The contract's effect of each tool: stopping or restarting ends a running process, so both are destructive.
EFFECTS = {'lifecycle.ensure_running': 'draft', 'lifecycle.stop': 'deploy', 'lifecycle.restart': 'deploy'}

#: Read by every MCP client at `initialize`: the one place every agent learns the rules. Neutral by design: no app,
#: caller or machine is named here (tests/test_node_lifecycle_service.py pins the phrases and the length).
INSTRUCTIONS = (
    "Project Observatory's node.lifecycle wakes, stops and restarts the desktop apps and background services the "
    "operator enrolled on this machine. Never launch, quit or kill an enrolled app yourself (no `open -a`, "
    "`osascript`, `pkill`): ask here. To use an app: lifecycle.list (its policyRevision and your grantRevision), then "
    "lifecycle.ensure_running with a fresh requestId, then poll lifecycle.operation.get until a final state; ready "
    "and already_ready are success. While you keep working with an app the node started, call ensure_running again "
    "at least every 10 minutes: it is cheap when the app is up and it renews your lease, because an app the node "
    "started that nobody renews and nobody uses is stopped after its idle minutes. lifecycle.restart fixes a running "
    "app that does not answer; lifecycle.stop ends one you started. Both act only on apps the node started that the "
    "person has not taken over and is not using right now: in_use_by_person means the person opened, took over or "
    "is looking at it, so tell the person instead of retrying. held_off means the operator switched it off on "
    "purpose: do not work around it. lifecycle.status never starts, stops or renews anything. denied means the "
    "operator has not allowed you that tool or target (the operator's `full lifecycle allow`); "
    "unsupported_platform means this system has no adapter yet."
)

#: What a refusal tells its caller to do. Every code the tools answer has a line; an unknown one says so.
HINTS = {
    'unauthorized': 'send the bearer of an access binding the operator issued for this workspace '
                    '(`project-observatory full access-binding issue PRINCIPAL --project PROJECT --token-file PATH`)',
    'denied': 'the operator has not allowed this binding that tool or target: '
              '`project-observatory full lifecycle allow PRINCIPAL --binding BINDING_ID --tool TOOL --target TARGET`',
    'not_enrolled': 'the operator has not enrolled that target: `project-observatory full lifecycle status` lists them',
    'policy_changed': 'call lifecycle.list for the current policyRevision and grantRevision, then ask again',
    'idempotency_conflict': 'this requestId already names another request; use a fresh one',
    'held_off': 'the operator switched this target off on purpose; do not work around it',
    'in_use_by_person': 'the person is using it or took it over; tell the person instead of retrying',
    'busy': 'too much is running, or the server is stopping; ask again in a moment',
    'unsupported_platform': 'this system has no lifecycle adapter yet; nothing was started or stopped',
    'invalid_argument': 'the arguments do not match the tool\'s input schema',
    'not_found': 'no operation of yours has that id (receipts are kept 7 days)',
    'invalid_config': 'config/node-lifecycle.json fails its schema; `project-observatory full lifecycle status` '
                      'names the fault',
    'unsafe_config': 'config/node-lifecycle.json must be an owner-only regular file reached through no link',
    'setup_required': 'the macOS helper is not built: `project-observatory full lifecycle build-helper`',
    'already_running': 'another process holds this workspace\'s lifecycle state',
}


class ConfigError(LifecycleError):
    """`config/node-lifecycle.json` is refused; `detail` says which rule it broke, never a value from it."""

    def __init__(self, detail: str):
        super().__init__('invalid_config')
        self.detail = detail


def config_path() -> Path:
    import paths
    return paths.config_file(CONFIG_NAME)


def state_dir() -> Path:
    return broker.default_state_dir()


def empty_config() -> dict:
    return {'schemaVersion': SCHEMA_VERSION, 'targets': {}, 'callers': {}}


def validate_config(doc: Any) -> dict:
    """The closed schema: `{schemaVersion: 1, targets: {id: target}, callers: {principal: grant}}`. The targets are
    the broker's registry and are checked by it; a grant is `{bindingId, revision, revoked, allowedTools,
    allowedTargets, operatorProxy?}` and never carries `owner`, a token or a token hash: the caller's credential is
    the one binding the grant names."""
    if not isinstance(doc, dict) or set(doc) != {'schemaVersion', 'targets', 'callers'}:
        raise ConfigError('the document must be {schemaVersion, targets, callers}')
    if doc['schemaVersion'] != SCHEMA_VERSION:
        raise ConfigError(f'schemaVersion must be {SCHEMA_VERSION}')
    try:
        broker.validate_registry({'schemaVersion': 1, 'targets': doc['targets']})
    except LifecycleError as exc:
        raise ConfigError(f'targets: {exc.code}') from None
    callers = doc['callers']
    if not isinstance(callers, dict):
        raise ConfigError('callers must be an object')
    for principal, grant in callers.items():
        where = f'callers[{principal!r}]' if isinstance(principal, str) and len(principal) <= 80 else 'a caller'
        if not isinstance(principal, str) or not AB.PRINCIPAL.match(principal) or not broker.ID.fullmatch(principal):
            raise ConfigError(f'{where}: a caller is agent:<name> or service:<name>, the principal of its binding')
        if not isinstance(grant, dict) or set(grant) - set(GRANT_FIELDS + GRANT_OPTIONAL) \
                or set(GRANT_FIELDS) - set(grant):
            raise ConfigError(f'{where} must be {{bindingId, revision, revoked, allowedTools, allowedTargets, '
                              f'operatorProxy?}}')
        if not isinstance(grant['bindingId'], str) or not AB.BINDING_ID.match(grant['bindingId']):
            raise ConfigError(f'{where}: bindingId names the one access binding that proves this caller')
        if type(grant['revision']) is not int or grant['revision'] < 1 or type(grant['revoked']) is not bool:
            raise ConfigError(f'{where}: revision is an integer of at least 1 and revoked a boolean')
        if 'operatorProxy' in grant and grant['operatorProxy'] is not True:
            raise ConfigError(f'{where}: operatorProxy is true or absent')
        tools, targets = grant['allowedTools'], grant['allowedTargets']
        if not isinstance(tools, list) or len(set(map(str, tools))) != len(tools) or any(t not in TOOLS for t in tools):
            raise ConfigError(f'{where}: allowedTools lists distinct lifecycle tools')
        if not isinstance(targets, list) or len(set(map(str, targets))) != len(targets) \
                or not all(isinstance(t, str) and broker.ID.fullmatch(t) for t in targets):
            raise ConfigError(f'{where}: allowedTargets lists distinct target ids; the wildcard is nobody\'s')
    return doc


def read_config(path: Optional[Path] = None) -> dict:
    """The document as it is now, or the empty one when it does not exist. Owner-only, no link, schema-checked."""
    doc = broker.private_json(path or config_path(), empty_config())
    return validate_config(copy.deepcopy(doc))


def registry_of(doc: dict) -> dict:
    return {'schemaVersion': 1, 'targets': doc['targets']}


def write_config(doc: dict, path: Optional[Path] = None) -> None:
    """Checked first, then replaced whole, owner-only. The caller holds `config_lock()`."""
    validate_config(doc)
    private_io.write(path or config_path(), json.dumps(doc, indent=2, sort_keys=True) + '\n')


def config_lock(state: Optional[Path] = None):
    """The operator's writers take this in turn: two terminals editing grants must not lose one another's change."""
    return private_io.lock((state or state_dir()) / 'config.lock')


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _bindings() -> AB.Registry:
    import memory_access as MA
    return MA.registry()


def _audience() -> str:
    import memory_access as MA
    return MA.own_audience()


class BindingAuthority(broker.Authority):
    """The broker's authority with the access bindings as its credentials.

    `authenticate` turns a bearer into a principal only through the binding the caller's grant names (`bindingId`),
    of this workspace's audience; another binding of the same principal is `denied`. `grant` is asked again by the
    broker at admission and before every effect, and answers only while BOTH the caller's grant in
    `config/node-lifecycle.json` and that very binding still stand."""

    def __init__(self, config: Callable[[], dict], *, bindings: Callable[[], AB.Registry] = _bindings,
                 audience: Callable[[], str] = _audience, now: Callable[[], str] = _now_iso):
        super().__init__(lambda: {'schemaVersion': 1, 'callers': {}})
        self.config, self.bindings, self.audience, self.now = config, bindings, audience, now

    def _binding(self, digest: str) -> Optional[AB.Binding]:
        try:
            registry = self.bindings()
        except AB.BindingError:
            return None                                    # a broken registry serves nobody, like memory-http
        observed = {'channel': 'http', 'audience': self.audience(), 'bearerDigest': digest}
        return AB.resolve(registry, observed, now=self.now()).binding

    def document(self) -> dict:
        return {'schemaVersion': 1, 'callers': self.config()['callers']}

    def authenticate(self, header: Any) -> dict:
        if not isinstance(header, str) or header[:7].lower() != 'bearer ' or len(header) > 512:
            raise LifecycleError('unauthorized')
        try:
            digest = AB.bearer_digest(header[7:].strip())
        except UnicodeEncodeError:                         # a lone surrogate is no bearer anyone was issued
            raise LifecycleError('unauthorized') from None
        binding = self._binding(digest)
        if binding is None:
            raise LifecycleError('unauthorized')
        grant = self.config()['callers'].get(binding.principal)
        if grant is None or grant['revoked'] is not False or grant['bindingId'] != binding.binding_id:
            raise LifecycleError('denied')                  # a binding the lifecycle grant does not name
        return {'caller': binding.principal, 'grantRevision': grant['revision'], 'tokenHash': digest}

    def grant(self, principal: dict) -> dict:
        grant = self.config()['callers'].get(principal.get('caller'))
        if grant is None or grant['revoked'] is not False or grant['revision'] != principal.get('grantRevision'):
            raise LifecycleError('denied')
        binding = self._binding(str(principal.get('tokenHash', '')))
        if binding is None or binding.principal != principal['caller'] or binding.binding_id != grant['bindingId']:
            raise LifecycleError('denied')                  # revoked, expired or no longer the bound binding
        return dict(grant, owner=False)


class EventLog(fs.JsonlEventLog):
    """The kit's log rewrites the whole file on append: appends from request threads, workers and (from L4) the
    supervisor would lose an event or reuse an id. One lock per file serializes them."""

    _locks: dict = {}
    _guard = threading.Lock()

    def __init__(self, path: Path, keep: int = EVENTS_KEEP):
        super().__init__(path, keep)
        with self._guard:
            self._lock = self._locks.setdefault(str(Path(path).resolve()), threading.Lock())

    def append(self, kind: str, level: str, text: str, **extra: Any) -> dict:
        with self._lock:
            return super().append(kind, level, text, **extra)


EVENT_TEXT = {
    ('ensure', 'always_on', True): ('lifecycle.supervisor_started', '{name} was not running and was started again.'),
    ('ensure', 'always_on', False): ('lifecycle.supervisor_failed', '{name} is not running and could not be started.'),
    ('stop', 'idle', True): ('lifecycle.idle_stopped', '{name} was idle and was stopped.'),
    ('stop', 'idle', False): ('lifecycle.idle_stop_failed', '{name} was idle but could not be stopped.'),
    ('stop', None, True): ('lifecycle.stopped', '{name} was stopped on request.'),
    ('stop', None, False): ('lifecycle.stop_failed', '{name} could not be stopped.'),
    ('restart', None, True): ('lifecycle.restarted', '{name} was restarted on request.'),
    ('restart', None, False): ('lifecycle.restart_failed', '{name} could not be restarted.'),
    ('ensure', None, True): ('lifecycle.completed', '{name} is running.'),
    ('ensure', None, False): ('lifecycle.failed', '{name} could not be started.'),
}
SUCCESS = frozenset(('ready', 'already_ready', 'stopped', 'already_stopped', 'lease_renewed'))
#: Answers that are correct refusals, not faults: nobody needs to be woken for them.
REFUSALS = frozenset(('in_use_by_person', 'held_off', 'denied', 'policy_changed'))


def trace_ids(traceparent: Optional[str]) -> dict:
    parsed = interop.parse_traceparent(traceparent)
    return {'trace_id': parsed[0], 'span_id': parsed[1]} if parsed else {}


def default_adapter():
    """The adapter of this system: macOS's, with its verified-bundle cache beside the store; elsewhere one that
    answers `unsupported_platform` to everything."""
    from node.lifecycle import macos
    return macos.platform_adapter(cache_path=macos.default_cache())


def tool_definitions() -> list:
    text = {'type': 'string', 'minLength': 1, 'maxLength': 128}
    revision = {'type': 'integer', 'minimum': 1}
    admit = {'targetId': text, 'requestId': text, 'expectedPolicyRevision': revision, 'grantRevision': revision,
             'deadlineAt': {'type': 'number'}}
    required = ['targetId', 'requestId', 'expectedPolicyRevision', 'grantRevision']
    properties = {
        'lifecycle.list': ({}, []),
        'lifecycle.status': ({'targetId': text}, ['targetId']),
        'lifecycle.ensure_running': (dict(admit, requiredCapability=text), required),
        'lifecycle.stop': (dict(admit, force={'type': 'boolean'}), required),
        'lifecycle.restart': (dict(admit, requiredCapability=text), required),
        'lifecycle.operation.get': ({'operationId': text}, ['operationId']),
    }
    tools = []
    for name in TOOLS:
        props, req = properties[name]
        effect = EFFECTS.get(name, 'none')
        tools.append({
            'name': name,
            'description': DESCRIPTIONS[name],
            'inputSchema': {'type': 'object', 'additionalProperties': False, 'properties': props, 'required': req},
            'outputSchema': {'type': 'object', 'additionalProperties': True},
            'annotations': interop.annotations({'effect': effect,
                                                'idempotency': 'required' if effect != 'none' else 'none'}),
        })
    return tools


def refusal(code: str) -> dict:
    return {'error': code, 'hint': HINTS.get(code, 'see docs/design/LIFECYCLE.md'), 'degraded': []}


class Service:
    """The broker, its authority and its two logs, for one workspace. `handle` answers one MCP message."""

    def __init__(self, *, config: Optional[Path] = None, state: Optional[Path] = None, adapter=None,
                 bindings: Callable[[], AB.Registry] = _bindings, audience: Callable[[], str] = _audience,
                 clock: Callable[[], float] = time.time, version: Optional[str] = None):
        self.config_path = Path(config) if config else config_path()
        self.state = Path(state) if state else state_dir()
        self.adapter = adapter if adapter is not None else default_adapter()
        self.version = version or _version()
        self.authority = BindingAuthority(self.read, bindings=bindings, audience=audience)
        self.events = EventLog(self.state / EVENTS_FILE)
        self.log = fs.RotatingLog(self.state / 'logs' / LOG_FILE)
        self.broker = broker.Broker(lambda: registry_of(self.read()), self.state, self.authority, self.adapter,
                                    clock=clock, on_finish=self.operation_finished, on_admit=self.operation_admitted,
                                    on_gave_up=self.supervision_gave_up)
        # Through `note`, which never raises: the broker above already holds the state's lock, and a log that
        # cannot be written must not leave a half-built service holding it.
        self.note('service.started', platform=sys.platform)

    def read(self) -> dict:
        return read_config(self.config_path)

    # -- what happened -------------------------------------------------------------------------------------------------

    def target_name(self, target_id: Optional[str]) -> str:
        """An app by its bundle's name (`Alpha` for `/Applications/Alpha.app`), a background job by its target id."""
        try:
            target = self.read()['targets'].get(target_id)
        except LifecycleError:
            target = None
        if target and target['kind'] == 'app':
            return Path(target['path']).stem
        return target_id or 'An enrolled target'

    def note(self, event: str, level: str = 'info', **fields: Any) -> None:
        """One structured line; never a bearer, a request body or raw OS output."""
        try:
            self.log.write(level, event, event=event, **{k: v for k, v in fields.items() if v is not None})
        except OSError:
            pass

    def operation_admitted(self, info: dict) -> None:
        name = self.target_name(info.get('targetId'))
        verb = {'ensure': 'start', 'stop': 'stop', 'restart': 'restart'}.get(info.get('kind'), 'act on')
        by = 'the supervisor' if info.get('caller') == 'supervisor' else 'a caller'
        self.events.append('lifecycle.admitted', 'info', f'Asked to {verb} {name} ({by}).',
                           subject={'kind': 'target', 'id': info['targetId']}, **trace_ids(info.get('traceparent')))
        self.note('operation.admitted', **{('trigger' if k == 'reason' else k): v
                                           for k, v in info.items() if k != 'traceparent'})

    def operation_finished(self, result: dict, traceparent: Optional[str], context: Optional[dict] = None) -> None:
        context = context or {}
        state = result['state']
        ok = state in SUCCESS
        key = (context.get('kind', 'ensure'), context.get('reason'), ok)
        kind, text = EVENT_TEXT.get(key) or EVENT_TEXT[(key[0], None, ok)]
        refused = not ok and state in REFUSALS
        name = self.target_name(context.get('targetId'))
        # The reason is the broker's word ("capability missing"), never raw OS output.
        why = state.replace('_', ' ')
        if result.get('reason'):
            why += ' (' + str(result['reason']).replace('_', ' ') + ')'
        sentence = text.format(name=name)
        if refused:
            sentence = f'A request about {name} was refused: {why}.'
        elif not ok:
            sentence = sentence.rstrip('.') + f': {why}.'
        subject = {'kind': 'target', 'id': context['targetId']} if context.get('targetId') else None
        self.events.append('lifecycle.refused' if refused else kind, 'info' if ok or refused else 'error', sentence,
                           subject=subject, notify=not ok and not refused, **trace_ids(traceparent))
        self.note('operation.finished', 'info' if ok or refused else 'warning', state=state,
                  reason=result.get('reason'), action=result.get('action'),
                  **{('trigger' if k == 'reason' else k): v for k, v in context.items()})

    def supervision_gave_up(self, info: dict) -> None:
        """The always_on restart budget is spent: one notify event, in words a person acts on."""
        target_id = info['targetId']
        try:
            target = self.read()['targets'].get(target_id)
        except LifecycleError:
            target = None
        where = 'open it' if not target or target['kind'] == 'app' else 'read its log'
        self.events.append('lifecycle.supervisor_gave_up', 'error',
                           f'{self.target_name(target_id)} keeps failing to start, so the node stopped retrying; '
                           f'{where} to see why.',
                           subject={'kind': 'target', 'id': target_id}, notify=True)
        self.note('supervision.gave_up', 'warning', targetId=target_id, failedStarts=info.get('failedStarts'),
                  windowSeconds=info.get('windowSeconds'), causes=info.get('causes'))

    # -- the wire ------------------------------------------------------------------------------------------------------

    def authenticate(self, header: Any) -> dict:
        return self.authority.authenticate(header)

    def handle(self, principal: dict, message: Any) -> Optional[dict]:
        """One JSON-RPC message from an authenticated caller; None for a notification."""
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str):
            return _rpc_error(message.get('id') if isinstance(message, dict) else None, -32600, 'Invalid request.')
        if 'id' not in message:
            return None
        rid, method = message['id'], message['method']
        params = message.get('params') or {}
        if not isinstance(params, dict):
            return _rpc_error(rid, -32602, 'params must be an object.')
        span = interop.span_for(params.get('_meta') if isinstance(params.get('_meta'), dict) else None)
        if method == 'initialize':
            asked = params.get('protocolVersion')
            return _rpc(rid, {'protocolVersion': asked if asked in HANDSHAKE_REVISIONS else MCP_REVISION,
                              'capabilities': {'tools': {'listChanged': False}},
                              'serverInfo': {'name': 'project-observatory-node-lifecycle', 'version': self.version},
                              'instructions': INSTRUCTIONS})
        if method == 'ping':
            return _rpc(rid, {})
        if method == 'tools/list':
            allowed = self._allowed(principal)
            return _rpc(rid, {'tools': [t for t in tool_definitions() if t['name'] in allowed],
                              '_meta': span.meta()})
        if method != 'tools/call':
            return _rpc_error(rid, -32601, f'Method {method} is not served here.')
        name, arguments = params.get('name'), params.get('arguments') or {}
        if name not in TOOLS:
            return _rpc(rid, _tool_result({'error': 'unknown-tool', 'detail': 'the lifecycle tools are '
                                           + ', '.join(TOOLS)}, span, is_error=True))
        try:
            structured = self._call(principal, name, arguments, span.traceparent)
        except LifecycleError as exc:
            self.note('tool.refused', tool=name, code=exc.code, caller=principal.get('caller'))
            return _rpc(rid, _tool_result(refusal(exc.code), span, is_error=exc.code == 'invalid_argument'))
        return _rpc(rid, _tool_result(structured, span))

    def _allowed(self, principal: dict) -> set:
        try:
            return set(self.authority.grant(principal)['allowedTools'])
        except LifecycleError:
            return set()

    def _call(self, principal: dict, name: str, args: Any, traceparent: str) -> dict:
        if not isinstance(args, dict):
            raise LifecycleError('invalid_argument')
        self.authority.check(principal, name)
        if name == 'lifecycle.list':
            broker.closed(args, ())
            return self.broker.list(principal)
        if name == 'lifecycle.status':
            broker.closed(args, ('targetId',), ('targetId',))
            return self.broker.status(principal, args['targetId'])
        if name == 'lifecycle.operation.get':
            broker.closed(args, ('operationId',), ('operationId',))
            return self.broker.get(principal, args['operationId'])
        admit = {'lifecycle.ensure_running': self.broker.ensure, 'lifecycle.stop': self.broker.stop,
                 'lifecycle.restart': self.broker.restart}[name]
        return admit(principal, args, traceparent)

    def close(self) -> None:
        self.broker.close()
        close = getattr(self.adapter, 'close', None)
        if close:
            close()


def _rpc(rid: Any, result: dict) -> dict:
    return {'jsonrpc': '2.0', 'id': rid, 'result': result}


def _rpc_error(rid: Any, code: int, message: str) -> dict:
    return {'jsonrpc': '2.0', 'id': rid, 'error': {'code': code, 'message': message}}


def _tool_result(structured: dict, span: interop.Span, *, is_error: bool = False) -> dict:
    return {'content': [{'type': 'text', 'text': json.dumps(structured, ensure_ascii=False)}],
            'structuredContent': structured, 'isError': is_error, '_meta': span.meta()}


def _version() -> str:
    try:
        import configuration
        return configuration.VERSION
    except Exception:  # noqa: BLE001 - a version is a label, never a reason to refuse
        return '0.0.0'


class Host:
    """What the local server holds: the service, built when `config/node-lifecycle.json` exists and not before.

    `start()` at the server's start recovers operations a previous process left running; `service()` builds it the
    first time a caller arrives after the operator enrolled while the server ran. A start that fails (another process
    holds the state, an unreadable document) is a `degraded` row of the server, never a crash of it."""

    def __init__(self, factory: Callable[[], Service] = Service, config: Optional[Path] = None):
        self.factory = factory
        self.config = config
        self._service: Optional[Service] = None
        self._problem: Optional[str] = None
        self._lock = threading.Lock()
        self._closed = False

    def _config(self) -> Path:
        return self.config or config_path()

    def start(self) -> None:
        """Never raises: the local server calls it at its start and on every heartbeat."""
        try:
            exists = self._config().exists()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._problem = f'lifecycle: unavailable, {type(exc).__name__}'
            return
        if exists:
            self.service()

    def service(self) -> Optional[Service]:
        with self._lock:
            if self._service is not None or self._closed:
                return self._service
            try:
                if not self._config().exists():
                    return None
                self._service = self.factory()
                self._problem = None
            except LifecycleError as exc:
                self._problem = f'lifecycle: unavailable, {exc.code}'
            except Exception as exc:  # noqa: BLE001 - a damaged store (sqlite3.DatabaseError) is a degraded row,
                self._problem = f'lifecycle: unavailable, {type(exc).__name__}'  # never the server's crash
            return self._service

    def degraded(self) -> list:
        with self._lock:
            return [{'source': 'node-lifecycle', 'reason': self._problem}] if self._problem else []

    def entry(self) -> dict:
        """The well-known document's `node.lifecycle` entry: this node manages its enrolled targets' lifecycle
        (`lifecycle.manager: node`), at which surface, on which platform, and whether it serves now."""
        with self._lock:
            state = 'serving' if self._service is not None else ('failed' if self._problem else 'inert')
        return {'service': SERVICE, 'lifecycle': {'manager': 'node'}, 'surface': 'mcp', 'path': MCP_PATH,
                'capabilities': list(TOOLS), 'auth': 'access-binding', 'platform': sys.platform,
                'supported': sys.platform == 'darwin', 'state': state}

    def close(self) -> None:
        with self._lock:
            self._closed = True
            service, self._service = self._service, None
        if service is not None:
            service.close()
