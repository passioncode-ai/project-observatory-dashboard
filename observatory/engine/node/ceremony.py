"""The estate ceremony surface (OBS-197, NODE slice 5): what a person's screen shows at each step of pairing a machine.

`node/pairing.py` holds the ceremony's checks (OBS-133, contract DEC-0084). This module reads one pairing session
through those checks and answers the one screen a desktop app draws for it, so the Mac app and the Windows and Linux
app show the same state for the same session and neither decides a ceremony rule on its own:

- `not-paired`: no session; the person may start one;
- `hub-unreachable`: a session waits on the network's hub, which does not run pairing yet or cannot be reached. Nothing
  was sent and no machine was added; the screen says so and never shows progress it did not make;
- `waiting-for-hub`: the hub carries the rendezvous and the other machine has not answered yet (`step: other-side`),
  or the codes matched and the new machine waits for the revision that lists it (`step: revision`);
- `code-shown`: both reveals opened their commitments, so the six digits are safe to compare;
- `passkey`: the trusted machine's person confirmed the codes; the screen shows what the passkey would sign (the new
  machine and its key fingerprints) before anything is signed;
- `confirmed`: a revision lists the new machine with exactly its revealed keys and the trusted machine with its `#sig`
  key (`pairing.join_listed`);
- `refused`: the rendezvous burned, the code could not be derived, or the revision does not list what was revealed,
  with the check's own `reason` and a `group` a screen words;
- `expired`: the rendezvous passed its 600 s.

Every function is total: a malformed session answers `refused` with reason `malformed`, never an exception. The live
ceremony needs the hub's side (the rendezvous and the passkey page); until it exists the engine reports the hub as
`not-ready`, and a started session stays at `hub-unreachable`.
"""
from __future__ import annotations

from typing import Any, Optional

from .estate import device_key_fingerprint
from .pairing import (PAIRING_ROLES, PAIRING_SCHEMA, RENDEZVOUS_SECONDS, checked_comparison_code, estate_fingerprint,
                      join_listed, machine_of_keys, pairing_commit, rendezvous_verdict)
from .relay import strict_time

SCREEN_SCHEMA = 'node-ceremony/1'
SESSION_SCHEMA = 'node-ceremony-session/1'
STATES = ('not-paired', 'hub-unreachable', 'waiting-for-hub', 'code-shown', 'passkey', 'confirmed', 'refused',
          'expired')
#: The hub's states as the engine reports them: `ready` (it carries rendezvous and the passkey page), `not-ready` (it
#: does not run pairing yet: the hub's side of DEC-0084 is not live), `unreachable` (it runs pairing and did not answer).
HUB_STATES = ('ready', 'not-ready', 'unreachable')

#: A check's reason, as the small set of sentences a screen says. A reason not listed reads as `mismatch`: a message
#: that did not match what its machine promised, which is what every other refusal of the rendezvous and the code is.
REASON_GROUPS = {
    'codes-differ': 'codes-differ',
    'aborted': 'aborted',
    'other-estate': 'other-estate',
    'not-listed': 'not-listed',
    'other-keys': 'not-listed',
    'trusted-not-listed': 'trusted-not-listed',
}


def _screen(state: str, **members: Any) -> dict:
    return dict({'schema': SCREEN_SCHEMA, 'state': state}, **members)


def _refused(reason: str, **members: Any) -> dict:
    return _screen('refused', reason=reason, group=REASON_GROUPS.get(reason, 'mismatch'), **members)


def _own_side(session: dict) -> Optional[dict]:
    """This machine's side as it committed: a pairing side the commit accepts, of the session's role."""
    side = session.get('own')
    if not isinstance(side, dict) or side.get('role') != session.get('role'):
        return None
    return side if pairing_commit(side)['ok'] else None


def _find(messages: list, step: str, role: str) -> Optional[dict]:
    return next((m for m in messages if isinstance(m, dict) and m.get('step') == step and m.get('role') == role), None)


