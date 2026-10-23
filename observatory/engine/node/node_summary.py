"""A node's summary of its harness, `node-summary/1`, and its drift from an organization's baseline (contract
DEC-0101, corrected by DEC-0102; engine OBS-192).

An organization sees what each enrolled machine runs (its target S8) and how that differs from what it expects (S10).
This module is the node's half, ported from the contract's `src/node-summary.ts` at the commit the engine pins
(`fabric/node-vectors/README.md`) and held to its vectors by `tests/test_node_summary.py`:

- `harness_drift(baseline_items, summary)` is `harnessDrift`: one state per baseline item, in the baseline's order,
  the first of `missing`, `stopped`, `changed`, `outdated` that holds.
- `version_below`, `resolve_policy`, `signed_policy_value` and `harness_baseline` are their namesakes: the baseline is
  read from the signed layers only (`mdm`, `server`), never from a person's own `user` layer (DEC-0102).
- `summary_findings` is FAC-SEM-128 over `{summary, baseline?, org?}` and `document_findings` its clauses that read a
  summary alone; `build` checks what it built with the latter and refuses to hand over a summary it would flag.
- `build` turns this machine's harness scan (`collectors/scan_harness.py`, `store/raw/harness.json`) and MCP scan
  (`collectors/scan_mcp.py`, `store/raw/mcp.json`) into the summary for one organization's context.

WHAT NEVER LEAVES. The summary names software and services: catalogue ids and versions, MCP server names with their
source, transport and health, skill and plugin names with a version or digest, the number of hooks. It copies an
ALLOW-LIST of fields from the scans, so a field a scan grows later cannot reach an organization by being passed through:
never a path, a command, an argument, an environment value, a URL, a hook's command, a prompt, an output or a file's
content. A name the schema would refuse (a space, a slash, an e-mail-like suffix) is left out and counted in
`omitted`, which stays on this machine; it is never rewritten into a name the harness does not show.

Every function answers and never raises on malformed input: malformed input is an answer, as in the contract.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import re
from typing import Any, Dict, List, Optional, Tuple

from .estate import _show, secret_value

TYPE = 'node-summary/1'
KINDS = ('coding-agent', 'mcp-server', 'skill', 'plugin', 'service')
DRIFT_STATES = ('missing', 'stopped', 'changed', 'outdated')
#: The schema's name pattern (`node-summary.schema.json`): no slash, backslash, tilde or space, at most one `@` suffix
#: without a dot. A name outside it is left out of the summary, never rewritten.
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}(?:@[A-Za-z0-9_:+-]{1,64})?\Z')
SLUG = re.compile(r'[a-z0-9][a-z0-9-]*\Z')
VERSION = re.compile(r'[0-9A-Za-z][0-9A-Za-z.+-]{0,63}\Z')
SERVICE_KEY = re.compile(r'[a-z][a-z0-9-]{1,62}\.[a-z0-9][a-z0-9-]{0,62}\Z')
DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
DOTTED = re.compile(r'[0-9]+(?:\.[0-9]+)*\Z')
#: Policy precedence, highest first, and what a key resolves to when no layer sets it (`src/device-rules.ts`).
POLICY_PRECEDENCE = ('mdm', 'server', 'user')
POLICY_DEFAULTS = {'build_channel.allowed': 'official', 'machine.mode': 'C', 'models.personal_key_sharing': 'forbidden'}
#: Policy keys a `user` layer never sets (DEC-0102).
SIGNED_ONLY_KEYS = ('harness.baseline', 'repair.packages')
#: The schema's list bounds: a machine past one is summarised up to it and the rest counted in `omitted`.
LIMITS = {'codingAgents': 64, 'mcpServers': 256, 'skills': 1024, 'plugins': 256, 'services': 256}
_UNDEFINED = object()


def _list(value: Any) -> List[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _shown(row: dict, member: str) -> str:
    """`show(row[member])` as JavaScript writes it, `undefined` for an absent member."""
    value = row.get(member, _UNDEFINED)
    return 'undefined' if value is _UNDEFINED else _show(value)


def _js_number(text: str) -> float:
    """`Number(text)` for a part of a dotted version: blank is 0, anything not a decimal integer is NaN."""
    text = text.strip()
    if text == '':
        return 0.0
    return float(text) if re.fullmatch(r'[0-9]+', text) else math.nan


def version_below(version: Optional[str], minimum: str) -> bool:
    """Dotted numbers compared number by number; a missing part is 0, and a version that is not dotted numbers is
    outdated (`versionBelow`)."""
    if not isinstance(version, str) or not DOTTED.fullmatch(version):
        return True
    have = [float(part) for part in version.split('.')]
    want = [_js_number(part) for part in str(minimum).split('.')]
    for i in range(max(len(have), len(want))):
        a = have[i] if i < len(have) else 0.0
        b = want[i] if i < len(want) else 0.0
        if a != b:                                   # NaN differs from everything and is below nothing, as in JS
            return a < b
    return False


def _installed(summary: dict, kind: str) -> Dict[str, dict]:
    """What the harness holds of one kind, by name; a later row of the same name wins, as a JavaScript Map's does."""
    harness = summary.get('harness') if isinstance(summary.get('harness'), dict) else {}
    lists = {'coding-agent': (harness.get('codingAgents'), 'id'), 'mcp-server': (harness.get('mcpServers'), 'name'),
             'skill': (harness.get('skills'), 'name'), 'plugin': (harness.get('plugins'), 'name'),
             'service': (summary.get('services'), 'key')}
    rows, member = lists[kind]
    return {_shown(row, member): row for row in _list(rows)}


