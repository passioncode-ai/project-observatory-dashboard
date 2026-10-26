"""Importing an existing lifecycle broker's enrollments and grants (OBS-79 step L5, docs/design/LIFECYCLE.md).

The node's mechanics came from an operator's private broker; that broker's runtime state (its enrolled targets, its
callers' grants, its SQLite store) never moved with the code. `full lifecycle import --from PATH` is the one way it
moves: the operator names the broker's state folder on the same machine, and the import reads it there, locally,
and writes what the engine accepts into `config/node-lifecycle.json`. Nothing of it is copied anywhere else.

What the source folder holds (the format this reads, and nothing more):

- `registry.json`: `{schemaVersion: 1, targets: {id: target}}`, the same closed target record the engine's registry
  validates (`broker.validate_registry`), except that an app enrolled before the requirement scheme may still carry
  a legacy identity (`bundleId`, `codeHash`, `bundleMetadataHash`, no `scheme`). Its `allowedCallers` name the
  broker's callers by their bare names, and always include its `owner`.
- `callers.json`: `{schemaVersion: 1, callers: {name: grant}}`, a grant being `{tokenHash, revision, revoked,
  owner?, host?, operatorProxy?, allowedTools, allowedTargets}`; `tokenHash` is the SHA-256 hex of the bearer.
- `lifecycle.sqlite3`: its operations and the processes it started. Read only to count what is still in flight;
  no row of it is imported (the engine's store is its own, and an operation id of another broker means nothing here).

The rules, and why:

- **Read-only on the source.** The folder must be a real folder of this account that nobody else may change, named
  absolutely, reached through no link; each document is an owner-only regular file opened without following a link
  (`broker.private_json`); the store and its write-ahead log are read as bytes into a private temporary copy, which
  is queried, so nothing is created or written in the folder, not even SQLite's shared-memory index.
- **Everything goes through the engine's own validation.** Each target is checked by the registry's schema on its
  own, each grant by the config's; whatever is refused is one item with a code, and the rest is written together in
  one replacement of the owner-only document, validated whole first. Nothing is written half.
- **Idempotent.** A target or grant already in the engine with the same content is `unchanged`; one there with other
  content is refused `already_enrolled` or `already_granted` (the engine's own record wins and is never overwritten).
  A second run over the same source changes nothing and says `changed: false`.
- **No credential is copied.** The engine's grants hold no token hash: each names one access binding. The broker's
  token hash is the same scheme as a binding's credential (`bearer-sha256`), so a grant is imported when a binding in
  force for this workspace, of the caller's principal, already carries that very digest; otherwise the caller is
  `reissue_required`, with the commands that issue a binding and allow it. The hash is compared, never written.
- **always_on comes over as on_demand.** The engine's supervisor acts on the document as soon as it is written,
  while the other broker runs until the operator switches it off last; two brokers each taking the other's process
  for the person's would restart a crashed job twice, with two restart budgets. `--keep-always-on` keeps the state,
  with that warning; the report names the `policy` command that turns each target back.
- **The owner and the host are not callers here.** The engine's owner is the operator at a terminal, and Fabric
  reaches the local server through its own descriptor, so those two grants are skipped (reported, never silent).
- **The identity migration.** An app still enrolled under the legacy identity is verified now, on this machine: an
  unchanged bundle (its metadata hash equals the enrolled one) is adopted under its own signing team; a changed one
  only under a team the operator names (`--trusted-team`, none by default); a changed bundle id never. A migrated
  target's policy revision goes up by one; a skipped one is refused `reenroll_required` with the reason.
- **Counts and ids only.** The report names target ids, callers, counts and codes: never an app's path, a signing
  team, a hash, a token or anything of the source beyond the folder the operator named.
"""
from __future__ import annotations

import copy
import os
import sqlite3
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import access_binding as AB
import osprivacy
import paths

from node.lifecycle import broker
from node.lifecycle import service as S
from node.lifecycle.broker import TOOLS, LifecycleError

