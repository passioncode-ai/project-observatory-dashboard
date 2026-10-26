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
  (`collectors/scan_mcp.py`, `store/raw/mcp.json`) into the summary for one organization's context. How much it lists
  follows `machine.mode` (DEC-0118, `policy_scope`): under `C`, the default, only what belongs to that organization's
  context (the coding agents, the hook count, and the MCP servers, skills and plugins installed in one of its own
  `workspace.folders` or named by its baseline); under `A`, set only by an `mdm` layer, the whole harness.

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
from .relay import strict_time

TYPE = 'node-summary/1'
KINDS = ('coding-agent', 'mcp-server', 'skill', 'plugin', 'service')
DRIFT_STATES = ('missing', 'stopped', 'changed', 'outdated')
#: The schema's name pattern (`node-summary.schema.json`): no slash, backslash, tilde or space, at most one `@` suffix
#: without a dot. A name outside it is left out of the summary, never rewritten.
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}(?:@[A-Za-z0-9_:+-]{1,64})?\Z')
#: `common.schema.json#/$defs/slug`: a coding agent's id in a summary and its name in a baseline (DEC-0102).
SLUG = re.compile(r'[a-z][a-z0-9-]{1,62}\Z')
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



# -- what a summary lists: machine.mode (DEC-0118) -----------------------------------------------------------------

#: `machine.mode`'s values (`device-policy.schema.json`): `A`, the organization administers the whole machine (set only
#: from an `mdm` layer, DEC-0053); `C`, the default, it manages only its own work on the machine.
MODES = ('A', 'C')
#: `workspace.folders`' item shape (`device-policy.schema.json`): relative to the person's home, no `..` segment.
FOLDER_PATTERN = re.compile(r'(?![/\\~])(?![A-Za-z]:)(?!(?:[\s\S]*[/\\])?\.\.(?:[/\\]|\Z))[A-Za-z0-9._*/\\-]+\Z')
_SEPARATOR = re.compile(r'[/\\]')


def _segment(pattern: str) -> 're.Pattern':
    """One segment of a `workspace.folders` pattern: `*` any run of characters inside one segment, the rest
    literal."""
    return re.compile(''.join('[^/\\\\]*' if part == '*' else re.escape(part) for part in re.split(r'(\*)', pattern))
                      + r'\Z')


def folder_matches(folder: Any, patterns: Any) -> bool:
    """Whether `folder`, a path relative to the person's home (`projects/acme/app`, or `~/projects/acme/app` as the
    MCP scan spells it), is one of the organization's `workspace.folders` or inside one: each pattern segment matches
    one folder segment, `*` any run of characters in it. A folder outside the home, an absolute one, one with a `..`
    segment, and a pattern outside the schema's shape match nothing."""
    if not isinstance(folder, str):
        return False
    folder = folder[2:] if folder.startswith('~/') else folder
    if not folder or not FOLDER_PATTERN.fullmatch(folder) or '*' in folder:
        return False
    parts = [part for part in _SEPARATOR.split(folder) if part not in ('', '.')]
    for pattern in _list_of_str(patterns):
        if not FOLDER_PATTERN.fullmatch(pattern):
            continue
        wanted = [part for part in _SEPARATOR.split(pattern) if part not in ('', '.')]
        if wanted and len(parts) >= len(wanted) \
                and all(_segment(want).match(have) for want, have in zip(wanted, parts)):
            return True
    return False


def _list_of_str(value: Any) -> List[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, (list, tuple)) else []


def policy_scope(layers: Any) -> dict:
    """What a summary lists under these policy layers (DEC-0118): `{mode, folders}`, `mode` the effective
    `machine.mode` (`A` counts only from an `mdm` layer, `resolve_policy`; anything else is the default `C`) and
    `folders` the organization's own `workspace.folders`, read from the signed layers only: a person's `user` layer is
    not the organization's and never widens what it is shown."""
    mode = (resolve_policy(layers).get('machine.mode') or {}).get('value')
    return {'mode': mode if mode in MODES else 'C',
            'folders': _list_of_str(signed_policy_value(layers, 'workspace.folders'))}