def harness_drift(baseline_items: Any, summary: Any) -> List[dict]:
    """The drift of a summary's harness from a baseline (the policy key `harness.baseline`'s `value.items`), in the
    baseline's order: `missing` when the item is not there; `stopped` for a service whose state is not `ok` or
    `degraded`, or an MCP server whose health is `down`; `changed` when the baseline names a digest and a skill or a
    plugin carries another, or none; `outdated` when the baseline names a `minVersion` and a coding agent's or a
    plugin's version is below it or not dotted numbers. One state per item, the first that holds (DEC-0101, DEC-0102).
    An item of another kind, or without a string name, is skipped."""
    doc = summary if isinstance(summary, dict) else {}
    drift: List[dict] = []
    for item in _list(baseline_items):
        kind, name = item.get('kind'), _str(item.get('name'))
        if name is None or not isinstance(kind, str) or kind not in KINDS:
            continue
        have = _installed(doc, kind).get(name)
        digest, minimum = item.get('digest'), item.get('minVersion')
        if have is None:
            state = 'missing'
        elif kind == 'service' and _shown(have, 'state') not in ('ok', 'degraded'):
            state = 'stopped'
        elif kind == 'mcp-server' and have.get('health') == 'down':
            state = 'stopped'
        elif isinstance(digest, str) and kind in ('skill', 'plugin') and have.get('digest') != digest:
            state = 'changed'
        elif isinstance(minimum, str) and kind in ('coding-agent', 'plugin') \
                and version_below(_str(have.get('version')), minimum):
            state = 'outdated'
        else:
            continue
        drift.append({'kind': kind, 'name': name, 'state': state})
    return drift


# -- the policy layers the baseline is read from -------------------------------------------------------------------