SOURCE_REGISTRY = 'registry.json'
SOURCE_CALLERS = 'callers.json'
SOURCE_STORE = 'lifecycle.sqlite3'
#: The source's own owner and the Fabric host's credential: grants the engine has no caller for.
OWNER_NAMES = frozenset(('owner',))
HOST_NAMES = frozenset(('fabric-host',))
#: The subject of the import's journal line.
SERVICE_SUBJECT = 'node.lifecycle'
#: The largest store (and write-ahead log) the import copies to count what is in flight.
MAX_STORE_BYTES = 256 * 1048576
#: The one store version this reader knows (`meta.version`); another is never guessed at.
SOURCE_STORE_VERSION = 1


class SourceError(LifecycleError):
    """The source folder as a whole is refused; `detail` says which rule, never a value from it."""

    def __init__(self, code: str, detail: str):
        super().__init__(code)
        self.detail = detail


# -- reading the source ----------------------------------------------------------------------------------------------

def check_folder(path: Any) -> Path:
    """The folder the operator named: absolute, a real folder of this account, nobody else may change it, and no link
    on the way to it (the path is its own resolution)."""
    folder = Path(path)
    if not folder.is_absolute():
        raise SourceError('invalid_argument', '--from is the absolute state folder of the broker to import')
    try:
        info = os.lstat(folder)
    except FileNotFoundError:
        raise SourceError('not_found', 'the folder does not exist') from None
    except OSError:
        raise SourceError('unsafe_source', 'the folder cannot be examined') from None
    if stat.S_ISLNK(info.st_mode):
        raise SourceError('unsafe_source', 'the folder is a link; name the folder itself')
    if not stat.S_ISDIR(info.st_mode):
        raise SourceError('invalid_argument', 'the path is not a folder')
    if folder.resolve() != folder.absolute():
        raise SourceError('unsafe_source', 'a link sits among the folder\'s parents; name its real place')
    if not osprivacy.owned_by_me(folder):
        raise SourceError('unsafe_source', 'the folder belongs to another account')
    if osprivacy.others_may_write(folder):
        raise SourceError('unsafe_source', 'others may write the folder')
    return folder


def _document(folder: Path, name: str, default: Optional[dict]) -> dict:
    try:
        return broker.private_json(folder / name, default)
    except LifecycleError as exc:
        if exc.code == 'setup_required':
            raise SourceError('not_a_broker_state', f'{name} is missing: this is not a lifecycle broker\'s state '
                                                    'folder') from None
        if exc.code == 'unsafe_config':
            raise SourceError('unsafe_source', f'{name} must be an owner-only regular file reached through no '
                                               'link') from None
        raise SourceError('invalid_source', f'{name} is not a JSON document') from None