#: What a baseline item may carry, by kind (`device-policy.schema.json`, `harness.baseline`, DEC-0102): a version only
#: for a coding agent or a plugin, a digest only for a skill or a plugin, the kinds whose summary rows carry them.
BASELINE_MIN_VERSION = ('coding-agent', 'plugin')
BASELINE_DIGEST = ('skill', 'plugin')
BASELINE_VERSION = re.compile(r'[0-9]+(?:\.[0-9]+){0,3}\Z')
BASELINE_MAX_ITEMS = 1024


def baseline_problems(value: Any) -> List[str]:
    """What `device-policy.schema.json` refuses in a `harness.baseline` value (`{items: [...]}`), one line each; empty
    when it is one. DEC-0102 narrowed it: an item's name takes a summary's name shape (a coding agent's an agent id, a
    slug; a service's its `id.instance` key), `minVersion` only for a coding agent or a plugin, `digest` only for a
    skill or a plugin. A policy whose baseline has a problem is refused whole, and the node keeps the revision it
    holds: the step that verifies and applies a signed policy (OE-06) runs this before it takes one."""
    if not isinstance(value, dict) or set(value) != {'items'} or not isinstance(value.get('items'), list):
        return ['a baseline is {items: [...]} and nothing else']
    items = value['items']
    problems = ['a baseline lists at most %d items' % BASELINE_MAX_ITEMS] if len(items) > BASELINE_MAX_ITEMS else []
    for index, item in enumerate(items):
        at = '/items/%d' % index
        if not isinstance(item, dict) or not {'kind', 'name'} <= set(item) \
                or not set(item) <= {'kind', 'name', 'minVersion', 'digest'}:
            problems.append(at + ': an item is {kind, name, minVersion?, digest?}')
            continue
        kind, name = item['kind'], item['name']
        if kind not in KINDS:
            problems.append(at + '/kind: one of ' + ', '.join(KINDS))
            continue
        shape = SLUG if kind == 'coding-agent' else SERVICE_KEY if kind == 'service' else NAME
        if not isinstance(name, str) or not NAME.fullmatch(name) or not shape.fullmatch(name):
            problems.append(at + '/name: a %s name takes the summary\'s shape' % kind)
        if 'minVersion' in item and (kind not in BASELINE_MIN_VERSION or not isinstance(item['minVersion'], str)
                                     or not BASELINE_VERSION.fullmatch(item['minVersion'])):
            problems.append(at + '/minVersion: dotted numbers, only for a coding agent or a plugin')
        if 'digest' in item and (kind not in BASELINE_DIGEST or not isinstance(item['digest'], str)
                                 or not DIGEST.fullmatch(item['digest'])):
            problems.append(at + '/digest: sha256:<64 hex>, only for a skill or a plugin')
    return problems

# -- FAC-SEM-128 ---------------------------------------------------------------------------------------------------

EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}')
_SLASH = r'(?:[/\\]|%(?:25)?2[fF])'
#: The contract's home-path shapes (`HOME_PATH`, `src/activity-rules.ts`): a `Users` or `home` segment followed by a
#: name, the dashed form a transcript folder takes after any character that is not an ASCII letter or digit, or at the
#: start (DEC-0105: `x.-Users-<name>`, `p@-Users-<name>`), and the tilde forms.
HOME_PATH = (re.compile(r'(?:^|[^A-Za-z0-9]|%(?:25)?2[fF])(?:[Uu]sers|home)' + _SLASH + r'[A-Za-z0-9_]'),
             re.compile(r'(?:^|[^A-Za-z0-9])-(?:[Uu]sers|home)-[A-Za-z0-9]'),
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



def check_in_summary_findings(check_in: Any) -> List[dict]:
    """`checkInSummaryFindings` (DEC-0114 item 1, kind `device-check-in`): a check-in's `records.nodeSummary` read with
    the check-in, FAC-SEM-128's clauses that need no policy under the member's path, and the summary's `device.id`
    the checking-in device's."""
    doc = check_in if isinstance(check_in, dict) else {}
    records = doc.get('records') if isinstance(doc.get('records'), dict) else {}
    summary = records.get('nodeSummary')
    if not isinstance(summary, dict):
        return []
    findings = document_findings(summary, '/records/nodeSummary')
    own = doc['device'].get('id') if isinstance(doc.get('device'), dict) else None
    named = summary['device'].get('id') if isinstance(summary.get('device'), dict) else None
    if not isinstance(named, str) or named != own:
        findings.append({'code': 'FAC-SEM-128', 'instancePath': '/records/nodeSummary/device/id',
                         'message': "a check-in's node summary is the checking-in device's"})
    return findings


def check_in_attribution_findings(check_in: Any, binding: Any) -> List[dict]:
    """FAC-SEM-045's clauses on a check-in's records (DEC-0114 item 2, kind `device-attribution`), judged against the
    client certificate's binding (`{org: {id}, device: {id}}`) as written: the node summary's `device.id` is the
    certificate's device and its `context` is `org:` and the certificate's `org.id`, and every `credentialRequests`
    and `callRecords` entry names that same context."""
    findings: List[dict] = []
    flag = lambda at, message: findings.append({'code': 'FAC-SEM-045', 'instancePath': at,      # noqa: E731
                                                'message': message})
    binding = binding if isinstance(binding, dict) else {}
    device = binding['device'].get('id', _UNDEFINED) if isinstance(binding.get('device'), dict) else _UNDEFINED
    org = binding['org'].get('id') if isinstance(binding.get('org'), dict) else None
    own_context = 'org:' + org if isinstance(org, str) else None
    doc = check_in if isinstance(check_in, dict) else {}
    records = doc.get('records') if isinstance(doc.get('records'), dict) else {}
    summary = records.get('nodeSummary')
    if isinstance(summary, dict):
        if not isinstance(summary.get('device'), dict) or not _same(summary['device'].get('id', _UNDEFINED), device):
            flag('/checkIn/records/nodeSummary/device/id', "a node summary is the client certificate's device's, "
                                                           'never another\'s')
        if own_context is None or summary.get('context') != own_context:
            flag('/checkIn/records/nodeSummary/context', "a node summary names the client certificate's "
                                                         'organization, org: and its org.id as written')
    for member in ('credentialRequests', 'callRecords'):
        entries = records.get(member) if isinstance(records.get(member), list) else []
        for index, entry in enumerate(entries):
            if isinstance(entry, dict) and (own_context is None or entry.get('context') != own_context):
                flag('/checkIn/records/%s/%d/context' % (member, index), "a check-in's credential requests and call "
                                                                          "records name the client certificate's "
                                                                          'organization')
    return findings


def _same(a: Any, b: Any) -> bool:
    """JavaScript's `===` over parsed JSON scalars: a boolean is never a number, an object never equal, and an absent
    member (`_UNDEFINED`) equal only to another absent one."""
    if a is _UNDEFINED or b is _UNDEFINED:
        return a is b
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b

# -- building the summary from this machine's scans ----------------------------------------------------------------

#: The MCP scan's verdicts as a summary's health: heard answering, heard needing sign-in, heard failing; anything the
#: scan did not hear (`not-probed`, `not-listed`, no verdict) is `unknown`, never `down`.
MCP_HEALTH = {'connected': 'ok', 'needs-auth': 'degraded', 'failed': 'down'}
#: How old a verdict may be and still be a server's health (OBS-221): the same 48 hours a scan is built from. The MCP
#: scan's declarations-only run (the tick's) carries the last on-demand probe's verdicts for days under a fresh
#: `scanned_at`, so each verdict is judged by its own probe time (`liveness_at`); an older one, or one with no time,
#: is `unknown`, never yesterday's `ok` or `down` presented as current.
LIVENESS_MAX_AGE_SECONDS = 48 * 3600
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


#: A dashed home folder the contract's home-path shapes do not catch, which the node also keeps out of a name: since
#: DEC-0105 the contract catches `-Users-`/`-home-` after any character that is not a letter or digit (`notes.-Users-x`,
#: `x@-home-alice`), so the node's own check keeps only the shape the contract leaves to a reader: one glued to a letter
#: or digit and followed by two more dash segments, the way a flattened path continues (`a-Users-alice-projects-x`).
#: `smart-home-mcp`, `end-users-guide`, `my-home-page` and `Users-guide` are names, not folders, and pass both.
DASHED_HOME = re.compile(r'-(?:[Uu]sers|home)-[A-Za-z0-9_.+]+-[A-Za-z0-9_.+]+-[A-Za-z0-9]')


def hides_home(name: str) -> bool:
    """Whether a harness name may hide a home folder: the contract's shapes (`has_home_path`) or the node's own."""
    return has_home_path(name) or DASHED_HOME.search(name) is not None
#: The most drift items a summary carries (the schema's `drift.items` bound).
MAX_DRIFT = 1024


class _Gate:
    """Which names may leave: the schema's shape, no home folder (`hides_home`), and none the person excluded
    (`exclude`, shell-style globs matched against the whole name). A name refused for its shape is counted in
    `omitted`, one the person excluded in `excluded`; both stay on this machine."""

    def __init__(self, exclude: Any = ()):
        self.exclude = [g for g in (exclude or ()) if isinstance(g, str) and g]
        self.omitted: Dict[str, int] = {}
        self.excluded: Dict[str, int] = {}
        self._refused: set = set()

    def admit(self, name: Any, member: str, pattern: 're.Pattern' = NAME) -> bool:
        """Whether `name` may leave; a refused name is counted once per list however often the scans repeat it."""
        if isinstance(name, str) and pattern.fullmatch(name) and not hides_home(name) \
                and not any(fnmatch.fnmatchcase(name, glob) for glob in self.exclude):
            return True
        key = (member, name if isinstance(name, str) else repr(name))
        if key not in self._refused:
            self._refused.add(key)
            shaped = isinstance(name, str) and pattern.fullmatch(name) and not hides_home(name)
            counts = self.excluded if shaped else self.omitted
            counts[member] = counts.get(member, 0) + 1
        return False


class _Scope:
    """What a summary under `machine.mode` `C` lists of the MCP servers, skills and plugins (DEC-0118): an item
    installed in a folder matching the organization's own `workspace.folders` (an MCP server declared for such a
    folder, a project's own skill in one), or named by its `harness.baseline` with the item's kind. A package placed in this context would count
    too, but no package on this machine names a context yet, so none is admitted that way. Everything else (installed
    for the person, machine-wide, for another context) is left out and counted per list in `out_of_context`, which stays
    on this machine with the names it counts."""

    def __init__(self, folders: Any = (), baseline_items: Any = ()):
        self.folders = _list_of_str(folders)
        self.named: Dict[str, set] = {}
        for item in _list(baseline_items):
            if isinstance(item.get('kind'), str) and isinstance(item.get('name'), str):
                self.named.setdefault(item['kind'], set()).add(item['name'])
        self.out_of_context: Dict[str, int] = {}
        self._left: set = set()

    def _leave(self, member: str, name: str) -> None:
        if (member, name) not in self._left:
            self._left.add((member, name))
            self.out_of_context[member] = self.out_of_context.get(member, 0) + 1

    def keeps(self, kind: str, name: str, folders: Any = ()) -> bool:
        """Whether an item of `kind` is listed: the baseline names it (wherever it is installed, a project outside
        the work folders included, DEC-0118), or (a project's skill) one of its `folders`, as the harness scan spells
        them (`~/<path>`), is one of the organization's work folders or inside one."""
        if name in self.named.get(kind, ()) or any(folder_matches(f, self.folders) for f in _list_of_str(folders)):
            return True
        self._leave({'skill': 'skills', 'plugin': 'plugins'}[kind], name)
        return False

    def keeps_server(self, row: dict) -> bool:
        name, where = row.get('name'), row.get('scope')
        if name in self.named.get('mcp-server', ()) or (isinstance(where, str) and where.startswith('project:')
                                                        and folder_matches(where[len('project:'):], self.folders)):
            return True
        self._leave('mcpServers', name)
        return False

    def settle(self, harness: dict) -> None:
        """Forgets a name left out under one row and listed under another: a server declared for a matching folder
        too, a skill a project in a work folder holds under the name the person's own skill or another project's
        has."""
        for member in ('mcpServers', 'skills'):
            listed = {row['name'] for row in harness.get(member, [])}
            left = sum(1 for held, name in self._left if held == member and name not in listed)
            if left:
                self.out_of_context[member] = left
            else:
                self.out_of_context.pop(member, None)


def _fresh_health(row: dict, at_ms: Optional[int]) -> str:
    """A declaration's verdict as a summary's health, `unknown` unless its probe (`liveness_at`) is at most
    `LIVENESS_MAX_AGE_SECONDS` before the summary's own time (`at_ms`; none: the verdict is not judged by age)."""
    health = MCP_HEALTH.get(row.get('liveness'), 'unknown')
    if health == 'unknown' or at_ms is None:
        return health
    probed = strict_time(row.get('liveness_at'))
    if probed is None or at_ms - probed > LIVENESS_MAX_AGE_SECONDS * 1000:
        return 'unknown'
    return health


def _mcp_servers(scan: dict, gate: _Gate, at_ms: Optional[int] = None, scope: Optional['_Scope'] = None) -> List[dict]:
    """One row per server name the coding agents load: a declaration switched off is not loaded and not listed; the
    strongest current verdict of its declarations is its health (`_fresh_health`). Under `machine.mode` `C` only the
    declarations `scope` keeps are read (DEC-0118)."""
    rows: Dict[str, dict] = {}
    for row in _list(scan.get('servers')):
        name = row.get('name')
        if not isinstance(name, str) or row.get('disabled') is True:
            continue
        if scope is not None and not scope.keeps_server(row):
            continue
        if name not in rows and not gate.admit(name, 'mcpServers'):
            continue
        health = _fresh_health(row, at_ms)
        transport = MCP_TRANSPORT.get(row.get('wire') or row.get('transport'), 'other')
        held = rows.get(name)
        if held is None:
            rows[name] = {'name': name, 'source': _mcp_source(row.get('scope')), 'transport': transport,
                          'health': health}
        elif MCP_HEALTH_ORDER.index(health) < MCP_HEALTH_ORDER.index(held['health']):
            held['health'] = health
    return [rows[name] for name in sorted(rows)]


def _skill_rows(scan: dict) -> List[Tuple[str, str, List[str], Any]]:
    """(name, summary source, folders, digest) of every skill the scan read, in the order a name is first listed:
    Claude Code's user-level and plugin skills (`skills`), a project's own (`project_skills`, source `project`, every
    spelling of its folder as `~/<path>`), then Codex's (`codex_skills`, its own `user` folders)."""
    rows = []
    for row in _list(scan.get('skills')):
        name, source = row.get('name'), row.get('source')
        if isinstance(name, str) and isinstance(source, str):
            rows.append((name, 'plugin' if source.startswith('plugin:') else SKILL_SOURCES.get(source, 'other'),
                         [], row.get('digest')))
    for row in _list(scan.get('project_skills')):
        if isinstance(row.get('name'), str):
            rows.append((row['name'], 'project', _list_of_str(row.get('folders')), row.get('digest')))
    for row in _list(scan.get('codex_skills')):
        if isinstance(row.get('name'), str):
            source = row.get('source')
            rows.append((row['name'], source if source in ('user', 'managed') else 'other', [], row.get('digest')))
    return rows


def _skills(scan: dict, gate: _Gate, scope: Optional['_Scope'] = None) -> List[dict]:
    """Every skill the coding agents load (`_skill_rows`), by the name the harness shows (a plugin's skill under its
    own name), once each: the first the scan read wins, Claude Code's user-level before a plugin's, then a project's,
    then Codex's. Under `machine.mode` `C` a project's skill is listed when one of its folders is one of the
    organization's work folders, any skill when the baseline names it (`_Scope.keeps`); the folders never leave.

    The person's own skill wins under either mode, as it does in Claude Code (a personal skill shadows a project's
    of the same name): under `C` a project's skill whose name the person's own skill (source `user`) already holds is
    not listed, so the organization never reads a project's `x` where the agent loads the person's `x`, and the
    person's `x` stays out unless the baseline names it."""
    out: Dict[str, dict] = {}
    shadowed: set = set()
    for name, source, folders, digest in _skill_rows(scan):
        if name in out or (source == 'project' and name in shadowed):
            continue
        if scope is not None and not scope.keeps('skill', name, folders):
            if source == 'user':
                shadowed.add(name)
            continue
        if not gate.admit(name, 'skills'):
            continue
        entry = {'name': name, 'source': source}
        if isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest):
            entry['digest'] = 'sha256:' + digest
        out[name] = entry
    return list(out.values())