def resolve_policy(layers: Any) -> Dict[str, dict]:
    """The effective value of every key across policy layers (`resolvePolicy`, DEC-0031): a key locked by any signed
    layer takes the value of the highest layer that locks it; otherwise the user's own value; otherwise the highest
    layer that sets it. `machine.mode` A counts only from an mdm layer (DEC-0053); `harness.baseline` and
    `repair.packages` count only from a signed layer (DEC-0102). A key no layer sets takes its default."""
    given = _list(layers)
    ordered = [layer for layer in (next((x for x in given if x.get('source') == source), None)
                                   for source in POLICY_PRECEDENCE) if layer is not None]

    def keys_of(layer: dict) -> dict:
        everything = layer.get('keys') if isinstance(layer.get('keys'), dict) else {}
        keys = {k: v for k, v in everything.items() if k not in SIGNED_ONLY_KEYS} \
            if layer.get('source') == 'user' else everything
        mode = keys.get('machine.mode')
        if layer.get('source') == 'mdm' or not isinstance(mode, dict) or mode.get('value') != 'A':
            return keys
        return {k: v for k, v in keys.items() if k != 'machine.mode'}

    names: List[str] = []
    for layer in ordered:
        for name in keys_of(layer):
            if name not in names:
                names.append(name)
    effective: Dict[str, dict] = {}
    for name in names:
        setting = lambda layer: keys_of(layer).get(name)                       # noqa: E731
        locker = next((layer for layer in ordered if layer.get('source') != 'user' and isinstance(setting(layer), dict)
                       and setting(layer).get('locked') is True), None)
        user = next((layer for layer in ordered if layer.get('source') == 'user' and isinstance(setting(layer), dict)),
                    None)
        top = next((layer for layer in ordered if isinstance(setting(layer), dict)), None)
        winner = locker or user or top
        if winner is not None:
            entry = {'from': winner.get('source')}
            if 'value' in setting(winner):
                entry['value'] = setting(winner)['value']
            effective[name] = entry
    for name, value in POLICY_DEFAULTS.items():
        effective.setdefault(name, {'value': value, 'from': 'default'})
    return effective


def _signed(layers: Any) -> List[dict]:
    return [layer for layer in _list(layers) if layer.get('source') != 'user']


def signed_policy_value(layers: Any, key: str) -> Any:
    """The value of a policy key resolved over the signed layers only (`signedPolicyValue`, DEC-0102); None when no
    signed layer sets it."""
    return (resolve_policy(_signed(layers)).get(key) or {}).get('value')


def harness_baseline(layers: Any) -> Optional[dict]:
    """The baseline a node compares against (`harnessBaseline`): `harness.baseline` from the signed layers, with the
    revision of the layer that sets it, or None when no signed layer does."""
    signed = _signed(layers)
    setting = resolve_policy(signed).get('harness.baseline')
    if setting is None or setting.get('from') == 'default':
        return None
    layer = next((candidate for candidate in signed if candidate.get('source') == setting.get('from')), {})
    value = setting.get('value')
    return {'revision': layer.get('revision'), 'items': value.get('items') if isinstance(value, dict) else None}


# -- FAC-SEM-128 ---------------------------------------------------------------------------------------------------

EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}')
_SLASH = r'(?:[/\\]|%(?:25)?2[fF])'
#: The contract's home-path shapes (`HOME_PATH`, `src/activity-rules.ts`): a `Users` or `home` segment followed by a
#: name, the dashed form a transcript folder takes, and the tilde forms.
HOME_PATH = (re.compile(r'(?:^|[^A-Za-z0-9]|%(?:25)?2[fF])(?:[Uu]sers|home)' + _SLASH + r'[A-Za-z0-9_]'),
             re.compile(r'(?:^|[/\\:-])-(?:[Uu]sers|home)-[A-Za-z0-9]'),
             re.compile(r'''(?:^|[\s"'=/\\])~(?:[A-Za-z_][A-Za-z0-9_.-]*)?(?:[/\\]|\Z)'''))


def has_home_path(value: str) -> bool:
    return any(pattern.search(value) for pattern in HOME_PATH)