def _stamp(path: Path) -> Optional[tuple]:
    """What changes when a file is written: its inode, size and modification and change times; None when absent."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    return info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _copy_store(folder: Path, tmp: Path) -> Optional[str]:
    """Copy the store and its write-ahead log into `tmp`; None, or why it was not copied. Both files are stamped
    before and after: a checkpoint or a write between the two reads would pair a database with a log of another
    moment, so a copy whose stamps moved is taken again once, and a second one that moved is not trusted."""
    names = (SOURCE_STORE, SOURCE_STORE + '-wal')
    for _ in range(2):
        before = [_stamp(folder / n) for n in names]
        for n in names:
            (tmp / n).unlink(missing_ok=True)
        for n, stamp in zip(names, before):
            if stamp is None:
                continue
            original = folder / n
            if not osprivacy.owned_by_me(original) or not osprivacy.private(original):
                return ('its write-ahead log is readable or writable by others, so its counts are not read'
                        if n.endswith('-wal') else 'the store is not an owner-only regular file; not read')
            data = osprivacy.read_regular(original, MAX_STORE_BYTES)
            fd = osprivacy.open(tmp / n, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
        if [_stamp(folder / n) for n in names] == before:
            return None
    return 'the store changed while it was copied, twice; its counts are not read'


def read_store(folder: Path) -> tuple[Optional[dict], list]:
    """What the source broker still has in flight: operations running and processes it started that the person has
    not taken over. (None, degraded) when there is no store or it cannot be read.

    Nothing in the source folder is opened for writing, not even SQLite's shared-memory index: even a `mode=ro`
    connection to a database in WAL mode creates `-shm` beside it. So the store and its write-ahead log are read as
    bytes (owner-only regular files, no link followed) into a private temporary folder, the copy is queried, and the
    copy is removed. A live broker's last writes are in the log, so they are counted too. The two files are read one
    after the other, so a checkpoint between the reads could pair them inconsistently: `_copy_store` stamps both and
    copies again once when they moved. A store whose `meta` version is not the one this reader knows is not counted:
    its tables may mean something else."""
    path = folder / SOURCE_STORE
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, []
    except OSError:
        return None, [{'source': 'store', 'reason': 'the store cannot be examined'}]
    if not stat.S_ISREG(info.st_mode) or not osprivacy.owned_by_me(path) or not osprivacy.private(path):
        return None, [{'source': 'store', 'reason': 'the store is not an owner-only regular file; not read'}]
    with tempfile.TemporaryDirectory(prefix='lifecycle-import-') as tmp:
        copy_path = Path(tmp) / SOURCE_STORE
        try:
            problem = _copy_store(folder, Path(tmp))
        except (OSError, ValueError):
            problem = ('the store cannot be read as an owner-only regular file of at most '
                       f'{MAX_STORE_BYTES // 1048576} MB')
        if problem:
            return None, [{'source': 'store', 'reason': problem}]
        try:
            # Read-only even on the copy: the count is a reading, never a repair of what it found.
            db = sqlite3.connect(paths.sqlite_uri(copy_path), uri=True)
        except sqlite3.Error:
            return None, [{'source': 'store', 'reason': 'the store cannot be opened'}]
        try:
            db.execute('PRAGMA query_only=1')
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            version = None
            if 'meta' in tables:
                rows = db.execute('SELECT version FROM meta').fetchall()
                version = rows[0][0] if len(rows) == 1 else None
            if version != SOURCE_STORE_VERSION:
                shown = version if isinstance(version, int) else 'unknown'
                return None, [{'source': 'store', 'reason': f'the store\'s version is {shown}, not '
                                                            f'{SOURCE_STORE_VERSION}; its counts are not read'}]
            running = started = 0
            if 'operations' in tables:
                running = db.execute("SELECT count(*) FROM operations WHERE state='running'").fetchone()[0]
            if 'launches' in tables:
                columns = {r[1] for r in db.execute('PRAGMA table_info(launches)')}
                query = 'SELECT count(*) FROM launches' + (' WHERE adopted IS NULL' if 'adopted' in columns else '')
                started = db.execute(query).fetchone()[0]
            return {'runningOperations': running, 'startedProcesses': started}, []
        except sqlite3.Error as exc:
            return None, [{'source': 'store', 'reason': f'the store cannot be read ({type(exc).__name__})'}]
        finally:
            db.close()


def read_source(path: Any) -> dict:
    """The source folder's two documents and its store's counts; the top-level shapes are checked here, each item
    later, so one bad record refuses that record and not the import."""
    folder = check_folder(path)
    registry = _document(folder, SOURCE_REGISTRY, None)
    callers = _document(folder, SOURCE_CALLERS, {'schemaVersion': 1, 'callers': {}})
    if not isinstance(registry, dict) or registry.get('schemaVersion') != 1 or not isinstance(registry.get('targets'),
                                                                                                dict):
        raise SourceError('invalid_source', f'{SOURCE_REGISTRY} must be {{schemaVersion: 1, targets: {{...}}}}')
    if len(registry['targets']) > broker.MAX_TARGETS:
        raise SourceError('invalid_source', f'{SOURCE_REGISTRY} holds more than {broker.MAX_TARGETS} targets')
    if not isinstance(callers, dict) or callers.get('schemaVersion') != 1 or not isinstance(callers.get('callers'),
                                                                                            dict):
        raise SourceError('invalid_source', f'{SOURCE_CALLERS} must be {{schemaVersion: 1, callers: {{...}}}}')
    store, degraded = read_store(folder)
    return {'folder': folder, 'targets': registry['targets'], 'callers': callers['callers'], 'store': store,
            'degraded': degraded}


# -- the identity migration ------------------------------------------------------------------------------------------

def legacy_identity(target: Any) -> bool:
    """An app enrolled before the requirement scheme: no `scheme`, a code hash."""
    identity = target.get('identity') if isinstance(target, dict) else None
    return (isinstance(identity, dict) and target.get('kind') == 'app' and identity.get('scheme') is None
            and 'codeHash' in identity)


def migrate_identity(target: dict, derive: Callable[[str], tuple], trusted_teams: frozenset) -> tuple[dict, str]:
    """(the requirement-scheme identity, the basis) for a legacy app record, or `LifecycleError('reenroll_required')`
    with `reason`. `derive(path)` verifies the installed bundle now and answers (identity, metadata hash)."""
    legacy = target['identity']
    try:
        identity, metadata_hash = derive(target.get('path'))
    except LifecycleError as exc:
        raise _reenroll(exc.code) from None
    except (OSError, ValueError, KeyError, TypeError):
        raise _reenroll('unreadable_bundle') from None
    if not isinstance(identity, dict):
        raise _reenroll('unreadable_bundle')
    exact = bool(legacy.get('bundleMetadataHash')) and legacy['bundleMetadataHash'] == metadata_hash
    if identity.get('bundleId') != legacy.get('bundleId'):
        raise _reenroll('bundle_id_changed')
    if not exact and identity.get('teamId') not in trusted_teams:
        raise _reenroll('untrusted_team')
    return identity, 'exact_enrolled_bundle' if exact else 'trusted_team'


def _reenroll(reason: str) -> LifecycleError:
    exc = LifecycleError('reenroll_required')
    exc.reason = reason
    return exc


def default_derive(adapter) -> Callable[[str], tuple]:
    """Verify an installed bundle the way enrollment does (`macos.inspect_app`): signature, requirement, metadata."""
    def derive(path):
        if getattr(adapter, 'platform', None) != 'darwin' or not hasattr(adapter, 'run'):
            raise LifecycleError('unsupported_platform')
        from node.lifecycle import macos
        return macos.inspect_app(path, run=adapter.run, roots=adapter.roots)
    return derive


# -- the plan --------------------------------------------------------------------------------------------------------

def default_principal(name: str) -> Optional[str]:
    """The engine's principal for a source caller name: itself when it already is one, else `agent:<name>`."""
    candidate = name if AB.PRINCIPAL.match(name) else f'agent:{name}'
    return candidate if AB.PRINCIPAL.match(candidate) and broker.ID.fullmatch(candidate) else None


