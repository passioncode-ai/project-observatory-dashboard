"""Whether the node takes a call of one declared action, and as whose act (contract DEC-0121 AP-2, AP-4; OBS-291).

A port of the contract's `actionDecision` (`src/actions.ts`), run over its `actions.json` vectors by
`tests/test_node_actions.py`. The node runs it before an agent twin of a button acts: the secret prompt's "Decline" and
a recipient's "Open a share" are `gate: relayed` (DEC-0147 items 3 and 4), so a call takes effect only with the
operator's decision in `_meta.decision`; the recipient's "Accept" is the person's consent (`gate: human`, `humanStep:
consent`) and no decision makes it an agent's.

What the port leaves out: the operator's mandate (DEC-0125, `byMandate`), which the node's twins do not read, so a
call carrying a mandate and no decision is answered `operator-decision-required`.

`decided` (OBS-611) is the engine's one operator-decision check around the port: every door that takes the operator's
relayed words (the configuration door, the key acts, the operations verbs, the organization selection, the secret
prompt's twins, the own-share acts) calls it, with one refusal vocabulary (the contract's spelling) and one rule for a
person's caller, a decision without a target and the words a record keeps.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

from . import relay

OPERATOR_DECISION_REQUIRED = 'operator-decision-required'
DECISION_INVALID = 'decision-invalid'
HUMAN_STEP_REQUIRED = 'human-step-required'
APPROVAL_REQUIRED = 'approval-required'
#: How long after the operator decided a relayed decision still stands: `approval/1`'s lifetime (DL-17).
DECISION_LIFETIME_SECONDS = 24 * 60 * 60
HUMAN_STEPS = ('sign-in', 'consent', 'payment', 'secret', 'spend-above-limit', 'passkey', 'comparison')
GATES = ('none', 'relayed', 'human')

#: The node's twin of the secret prompt's "Decline" (local channel SP-4, DEC-0147 item 3).
SECRET_DECLINE = {'id': 'secret.decline', 'title': 'Decline', 'tool': 'secret.decline', 'gate': 'relayed'}
#: The node's twin of a recipient's "Open a share" (peer share PS-4, DEC-0147 item 4).
SHARE_OPEN = {'id': 'share.open', 'title': 'Open a share', 'tool': 'share.open', 'gate': 'relayed'}
#: The recipient's "Accept", its consent to the reports (PS-5, PS-8): the person's step, never an agent's.
SHARE_ACCEPT = {'id': 'share.accept', 'title': 'Accept', 'tool': 'share.accept', 'gate': 'human',
                'humanStep': 'consent'}
#: The sender's comparison of the codes (PS-6): the person's step, never an agent's, outside CT-298's case.
SHARE_CONFIRM = {'id': 'share.confirm', 'title': 'Confirm the codes', 'tool': 'share.confirm', 'gate': 'human',
                 'humanStep': 'comparison'}
#: CT-298 (OBS-531): one person's own two machines, finished by their agent. Three ids of their own beside the human
#: ones (OM-2: FAC-SEM-142 takes one gate per id), admitted only under OM-1 (`own_share.py`).
SHARE_ACCEPT_OWN = {'id': 'share.accept-own', 'title': 'Accept on my own machine', 'tool': 'share.accept-own',
                    'gate': 'relayed'}
SHARE_READ_CODE = {'id': 'share.read-code', 'title': 'Read the code on my own machine', 'tool': 'share.read-code',
                   'gate': 'relayed'}
SHARE_CONFIRM_OWN = {'id': 'share.confirm-own', 'title': 'Confirm the codes on my own machine',
                     'tool': 'share.confirm-own', 'gate': 'relayed'}

_REQUEST_KEY = re.compile(r'[A-Za-z0-9_.-]{1,64}')                 # interop-common.schema.json#/$defs/requestKey
_CAPABILITY = re.compile(r'[a-z][a-z0-9._-]{1,127}')               # common.schema.json#/$defs/capabilityName
_SOURCE_PART = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')    # mandate.schema.json#/$defs/source
_SHA256 = re.compile(r'sha256:[0-9a-f]{64}')
_STEP_URL = re.compile(r'(https://|/)')
_ABSENT = object()


def _text(value: Any, low: int, high: int, pattern: 're.Pattern | None' = None) -> bool:
    return (isinstance(value, str) and low <= len(value) <= high
            and (pattern is None or pattern.fullmatch(value) is not None))


def _non_empty(value: Any) -> bool:
    return isinstance(value, str) and len(value) > 0


def readable_action(action: Any) -> bool:
    """One valid `actions[]` entry (`service-actions-extension.schema.json#/$defs/action`) whose `humanStep` is present
    exactly when `gate` is `human`."""
    if not isinstance(action, dict) or not {'id', 'title', 'tool', 'gate'} <= set(action) \
            or not set(action) <= {'id', 'title', 'tool', 'gate', 'humanStep'}:
        return False
    if not (_text(action['id'], 1, 64, _REQUEST_KEY) and _text(action['title'], 1, 80)
            and _text(action['tool'], 2, 128, _CAPABILITY) and action['gate'] in GATES):
        return False
    if 'humanStep' in action and action['humanStep'] not in HUMAN_STEPS:
        return False
    return (action['gate'] == 'human') == ('humanStep' in action)


def _source(value: Any) -> bool:
    if not isinstance(value, dict) or 'project' not in value or not set(value) <= {'project', 'session', 'thread'}:
        return False
    if 'session' not in value and 'thread' not in value:
        return False
    return all(_text(v, 1, 128, _SOURCE_PART) for v in value.values())


def decision_shape(value: Any) -> bool:
    """Valid against `decision.schema.json`: the operator's decision on one relayed action, nothing more."""
    members = {'by', 'via', 'action', 'target', 'instruction', 'at', 'source'}
    if not isinstance(value, dict) or not {'by', 'via', 'action', 'instruction', 'at'} <= set(value) \
            or not set(value) <= members:
        return False
    return (value['by'] == 'operator' and value['via'] == 'relayed-instruction'
            and _text(value['action'], 1, 64, _REQUEST_KEY) and _text(value['instruction'], 1, 500)
            and isinstance(value['at'], str) and ('target' not in value or _text(value['target'], 1, 200))
            and ('source' not in value or _source(value['source'])))