def _strings(value: Any, at: str) -> List[Tuple[str, str]]:
    """Every string inside `value` with its JSON pointer, in document order, walked with an explicit stack so depth
    never raises (DEC-0102)."""
    out: List[Tuple[str, str]] = []
    stack: List[Tuple[Any, str]] = [(value, at)]
    while stack:
        node, where = stack.pop()
        if isinstance(node, str):
            out.append((where, node))
        elif isinstance(node, list):
            for i in range(len(node) - 1, -1, -1):
                stack.append((node[i], '%s/%d' % (where, i)))
        elif isinstance(node, dict):
            for key in reversed(list(node)):
                stack.append((node[key], '%s/%s' % (where, str(key).replace('~', '~0').replace('/', '~1'))))
    return out


def document_findings(summary: Any, prefix: str = '') -> List[dict]:
    """FAC-SEM-128's clauses that read a summary alone (`node-summary-document`): no string carries a credential, an
    e-mail address or a home path, and no list names an item twice."""
    doc = summary if isinstance(summary, dict) else {}
    findings: List[dict] = []
    flag = lambda at, message: findings.append({'code': 'FAC-SEM-128', 'instancePath': at,      # noqa: E731
                                                'message': message})
    for at, text in _strings(doc, prefix):
        if secret_value(text):
            flag(at, 'a node summary carries no credential')
        elif EMAIL.search(text):
            flag(at, 'a node summary names no person: no e-mail address')
        elif has_home_path(text):
            flag(at, 'a node summary carries no path')
    harness = doc.get('harness') if isinstance(doc.get('harness'), dict) else {}

    def unique(rows: List[dict], member: str, at: str) -> None:
        seen = set()
        for i, row in enumerate(rows):
            name = _shown(row, member)
            if name in seen:
                flag('%s/%d/%s' % (at, i, member), '%s is listed twice' % name)
            seen.add(name)
    unique(_list(harness.get('codingAgents')), 'id', prefix + '/harness/codingAgents')
    unique(_list(harness.get('mcpServers')), 'name', prefix + '/harness/mcpServers')
    unique(_list(harness.get('skills')), 'name', prefix + '/harness/skills')
    unique(_list(harness.get('plugins')), 'name', prefix + '/harness/plugins')
    unique(_list(doc.get('services')), 'key', prefix + '/services')
    return findings


def summary_findings(value: Any) -> List[dict]:
    """FAC-SEM-128 on `{summary, baseline?, org?}`: the document's clauses, `context` exactly `org:` and `org`, and,
    with a baseline (`{revision, items}`), `drift` under its revision equal to `harness_drift`; without one, no drift."""
    value = value if isinstance(value, dict) else {}
    summary = value.get('summary') if isinstance(value.get('summary'), dict) else {}
    findings = document_findings(summary, '/summary')
    flag = lambda at, message: findings.append({'code': 'FAC-SEM-128', 'instancePath': at,      # noqa: E731
                                                'message': message})
    baseline = value.get('baseline') if isinstance(value.get('baseline'), dict) else None
    if isinstance(value.get('org'), str) and summary.get('context') != 'org:' + value['org']:
        flag('/summary/context', "a node summary names its enrollment's own organization, org: and the certificate's "
                                 'org.id as written')
    drift = summary.get('drift') if isinstance(summary.get('drift'), dict) else None
    if baseline is None:
        if drift is not None:
            flag('/summary/drift', 'a summary reports drift only against a baseline its policy carries')
    elif drift is None:
        flag('/summary/drift', 'the policy carries a baseline, so the summary reports its drift (an empty list when '
                               'none)')
    else:
        if drift.get('policyRevision') != baseline.get('revision'):
            flag('/summary/drift/policyRevision', 'the drift names policy revision %s, the baseline is revision %s'
                 % (_shown(drift, 'policyRevision'), _shown(baseline, 'revision')))
        want = harness_drift(baseline.get('items'), summary)
        have = [{'kind': row.get('kind'), 'name': row.get('name'), 'state': row.get('state')}
                for row in _list(drift.get('items'))]
        if have != want:
            flag('/summary/drift/items', 'the drift is %s, as harnessDrift computes it' % json.dumps(want))
    return findings


# -- building the summary from this machine's scans ----------------------------------------------------------------