def _item(kind: str, ident: str, outcome: str, code: Optional[str] = None, **extra) -> dict:
    row = {'kind': kind, 'id': ident, 'outcome': outcome}
    if code:
        row['code'] = code
    row.update({k: v for k, v in extra.items() if v not in (None, [], {})})
    return row


def _not_callers(callers: dict) -> set:
    """Names the engine has no caller for: the owner, the host credential, and any grant marked as one."""
    out = set(OWNER_NAMES | HOST_NAMES)
    for name, grant in callers.items():
        if isinstance(grant, dict) and (grant.get('owner') is True or grant.get('host') is True):
            out.add(name)
    return out


def _bound_binding(registry: Optional[AB.Registry], audience: Optional[str], principal: str,
                   token_hash: Any) -> Optional[str]:
    """The id of a binding in force for this workspace, of this principal, whose credential is this very digest."""
    if registry is None or not isinstance(token_hash, str) or not token_hash:
        return None
    for b in registry.bindings:
        if (b.principal == principal and b.audience == audience and b.digest == token_hash.lower()
                and b.revoked_at is None
                and (b.expires_at is None or AB._when(b.expires_at) > datetime.now(timezone.utc))):
            return b.binding_id
    return None


def plan(source: dict, current: dict, *, derive: Callable[[str], tuple], trusted_teams=(),
         principals: Optional[dict] = None, bindings: Optional[AB.Registry] = None,
         audience: Optional[str] = None, keep_always_on: bool = False) -> dict:
    """What the import would do to `current` (the engine's document): `{'doc', 'items', 'changed'}`.

    Pure apart from `derive` (which verifies an installed bundle) and reads nothing else. `principals` maps a source
    caller name to the engine principal the operator chose; the default is `agent:<name>`.

    An always_on target comes over as on_demand unless `keep_always_on`: the engine's supervisor acts on the
    document the moment it is written, while the other broker still runs until the operator switches it off last,
    and each would take the other's process for the person's, so a crashed job would get two restarts and two
    restart budgets. The report names the command that turns each back once the other broker is off."""
    principals = dict(principals or {})
    trusted = frozenset(trusted_teams)
    doc = copy.deepcopy(current)
    items: list = []
    dropped = _not_callers(source['callers'])

    def principal_of(name: Any) -> Optional[str]:
        if not isinstance(name, str):
            return None
        return principals.get(name) or default_principal(name)

    # Targets: each one alone first, through the registry's own schema.
    candidates: dict = {}
    for key, raw in sorted(source['targets'].items(), key=lambda kv: str(kv[0])):
        ident = key if isinstance(key, str) and broker.ID.fullmatch(key) else 'invalid-id'
        if ident == 'invalid-id':
            items.append(_item('target', ident, 'refused', 'invalid_registry'))
            continue
        if not isinstance(raw, dict):
            items.append(_item('target', key, 'refused', 'invalid_registry'))
            continue
        target = copy.deepcopy(raw)
        notes = []
        try:
            if legacy_identity(target):
                identity, basis = migrate_identity(target, derive, trusted)
                target['identity'] = identity
                if type(target.get('policyRevision')) is int:
                    target['policyRevision'] += 1
                notes.append(f'identity migrated ({basis})')
            readiness = target.get('readiness')
            if (isinstance(readiness, dict) and readiness.get('kind') in broker.PROFILE_CAPABILITY
                    and readiness.get('capability') == broker.PROFILE_CAPABILITY[readiness['kind']]):
                # The engine's product profiles carry no field of their own: the capability is the profile's.
                del readiness['capability']
                notes.append('readiness capability implied by its profile')
            if target.get('desiredState') == 'always_on' and not keep_always_on:
                target['desiredState'] = 'on_demand'
                notes.append('always_on imported as on_demand until the other broker is off')
            callers = target.get('allowedCallers')
            if isinstance(callers, list):
                mapped = []
                for name in callers:
                    if isinstance(name, str) and name in dropped:
                        continue
                    principal = principal_of(name)
                    if principal is None:
                        raise LifecycleError('caller_unmapped')
                    if principal not in mapped:
                        mapped.append(principal)
                if not mapped:
                    raise LifecycleError('no_callers')
                target['allowedCallers'] = mapped
            deps = target.get('dependencies')
            if not isinstance(deps, list) or len(deps) > 16 or not all(
                    isinstance(d, str) and broker.ID.fullmatch(d) for d in deps):
                raise LifecycleError('invalid_registry')
            alone = dict(target, dependencies=[])
            broker.validate_registry({'schemaVersion': 1, 'targets': {key: alone}})
        except LifecycleError as exc:
            items.append(_item('target', key, 'refused', exc.code, reason=getattr(exc, 'reason', None)))
            continue
        candidates[key] = (target, notes)

    # Against the engine's document: the same record is unchanged, another one is the engine's and is kept.
    accepted: dict = {}
    for key, (target, notes) in candidates.items():
        existing = doc['targets'].get(key)
        if existing is None:
            accepted[key] = (target, notes, 'imported')
        elif broker.canonical(existing) == broker.canonical(target):
            accepted[key] = (target, notes, 'unchanged')
        else:
            items.append(_item('target', key, 'refused', 'already_enrolled', notes=notes))

    # A dependency cycle, among the imported targets or through the engine's own, refuses only the targets on it.
    def graph() -> dict:
        edges = {k: list(t['dependencies']) for k, t in doc['targets'].items()}
        edges.update({k: list(v[0]['dependencies']) for k, v in accepted.items()})
        return edges

    def on_cycle(start: str, edges: dict) -> bool:
        seen, stack = set(), list(edges.get(start, []))
        while stack:
            node = stack.pop()
            if node == start:
                return True
            if node not in seen:
                seen.add(node)
                stack.extend(edges.get(node, []))
        return False

    edges = graph()
    for key in [k for k in sorted(accepted) if on_cycle(k, edges)]:
        items.append(_item('target', key, 'refused', 'invalid_dependencies', notes=accepted[key][1]))
        del accepted[key]

    # Dependencies: a target whose dependency is neither accepted nor already enrolled cannot come alone.
    changed = True
    while changed:
        changed = False
        for key in list(accepted):
            target = accepted[key][0]
            missing = [d for d in target['dependencies'] if d not in accepted and d not in doc['targets']]
            if missing:
                items.append(_item('target', key, 'refused', 'dependency_refused', notes=accepted[key][1]))
                del accepted[key]
                changed = True
    for key, (target, notes, outcome) in sorted(accepted.items()):
        doc['targets'][key] = target
        items.append(_item('target', key, outcome, notes=notes))

    # Grants: the engine's grant names one binding; the source's token hash only finds one.
    known_targets = set(doc['targets'])
    for name, grant in sorted(source['callers'].items(), key=lambda kv: str(kv[0])):
        if not isinstance(name, str) or not broker.ID.fullmatch(name) or not isinstance(grant, dict):
            items.append(_item('caller', name if isinstance(name, str) and broker.ID.fullmatch(name) else 'invalid-id',
                               'refused', 'invalid_grant'))
            continue
        if name in dropped:
            code = 'host_not_imported' if (name in HOST_NAMES or grant.get('host') is True) else 'owner_not_imported'
            items.append(_item('caller', name, 'skipped', code))
            continue
        if grant.get('revoked', True) is not False:
            items.append(_item('caller', name, 'skipped', 'revoked'))
            continue
        tools, targets = grant.get('allowedTools'), grant.get('allowedTargets')
        if not isinstance(tools, list) or not isinstance(targets, list) or type(grant.get('revision')) is not int \
                or grant['revision'] < 1:
            items.append(_item('caller', name, 'refused', 'invalid_grant'))
            continue
        if '*' in targets:
            items.append(_item('caller', name, 'refused', 'wildcard_refused'))
            continue
        if any(t not in TOOLS for t in tools) or len(set(map(str, tools))) != len(tools):
            items.append(_item('caller', name, 'refused', 'unknown_tool'))
            continue
        principal = principal_of(name)
        if principal is None:
            items.append(_item('caller', name, 'refused', 'caller_unmapped'))
            continue
        kept = [t for t in dict.fromkeys(targets) if isinstance(t, str) and t in known_targets]
        # Only ids reach the report: a source string that is not one is counted, never echoed.
        left = [t for t in dict.fromkeys(map(str, targets)) if t not in kept]
        left_out = sorted(t for t in left if broker.ID.fullmatch(t))
        unnamed = len(left) - len(left_out)
        proxy = grant.get('operatorProxy') is True
        tools = list(dict.fromkeys(tools))
        existing = doc['callers'].get(principal)
        if existing is not None:
            same = (sorted(existing['allowedTools']) == sorted(tools) and sorted(existing['allowedTargets'])
                    == sorted(kept) and (existing.get('operatorProxy') is True) == proxy
                    and existing['revoked'] is False)
            items.append(_item('caller', name, 'unchanged' if same else 'refused',
                               None if same else 'already_granted', principal=principal, notImported=left_out,
                               notImportedUnnamed=unnamed or None))
            continue
        binding_id = _bound_binding(bindings, audience, principal, grant.get('tokenHash'))
        if binding_id is None:
            items.append(_item('caller', name, 'reissue_required', 'no_binding', principal=principal,
                               allowedTools=tools, allowedTargets=kept, notImported=left_out,
                               notImportedUnnamed=unnamed or None))
            continue
        new = {'bindingId': binding_id, 'revision': grant['revision'], 'revoked': False, 'allowedTools': tools,
               'allowedTargets': kept}
        if proxy:
            new['operatorProxy'] = True
        doc['callers'][principal] = new
        items.append(_item('caller', name, 'imported', principal=principal, binding=binding_id,
                           notImported=left_out, notImportedUnnamed=unnamed or None))

    S.validate_config(doc)                                  # the whole document, before anything is written
    changed = any(i['outcome'] == 'imported' for i in items)
    return {'doc': doc, 'items': items, 'changed': changed}