def action_decision(*, action: Any = None, tier: Any = None, caller: Any = None, decision: Any = _ABSENT,
                    target: Any = None, elicited: Any = None, approval: Any = None, actionDigest: Any = None,
                    humanStepUrl: Any = None, now: Any = None) -> dict:
    """`actionDecision` (DEC-0121 AP-2, AP-4; DL-17, DL-18, DL-25, DL-26). `none` is the caller's own act; `human` is
    refused `human-step-required` with the step and its link at every tier; `relayed`, in this order: an accepted
    approval is the approver's, a T2 caller without one is refused `approval-required`, the service's own accepted
    elicitation is the operator's, no decision is `operator-decision-required`, a decision that is not the schema's,
    for another action, naming another object, or whose `at` does not read strictly or lies more than 60 s ahead of
    `now` or more than 24 h behind it is `decision-invalid`; otherwise the operator's act, recorded with `caller`, the
    node's own identity of who relayed it. `{fault}` is the node's own and is never sent. `decision` absent and
    `decision=None` differ: the second is a decision that is null, which is invalid."""
    if not readable_action(action):
        return {'fault': 'action-invalid'}
    if action['gate'] == 'none':
        return {'ok': True, 'recordAs': 'caller'}
    if action['gate'] == 'human':
        if not _non_empty(humanStepUrl) or not _STEP_URL.match(humanStepUrl):
            return {'fault': 'url-missing'}
        return {'ok': False, 'code': HUMAN_STEP_REQUIRED, 'data': {'humanStep': action['humanStep'], 'url': humanStepUrl}}
    if not _non_empty(caller):
        return {'fault': 'caller-missing'}
    if isinstance(approval, dict) and approval.get('ok') is True and _non_empty(approval.get('approver')):
        return {'ok': True, 'recordAs': {'by': approval['approver'], 'via': 'approval', 'caller': caller}}
    if tier not in ('T0', 'T1'):
        if not isinstance(actionDigest, str) or not _SHA256.fullmatch(actionDigest):
            return {'fault': 'digest-missing'}
        return {'ok': False, 'code': APPROVAL_REQUIRED, 'data': {'actionDigest': actionDigest}}
    if elicited is True:
        return {'ok': True, 'recordAs': {'by': 'operator', 'via': 'elicitation', 'caller': caller}}
    clock = relay.strict_time(now)
    if clock is None:
        return {'fault': 'now-unreadable'}
    if decision is _ABSENT:
        return {'ok': False, 'code': OPERATOR_DECISION_REQUIRED}
    if not decision_shape(decision):
        return {'ok': False, 'code': DECISION_INVALID}
    if decision['action'] != action['id'] or ('target' in decision and decision['target'] != target):
        return {'ok': False, 'code': DECISION_INVALID}
    at = relay.strict_time(decision['at'])
    if at is None or at > clock + relay.CLOCK_SKEW_SECONDS * 1000 or at < clock - DECISION_LIFETIME_SECONDS * 1000:
        return {'ok': False, 'code': DECISION_INVALID}
    record = {'by': 'operator', 'via': 'relayed-instruction', 'caller': caller}
    if 'source' in decision:
        record['source'] = dict(decision['source'])                # where the operator said it (DEC-0126 OA-6)
    return {'ok': True, 'recordAs': record}