#: The MCP scan's verdicts as a summary's health: heard answering, heard needing sign-in, heard failing; anything the
#: scan did not hear (`not-probed`, `not-listed`, no verdict) is `unknown`, never `down`.
MCP_HEALTH = {'connected': 'ok', 'needs-auth': 'degraded', 'failed': 'down'}
MCP_HEALTH_ORDER = ('ok', 'degraded', 'down', 'unknown')
MCP_TRANSPORT = {'stdio': 'stdio', 'streamable-http': 'http', 'http': 'http', 'sse': 'sse'}
SKILL_SOURCES = {'plain': 'user'}
PLUGIN_SOURCES = ('marketplace', 'local', 'managed', 'other')


def _mcp_source(scope: Any) -> str:
    """The MCP scan's scope as the summary's source: a user-level declaration is `user`, one Claude Code keeps per
    folder in its own config is `local` (its own word for that scope), a plugin's is `plugin`, anything else (a
    connector of the account) is `other`. The folder a `local` one belongs to never leaves."""
    scope = scope if isinstance(scope, str) else ''
    if scope == 'user':
        return 'user'
    if scope.startswith('project:'):
        return 'local'
    if scope.startswith('plugin:'):
        return 'plugin'
    return 'other'


#: A name a dashed home folder could hide in: the contract's home-path shapes miss one after a dot or an `@`
#: (`notes.-Users-alice-x`, `x@-home-alice`), so the node refuses it itself, beside FAC-SEM-128, and leaves the name
#: out. Two shapes: `-Users-`/`-home-` right after a separator (start, `.`, `@`, `_`, `+`, `:`), or one followed by two
#: more dash segments, the way a flattened path continues (`a-Users-alice-projects-x`). `smart-home-mcp` and
#: `end-users-guide` are names, not folders, and pass.
DASHED_HOME = re.compile(r'(?:^|[.@_+:])-(?:[Uu]sers|home)-[A-Za-z0-9]'
                         r'|-(?:[Uu]sers|home)-[A-Za-z0-9_.+]+-[A-Za-z0-9_.+]+-[A-Za-z0-9]')
#: The most drift items a summary carries (the schema's `drift.items` bound).
MAX_DRIFT = 1024


class _Gate:
    """Which names may leave: the schema's shape, no dashed home folder, and none the person excluded
    (`exclude`, shell-style globs matched against the whole name). A name refused for its shape is counted in
    `omitted`, one the person excluded in `excluded`; both stay on this machine."""

    def __init__(self, exclude: Any = ()):
        self.exclude = [g for g in (exclude or ()) if isinstance(g, str) and g]
        self.omitted: Dict[str, int] = {}
        self.excluded: Dict[str, int] = {}
        self._refused: set = set()

    def admit(self, name: Any, member: str, pattern: 're.Pattern' = NAME) -> bool:
        """Whether `name` may leave; a refused name is counted once per list however often the scans repeat it."""
        if isinstance(name, str) and pattern.fullmatch(name) and not DASHED_HOME.search(name) \
                and not any(fnmatch.fnmatchcase(name, glob) for glob in self.exclude):
            return True
        key = (member, name if isinstance(name, str) else repr(name))
        if key not in self._refused:
            self._refused.add(key)
            shaped = isinstance(name, str) and pattern.fullmatch(name) and not DASHED_HOME.search(name)
            counts = self.excluded if shaped else self.omitted
            counts[member] = counts.get(member, 0) + 1
        return False


def _mcp_servers(scan: dict, gate: _Gate) -> List[dict]:
    """One row per server name the coding agents load: a declaration switched off is not loaded and not listed; the
    strongest verdict of its declarations is its health."""
    rows: Dict[str, dict] = {}
    for row in _list(scan.get('servers')):
        name = row.get('name')
        if not isinstance(name, str) or row.get('disabled') is True:
            continue
        if name not in rows and not gate.admit(name, 'mcpServers'):
            continue
        health = MCP_HEALTH.get(row.get('liveness'), 'unknown')
        transport = MCP_TRANSPORT.get(row.get('wire') or row.get('transport'), 'other')
        held = rows.get(name)
        if held is None:
            rows[name] = {'name': name, 'source': _mcp_source(row.get('scope')), 'transport': transport,
                          'health': health}
        elif MCP_HEALTH_ORDER.index(health) < MCP_HEALTH_ORDER.index(held['health']):
            held['health'] = health
    return [rows[name] for name in sorted(rows)]