def summary(items: list) -> dict:
    out: dict = {'targets': {}, 'callers': {}}
    for item in items:
        bucket = out['targets' if item['kind'] == 'target' else 'callers']
        bucket[item['outcome']] = bucket.get(item['outcome'], 0) + 1
    return out


def next_steps(items: list, store: Optional[dict], dry_run: bool, keep_always_on: bool = False) -> list:
    """What the operator runs next, in order. Commands with placeholders, never a value of the source."""
    steps = []
    if dry_run:
        steps.append('run the same command without --dry-run to write it')
    held = sorted(i['id'] for i in items if i['kind'] == 'target' and i['outcome'] in ('imported', 'unchanged')
                  and any(n.startswith('always_on imported as on_demand') for n in i.get('notes', [])))
    if held:
        steps.append('these always_on targets came over as on_demand, so the two brokers never both restart them: '
                     'once the other broker is off, turn each back with `project-observatory full lifecycle policy '
                     'TARGET_ID --desired always_on`: ' + ', '.join(held))
    if keep_always_on:
        steps.append('--keep-always-on: until the other broker is off, both restart a crashed always_on target, each '
                     'with its own restart budget; switch the other broker off soon')
    for item in items:
        if item['outcome'] == 'reissue_required':
            tools = ' '.join(f'--tool={t}' for t in item.get('allowedTools', []))
            targets = ' '.join(f'--target={t}' for t in item.get('allowedTargets', []))
            steps.append(f"issue {item['principal']} a binding and allow it: `project-observatory full access-binding "
                         f"issue {item['principal']} --project PROJECT --token-file PATH`, then `project-observatory "
                         f"full lifecycle allow {item['principal']} --binding BINDING_ID {tools} {targets}`".rstrip()
                         .replace('  ', ' '))
    if any(i['outcome'] == 'refused' and i.get('code') == 'reenroll_required' for i in items):
        steps.append('enroll each reenroll_required target again with `project-observatory full lifecycle enroll '
                     'TARGET_ID --kind app --path PATH --caller PRINCIPAL --startup-effect TEXT`, or name its signing '
                     'team with --trusted-team when you trust it')
    if store and (store.get('runningOperations') or store.get('startedProcesses')):
        steps.append('the other broker still has operations running or processes it started: once you switch it off, '
                     'the node treats those processes as yours and never idles them out')
    if not dry_run:
        steps.append('check the result with `project-observatory full lifecycle status`, point each agent at the '
                     'local server\'s /mcp with its binding, and only then switch the other broker off with its own '
                     'uninstall')
    return steps