#: The callers that are a person: the terminal, and the page the local control server serves with its token (OBS-562).
PERSON_CALLERS = ('person:cli', 'person:page')
#: Relayed acts whose object is in the operator's words themselves: "Open a share" carries the link, which pins the
#: sender's key only when the words hold it unchanged (PS-4). Such an act needs no `target`, and its words are not read
#: for a credential's shape, because the link's fragment is one and `redact_link` replaces it before it is kept.
OBJECT_IN_WORDS = frozenset({SHARE_OPEN['id']})
#: The refusal codes as the configuration door spelled them before OBS-611: read, and answered in the contract's spelling.
LEGACY_CODES = {'operator_decision_required': OPERATOR_DECISION_REQUIRED, 'decision_invalid': DECISION_INVALID}
#: The words a door answers each refusal of `decided` with, by its `why`; `absent` is worded by each door, naming its
#: own act and object.
REASONS = {
    'shape': "the decision is not one JSON object in contract DEC-0121's shape",
    'unicode': "the decision is not valid Unicode text in contract DEC-0121's shape",
    'credential': ('the decision carries something shaped like a credential (in the words, the object or where they '
                   'were said); send it without'),
    'contract': ("the decision is not the operator's for this action and object, or is older than 24 hours: ask "
                 'again and send their words as they said them'),
    'fault': 'the decision could not be checked on this machine now; call again',
}
REASONS['target'] = REASONS['contract']
_DIGEST = re.compile(r'\bsha256:[0-9a-f]{64}\b')
_RECORD = ('by', 'via', 'action', 'target', 'instruction', 'at', 'source')


def refusal_code(code: Any) -> Any:
    """A refusal code in the one spelling this engine answers: the contract's (`operator-decision-required`,
    `decision-invalid`). The configuration door's earlier spelling is read as the same code; any other passes."""
    return LEGACY_CODES.get(code, code) if isinstance(code, str) else code


def _kept_words(decision: dict, target: str) -> list:
    """The decision's fields a record keeps verbatim, with what is no credential taken out: a `sha256:` digest (a plan's
    own, which the operator may repeat) and this act's own object, as a whole token (a share or request id)."""
    source = decision.get('source')
    kept = [decision.get('instruction'), decision.get('target'),
            *(source.values() if isinstance(source, dict) else [])]
    whole = re.compile(r'(?<![A-Za-z0-9_-])' + re.escape(target) + r'(?![A-Za-z0-9_-])') if _non_empty(target) else None
    out = []
    for value in kept:
        if isinstance(value, str):
            value = _DIGEST.sub('sha256:digest', value)
            out.append(whole.sub('object', value) if whole else value)
    return out