def _plugins(scan: dict, gate: _Gate, scope: Optional['_Scope'] = None) -> List[dict]:
    out: Dict[str, dict] = {}
    for row in _list(scan.get('plugins')):
        name = row.get('name')
        if not isinstance(name, str) or name in out or (scope is not None and not scope.keeps('plugin', name)) \
                or not gate.admit(name, 'plugins'):
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


def harness_of(harness_scan: Any, mcp_scan: Any, gate: Optional[_Gate] = None,
               at: Optional[str] = None, scope: Optional[_Scope] = None) -> Tuple[dict, Dict[str, int]]:
    """The `harness` member from the two scans, and what was left out of each list for its shape (counts only).
    `at` is the summary's time: an MCP verdict probed more than 48 hours before it reads `unknown`. With `scope`
    (`machine.mode` `C`, DEC-0118) the MCP servers, skills and plugins are those it keeps; the coding agents and the
    hook count are listed under either mode."""
    gate = gate or _Gate()
    harness_scan = harness_scan if isinstance(harness_scan, dict) else {}
    mcp_scan = mcp_scan if isinstance(mcp_scan, dict) else {}
    omitted = gate.omitted
    harness = {'codingAgents': _bounded(_coding_agents(harness_scan, gate), 'codingAgents', omitted),
               'mcpServers': _bounded(_mcp_servers(mcp_scan, gate, strict_time(at), scope), 'mcpServers', omitted),
               'skills': _bounded(_skills(harness_scan, gate, scope), 'skills', omitted),
               'plugins': _bounded(_plugins(harness_scan, gate, scope), 'plugins', omitted),
               'hooks': {'count': min(len(_list(harness_scan.get('hooks'))), 100000)}}
    if scope is not None:
        scope.settle(harness)
    return harness, omitted