def run(path: Any, *, dry_run: bool, derive: Callable[[str], tuple], trusted_teams=(), principals=None,
        bindings: Optional[AB.Registry] = None, audience: Optional[str] = None,
        config: Optional[Path] = None, state: Optional[Path] = None, keep_always_on: bool = False) -> dict:
    """Read the source, plan, and (unless `dry_run`) write the accepted items in one replacement of the document and
    journal it. Bundles are verified before the operator's lock is taken; the merge runs under it."""
    source = read_source(path)
    config = config or S.config_path()
    state = state or S.state_dir()
    # Verified once, before the operator's lock is taken: a signature check can take a while and changes nothing. The
    # plan made under the lock replays these answers, so the merge sees the document as it is when it is written.
    answers: dict = {}
    for raw in source['targets'].values():
        if legacy_identity(raw) and isinstance(raw.get('path'), str) and raw['path'] not in answers:
            try:
                answers[raw.get('path')] = (derive(raw.get('path')), None)
            except Exception as exc:                        # noqa: BLE001  replayed as raised, item by item
                answers[raw.get('path')] = (None, exc)

    def replay(p):
        value, exc = answers[p]
        if exc is not None:
            raise exc
        return value

    def make(current):
        return plan(source, current, derive=replay, trusted_teams=trusted_teams, principals=principals,
                    bindings=bindings, audience=audience, keep_always_on=keep_always_on)

    if dry_run:
        result = make(S.read_config(config))
    else:
        with S.config_lock(state):
            result = make(S.read_config(config))
            if result['changed']:
                S.write_config(result['doc'], config)
                counts = summary(result['items'])
                S.EventLog(state / S.EVENTS_FILE).append(
                    'lifecycle.imported', 'info',
                    f"{counts['targets'].get('imported', 0)} targets and {counts['callers'].get('imported', 0)} "
                    "callers were imported from another lifecycle broker's state.",
                    subject={'kind': 'service', 'id': SERVICE_SUBJECT})
    return {'source': str(source['folder']), 'dryRun': dry_run, 'changed': result['changed'] and not dry_run,
            'wouldChange': result['changed'] if dry_run else None, **summary(result['items']),
            'items': result['items'], 'store': source['store'], 'degraded': source['degraded'],
            'next': next_steps(result['items'], source['store'], dry_run, keep_always_on)}