def decided(*, action: Any, target: Any, caller: Any, now: Any, decision: Any = None) -> dict:
    """The one operator-decision check of this engine (OBS-611): who decided a relayed act on `target`.

    One rule for every door that takes the operator's words (the configuration door, the key acts, the organization
    selection, the secret prompt's twins, the own-share acts), in this order:

    1. no decision (absent or null) from a person's caller (`PERSON_CALLERS`) is the person's own act;
    2. no decision from anyone else is `operator-decision-required`;
    3. a decision that is not one JSON object, or whose text is not valid Unicode (a record could not keep it), is
       `decision-invalid`;
    4. `action_decision` checks it (the contract's `actionDecision`, T0): its refusal is answered; a fault of the node
       is `unavailable` and names the fault, never a decision;
    5. the decision names this object as its `target` (the contract leaves it optional; here it is required, so words
       about one object never decide another), except for an act whose object is in the words (`OBJECT_IN_WORDS`);
    6. none of the words kept verbatim is shaped like a credential, a digest and the act's own object aside (same
       exception).

    Answers `{ok: True, record, recordAs}` (`record`: the decision's own fields and `caller`, as a journal keeps it;
    `recordAs`: the node's), `{ok: True, person: True, record}` for a person, or `{ok: False, code, why}` with `why`
    one of `absent`, `shape`, `unicode`, `fault`, `contract`, `target`, `credential`. Each door words its own refusal
    from `why`; the code is the contract's."""
    if decision is None:
        if caller in PERSON_CALLERS:
            return {'ok': True, 'person': True, 'record': {'by': 'person', 'via': caller.split(':', 1)[1]}}
        return {'ok': False, 'code': OPERATOR_DECISION_REQUIRED, 'why': 'absent'}
    if not isinstance(decision, dict):
        return {'ok': False, 'code': DECISION_INVALID, 'why': 'shape'}
    try:
        json.dumps(decision, ensure_ascii=False).encode('utf-8')
    except (UnicodeError, ValueError, TypeError, RecursionError):
        return {'ok': False, 'code': DECISION_INVALID, 'why': 'unicode'}
    answer = action_decision(action=action, tier='T0', caller=caller, decision=decision, target=target, now=now)
    if 'fault' in answer:
        return {'ok': False, 'code': 'unavailable', 'why': 'fault', 'fault': answer['fault']}
    if answer.get('ok') is not True:
        return {'ok': False, 'code': answer.get('code') or DECISION_INVALID, 'why': 'contract'}
    in_words = action['id'] in OBJECT_IN_WORDS
    if not in_words and decision.get('target') != target:
        return {'ok': False, 'code': DECISION_INVALID, 'why': 'target'}
    if not in_words:
        import credential_shape
        if any(credential_shape.shaped(v, uuids=False) for v in _kept_words(decision, target)):
            return {'ok': False, 'code': DECISION_INVALID, 'why': 'credential'}
    record = {k: copy.deepcopy(decision[k]) for k in _RECORD if k in decision}
    record['caller'] = caller
    return {'ok': True, 'record': record, 'recordAs': answer['recordAs']}


def redact_link(decision: dict, link: str, share_id: str) -> dict:
    """The decision as the node's audit record keeps it after an "Open a share" (DEC-0147 item 4, AP-2): the link's
    fragment (`c` and `k`) replaced by the share id wherever the words hold it, the one redaction of that record. The
    claim code and the fingerprint are also replaced where they appear on their own. The caller's copy is unchanged."""
    kept = copy.deepcopy(decision)
    words = kept.get('instruction')
    if isinstance(words, str) and '#' in link:
        fragment = link.split('#', 1)[1]
        words = words.replace('#' + fragment, '#' + share_id)
        for part in fragment.split('&'):
            value = part.split('=', 1)[1] if '=' in part else ''
            if len(value) >= 16:
                words = words.replace(value, share_id)
        kept['instruction'] = words
    return kept