def _skills(scan: dict, gate: _Gate) -> List[dict]:
    """Claude Code's user-level and plugin skills, by the name the harness shows (a plugin's skill under its own
    name), once each: the first the scan read wins, the user's own before a plugin's."""
    out: Dict[str, dict] = {}
    for row in _list(scan.get('skills')):
        name, source = row.get('name'), row.get('source')
        if not isinstance(name, str) or not isinstance(source, str):
            continue
        if name in out or not gate.admit(name, 'skills'):
            continue
        entry = {'name': name, 'source': 'plugin' if source.startswith('plugin:') else SKILL_SOURCES.get(source, 'other')}
        digest = row.get('digest')
        if isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest):
            entry['digest'] = 'sha256:' + digest
        out[name] = entry
    return list(out.values())


def _plugins(scan: dict, gate: _Gate) -> List[dict]:
    out: Dict[str, dict] = {}
    for row in _list(scan.get('plugins')):
        name = row.get('name')
        if not isinstance(name, str) or name in out or not gate.admit(name, 'plugins'):
            continue
        entry = {'name': name, 'source': row.get('source') if row.get('source') in PLUGIN_SOURCES else 'other'}
        if isinstance(row.get('version'), str) and VERSION.fullmatch(row['version']):
            entry['version'] = row['version']
        out.setdefault(name, entry)
    return list(out.values())


def _coding_agents(scan: dict, gate: _Gate) -> List[dict]:
    out: Dict[str, dict] = {}
    for row in _list(scan.get('coding_agents')):
        agent = row.get('id')
        if agent in out or not gate.admit(agent, 'codingAgents', SLUG):
            continue
        entry = {'id': agent}
        if isinstance(row.get('version'), str) and VERSION.fullmatch(row['version']):
            entry['version'] = row['version']
        out.setdefault(agent, entry)
    return list(out.values())


def _services(services: Any, gate: _Gate) -> List[dict]:
    out: Dict[str, dict] = {}
    for row in _list(services):
        key = row.get('key')
        if key in out or not gate.admit(key, 'services', SERVICE_KEY):
            continue
        entry = {'key': key, 'state': row.get('state') if row.get('state') in ('ok', 'degraded', 'down', 'stopped')
                 else 'unknown'}
        protocols = row.get('protocols')
        if isinstance(protocols, list) and protocols and all(isinstance(p, str) for p in protocols):
            entry['protocols'] = list(dict.fromkeys(protocols))
        if isinstance(row.get('a2aCardDigest'), str) and DIGEST.fullmatch(row['a2aCardDigest']):
            entry['a2aCardDigest'] = row['a2aCardDigest']
        endpoint = row.get('mcpEndpoint')
        if isinstance(endpoint, str) and len(endpoint) <= 2048 \
                and re.fullmatch(r'https://[^\s?#@/]+(?:/[^\s?#@]*)?', endpoint):
            entry['mcpEndpoint'] = endpoint
        out.setdefault(key, entry)
    return list(out.values())


def _bounded(rows: List[dict], member: str, omitted: Dict[str, int]) -> List[dict]:
    limit = LIMITS[member]
    if len(rows) > limit:
        omitted[member] = omitted.get(member, 0) + len(rows) - limit
    return rows[:limit]