def _seconds_left(opened_ms: int, at_ms: int) -> int:
    return max(0, (opened_ms + RENDEZVOUS_SECONDS * 1000 - at_ms + 999) // 1000)


def _expires_at(opened_at: str) -> Optional[str]:
    from datetime import datetime, timedelta, timezone
    opened = strict_time(opened_at)
    if opened is None:
        return None
    end = datetime.fromtimestamp(opened / 1000, tz=timezone.utc) + timedelta(seconds=RENDEZVOUS_SECONDS)
    return end.strftime('%Y-%m-%dT%H:%M:%SZ')


def _waiting(hub: str, step: str, **members: Any) -> dict:
    """A step that needs the hub: the hub's own state when it does not carry the ceremony, never a pretend wait."""
    if hub != 'ready':
        return _screen('hub-unreachable', hub=hub if hub in HUB_STATES else 'unreachable', step=step, **members)
    return _screen('waiting-for-hub', step=step, **members)


def _fingerprints(keys: list) -> list:
    return [{'kid': key['kid'], 'use': key['use'], 'fingerprint': device_key_fingerprint(key)} for key in keys]


def pairing_screen(session: Any, at_ms: Any, hub: Any = 'not-ready') -> dict:
    """The screen for one pairing session at `at_ms` (milliseconds), with the hub in one of `HUB_STATES`.

    `session` is `None` (no pairing) or a `node-ceremony-session/1`: `role` (`new` or `trusted`), `own` (this machine's
    side as it committed), `openedAt` (the rendezvous's RFC 3339 opening), `messages` (what the rendezvous carried,
    oldest first, this machine's own included), and optionally `confirmed` (the person said the codes match),
    `genesis` (for a new machine: the digest of the revision 1 it verified), `record` with `recordAt` (the revision
    that arrived, and when). The checks run in the order a person meets them: a revision that lists both machines is
    `confirmed` whatever the clock says now; otherwise the rendezvous's verdict at `at_ms` decides."""
    if session is None:
        return _screen('not-paired')
    if not isinstance(session, dict) or session.get('schema') != SESSION_SCHEMA \
            or session.get('role') not in PAIRING_ROLES or not isinstance(session.get('messages'), list) \
            or isinstance(at_ms, bool) or not isinstance(at_ms, int):
        return _refused('malformed')
    role = session['role']
    own = _own_side(session)
    opened_at = session.get('openedAt')
    opened_ms = strict_time(opened_at)
    if own is None or opened_ms is None:
        return _refused('malformed')
    messages = session['messages']
    mine = {'schema': PAIRING_SCHEMA, 'step': 'commit', 'role': role, 'commit': pairing_commit(own)['commit']}
    if _find(messages, 'commit', role) != mine:
        return _refused('malformed')
    timing = {'expiresAt': _expires_at(opened_at)}

    if 'record' in session:
        # The revision arrived inside the rendezvous: judge the rendezvous as it stood then, and the revision itself.
        record_ms = strict_time(session.get('recordAt'))
        verdict = rendezvous_verdict(opened_at, messages, record_ms) if record_ms is not None else None
        if verdict is None or verdict != {'state': 'complete'}:
            return _refused('malformed')
        if session.get('confirmed') is not True:
            # Nobody here compared the code: a revision alone never reads as paired (review of #380, M1).
            return _refused('malformed')
        reveals = _find(messages, 'reveal', 'new'), _find(messages, 'reveal', 'trusted')
        genesis = session.get('genesis') if role == 'new' else own.get('genesis')
        joined = join_listed(session['record'], reveals[0], reveals[1], genesis)
        if not joined['ok']:
            return _refused(joined['problem'])
        fingerprint = estate_fingerprint(reveals[1]['genesis'])
        return _screen('confirmed', machine=machine_of_keys(reveals[0]['keys']),
                       fingerprint=fingerprint.get('fingerprint'))

    verdict = rendezvous_verdict(opened_at, messages, at_ms)
    if verdict['state'] == 'burned':
        if verdict['reason'] == 'expired':
            return _screen('expired', **timing)
        reason = verdict['reason']
        if reason == 'aborted' and messages[verdict['index']].get('role') == role:
            reason = 'codes-differ'                      # this machine's person said the codes differ
        return _refused(reason, index=verdict['index'])
    timing['secondsLeft'] = _seconds_left(opened_ms, at_ms)
    if verdict['state'] == 'open':
        return _waiting(hub, 'other-side', **timing)

    code = checked_comparison_code(_find(messages, 'commit', 'new'), _find(messages, 'reveal', 'new'),
                                   _find(messages, 'commit', 'trusted'), _find(messages, 'reveal', 'trusted'))
    if not code['ok']:
        return _refused(code['problem'])
    # A `complete` verdict holds one commitment and one reveal per role, each reveal opened against its commitment.
    trusted_reveal = _find(messages, 'reveal', 'trusted')
    new_reveal = _find(messages, 'reveal', 'new')
    shown = {'code': code['code'], 'fingerprint': estate_fingerprint(trusted_reveal['genesis']).get('fingerprint')}
    if session.get('confirmed') is not True:
        return _screen('code-shown', **shown, **timing)
    if role == 'trusted':
        # The passkey prompt: what would be signed, before anything is. Signing happens on the hub's page.
        return _screen('passkey', machine=machine_of_keys(new_reveal['keys']), keys=_fingerprints(new_reveal['keys']),
                       canSign=hub == 'ready', hub=hub if hub in HUB_STATES else 'unreachable', **shown, **timing)
    return _waiting(hub, 'revision', **shown, **timing)


# The session a workspace keeps, and the steps a person takes on it. A desktop app reaches these through
# `project-observatory full assistant ceremony` (`agent/assistant.py`); the screen above is every answer.

SESSION_FILE = 'ceremony.json'
#: What a step refuses, by code: a screen words each one. `not-now` is a step the current screen does not offer.
STEP_REFUSALS = ('ceremony-in-progress', 'no-estate', 'not-now', 'hub-not-ready', 'invalid-input',
                 'device-keys-refused')


class CeremonyRefused(Exception):
    """A step the session refuses; `str()` is one of `STEP_REFUSALS`."""


def hub_state() -> str:
    """The hub's state for pairing. The rendezvous and the passkey page are the hub's side of DEC-0084, which no hub
    runs yet, so this answers `not-ready` and a started session stays at `hub-unreachable`: nothing is sent anywhere."""
    return 'not-ready'


def read_session(folder) -> Optional[dict]:
    """The stored session, or None. A file that is not a JSON object reads as a session the screen refuses as
    `malformed`, so a damaged file is shown and can be cancelled, never mistaken for no pairing."""
    import json
    from pathlib import Path
    path = Path(folder) / SESSION_FILE
    try:
        text = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return {'schema': 'unreadable'}
    return value if isinstance(value, dict) else {'schema': 'unreadable'}


def _write_session(folder, session: dict) -> None:
    """The session, owner-only, written whole or not at all; a link in its place is refused, never followed."""
    import json
    import os
    from pathlib import Path
    import osprivacy
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = folder / SESSION_FILE
    temporary = folder / (SESSION_FILE + '.tmp')
    descriptor = osprivacy.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | osprivacy.NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        json.dump(session, handle, ensure_ascii=False, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    osprivacy.make_private(temporary)
    os.replace(temporary, target)


def _iso(at_ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(at_ms // 1000, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def new_nonce() -> str:
    """A fresh 32-byte nonce in unpadded base64url, the form a pairing side commits to."""
    import base64
    import secrets
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('ascii').rstrip('=')


def new_session(role: str, keys: list, at_ms: int, genesis: Optional[str] = None) -> dict:
    """A session whose own side commits to `keys` and a fresh nonce (and, for a trusted machine, its revision 1's
    digest), opened at `at_ms`; its first message is this machine's commitment."""
    own = {'role': role, 'keys': keys, 'nonce': new_nonce()}
    if role == 'trusted':
        own['genesis'] = genesis
    made = pairing_commit(own)
    if not made['ok']:
        raise CeremonyRefused('device-keys-refused')
    return {'schema': SESSION_SCHEMA, 'role': role, 'own': own, 'openedAt': _iso(at_ms),
            'messages': [{'schema': PAIRING_SCHEMA, 'step': 'commit', 'role': role, 'commit': made['commit']}]}


def _terminal(screen: dict) -> bool:
    return screen['state'] in ('not-paired', 'confirmed', 'refused', 'expired')


def step(folder, device_folder, op: Any, role: Any, at_ms: int, hub: Optional[str] = None) -> dict:
    """One step a person takes: `status`, `start` (with `role`), `confirm` and `differ` (on `code-shown`), `sign` (on
    `passkey`) or `cancel`. Answers `{screen, hub}`; a refused step raises `CeremonyRefused`.

    A trusted machine is one already in a network: it holds an applied estate record. The engine keeps none yet (the
    node service, slice 6, applies one), so `start` as `trusted` is refused with `no-estate`. `start` as `new` creates
    this machine's device keys when it has none (as `full node --init` does) and commits to them."""
    hub = hub or hub_state()
    session = read_session(folder)
    screen = pairing_screen(session, at_ms, hub)
    if op == 'status':
        pass
    elif op == 'start':
        if role not in PAIRING_ROLES:
            raise CeremonyRefused('invalid-input')
        if not _terminal(screen):
            raise CeremonyRefused('ceremony-in-progress')
        if role == 'trusted':
            raise CeremonyRefused('no-estate')
        from pathlib import Path
        try:
            from .device_keys import DeviceKeys
            keys = DeviceKeys.load_or_create(Path(device_folder).resolve()).enrollment_keys()
        except (RuntimeError, ValueError, OSError):
            raise CeremonyRefused('device-keys-refused') from None
        session = new_session(role, keys, at_ms)
        _write_session(folder, session)
        screen = pairing_screen(session, at_ms, hub)
    elif op in ('confirm', 'differ'):
        if screen['state'] != 'code-shown':
            raise CeremonyRefused('not-now')
        if op == 'confirm':
            session['confirmed'] = True
        else:
            # The person said the codes differ: this side aborts, which burns the rendezvous for both.
            session['messages'].append({'schema': PAIRING_SCHEMA, 'step': 'abort', 'role': session['role']})
        _write_session(folder, session)
        screen = pairing_screen(session, at_ms, hub)
    elif op == 'sign':
        if screen['state'] != 'passkey':
            raise CeremonyRefused('not-now')
        # The passkey signs on the hub's page (its WebAuthn origin), which no hub serves yet: never a pretend signature.
        raise CeremonyRefused('hub-not-ready')
    elif op == 'cancel':
        from pathlib import Path
        (Path(folder) / SESSION_FILE).unlink(missing_ok=True)
        screen = pairing_screen(None, at_ms, hub)
    else:
        raise CeremonyRefused('invalid-input')
    return {'screen': screen, 'hub': hub}