#: Why a plugin's drift reads `changed` while the node sends no plugin digest (OBS-192, OBS-275): the contract defines
#: `plugins[].digest` (`docs/specification/node-summary.md` line 28 at the pin, `node-summary.schema.json`) but not what
#: it covers, so a baseline naming a plugin's digest reads `changed` for every plugin it names ("another, or none").
PLUGIN_DIGEST_CAUSE = 'this node sends no plugin digest; the contract does not say what it covers'


def drift_causes(drift: Any) -> List[dict]:
    """Beside a summary's drift, the drift this node causes itself: every plugin item reading `changed` (only a
    digest makes a plugin `changed`, and the node sends none), with `PLUGIN_DIGEST_CAUSE`. Empty otherwise; shown to
    the person in `org summary`, `org status` and Health, never sent."""
    items = drift.get('items') if isinstance(drift, dict) else None
    return [{'kind': 'plugin', 'name': item.get('name'), 'cause': PLUGIN_DIGEST_CAUSE} for item in _list(items)
            if item.get('kind') == 'plugin' and item.get('state') == 'changed']


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
          services: Any = (), baseline: Optional[dict] = None, exclude: Any = (), mode: str = 'C',
          folders: Any = (), scope: Optional[dict] = None) -> Tuple[dict, Dict[str, int], Dict[str, int]]:
    """The summary this machine sends to organization `org_id`, the counts it left out for their shape (`omitted`),
    and the counts the person's exclusion list kept back (`excluded`, the globs of `exclude`); both stay here.

    `services` are the agent services placed in this organization's context and no other; `baseline` is
    `harness_baseline(layers)` over the policy layers this machine verified and applied, or None, when the summary
    carries no drift. `mode` and `folders` are `policy_scope(layers)`: under `machine.mode` `C`, the default and the
    value of anything that is not `A`, the summary lists only this organization's context (DEC-0118, `_Scope`); under
    `A` the whole harness. A `scope` dict given is filled with `{mode, outOfContext}`, the counts `C` left out, which
    stay here. Raises SummaryRefused rather than hand over a summary FAC-SEM-128 would flag."""
    gate = _Gate(exclude)
    narrowed = None if mode == 'A' else _Scope(folders, (baseline or {}).get('items'))
    harness, omitted = harness_of(harness_scan, mcp_scan, gate, at, narrowed)
    if scope is not None:
        scope.update(mode='A' if narrowed is None else 'C',
                     outOfContext=dict(narrowed.out_of_context) if narrowed is not None else {})
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