def harness_of(harness_scan: Any, mcp_scan: Any, gate: Optional[_Gate] = None) -> Tuple[dict, Dict[str, int]]:
    """The `harness` member from the two scans, and what was left out of each list for its shape (counts only)."""
    gate = gate or _Gate()
    harness_scan = harness_scan if isinstance(harness_scan, dict) else {}
    mcp_scan = mcp_scan if isinstance(mcp_scan, dict) else {}
    omitted = gate.omitted
    harness = {'codingAgents': _bounded(_coding_agents(harness_scan, gate), 'codingAgents', omitted),
               'mcpServers': _bounded(_mcp_servers(mcp_scan, gate), 'mcpServers', omitted),
               'skills': _bounded(_skills(harness_scan, gate), 'skills', omitted),
               'plugins': _bounded(_plugins(harness_scan, gate), 'plugins', omitted),
               'hooks': {'count': min(len(_list(harness_scan.get('hooks'))), 100000)}}
    return harness, omitted


def content_digest(summary: dict) -> str:
    """What a summary says, without its sequence and time: two summaries with one digest say the same thing, so the
    node sends a summary when this changes, and otherwise at least once a day."""
    said = {k: v for k, v in summary.items() if k not in ('sequence', 'at')}
    return 'sha256:' + hashlib.sha256(json.dumps(said, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class SummaryRefused(ValueError):
    """`build` produced a summary FAC-SEM-128 would flag: it is never handed over (the findings name where)."""

    def __init__(self, findings: List[dict]):
        super().__init__('the summary would carry what node-summary/1 forbids: %s'
                         % ', '.join(sorted({f['instancePath'] for f in findings})))
        self.findings = findings


def build(*, device_id: str, org_id: str, sequence: int, at: str, harness_scan: Any, mcp_scan: Any,
          services: Any = (), baseline: Optional[dict] = None,
          exclude: Any = ()) -> Tuple[dict, Dict[str, int], Dict[str, int]]:
    """The summary this machine sends to organization `org_id`, the counts it left out for their shape (`omitted`),
    and the counts the person's exclusion list kept back (`excluded`, the globs of `exclude`); both stay here.

    `services` are the agent services placed in this organization's context and no other; `baseline` is
    `harness_baseline(layers)` over the policy layers this machine verified and applied, or None, when the summary
    carries no drift. Raises SummaryRefused rather than hand over a summary FAC-SEM-128 would flag."""
    gate = _Gate(exclude)
    harness, omitted = harness_of(harness_scan, mcp_scan, gate)
    summary = {'type': TYPE, 'device': {'id': device_id}, 'context': 'org:' + org_id, 'sequence': int(sequence),
               'at': at, 'harness': harness, 'services': _bounded(_services(services, gate), 'services', omitted)}
    if baseline is not None:
        drift = harness_drift(baseline.get('items'), summary)
        if len(drift) > MAX_DRIFT:
            omitted['drift'] = len(drift) - MAX_DRIFT
        summary['drift'] = {'policyRevision': baseline.get('revision'), 'items': drift[:MAX_DRIFT]}
    findings = summary_findings({'summary': summary, 'org': org_id,
                                 **({'baseline': baseline} if baseline is not None else {})})
    if 'drift' in omitted:
        # Past the schema's 1024 drift items the summary carries the first 1024, so it cannot equal harnessDrift's
        # whole list; the contract has no rule for that case yet, and the count left out stays here.
        findings = [f for f in findings if f['instancePath'] != '/summary/drift/items']
    if findings:
        raise SummaryRefused(findings)
    return summary, omitted, gate.excluded


def counts(summary: dict) -> dict:
    """How much a summary lists, for the person's view: never the names."""
    harness = summary.get('harness') if isinstance(summary.get('harness'), dict) else {}
    return {'codingAgents': len(_list(harness.get('codingAgents'))), 'mcpServers': len(_list(harness.get('mcpServers'))),
            'skills': len(_list(harness.get('skills'))), 'plugins': len(_list(harness.get('plugins'))),
            'hooks': (harness.get('hooks') or {}).get('count', 0) if isinstance(harness.get('hooks'), dict) else 0,
            'services': len(_list(summary.get('services')))}
